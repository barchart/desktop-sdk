"""The Desktop SDK: Openfeed messages turned into quote, book and cumulative volume state.

Threads, and what each may do:
- the socket thread (one per connection) applies each message to the state under the lock, so getters are always current,
  and queues what listeners should get. It never runs a listener, so a slow one can't hold up the state.
- the delivery thread runs every listener and settles every future, in order and one at a time. It also processes the
  queued subscribes and closes in batches, and delivers coalesced events.
- any thread can call the public methods. `subscribe` and `close` only queue a request.

The lock is never held while a listener runs, so a listener can call anything here. It mustn't wait on a future the SDK
settles, since that needs the thread it's running on.
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal, Self, TypeVar, cast

from openfeed.generated import openfeed_api_pb2, openfeed_instrument_pb2, openfeed_pb2

from ._connection import Connection, ConnectionListeners, get_request_field, settle_exception, settle_result
from ._formatted import FormattedQuote
from ._proto import to_time
from ._registry import Registry, get_message_market_id, get_wire_labels
from ._store import CumulativeVolumePart, InstrumentChange, InstrumentStore, LastMessages, PriceVolume, StateParts
from ._subscriber import Entry, EntryKey, Event, Method, Subscriber, Subscription, get_message_subscribers, get_subscriber_error
from ._types import (
    BookEvent,
    ConnectionState,
    Credentials,
    CumulativeVolumeEvent,
    EventKind,
    HeartbeatListener,
    Listener,
    MessageMetadata,
    QuoteEvent,
    RawListener,
    SendableRequest,
    SnapshotMessage,
    SubscriptionInfo,
)

if TYPE_CHECKING:
    from types import TracebackType

    from ._book import Book
    from ._instrument import Instrument
    from ._quote import QuoteRecord
    from ._types import Service, SubscriptionType

_T = TypeVar("_T")
_Call = Callable[[], None]
_MessageListener = Callable[[openfeed_api_pb2.OpenfeedGatewayMessage, MessageMetadata], None]

_DISPOSED = "The Desktop SDK was disposed"
_NOT_CONNECTED = "The Desktop SDK is not connected"
_REJECTED = "Openfeed rejected the credentials"
_DISPOSE_WAIT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class _Op:
    """A request for the delivery thread."""

    kind: Literal["subscribe", "close"]
    subscriber: Subscriber


@dataclass(frozen=True, slots=True)
class _Work:
    """Something for the delivery thread. What's `kept` survives a logout; the rest describes state that's gone."""

    call: _Call
    kept: bool


@dataclass(frozen=True, slots=True)
class _HeldChange:
    """A change held for the next delivery (coalesce_interval_ms), with the last message behind it."""

    change: InstrumentChange
    raw: openfeed_api_pb2.OpenfeedGatewayMessage


def _make_event(  # noqa: PLR0913, PLR0917
    subscriber: Subscriber, symbol: str, instrument: Instrument, kind: EventKind, raw: openfeed_api_pb2.OpenfeedGatewayMessage, parts: StateParts
) -> Event | None:
    """One subscriber's event: its method's part, if there is one (a change carries only the parts it changed; a first
    delivery, every part there is). Raw subscribers get no events.
    """
    if subscriber.method == "quote" and parts.quote is not None:
        return QuoteEvent(symbol, instrument, kind, raw, parts.quote.quote, parts.quote.formatted)
    if subscriber.method == "depth" and parts.depth is not None:
        return BookEvent(symbol, instrument, kind, raw, parts.depth.book)
    if subscriber.method == "cumulative_volume" and parts.cumulative_volume is not None:
        return CumulativeVolumeEvent(symbol, instrument, kind, raw, parts.cumulative_volume.levels, parts.cumulative_volume.changed)
    return None


def _merge_cumulative_volume(earlier: CumulativeVolumePart | None, later: CumulativeVolumePart | None, kind: str) -> CumulativeVolumePart | None:
    current = later or earlier
    # a snapshot started the market over, so every level is a change, as on a snapshot event; that holds when the
    # snapshot carried no volume at price too, since a consumer starts its levels over on any snapshot event
    if current is not None and kind == "snapshot":
        return CumulativeVolumePart(levels=current.levels, changed=current.levels)
    if earlier is None or later is None:
        return current
    changed = {level.price: level for level in (*earlier.changed, *later.changed)}
    return CumulativeVolumePart(levels=later.levels, changed=tuple(changed[price] for price in sorted(changed)))


def _merge_changes(earlier: InstrumentChange, later: InstrumentChange) -> InstrumentChange:
    """Two changes to one market as one: each part is the later one's, if it has it. It's a snapshot if either was, since a
    snapshot starts the market over; cumulative volume's changed levels add up.
    """
    kind: Literal["snapshot", "update"] = "snapshot" if "snapshot" in (earlier.kind, later.kind) else "update"
    return InstrumentChange(
        market_id=later.market_id,
        kind=kind,
        parts=StateParts(
            quote=later.parts.quote or earlier.parts.quote,
            depth=later.parts.depth or earlier.parts.depth,
            cumulative_volume=_merge_cumulative_volume(earlier.parts.cumulative_volume, later.parts.cumulative_volume, kind),
        ),
    )


class RawAccess:
    """`sdk.raw`: protocol-level access, for what the SDK doesn't model."""

    def __init__(self, sdk: DesktopSdk) -> None:
        """Created by the SDK; an application uses the one on the SDK."""
        self._sdk = sdk

    def subscribe(
        self, symbol: str, subscription_type: SubscriptionType, listener: RawListener | None = None, service: Service = openfeed_pb2.REAL_TIME
    ) -> Subscription:
        """Subscribes to anything Openfeed offers, without SDK state or events (a root such as `ZC^UDS`, say).

        Args:
            symbol: The symbol, root or alias to subscribe to.
            subscription_type: The Openfeed subscription type.
            listener: Gets the subscription's responses and every message for the markets those responses named, and the
                server's refusal if there is one.
            service: The Openfeed service, `REAL_TIME` by default.

        Returns:
            The subscription, `pending` until the server answers. Close it to unsubscribe.

        Raises:
            RuntimeError: If the SDK has been disposed.
        """
        return self._sdk._subscribe(service, subscription_type, symbol, "raw", listener)

    def request_snapshot(self, symbol: str, service: Service = openfeed_pb2.REAL_TIME_SNAPSHOT) -> Future[SnapshotMessage]:
        """Requests one snapshot of a symbol: subscribes, takes the first snapshot, and closes.

        Args:
            symbol: The symbol or alias.
            service: The Openfeed service, `REAL_TIME_SNAPSHOT` by default.

        Returns:
            A future with the definition and the snapshot. It fails with a `SubscriptionError` if the server refuses the
            subscription, and with a `RuntimeError` if the SDK is disposed first.
        """
        return self._sdk._request_snapshot(symbol, service)

    def on_message(self, listener: _MessageListener) -> Callable[[], None]:
        """Listens for every incoming message, after the SDK and its subscribers have handled it.

        For a consumer that forwards or records the whole stream.

        Args:
            listener: Called with each message and what the Openfeed client knows about it.

        Returns:
            A function that removes the listener.
        """
        return self._sdk._add_raw_listener(listener)

    def send(
        self,
        request: SendableRequest,
        correlation_id: int | None = None,
    ) -> int | None:
        """Sends a request the SDK doesn't model; its responses arrive through `on_message`.

        The SDK keeps no state for what it brings.

        Args:
            request: An exchange, instrument, instrument reference, list subscriptions or subscription request, such as
                `ExchangeRequest()`. The connection puts it in a gateway request, with its token and a correlation id;
                the request itself isn't changed.
            correlation_id: The id to match the responses by; one is made when it's None.

        Returns:
            The correlation id, or None when not connected.

        Raises:
            TypeError: If the request is of any other kind, connected or not; a type checker catches that first.
        """
        get_request_field(request)
        connection = self._sdk._get_connection()
        return None if connection is None else connection.send(request, correlation_id)

    def lookup_instruments(self, request: openfeed_api_pb2.InstrumentRequest) -> Future[list[openfeed_instrument_pb2.InstrumentDefinition]]:
        """Looks up instruments through the gateway, waiting for the connection.

        Args:
            request: Openfeed's `InstrumentRequest`, sent as given.

        Returns:
            A future with the definitions found. It fails with an `InstrumentLookupError` if the server refuses the lookup
            or finds nothing, and with an error if the connection drops first.
        """
        return self._sdk._lookup_instruments(request)


