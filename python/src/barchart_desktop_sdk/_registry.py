"""Subscriptions as desired state, reconciled with the wire per login.

Only the delivery thread calls acquire and remove. acquire sends a new entry when logged in; remove at 0 unsubscribes and
deletes at once. The SDK holds its lock around every call, and the connection only queues what it's given, so nothing here
waits on the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from openfeed.generated import openfeed_api_pb2, openfeed_instrument_pb2, openfeed_pb2

from ._subscriber import Entry, Subscriber, get_dependent_error, get_message_subscribers, subscription_key
from ._types import SubscriptionError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ._types import Service, SubscriptionType

_SERVICE_LABELS = {
    openfeed_pb2.DELAYED: "D",
    openfeed_pb2.DELAYED_SNAPSHOT: "DS",
    openfeed_pb2.END_OF_DAY: "EOD",
    openfeed_pb2.REAL_TIME: "RT",
    openfeed_pb2.REAL_TIME_SNAPSHOT: "RTS",
}
_TYPE_LABELS = {
    openfeed_api_pb2.ALL: "ALL",
    openfeed_api_pb2.CUMULATIVE_VOLUME: "CV",
    openfeed_api_pb2.DEPTH_PRICE: "DP",
    openfeed_api_pb2.TRADES: "T",
}


class SubscriptionClient(Protocol):
    """What the registry needs of a connection."""

    def subscribe(self, service: Service, subscription_type: SubscriptionType, symbol: str) -> int: ...

    def unsubscribe(self, subscription_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class ResponseResult:
    """What a response changed: the entries it matched, and the errors to give subscribers, once no lock is held."""

    matched: list[Entry]
    failures: list[tuple[Subscriber, SubscriptionError]]


def get_wire_labels(entries: Iterable[Entry]) -> str:
    """The entries that are on the wire (sent on this login), as `RT ZCZ26 ALL, RT ESZ26 T`; empty if none are."""
    return ", ".join(
        f"{_SERVICE_LABELS.get(entry.service, '?')} {entry.symbol} {_TYPE_LABELS.get(entry.type, '?')}"
        for entry in entries
        if entry.client_id is not None
    )


def _keeps_state(entry: Entry) -> bool:
    """State subscribers and dependencies make an entry's symbol link to the markets it named; raw subscribers don't."""
    return bool(entry.dependents) or any(subscriber.method != "raw" for subscriber in entry.subscribers)


def get_message_market_id(message: openfeed_api_pb2.OpenfeedGatewayMessage) -> str | None:
    """The protocol's marketId of a message, if it's about one market. An instrument action is about its old one."""
    if message.HasField("instrumentDefinition"):
        return str(message.instrumentDefinition.marketId)
    if message.HasField("marketSnapshot"):
        return str(message.marketSnapshot.marketId)
    if message.HasField("marketUpdate"):
        return str(message.marketUpdate.marketId)
    if message.HasField("instrumentAction") and message.instrumentAction.HasField("instrument"):
        return str(message.instrumentAction.instrument.marketId)
    return None


class Registry:
    """The one record of which subscribed symbol names which market: each entry's `markets` (what its successful responses
    named). A symbol "links" to a market when a state-keeping entry under it named it; nothing else keeps a copy. Indexed by
    symbol and by market, so the per-message lookups don't scan every entry.
    """

    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}
        self._entries_by_symbol: dict[str, dict[Entry, None]] = {}
        # kept in step with each entry's `markets` by _name and _unname
        self._entries_by_market: dict[str, dict[Entry, None]] = {}
        # a named market's definition symbol (ESZ6), both ways: how a root subscription's instruments, which have no
        # subscribed name, are found; kept while an entry names the market (_delete_definition_symbol)
        self._markets_by_definition_symbol: dict[str, str] = {}
        self._definition_symbols_by_market: dict[str, str] = {}
        self._client: SubscriptionClient | None = None

    def get_all_entries(self) -> list[Entry]:
        return list(self._entries.values())

    def get_entries(self, symbol: str) -> list[Entry]:
        return list(self._entries_by_symbol.get(symbol, ()))

    def get_market_id(self, symbol: str) -> str | None:
        """The market a subscribed symbol names: the one a state-keeping entry under it named last (an alias's new market,
        once its repeated response came, until ALIAS_CHANGED takes the old one away).
        """
        named = [market_id for entry in self._entries_by_symbol.get(symbol, ()) if _keeps_state(entry) for market_id in entry.markets]
        return named[-1] if named else self._markets_by_definition_symbol.get(symbol)

    def on_definition(self, definition: openfeed_instrument_pb2.InstrumentDefinition) -> None:
        """A definition: for a market an entry names, its symbol finds the market from now on; any other is ignored."""
        self._set_definition_symbol(str(definition.marketId), definition.symbol)

    def get_symbols_by_id(self, market_id: str) -> list[str]:
        """The subscribed symbols that link to the market: those of the state-keeping entries that named it."""
        return list(dict.fromkeys(entry.symbol for entry in self._entries_by_market.get(market_id, ()) if _keeps_state(entry)))

    def acquire(self, service: Service, subscription_type: SubscriptionType, symbol: str, created: list[Entry]) -> Entry:
        """Takes one reference, creating the entry (and sending it, when logged in) if needed. Entries created, its
        dependencies included, are collected in `created`.
        """
        entry = self._entries.get(subscription_key(service, subscription_type, symbol))
        if entry is None:
            entry = self._create(service, subscription_type, symbol, created)
        entry.refs += 1
        return entry

    def remove(self, entry: Entry) -> list[Entry]:
        """Gives back one reference. At 0 the entry is unsubscribed (when logged in) and removed, and its dependencies give
        back theirs, which can remove those too. Returns the removed entries.
        """
        # already removed
        if self._entries.get(entry.key) is not entry:
            return []
        entry.refs -= 1
        if entry.refs > 0:
            return []
        removed: list[Entry] = []
        self._delete(entry, removed)
        return removed

    def attach(self, client: SubscriptionClient) -> None:
        """Login: every entry goes on the new client."""
        self._client = client
        for entry in self._entries.values():
            self._send(entry)

    def detach(self) -> None:
        """Logout or rejected credentials: the client is being thrown away, so its ids, answers and errors go too."""
        self._client = None
        for entry in self._entries.values():
            entry.client_id = None
            entry.status = "pending"
            entry.error = None
            entry.response = None
            entry.markets.clear()
        self._entries_by_market.clear()
        self._markets_by_definition_symbol.clear()
        self._definition_symbols_by_market.clear()

    def clear(self) -> None:
        """Dispose: every subscriber is closed and every entry removed, without unsubscribing."""
        self._client = None
        for entry in self._entries.values():
            for subscriber in entry.subscribers:
                subscriber.closed = True
            entry.subscribers.clear()
        self._entries.clear()
        self._entries_by_symbol.clear()
        self._entries_by_market.clear()
        self._markets_by_definition_symbol.clear()
        self._definition_symbols_by_market.clear()

    def on_response(self, response: openfeed_api_pb2.SubscriptionResponse) -> ResponseResult:
        """Matches a response to its entries by symbol and type: the same symbol and type on two services get the same answer."""
        # errors on unsubscribe responses are ignored
        if response.unsubscribe:
            return ResponseResult(matched=[], failures=[])
        matched = [entry for entry in self._entries_by_symbol.get(response.symbol, ()) if entry.type == response.subscriptionType]
        failures: list[tuple[Subscriber, SubscriptionError]] = []

        for entry in matched:
            entry.response = response
            if response.status.result == openfeed_api_pb2.SUCCESS:
                entry.status = "active"
                entry.error = None
                # a response without a market names none
                if response.marketId:
                    self._name(entry, str(response.marketId))
            else:
                entry.status = "error"
                entry.error = SubscriptionError(
                    response.status.result, response.symbol, response.subscriptionType, entry.service, response.status.message
                )
                failures.extend((subscriber, entry.error) for subscriber in entry.subscribers)
                # cumulative volume doesn't update without its ALL; one already failed on its own has told its subscribers
                for dependent in entry.dependents:
                    if dependent.error is None:
                        error = get_dependent_error(entry.error, dependent)
                        failures.extend((subscriber, error) for subscriber in dependent.subscribers)
        return ResponseResult(matched=matched, failures=failures)

    def has_id(self, market_id: str, subscription_type: SubscriptionType | None = None) -> bool:
        """Whether any entry's successful responses named the market (state or raw: its state must stay while one does);
        with `subscription_type`, any entry of that subscription type.
        """
        return any(subscription_type is None or entry.type == subscription_type for entry in self._entries_by_market.get(market_id, ()))

    def get_message_subscribers_by_id(self, market_id: str) -> list[Subscriber]:
        """Subscribers that take raw messages (raw ones, and state ones with `on_message`) on entries that named this market."""
        return [subscriber for entry in self._entries_by_market.get(market_id, ()) for subscriber in get_message_subscribers(entry)]

    def alias_changed(self, alias: str, market_id: str) -> None:
        """`instrumentAction` ALIAS_CHANGED: the alias's entries stop naming the old instrument."""
        for entry in self._entries_by_symbol.get(alias, ()):
            self._unname(entry, market_id)

    def moved(self, from_id: str, to_id: str) -> None:
        """`instrumentAction` EXCHANGE_MOVE: entries that named the instrument name its new marketId."""
        symbol = self._definition_symbols_by_market.get(from_id)
        for entry in list(self._entries_by_market.get(from_id, ())):
            self._unname(entry, from_id)
            self._name(entry, to_id)
        if symbol is not None:
            self._set_definition_symbol(to_id, symbol)

    # `entry.markets` changes only through these two, so the market index stays in step.

    def _name(self, entry: Entry, market_id: str) -> None:
        entry.markets[market_id] = None
        self._entries_by_market.setdefault(market_id, {})[entry] = None

    def _unname(self, entry: Entry, market_id: str) -> None:
        entry.markets.pop(market_id, None)
        self._remove_from_market_index(entry, market_id)

    def _remove_from_market_index(self, entry: Entry, market_id: str) -> None:
        """The last entry to leave a market takes the market's definition symbol with it."""
        entries = self._entries_by_market.get(market_id)
        if entries is not None:
            entries.pop(entry, None)
            if not entries:
                del self._entries_by_market[market_id]
                self._delete_definition_symbol(market_id)

    def _set_definition_symbol(self, market_id: str, symbol: str) -> None:
        if not self.has_id(market_id) or not symbol:
            return
        self._delete_definition_symbol(market_id)
        self._markets_by_definition_symbol[symbol] = market_id
        self._definition_symbols_by_market[market_id] = symbol

    def _delete_definition_symbol(self, market_id: str) -> None:
        symbol = self._definition_symbols_by_market.pop(market_id, None)
        # another market's definition may have taken the symbol since
        if symbol is not None and self._markets_by_definition_symbol.get(symbol) == market_id:
            del self._markets_by_definition_symbol[symbol]

    def _create(self, service: Service, subscription_type: SubscriptionType, symbol: str, created: list[Entry]) -> Entry:
        entry = Entry(key=subscription_key(service, subscription_type, symbol), service=service, type=subscription_type, symbol=symbol)
        self._entries[entry.key] = entry
        self._entries_by_symbol.setdefault(symbol, {})[entry] = None

        # Cumulative volume is seeded by its own snapshot, then built from trades, which arrive on the symbol's ALL
        # subscription: the same entry a quote subscription on REAL_TIME uses, so a quote and cumulative volume share it.
        if subscription_type == openfeed_api_pb2.CUMULATIVE_VOLUME:
            everything = self.acquire(openfeed_pb2.REAL_TIME, openfeed_api_pb2.ALL, symbol, created)
            everything.dependents[entry] = None
            entry.dependencies.append(everything)

        self._send(entry)
        created.append(entry)
        return entry

    def _send(self, entry: Entry) -> None:
        if self._client is not None:
            entry.client_id = self._client.subscribe(entry.service, entry.type, entry.symbol)

    def _delete(self, entry: Entry, removed: list[Entry]) -> None:
        """Deletes the entry and removes its dependencies' references, which can remove those too; collects them in `removed`."""
        del self._entries[entry.key]
        by_symbol = self._entries_by_symbol[entry.symbol]
        del by_symbol[entry]
        if not by_symbol:
            del self._entries_by_symbol[entry.symbol]
        # out of the market index; `entry.markets` stays, so the caller can see what the removed entry named
        for market_id in entry.markets:
            self._remove_from_market_index(entry, market_id)
        removed.append(entry)
        if self._client is not None and entry.client_id is not None:
            self._client.unsubscribe(entry.client_id)
        for dependency in entry.dependencies:
            del dependency.dependents[entry]
            removed.extend(self.remove(dependency))
