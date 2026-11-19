"""Public types: events, listeners, SubscriptionError, ConnectionState."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Literal, TypeAlias, TypeVar

from openfeed.generated import openfeed_api_pb2, openfeed_pb2

if TYPE_CHECKING:
    from datetime import datetime

    from openfeed.generated import openfeed_instrument_pb2

    from ._book import Book
    from ._formatted import FormattedQuote
    from ._instrument import Instrument
    from ._quote import QuoteRecord
    from ._store import PriceVolume

    Result: TypeAlias = openfeed_api_pb2.Result
    Service: TypeAlias = openfeed_pb2.Service
    SubscriptionType: TypeAlias = openfeed_api_pb2.SubscriptionType

ConnectionState = Literal["logged-out", "connecting", "connected", "disconnected", "credentials-rejected"]
"""`credentials-rejected` lasts until the next `login` or `logout`. `logout` returns to `logged-out` from any state."""

SubscriptionState = Literal["pending", "active", "error", "closed"]
"""`pending` until the server answers, and again after each logout until the next login's answer."""

EventKind = Literal["initial", "snapshot", "update"]
"""`initial`: the current state, delivered once to a new subscriber; `raw` is the instrument's last snapshot message.
`snapshot` / `update`: a message from the wire was just applied; `raw` is that message.
"""

_T = TypeVar("_T")
_E = TypeVar("_E")


@dataclass(frozen=True, slots=True)
class Credentials:
    """What `sdk.login` connects with."""

    url: str
    """The Openfeed server's WebSocket URL, such as `ws://localhost:9000` for the Barchart Desktop API."""
    username: str
    """The username to log in with."""
    password: str = ""
    """The password to log in with."""


def _get_enum_name(get_name: Callable[[_T], str], value: _T) -> str:
    """An enum value's name; a value newer than this copy of the protocol types has none, so it stays a number."""
    try:
        return get_name(value)
    except ValueError:
        return str(value)


def _get_error_message(  # noqa: PLR0913, PLR0917
    result: Result, symbol: str, subscription_type: SubscriptionType, service: Service, server_message: str, required_by: SubscriptionType | None
) -> str:
    requirement = "" if required_by is None else f", which {_get_enum_name(openfeed_api_pb2.SubscriptionType.Name, required_by)} needs,"
    refusal = (
        f"{_get_enum_name(openfeed_api_pb2.Result.Name, result)}: {_get_enum_name(openfeed_api_pb2.SubscriptionType.Name, subscription_type)}"
        f" on {symbol} ({_get_enum_name(openfeed_pb2.Service.Name, service)}){requirement} was refused."
    )
    return f'{refusal} Server said: "{server_message}"' if server_message else refusal


class SubscriptionError(Exception):
    """A subscription the server refused.

    `message` says what was refused and ends with the server's own text, which is kept as it came in `server_message`.
    The message is for reading; code should decide on `result`, Openfeed's `Result` value.

    Example:
        ```python
        error.message  # 'INSUFFICIENT_PRIVILEGES: DEPTH_PRICE on ES*0 (REAL_TIME) was refused. Server said: "…"'
        ```
    """

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        result: Result,
        symbol: str,
        type: SubscriptionType,  # noqa: A002
        service: Service,
        server_message: str,
        required_by: SubscriptionType | None = None,
    ) -> None:
        """Created by the SDK, which passes it to `on_error`; an application has no need to create one."""
        super().__init__(_get_error_message(result, symbol, type, service, server_message, required_by))
        self.result = result
        """Openfeed's result code for the refusal. Decide on this, not on the message's text."""
        self.symbol = symbol
        """The symbol the subscription asked for."""
        self.type = type
        """The subscription the server refused."""
        self.service = service
        """The service that subscription asked for."""
        self.server_message = server_message
        """The server's own text, as it came; empty if it sent none."""
        self.required_by = required_by
        """Set when the refused subscription is one that the subscriber's own type depends on: on a cumulative volume
        subscriber's error, `CUMULATIVE_VOLUME`, with `type` being the `ALL` it holds. Otherwise None.
        """

    @property
    def message(self) -> str:
        """What was refused, then the server's text: the same as `str(error)`. For reading, not for matching."""
        return str(self)


class InstrumentLookupError(Exception):
    """An instrument lookup the server refused, or found nothing for.

    The text is for reading; code should decide on `result`, Openfeed's `Result` value.
    """

    def __init__(self, result: Result, server_message: str) -> None:
        """Created by the SDK, which fails `raw.lookup_instruments` with it; an application has no need to create one."""
        name = _get_enum_name(openfeed_api_pb2.Result.Name, result)
        super().__init__(
            f'{name}: the instrument lookup failed. Server said: "{server_message}"' if server_message else f"{name}: the lookup failed."
        )
        self.result = result
        """Openfeed's result code for the failed lookup. Decide on this, not on the message's text."""
        self.server_message = server_message
        """The server's own text, as it came; empty if it sent none."""


@dataclass(frozen=True, slots=True)
class MessageMetadata:
    """What the Openfeed client knows about a message beyond the protocol: its symbol names and the market's definition."""

    symbol_names: tuple[str, ...]
    """The names the message's instrument was subscribed under."""
    definition: openfeed_instrument_pb2.InstrumentDefinition | None
    """The instrument's definition, if the client has it."""