class Diagnostics:
    """`sdk.diagnostics`: what support and debugging need, not trading."""

    def __init__(self, sdk: DesktopSdk) -> None:
        """Created by the SDK; an application uses the one on the SDK."""
        self._sdk = sdk

    def get_connection_token(self) -> str | None:
        """The gateway's session token; None while not connected."""
        connection = self._sdk._get_connection()
        return None if connection is None else connection.token

    def get_last_messages(self, symbol: str) -> LastMessages | None:
        """The market's last snapshot and last applied update, as they came.

        Args:
            symbol: A subscribed symbol (an alias included), or the definition symbol of an instrument a subscription
                holds.

        Returns:
            The messages; None while no subscription holds the instrument.
        """
        return self._sdk._get_last_messages(symbol)

    def get_subscription_info(self) -> list[SubscriptionInfo]:
        """Every Openfeed subscription the SDK holds: service, type, symbol, state, number of subscribers, error and last
        response.
        """
        return self._sdk._get_subscription_info()


class DesktopSdk:
    """Live quotes, order books and cumulative volume from Openfeed, kept up to date.

    Create one for the application's lifetime and log in and out of it as needed. Subscriptions stay open across logins:
    one made while logged out waits, and every login sends them all again. Logging out clears market state.

    Every method is safe to call from any thread, listeners included. Listeners run one at a time, in order, on the SDK's
    delivery thread, and never inside the call that registered them.

    Example:
        ```python
        with DesktopSdk() as sdk:
            sdk.login(Credentials(url="ws://localhost:9000", username="your-username", password="your-password"))
            subscription = sdk.subscribe_quote(
                "ES*0", Listener(on_event=lambda event: print(event.kind, event.instrument.symbol, event.formatted.session.last))
            )
            ...
            subscription.close()
        ```
    """

    def __init__(self, *, logger: logging.Logger | None = None, coalesce_interval_ms: float = 0) -> None:
        """Creates the SDK, logged out. Nothing connects until `login`.

        Args:
            logger: Where the SDK and the Openfeed client log, `logging.getLogger("barchart_desktop_sdk")` by default.
            coalesce_interval_ms: Holds subscription events for up to this many milliseconds, then delivers one per
                subscription with the latest state, instead of one per message. 0, the default: an event per message.
        """
        self._log = logger if logger is not None else logging.getLogger("barchart_desktop_sdk")
        self._coalesce_seconds = coalesce_interval_ms / 1000
        self._lock = threading.Condition(threading.Lock())
        self._registry = Registry()
        self._store = InstrumentStore(self._log)
        self._connection_listeners = ConnectionListeners(
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            on_rejected=self._on_rejected,
            on_heartbeat=self._on_heartbeat,
            on_message=self._on_message,
            schedule=self._schedule,
        )

        self._state: ConnectionState = "logged-out"
        self._connection: Connection | None = None
        self._disposed = False

        self._ops: list[_Op] = []
        self._work: deque[_Work] = deque()
        self._held: dict[str, _HeldChange] = {}
        self._held_deadline: float | None = None

        self._listener_ids = itertools.count()
        self._state_listeners: dict[int, Callable[[ConnectionState], None]] = {}
        self._heartbeat_listeners: dict[int, HeartbeatListener] = {}
        self._raw_listeners: dict[int, _MessageListener] = {}
        self._removed_listeners: dict[int, Callable[[str], None]] = {}
        # the store's instruments → their copies per name (_get_named)
        self._named: weakref.WeakKeyDictionary[Instrument, dict[str, Instrument]] = weakref.WeakKeyDictionary()
        self._waiters: list[Future[None]] = []
        self._snapshot_requests: set[Future[SnapshotMessage]] = set()

        self.raw = RawAccess(self)
        """Protocol-level access: raw subscriptions, snapshots, messages and requests."""
        self.diagnostics = Diagnostics(self)
        """What support and debugging need."""

        self._delivery_thread = threading.Thread(target=self._run_delivery, name="barchart-desktop-sdk-delivery", daemon=True)
        self._delivery_thread.start()

    def __enter__(self) -> Self:
        """The SDK itself; the `with` block disposes of it when it ends."""
        return self

    def __exit__(self, _type: type[BaseException] | None, _error: BaseException | None, _traceback: TracebackType | None) -> None:
        """Disposes of the SDK."""
        self.dispose()

    # ---- lifetime ----

    @property
    def connection_state(self) -> ConnectionState:
        """The connection's current state. `on_connection_state` reports each change."""
        with self._lock:
            return self._state

    def login(self, credentials: Credentials) -> None:
        """Connects to the Openfeed server, and sends every open subscription once connected.

        Logging in while logged in logs out first. Returns at once: follow the connection with `on_connection_state` or
        wait for it with `when_connected`.

        Args:
            credentials: The server's URL, and the username and password to log in with.

        Raises:
            RuntimeError: If the SDK has been disposed.
        """
        with self._lock:
            self._assert_not_disposed()
            self._logout()
            connection = Connection(credentials, self._connection_listeners, self._log)
            self._connection = connection
            self._set_state("connecting")
            self._registry.attach(connection)
            self._log_wire("Subscribing to", self._registry.get_all_entries())
            connection.start()

    def logout(self) -> None:
        """Disconnects and clears all market state.

        Subscriptions stay open, as `pending`, and are sent again at the next login. Does nothing when logged out.
        """
        with self._lock:
            self._logout()

    def dispose(self) -> None:
        """Logs out, closes every subscription and removes every listener, and stops the SDK's threads.

        Pending `when_connected` and `raw.request_snapshot` futures fail. The SDK can't be used afterwards. Calling it
        again does nothing. A `with` block calls it when it ends.
        """
        with self._lock:
            if self._disposed:
                return
            self._logout()
            self._disposed = True
            # never processed, so not in the registry
            for op in self._ops:
                op.subscriber.closed = True
            self._ops = []
            self._registry.clear()
            self._reject_waiters(RuntimeError(_DISPOSED))
            for request in self._snapshot_requests:
                self._enqueue(partial(settle_exception, request, RuntimeError(_DISPOSED)), kept=True)
            self._snapshot_requests.clear()
            self._state_listeners.clear()
            self._heartbeat_listeners.clear()
            self._raw_listeners.clear()
            self._removed_listeners.clear()
            self._lock.notify_all()
        # what's queued still runs: futures that were rejected, say
        if threading.current_thread() is not self._delivery_thread:
            self._delivery_thread.join(_DISPOSE_WAIT_SECONDS)

    def on_connection_state(self, listener: Callable[[ConnectionState], None]) -> Callable[[], None]:
        """Listens for connection state changes.

        Args:
            listener: Called with each new state.

        Returns:
            A function that removes the listener.
        """
        return self._add_listener(self._state_listeners, listener)

    def on_heartbeat(self, listener: HeartbeatListener) -> Callable[[], None]:
        """Listens for the server's heartbeats.

        Args:
            listener: Called with each heartbeat's server time (None if it has none) and the `HeartBeat` message.

        Returns:
            A function that removes the listener.
        """
        return self._add_listener(self._heartbeat_listeners, listener)

    def on_removed(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """Listens for symbols that are no longer subscribed (see `is_subscribed`), because the last subscription holding
        them closed.

        A logout isn't reported here: it clears everything and reports `logged-out` through `on_connection_state`.

        Args:
            listener: Called with each removed symbol.

        Returns:
            A function that removes the listener.
        """
        return self._add_listener(self._removed_listeners, listener)

    def is_subscribed(self, symbol: str) -> bool:
        """Whether a subscription currently holds the symbol's instrument.

        That includes a raw subscription whose responses named it, such as a root subscription (`ZC^UDS`) with its
        instruments, by their definition symbols (`ZCZ26`): not by the symbol the raw subscription was made with. The
        `get_…` methods answer only while this is true.

        Args:
            symbol: A subscribed symbol (an alias included), or the definition symbol of an instrument a subscription
                holds.
        """
        with self._lock:
            return self._is_subscribed(symbol)

    def when_connected(self) -> Future[None]:
        """Waits for the connection, through a login if there hasn't been one yet.

        A listener mustn't wait on the result: settling it needs the delivery thread the listener runs on. Use
        `add_done_callback` there. In `asyncio`, `await asyncio.wrap_future(sdk.when_connected())`.

        Returns:
            A future that's done once connected, at once if already connected. It fails if the server rejects the
            credentials or the SDK is disposed first.
        """
        future: Future[None] = Future()
        with self._lock:
            if self._disposed:
                future.set_exception(RuntimeError(_DISPOSED))
            elif self._state == "connected":
                future.set_result(None)
            else:
                self._waiters.append(future)
        return future

    # ---- subscriptions ----

    def subscribe_quote(self, symbol: str, listener: Listener[QuoteEvent], service: Service = openfeed_pb2.REAL_TIME) -> Subscription:
        """Subscribes to a symbol's quote: last trade, session prices and volume, best bid and offer, and trading status.

        The first callback comes after this returns, never inside it: an `initial` event with the state the SDK already
        has, or the stored error if the server already refused the subscription. Subscriptions with the same symbol and
        service share one Openfeed subscription.

        Args:
            symbol: The symbol, or an alias such as `ZC*1`, which follows rolls.
            listener: Gets each `QuoteEvent`, and the server's refusal if there is one.
            service: The Openfeed service, `REAL_TIME` by default.

        Returns:
            The subscription, `pending` until the server answers. Close it to unsubscribe.

        Raises:
            RuntimeError: If the SDK has been disposed.

        Example:
            ```python
            def on_quote(event: QuoteEvent) -> None:
                session, bbo = event.formatted.session, event.formatted.best_bid_offer
                print(event.kind, session.last, bbo.bid_price, bbo.ask_price)


            subscription = sdk.subscribe_quote("ES*0", Listener(on_event=on_quote, on_error=print))
            ```
        """
        return self._subscribe(service, openfeed_api_pb2.ALL, symbol, "quote", cast("Listener[Event]", listener))

    def subscribe_depth(self, symbol: str, listener: Listener[BookEvent], service: Service = openfeed_pb2.REAL_TIME) -> Subscription:
        """Subscribes to a symbol's order book, by price level.

        The first callback comes after this returns, never inside it: an `initial` event with the state the SDK already
        has, or the stored error if the server already refused the subscription. Subscriptions with the same symbol and
        service share one Openfeed subscription.

        Args:
            symbol: The symbol, or an alias such as `ZC*1`, which follows rolls.
            listener: Gets each `BookEvent`, and the server's refusal if there is one.
            service: The Openfeed service, `REAL_TIME` by default.

        Returns:
            The subscription, `pending` until the server answers. Close it to unsubscribe.

        Raises:
            RuntimeError: If the SDK has been disposed.

        Example:
            ```python
            def on_book(event: BookEvent) -> None:
                print("best bid", event.book.bids[0].price, "best ask", event.book.asks[0].price)


            subscription = sdk.subscribe_depth("ES*0", Listener(on_event=on_book))
            ```
        """
        return self._subscribe(service, openfeed_api_pb2.DEPTH_PRICE, symbol, "depth", cast("Listener[Event]", listener))

    def subscribe_cumulative_volume(
        self, symbol: str, listener: Listener[CumulativeVolumeEvent], service: Service = openfeed_pb2.REAL_TIME_SNAPSHOT
    ) -> Subscription:
        """Subscribes to a symbol's cumulative volume: the volume traded at each price.

        It also holds the symbol's quote subscription on `REAL_TIME`, whose trades keep the volume up to date; if that one
        is refused, this subscription's listeners get its error, with `required_by` set. Not every Openfeed server sends
        volume at price: on one that doesn't, the subscription goes `active` but no events come.

        The first callback comes after this returns, never inside it, as for `subscribe_quote`.

        Args:
            symbol: The symbol, or an alias such as `ZC*1`, which follows rolls.
            listener: Gets each `CumulativeVolumeEvent`, and the server's refusal if there is one.
            service: The Openfeed service, `REAL_TIME_SNAPSHOT` by default.

        Returns:
            The subscription, `pending` until the server answers. Close it to unsubscribe.

        Raises:
            RuntimeError: If the SDK has been disposed.

        Example:
            ```python
            def on_volume(event: CumulativeVolumeEvent) -> None:
                for level in event.changed:
                    print(level.price, level.volume)


            subscription = sdk.subscribe_cumulative_volume("ES*0", Listener(on_event=on_volume))
            ```
        """
        return self._subscribe(service, openfeed_api_pb2.CUMULATIVE_VOLUME, symbol, "cumulative_volume", cast("Listener[Event]", listener))

    # ---- state ----
    # A symbol is a subscribed name (aliases included) or the definition symbol of a market a subscription holds.

    def get_instrument(self, symbol: str) -> Instrument | None:
        """The instrument's definition: its description, exchange, price format and more.

        Its `symbol` is the one passed here, so `get_instrument("ES*0").symbol` is `ES*0`; the contract's own is
        `definition.symbol`.

        Args:
            symbol: A subscribed symbol (an alias included), or the definition symbol of an instrument a subscription
                holds.

        Returns:
            The instrument; None until the SDK has its definition, or when no subscription holds it.
        """
        with self._lock:
            instrument = self._store.get_instrument_by_id(self._market_id_of(symbol))
            return None if instrument is None else self._get_named(instrument, symbol)

    def get_quote(self, symbol: str) -> QuoteRecord | None:
        """The current quote, as the last quote event delivered it. For the same values as strings, use
        `get_formatted_quote`.

        Args:
            symbol: A subscribed symbol (an alias included), or the definition symbol of an instrument a subscription
                holds.

        Returns:
            The quote; None until the SDK has one, or when no subscription holds it.
        """
        with self._lock:
            return self._store.get_quote_by_id(self._market_id_of(symbol))

    def get_formatted_quote(self, symbol: str) -> FormattedQuote | None:
        """The current quote as strings in the instrument's price format, as a quote event's `formatted` carries them.

        Args:
            symbol: A subscribed symbol (an alias included), or the definition symbol of an instrument a subscription
                holds.

        Returns:
            The formatted quote; None until the SDK has a quote, or when no subscription holds it.
        """
        with self._lock:
            market_id = self._market_id_of(symbol)
            quote = self._store.get_quote_by_id(market_id)
            instrument = self._store.get_instrument_by_id(market_id)
        return FormattedQuote(quote, instrument) if quote is not None and instrument is not None else None

    def get_book(self, symbol: str) -> Book | None:
        """The current order book, as the last book event delivered it.

        Each side has the instrument's `book_depth` levels, best price first; an empty level has price and size 0.

        Args:
            symbol: A subscribed symbol (an alias included), or the definition symbol of an instrument a subscription
                holds.

        Returns:
            The book; None until the SDK has one, or when no subscription holds it.
        """
        with self._lock:
            return self._store.get_book_by_id(self._market_id_of(symbol))

    def get_cumulative_volume(self, symbol: str) -> tuple[PriceVolume, ...] | None:
        """The volume traded at each price, as the last cumulative volume event delivered it.

        Args:
            symbol: A subscribed symbol (an alias included), or the definition symbol of an instrument a subscription
                holds.

        Returns:
            The levels, ascending by price; None until a snapshot brings volume at price, or when no subscription holds
            it.
        """
        with self._lock:
            return self._store.get_cumulative_volume_by_id(self._market_id_of(symbol))

    # ---- for RawAccess and Diagnostics ----

    def _get_connection(self) -> Connection | None:
        with self._lock:
            return self._connection

    def _get_last_messages(self, symbol: str) -> LastMessages | None:
        with self._lock:
            return self._store.get_last_messages_by_id(self._market_id_of(symbol))

    def _get_subscription_info(self) -> list[SubscriptionInfo]:
        with self._lock:
            return [
                SubscriptionInfo(
                    service=entry.service,
                    type=entry.type,
                    symbol=entry.symbol,
                    state=entry.status,
                    subscribers=len(entry.subscribers),
                    error=entry.error,
                    response=entry.response,
                )
                for entry in self._registry.get_all_entries()
            ]

    def _add_raw_listener(self, listener: _MessageListener) -> Callable[[], None]:
        return self._add_listener(self._raw_listeners, listener)

    def _request_snapshot(self, symbol: str, service: Service) -> Future[SnapshotMessage]:
        """A raw ALL subscription, closed once the first snapshot (or an error) arrives. Still pending at `dispose`, it fails
        there.
        """
        request: Future[SnapshotMessage] = Future()
        subscriptions: list[Subscription] = []

        def settle() -> None:
            with self._lock:
                self._snapshot_requests.discard(request)
            subscriptions[0].close()

        def on_message(message: openfeed_api_pb2.OpenfeedGatewayMessage, metadata: MessageMetadata) -> None:
            if message.HasField("marketSnapshot"):
                settle()
                snapshot = message.marketSnapshot
                definition = metadata.definition
                if definition is None:
                    with self._lock:
                        instrument = self._store.get_instrument_by_id(str(snapshot.marketId))
                    definition = instrument.definition if instrument is not None else None
                settle_result(request, SnapshotMessage(instrument_definition=definition, market_snapshot=snapshot))

        def on_error(error: Exception) -> None:
            settle()
            settle_exception(request, error)

        with self._lock:
            if self._disposed:
                request.set_exception(RuntimeError(_DISPOSED))
                return request
            self._snapshot_requests.add(request)
            subscriptions.append(self._queue_subscribe(service, openfeed_api_pb2.ALL, symbol, "raw", RawListener(on_message, on_error)))
        return request

    def _lookup_instruments(self, request: openfeed_api_pb2.InstrumentRequest) -> Future[list[openfeed_instrument_pb2.InstrumentDefinition]]:
        result: Future[list[openfeed_instrument_pb2.InstrumentDefinition]] = Future()

        def on_lookup(lookup: Future[list[openfeed_instrument_pb2.InstrumentDefinition]]) -> None:
            error = lookup.exception()
            if error is not None:
                settle_exception(result, error)
            else:
                settle_result(result, lookup.result())

        def on_connected(connected: Future[None]) -> None:
            error = connected.exception()
            connection = self._get_connection()
            if error is not None:
                settle_exception(result, error)
            # the connection can drop between when_connected finishing and this
            elif connection is None:
                settle_exception(result, ConnectionError(_NOT_CONNECTED))
            else:
                connection.get_instrument(request).add_done_callback(on_lookup)

        self.when_connected().add_done_callback(on_connected)
        return result

    # ---- subscribing, on any thread ----

    def _subscribe(
        self, service: Service, subscription_type: SubscriptionType, symbol: str, method: Method, listener: Listener[Event] | RawListener | None
    ) -> Subscription:
        with self._lock:
            return self._queue_subscribe(service, subscription_type, symbol, method, listener)

    def _queue_subscribe(
        self, service: Service, subscription_type: SubscriptionType, symbol: str, method: Method, listener: Listener[Event] | RawListener | None
    ) -> Subscription:
        self._assert_not_disposed()
        subscriber = Subscriber(EntryKey(service, subscription_type, symbol), method, listener, self._log, self._lock, self._queue_close)
        self._ops.append(_Op("subscribe", subscriber))
        self._lock.notify_all()
        return subscriber.handle

    def _queue_close(self, subscriber: Subscriber) -> None:
        self._ops.append(_Op("close", subscriber))
        self._lock.notify_all()

    # ---- the delivery thread ----

    def _run_delivery(self) -> None:
        while True:
            with self._lock:
                # a held change can set a deadline while this waits, so the wait is worked out again each time
                while not self._has_delivery():
                    self._lock.wait(self._get_seconds_to_held())
                calls = self._take_delivery()
                if calls is None:
                    return
            for call in calls:
                try:
                    call()
                except Exception:
                    self._log.exception("OF SDK > A listener threw")

    def _is_held_due(self) -> bool:
        return self._held_deadline is not None and time.monotonic() >= self._held_deadline

    def _get_seconds_to_held(self) -> float | None:
        return None if self._held_deadline is None else max(0.0, self._held_deadline - time.monotonic())

    def _has_delivery(self) -> bool:
        return bool(self._ops or self._work or self._disposed) or self._is_held_due()

    def _take_delivery(self) -> list[_Call] | None:
        """What to run next, or None once disposed with nothing left."""
        if self._ops:
            ops = self._ops
            self._ops = []
            return self._process(ops)
        if self._is_held_due():
            return self._take_held()
        if self._work:
            return [self._work.popleft().call]
        return None

    def _process(self, ops: list[_Op]) -> list[_Call]:
        """One batch of subscribes and closes, always in this order:
        1. subscribes: each joins its entry (created, and sent when logged in, if it's new). A subscriber closed in the same
           batch is skipped, so a subscribe and close together never reach the registry or the wire;
        2. closes: each leaves its entry; at zero references the entry is unsubscribed and removed, and symbols left without
           a state subscription, or a dependency, lose their instrument. Symbols no subscription has any more are reported to
           on_removed. Because subscribes come first, a remount (one subscriber closes, another subscribes to the same symbol)
           never takes the count to zero;
        3. first deliveries for the subscribers that opened: the entry's error, or the current state as an `initial` event.
           State is read here, under the lock that also covers the subscribe, so a message comes either in it or after it.

        Returns what to call, once the lock is released.
        """
        closed = dict.fromkeys(op.subscriber for op in ops if op.kind == "close")
        opened = [op.subscriber for op in ops if op.kind == "subscribe" and op.subscriber not in closed]
        created: list[Entry] = []
        opened_entries: list[Entry] = []
        for subscriber in opened:
            entry = self._registry.acquire(subscriber.key.service, subscriber.key.type, subscriber.key.symbol, created)
            entry.subscribers[subscriber] = None
            subscriber.entry = entry
            opened_entries.append(entry)

        # symbols that removed entries had: their own, and the own symbols of the instruments they named
        maybe_removed: dict[str, None] = {}
        removed_entries: list[Entry] = []
        for subscriber in closed:
            # one closed in the batch it subscribed in never opened, and has no entry
            for removed in self._leave(subscriber):
                removed_entries.append(removed)
                maybe_removed[removed.symbol] = None
                for market_id in removed.markets:
                    # its definition symbol, read before its state may go (the registry let go of it with the market's last entry)
                    instrument = self._store.get_instrument_by_id(market_id)
                    if instrument is not None:
                        maybe_removed[instrument.definition.symbol] = None
                    self._try_remove_id(market_id)

        calls: list[_Call] = [
            partial(listener, symbol) for symbol in maybe_removed if not self._is_subscribed(symbol) for listener in self._removed_listeners.values()
        ]
        # what this batch sent: entries created or removed while logged in
        self._log_wire("Subscribing to", created)
        self._log_wire("Unsubscribing from", removed_entries)

        for subscriber, entry in zip(opened, opened_entries, strict=True):
            first = self._get_first_delivery(subscriber, entry)
            if first is not None:
                calls.append(first)
        return calls

    def _leave(self, subscriber: Subscriber) -> list[Entry]:
        """A closing subscriber leaves its entry; returns the entries removed with it."""
        entry = subscriber.entry
        if entry is None:
            return []
        del entry.subscribers[subscriber]
        return self._registry.remove(entry)

    def _get_first_delivery(self, subscriber: Subscriber, entry: Entry) -> _Call | None:
        """A new subscriber's first delivery: the error it sees, or the instrument's current state as one `initial` event
        (nothing for a raw subscriber, or before the instrument has state).
        """
        error = get_subscriber_error(entry)
        if error is not None:
            return partial(subscriber.fail, error)

        market_id = self._market_id_of(entry.symbol)
        instrument = self._store.get_instrument_by_id(market_id)
        raw = self._store.get_last_snapshot_message_by_id(market_id)
        if instrument is None or raw is None:
            return None
        named = self._get_named(instrument, entry.symbol)
        event = _make_event(subscriber, entry.symbol, named, "initial", raw, self._store.get_state_parts_by_id(market_id))
        return None if event is None else partial(subscriber.emit, event)

    def _take_held(self) -> list[_Call]:
        held = list(self._held.values())
        self._clear_held()
        return [call for item in held for call in self._route_change(item.change, item.raw)]

    def _clear_held(self) -> None:
        self._held.clear()
        self._held_deadline = None

    def _route_change(self, change: InstrumentChange, raw: openfeed_api_pb2.OpenfeedGatewayMessage) -> list[_Call]:
        """A change's events, for the subscriptions open now."""
        instrument = self._store.get_instrument_by_id(change.market_id)
        if instrument is None:
            return []
        calls: list[_Call] = []
        for symbol in self._registry.get_symbols_by_id(change.market_id):
            for entry in self._registry.get_entries(symbol):
                named = self._get_named(instrument, entry.symbol)
                for subscriber in entry.subscribers:
                    event = _make_event(subscriber, entry.symbol, named, change.kind, raw, change.parts)
                    if event is not None:
                        calls.append(partial(subscriber.emit, event))
        return calls

    # ---- the socket thread ----

    def _on_connected(self, connection: Connection) -> None:
        with self._lock:
            if connection is self._connection:
                self._set_state("connected")

    def _on_disconnected(self, connection: Connection) -> None:
        with self._lock:
            if connection is self._connection:
                self._set_state("disconnected")

    def _on_rejected(self, connection: Connection) -> None:
        with self._lock:
            if connection is self._connection:
                self._registry.detach()
                self._clear_market_state()
                self._set_state("credentials-rejected")

    def _on_heartbeat(self, connection: Connection, heartbeat: openfeed_pb2.HeartBeat) -> None:
        server_time = to_time(heartbeat.transactionTime)
        with self._lock:
            if connection is self._connection:
                for listener in self._heartbeat_listeners.values():
                    self._enqueue(partial(listener, server_time, heartbeat))

    def _on_message(self, connection: Connection, message: openfeed_api_pb2.OpenfeedGatewayMessage, metadata: MessageMetadata) -> None:
        """Each message, in order: the state changes now, and its listeners are queued, each given what it would have got had
        it run now: the subscribers of an instrument about to go still get that instrument's last message.
        """
        with self._lock:
            if connection is not self._connection:
                return
            calls: list[_Call] = []
            subscribers: list[Subscriber] = []
            if message.HasField("subscriptionResponse"):
                result = self._registry.on_response(message.subscriptionResponse)
                calls.extend(partial(subscriber.fail, error) for subscriber, error in result.failures)
                subscribers = [subscriber for entry in result.matched for subscriber in get_message_subscribers(entry)]
            elif message.HasField("instrumentAction"):
                # subscribers of the old instrument get the action before their entries stop naming it
                subscribers = self._get_market_subscribers(message)
                self._on_instrument_action(message.instrumentAction)
            else:
                market_id = get_message_market_id(message)
                # only a market a subscription named has state; anything else (raw.send's, or one still arriving after
                # its subscription closed) reaches the raw listeners only
                if market_id is not None and self._registry.has_id(market_id):
                    if message.HasField("instrumentDefinition"):
                        self._registry.on_definition(message.instrumentDefinition)
                    change = self._store.apply(message)
                    if change is not None:
                        calls.extend(self._deliver_or_hold(change, message))
                    subscribers = self._registry.get_message_subscribers_by_id(market_id)

            calls.extend(partial(subscriber.deliver_message, message, metadata) for subscriber in subscribers)
            calls.extend(partial(listener, message, metadata) for listener in self._raw_listeners.values())
            for call in calls:
                self._enqueue(call)

    def _get_market_subscribers(self, message: openfeed_api_pb2.OpenfeedGatewayMessage) -> list[Subscriber]:
        market_id = get_message_market_id(message)
        return [] if market_id is None else self._registry.get_message_subscribers_by_id(market_id)

    def _deliver_or_hold(self, change: InstrumentChange, raw: openfeed_api_pb2.OpenfeedGatewayMessage) -> list[_Call]:
        """A change's events now or, when coalescing, held with the market's earlier changes. Routing a held one happens on
        delivery, so it follows the subscriptions open then.
        """
        if self._coalesce_seconds <= 0:
            return self._route_change(change, raw)
        held = self._held.get(change.market_id)
        self._held[change.market_id] = _HeldChange(_merge_changes(held.change, change) if held is not None else change, raw)
        if self._held_deadline is None:
            self._held_deadline = time.monotonic() + self._coalesce_seconds
            self._lock.notify_all()
        return []

    def _on_instrument_action(self, action: openfeed_pb2.InstrumentAction) -> None:
        """Alias rolls and exchange moves:
        - ALIAS_CHANGED: the alias (`ZC*1`, say) stops naming the old instrument, so its subscribers stop getting the old
          instrument's messages and `get_quote(alias)` is None. The server then repeats the subscription response for the
          alias, which names the new instrument, followed by its definition and snapshot.
        - EXCHANGE_MOVE: the instrument and its state move to the new marketId, and the entries that named it name the new one.
          So do the changes held for the old marketId. The action's new definition replaces the instrument.
        Other actions are ignored.
        """
        if not action.HasField("instrument"):
            return
        market_id = str(action.instrument.marketId)
        if action.action == openfeed_pb2.ALIAS_CHANGED:
            self._registry.alias_changed(action.oldAlias, market_id)
            self._try_remove_id(market_id)
        elif action.action == openfeed_pb2.EXCHANGE_MOVE and action.HasField("newInstrument"):
            new_id = str(action.newInstrument.marketId)
            self._store.moved(market_id, action.newInstrument)
            self._registry.moved(market_id, new_id)
            self._registry.on_definition(action.newInstrument)
            self._move_held(market_id, new_id)

    def _move_held(self, from_id: str, to_id: str) -> None:
        """Coalescing: a market's held changes, delivered by marketId, follow it to its new one."""
        held = self._held.pop(from_id, None) if from_id != to_id else None
        if held is None:
            return
        moved = dataclasses.replace(held.change, market_id=to_id)
        already = self._held.get(to_id)
        self._held[to_id] = _HeldChange(_merge_changes(moved, already.change), already.raw) if already is not None else _HeldChange(moved, held.raw)

    # ---- under the lock ----

    def _schedule(self, call: _Call) -> None:
        with self._lock:
            self._enqueue(call, kept=True)

    def _enqueue(self, call: _Call, *, kept: bool = False) -> None:
        self._work.append(_Work(call, kept))
        self._lock.notify_all()

    def _add_listener(self, listeners: dict[int, _T], listener: _T) -> Callable[[], None]:
        with self._lock:
            listener_id = next(self._listener_ids)
            listeners[listener_id] = listener

        def remove() -> None:
            with self._lock:
                listeners.pop(listener_id, None)

        return remove

    def _logout(self) -> None:
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        self._registry.detach()
        connection.close()
        self._clear_market_state()
        self._set_state("logged-out")

    def _clear_market_state(self) -> None:
        """The market state goes, and with it the events still waiting that describe it."""
        self._store.clear()
        self._clear_held()
        self._work = deque(work for work in self._work if work.kept)

    def _set_state(self, state: ConnectionState) -> None:
        if self._state == state:
            return
        self._state = state
        if state == "connected":
            waiters = self._waiters
            self._waiters = []
            for waiter in waiters:
                self._enqueue(partial(settle_result, waiter, None), kept=True)
        elif state == "credentials-rejected":
            self._reject_waiters(RuntimeError(_REJECTED))
        for listener in self._state_listeners.values():
            self._enqueue(partial(listener, state), kept=True)

    def _reject_waiters(self, error: Exception) -> None:
        waiters = self._waiters
        self._waiters = []
        for waiter in waiters:
            self._enqueue(partial(settle_exception, waiter, error), kept=True)

    def _market_id_of(self, symbol: str) -> str | None:
        """The market a subscribed symbol names, or a named market's definition symbol (a root subscription's instrument)."""
        return self._registry.get_market_id(symbol)

    def _get_named(self, instrument: Instrument, symbol: str) -> Instrument:
        """The instrument as `symbol` reaches it: the store's, with `symbol` set to that name. One per name and instrument,
        so a name's events share it; a new definition makes a new instrument, and so new copies.
        """
        by_name = self._named.setdefault(instrument, {})
        named = by_name.get(symbol)
        if named is None:
            named = by_name[symbol] = dataclasses.replace(instrument, symbol=symbol)
        return named

    def _is_subscribed(self, symbol: str) -> bool:
        market_id = self._market_id_of(symbol)
        return market_id is not None and self._registry.has_id(market_id)

    def _try_remove_id(self, market_id: str) -> None:
        """The one place that decides a market's state goes: removed only once no subscription names it (a raw one keeps its
        instruments' state: its wire subscription stays open), so it may not succeed.
        """
        if not self._registry.has_id(market_id):
            self._store.remove_id(market_id)

    def _log_wire(self, action: str, entries: list[Entry]) -> None:
        labels = get_wire_labels(entries)
        if labels:
            self._log.info("OF SDK > %s %s", action, labels)

    def _assert_not_disposed(self) -> None:
        if self._disposed:
            raise RuntimeError(_DISPOSED)
