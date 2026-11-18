"""FormattedQuote: a quote's groups as strings, formatted when read."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar, Generic, Self, TypeVar

from ._instrument import Instrument, PriceNotation
from ._js import number_to_string
from ._price_format import format_decimal
from ._quote import BestBidOfferRecord, QuoteRecord, SessionRecord

R = TypeVar("R")

FormattedValues = dict[str, str | dict[str, str]]
"""A view as a dict: its fields, and in the native view its decimal ones under "decimal"."""


def format_quantity(value: int | None) -> str:
    """A quantity with thousands separators; "" for none."""
    return "" if value is None else format_decimal(number_to_string(value))


class _View(Generic[R]):
    """A group's prices and quantities as strings, "" for a field that isn't set, and what every group's view shares:
    `values`, `decimal`, `to_dict`. Its fields are the subclass's own properties.
    """

    _fields: ClassVar[tuple[str, ...]]

    def __init_subclass__(cls) -> None:
        cls._fields = tuple(name for name, member in vars(cls).items() if isinstance(member, property))

    def __init__(self, record: R, instrument: Instrument, notation: PriceNotation) -> None:
        """Created by `FormattedQuote`; an application has no need to create one."""
        self._record = record
        self._instrument = instrument
        self._notation = notation
        self._values: Mapping[str, str] | None = None
        self._decimal: Self | None = None

    @property
    def values(self) -> Mapping[str, str]:
        """Every field, as a read-only mapping."""
        if self._values is None:
            self._values = MappingProxyType({field: getattr(self, field) for field in self._fields})
        return self._values

    @property
    def decimal(self) -> Self:
        """The same view with prices in decimal notation; built on its first read."""
        if self._decimal is None:
            self._decimal = type(self)(self._record, self._instrument, "decimal")
        return self._decimal

    def to_dict(self) -> FormattedValues:
        """The values as a plain dict, and in the native view the decimal ones under "decimal"."""
        values: FormattedValues = dict(self.values)
        if self._notation == "native":
            values["decimal"] = dict(self.decimal.values)
        return values

    def _price(self, price: float | None) -> str:
        return self._instrument.format_price(price, self._notation)


class FormattedSession(_View[SessionRecord]):
    """A session's prices and quantities as strings, with the same field names as `SessionRecord`; `""` for a field that
    isn't set.

    Prices are in the instrument's own notation, and in decimal notation under `decimal`. `values` has every field as
    one read-only mapping.

    Example:
        ```python
        formatted.session.last  # "112-16"
        formatted.session.decimal.last  # "112.50000"
        formatted.session.volume  # "1,234"
        ```
    """

    # prices

    @property
    def open(self) -> str:
        """The session's opening price."""
        return self._price(self._record.open)

    @property
    def high(self) -> str:
        """The session's highest price so far."""
        return self._price(self._record.high)

    @property
    def low(self) -> str:
        """The session's lowest price so far."""
        return self._price(self._record.low)

    @property
    def close(self) -> str:
        """The session's closing price."""
        return self._price(self._record.close)

    @property
    def settle(self) -> str:
        """The session's settlement price."""
        return self._price(self._record.settle)

    @property
    def prev_settle(self) -> str:
        """The previous session's settlement price, as the last snapshot sent it with this session."""
        return self._price(self._record.prev_settle)

    @property
    def last(self) -> str:
        """The last price."""
        return self._price(self._record.last)

    @property
    def last_trade(self) -> str:
        """The price of Openfeed's `lastTrade`."""
        return self._price(self._record.last_trade)

    # quantities

    @property
    def last_size(self) -> str:
        """The quantity traded at the last price."""
        return format_quantity(self._record.last_size)

    @property
    def volume(self) -> str:
        """The quantity traded in the session so far."""
        return format_quantity(self._record.volume)

    @property
    def open_interest(self) -> str:
        """The number of open contracts."""
        return format_quantity(self._record.open_interest)

    @property
    def number_of_trades(self) -> str:
        """How many trades the session has had so far."""
        return format_quantity(self._record.number_of_trades)


class FormattedBestBidOffer(_View[BestBidOfferRecord]):
    """The best bid and offer as strings, with the same field names as `BestBidOfferRecord`; `""` for a field that isn't
    set.

    Prices are in the instrument's own notation, and in decimal notation under `decimal`. `values` has every field as one
    read-only mapping.
    """

    # prices

    @property
    def bid_price(self) -> str:
        """The highest bid."""
        return self._price(self._record.bid_price)

    @property
    def ask_price(self) -> str:
        """The lowest offer."""
        return self._price(self._record.ask_price)

    # quantities

    @property
    def bid_size(self) -> str:
        """The quantity bid at `bid_price`."""
        return format_quantity(self._record.bid_size)

    @property
    def ask_size(self) -> str:
        """The quantity offered at `ask_price`."""
        return format_quantity(self._record.ask_size)

    @property
    def bid_order_count(self) -> str:
        """How many orders make up `bid_size`, when the feed sends it."""
        return format_quantity(self._record.bid_order_count)

    @property
    def ask_order_count(self) -> str:
        """How many orders make up `ask_size`, when the feed sends it."""
        return format_quantity(self._record.ask_order_count)


class FormattedQuote:
    """A quote as strings, in the record's groups: each session and the best bid and offer, each with its native and
    `decimal` notation; quantities with thousands separators in both. The status and dates stay on the record:
    formatting dates takes a time zone and a locale. Each group is built on its first read; `to_dict` gives every
    group's values.
    """

    def __init__(self, quote: QuoteRecord, instrument: Instrument) -> None:
        """Created by the SDK, for events and `get_formatted_quote`; an application has no need to create one."""
        self._quote = quote
        self._instrument = instrument
        self._session: FormattedSession | None = None
        self._previous_session: FormattedSession | None = None
        self._best_bid_offer: FormattedBestBidOffer | None = None

    @property
    def session(self) -> FormattedSession:
        """The current session."""
        if self._session is None:
            self._session = FormattedSession(self._quote.session, self._instrument, "native")
        return self._session

    @property
    def previous_session(self) -> FormattedSession:
        """The session before it."""
        if self._previous_session is None:
            self._previous_session = FormattedSession(self._quote.previous_session, self._instrument, "native")
        return self._previous_session

    @property
    def best_bid_offer(self) -> FormattedBestBidOffer:
        """The best bid and offer."""
        if self._best_bid_offer is None:
            self._best_bid_offer = FormattedBestBidOffer(self._quote.best_bid_offer, self._instrument, "native")
        return self._best_bid_offer

    def to_dict(self) -> dict[str, FormattedValues]:
        """Every group's values as plain dicts, each with its decimal ones under "decimal"."""
        return {
            "session": self.session.to_dict(),
            "previous_session": self.previous_session.to_dict(),
            "best_bid_offer": self.best_bid_offer.to_dict(),
        }