@dataclass(frozen=True, slots=True)
class Listener(Generic[_E]):
    """What a subscribe method calls.

    Every callback is optional. None is called inside `subscribe`, and none after the subscription's `close()` returns.
    The type parameter is the subscription's event: `QuoteEvent`, `BookEvent` or `CumulativeVolumeEvent`.
    """

    on_event: Callable[[_E], None] | None = None
    """Each event: the current state once (`initial`), then every change. Leave it out to only hold the subscription, or
    to only use `on_message`.
    """
    on_error: Callable[[SubscriptionError], None] | None = None
    """The server refused the subscription; the subscription is then in `error`."""
    on_message: Callable[[openfeed_api_pb2.OpenfeedGatewayMessage, MessageMetadata], None] | None = None
    """Also every raw message for the subscription's instrument, as a raw subscription gets it (responses, and every
    message for the instruments its responses named), after the SDK applied it: after the `on_event` it causes, or before
    it when events are coalesced (`coalesce_interval_ms`). For a consumer that needs the state and the raw stream from
    one subscription, such as a chart.
    """


@dataclass(frozen=True, slots=True)
class RawListener:
    """Gets the subscription's responses and every message for the markets those responses named."""

    on_message: Callable[[openfeed_api_pb2.OpenfeedGatewayMessage, MessageMetadata], None]
    """Each message, with what the Openfeed client knows about it."""
    on_error: Callable[[SubscriptionError], None] | None = None
    """The server refused the subscription; the subscription is then in `error`."""


HeartbeatListener = Callable[["datetime | None", openfeed_pb2.HeartBeat], None]
"""A gateway heartbeat: its server time (None when the heartbeat carries none) and the protocol message."""

SendableRequest = (
    openfeed_api_pb2.ExchangeRequest
    | openfeed_api_pb2.InstrumentRequest
    | openfeed_api_pb2.InstrumentReferenceRequest
    | openfeed_api_pb2.ListSubscriptionsRequest
    | openfeed_api_pb2.SubscriptionRequest
)
"""What `raw.send` takes: one request of a kind that carries a token and a correlation id, which `send` fills in."""


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    """What `subscribe_quote`'s listener gets: the quote as numbers and as strings."""

    symbol: str
    """The symbol as subscribed, an alias such as `ZC*0` included."""
    instrument: Instrument
    """The instrument the symbol stands for, with `symbol` set to it; after an alias rolls, the new contract."""
    kind: EventKind
    """Why the event came: the current state for a new subscriber, or a message just applied."""
    raw: openfeed_api_pb2.OpenfeedGatewayMessage
    """The message behind the event: the one just applied, or for `initial` the instrument's last snapshot."""
    quote: QuoteRecord
    """The whole quote, the same object as `get_quote`."""
    formatted: FormattedQuote
    """The same quote as strings, formatted when read: `formatted.session.last` → "112-16",
    `formatted.session.decimal.last` → "112.50000".
    """


@dataclass(frozen=True, slots=True)
class BookEvent:
    """What `subscribe_depth`'s listener gets."""

    symbol: str
    """The symbol as subscribed, an alias such as `ZC*0` included."""
    instrument: Instrument
    """The instrument the symbol stands for, with `symbol` set to it; after an alias rolls, the new contract."""
    kind: EventKind
    """Why the event came: the current state for a new subscriber, or a message just applied."""
    raw: openfeed_api_pb2.OpenfeedGatewayMessage
    """The message behind the event: the one just applied, or for `initial` the instrument's last snapshot."""
    book: Book
    """The whole book, the same object as `get_book`."""


@dataclass(frozen=True, slots=True)
class CumulativeVolumeEvent:
    """What `subscribe_cumulative_volume`'s listener gets: every level, and the ones this message changed."""

    symbol: str
    """The symbol as subscribed, an alias such as `ZC*0` included."""
    instrument: Instrument
    """The instrument the symbol stands for, with `symbol` set to it; after an alias rolls, the new contract."""
    kind: EventKind
    """Why the event came: the current state for a new subscriber, or a message just applied."""
    raw: openfeed_api_pb2.OpenfeedGatewayMessage
    """The message behind the event: the one just applied, or for `initial` the instrument's last snapshot."""
    levels: tuple[PriceVolume, ...]
    """Every level, ascending by price: the same tuple as `get_cumulative_volume`. A snapshot starts them over."""
    changed: tuple[PriceVolume, ...]
    """The levels this message changed, each price once with its new volume, ascending. On `initial` and `snapshot` the
    levels start over, so it's every level and any other level a consumer kept is gone.
    """


@dataclass(frozen=True, slots=True)
class SubscriptionInfo:
    """One Openfeed subscription the SDK holds, for diagnostics. Subscribers with the same symbol, type and service share
    one.
    """

    service: Service
    """The Openfeed service it was made on."""
    type: SubscriptionType
    """The Openfeed subscription type, such as `ALL` for quotes."""
    symbol: str
    """The symbol as subscribed."""
    state: Literal["pending", "active", "error"]
    """Where it stands: `pending`, `active` or `error`."""
    subscribers: int
    """How many subscriptions share it."""
    error: SubscriptionError | None
    """The server's refusal, while in `error`."""
    response: openfeed_api_pb2.SubscriptionResponse | None
    """The server's last answer; None until it answers."""


@dataclass(frozen=True, slots=True)
class SnapshotMessage:
    """What `raw.request_snapshot`'s future resolves with."""

    instrument_definition: openfeed_instrument_pb2.InstrumentDefinition | None
    """The instrument's definition, if the server sent one."""
    market_snapshot: openfeed_pb2.MarketSnapshot
    """The snapshot: the market's state when the server sent it."""
