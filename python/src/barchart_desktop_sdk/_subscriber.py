"""One subscribe call, and what its subscribers see of their registry entry."""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from ._types import BookEvent, CumulativeVolumeEvent, Listener, MessageMetadata, QuoteEvent, RawListener, SubscriptionError, SubscriptionState

if TYPE_CHECKING:
    import logging
    import threading
    from collections.abc import Callable

    from openfeed.generated import openfeed_api_pb2

    from ._types import Service, SubscriptionType

Method = Literal["quote", "depth", "cumulative_volume", "raw"]
"""Which events a subscriber gets; `raw` subscribers get messages instead."""

Event = QuoteEvent | BookEvent | CumulativeVolumeEvent
EntryState = Literal["pending", "active", "error"]


def subscription_key(service: Service, subscription_type: SubscriptionType, symbol: str) -> str:
    """The key of one subscription, `service|type|symbol`: entries are keyed by it."""
    return f"{service}|{subscription_type}|{symbol}"


@dataclass(frozen=True, slots=True)
class EntryKey:
    """What a subscriber asked for."""

    service: Service
    type: SubscriptionType
    symbol: str


@dataclass(eq=False, slots=True)
class Entry:
    """One subscription on the wire. Dicts stand in for ordered sets: an alias's latest market is the last one it named."""

    key: str
    service: Service
    type: SubscriptionType
    symbol: str

    # desired: kept across logins
    subscribers: dict[Subscriber, None] = field(default_factory=dict)
    refs: int = 0
    """Subscribers plus dependent entries."""
    dependents: dict[Entry, None] = field(default_factory=dict)
    """Entries that hold this one as a dependency (cumulative volume on ALL); its error is theirs too."""
    dependencies: list[Entry] = field(default_factory=list)

    # wire: one login's
    client_id: int | None = None
    status: EntryState = "pending"
    error: SubscriptionError | None = None
    response: openfeed_api_pb2.SubscriptionResponse | None = None
    markets: dict[str, None] = field(default_factory=dict)
    """The markets this entry's successful responses named; raw subscribers get their messages. Read-only outside the
    registry, which changes it only through its name and unname methods, so its market index stays in step.
    """


_dependent_errors: weakref.WeakKeyDictionary[SubscriptionError, SubscriptionError] = weakref.WeakKeyDictionary()


def get_dependent_error(error: SubscriptionError, dependent: Entry) -> SubscriptionError:
    """A dependency's error as its dependent's subscribers see it, naming what needed it. One per error, as only
    cumulative volume has a dependency, so `on_error` and the handle's `error` give the same object.
    """
    dependent_error = _dependent_errors.get(error)
    if dependent_error is None:
        dependent_error = SubscriptionError(error.result, error.symbol, error.type, error.service, error.server_message, dependent.type)
        _dependent_errors[error] = dependent_error
    return dependent_error


def get_subscriber_error(entry: Entry) -> SubscriptionError | None:
    """The error an entry's subscribers see: its own, else a failed dependency's, since cumulative volume doesn't update
    without its ALL. Diagnostics show each entry's own.
    """
    if entry.error is not None:
        return entry.error
    dependency_error = next((dependency.error for dependency in entry.dependencies if dependency.error is not None), None)
    return None if dependency_error is None else get_dependent_error(dependency_error, entry)


def get_subscriber_state(entry: Entry) -> EntryState:
    """The state an entry's subscribers see: `error` when get_subscriber_error has one, else the entry's own."""
    return "error" if get_subscriber_error(entry) is not None else entry.status


class Subscription:
    """What a subscribe method returns. Closing it is the only way to give up the subscription.

    After `subscribe` returns, the listener gets the current state as one `initial` event (or a stored error through
    `on_error`), on the delivery thread; events report changes from then on.
    """

    __slots__ = ("_subscriber",)

    def __init__(self, subscriber: Subscriber) -> None:
        """Created by the SDK, which returns it from a subscribe method; an application has no need to create one."""
        self._subscriber = subscriber

    @property
    def state(self) -> SubscriptionState:
        """Where the subscription stands: `pending` until the server answers, then `active` or `error`; `closed` once
        closed.
        """
        return self._subscriber.get_state()

    @property
    def error(self) -> SubscriptionError | None:
        """The server's refusal, if the subscription is in `error` (also one stored from an earlier subscriber)."""
        return self._subscriber.get_error()

    def close(self) -> None:
        """Idempotent. No new callback starts after it returns; one already running on the delivery thread finishes."""
        self._subscriber.close()


class Subscriber:
    """One subscribe call. Callers only ever see its handle. `lock` guards its entry and closing; `on_close` is called
    with it held.
    """

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        key: EntryKey,
        method: Method,
        listener: Listener[Event] | RawListener | None,
        log: logging.Logger,
        lock: threading.Condition,
        on_close: Callable[[Subscriber], None],
    ) -> None:
        self.key = key
        self.method = method
        self.listener = listener
        self.closed = False
        self.entry: Entry | None = None
        """Set when the delivery thread processes the subscribe; until then the handle is `pending`."""
        self.handle = Subscription(self)
        self._log = log
        self._lock = lock
        self._on_close = on_close

    def get_state(self) -> SubscriptionState:
        with self._lock:
            if self.closed:
                return "closed"
            return get_subscriber_state(self.entry) if self.entry is not None else "pending"

    def get_error(self) -> SubscriptionError | None:
        with self._lock:
            return None if self.closed or self.entry is None else get_subscriber_error(self.entry)

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._on_close(self)

    def _call(self, call: Callable[[], None]) -> None:
        """Calls the listener, logging instead of raising."""
        if self.closed:
            return
        try:
            call()
        except Exception:
            self._log.exception("OF SDK > A subscription listener threw: %s", subscription_key(self.key.service, self.key.type, self.key.symbol))

    def fail(self, error: SubscriptionError) -> None:
        on_error = self.listener.on_error if self.listener is not None else None
        if on_error is not None:
            self._call(lambda: on_error(error))

    def emit(self, event: Event) -> None:
        on_event = self.listener.on_event if isinstance(self.listener, Listener) else None
        if on_event is not None:
            self._call(lambda: on_event(event))

    def deliver_message(self, message: openfeed_api_pb2.OpenfeedGatewayMessage, metadata: MessageMetadata) -> None:
        on_message = self.listener.on_message if self.listener is not None else None
        if on_message is not None:
            self._call(lambda: on_message(message, metadata))


def get_message_subscribers(entry: Entry) -> list[Subscriber]:
    """An entry's subscribers that take raw messages: raw subscribers, and state subscribers whose listener has `on_message`."""
    return [subscriber for subscriber in entry.subscribers if subscriber.listener is not None and subscriber.listener.on_message is not None]
