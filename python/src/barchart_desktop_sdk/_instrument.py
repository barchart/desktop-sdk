"""Instrument: the protocol-derived part of an instrument: scaling, price format and price formatting."""

import logging
import re
from dataclasses import dataclass, field
from typing import Literal, overload

from openfeed.generated import openfeed_instrument_pb2

from ._price_format import PriceFormat, PriceFraction, format_price, format_price_decimal, get_integer_part, get_unit_decimals, price_to_number

PriceNotation = Literal["native", "decimal"]
"""`native`: the instrument's own notation (fractional for 32nds, say: "112-16"); `decimal`: "112.50000"."""

_InstrumentType = openfeed_instrument_pb2.InstrumentDefinition.InstrumentType
_OptionType = openfeed_instrument_pb2.InstrumentDefinition.OptionType

# A Barchart futures symbol: root, month code, year (ZN, Z, 26 in ZNZ26).
_FUTURES_SYMBOL = re.compile(r"^([A-Z][A-Z0-9$\-!.]{0,2})([A-Z]|[*])([0-9]{4}|[0-9]{1,2})$")

# Underlying roots of the treasury options, which are written in 64ths. Their futures can have the same denominator and
# are written in parts of a 32nd, so the definition's price format alone can't tell them apart.
_TREASURY_OPTION_ROOTS = frozenset({"ZB", "ZN", "UD", "TN", "ZT", "ZF"})


# Compared by identity, as the SDK keeps one per market and hands out a copy per name (DesktopSdk._get_named).
@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class Instrument:
    """An instrument, from its Openfeed definition: what it is, where it trades, and how its prices are read and written.

    Every event carries one, and `sdk.get_instrument` returns it.
    """

    market_id: str
    """Openfeed's id for the market, as a string."""
    symbol: str
    """The symbol it was subscribed with, as the event's `symbol`, or the one passed to `sdk.get_instrument`: for `ES*0`,
    it's `ES*0`. The definition's own symbol, which can be the exchange's (`ESZ6`), is `definition.symbol`.
    """
    description: str
    """A human-readable description of the market."""
    instrument_type: int
    """What kind of instrument it is, such as a future, an option or an equity: an Openfeed `InstrumentType` value."""
    exchange_code: str
    """The exchange's ISO 10383 market identifier code (MIC)."""
    time_zone_name: str
    """The exchange's time zone, as a tz database name such as `America/Chicago`."""
    book_depth: int
    """How many price levels each side of the order book has."""
    minimum_price_increment: float
    """The smallest price change, in the market's currency."""
    price_format: PriceFormat
    """How the instrument's prices are written, which `format_price` and `parse_price` use."""
    definition: openfeed_instrument_pb2.InstrumentDefinition = field(repr=False)
    """The Openfeed definition everything else here comes from, with the fields the SDK doesn't model."""
    _price_denominator: int = field(repr=False)
    _quantity_denominator: int = field(repr=False)

    @overload
    def price(self, raw: int) -> float: ...
    @overload
    def price(self, raw: None) -> None: ...
    def price(self, raw: int | None) -> float | None:
        """Converts a price from an Openfeed message to a number; None when the message has none."""
        return None if raw is None else (float(raw) / self._price_denominator) * self.price_format.price_scaling

    @overload
    def quantity(self, raw: int) -> int: ...
    @overload
    def quantity(self, raw: None) -> None: ...
    def quantity(self, raw: int | None) -> int | None:
        """Converts a quantity from an Openfeed message to a number, without its fraction; None when the message has none."""
        return None if raw is None else get_integer_part(float(raw) / self._quantity_denominator)

    def format_price(self, price: float | None, notation: PriceNotation = "native") -> str:
        """Formats a price in the instrument's own format.

        Args:
            price: The price; None gives `""`.
            notation: `native` (the default) for the instrument's own notation, such as `"112-16"`, or `decimal` for
                `"112.50000"`.

        Returns:
            The formatted price.
        """
        return format_price_decimal(price, self.price_format) if notation == "decimal" else format_price(price, self.price_format)

    def parse_price(self, text: str) -> float | None:
        """Reads a price written in the instrument's own format, the reverse of `format_price`.

        Commas as thousands separators are allowed. A fraction with a subdenominator ends in one digit for the part of
        the unit, as `format_price` writes it: in quarter 32nds, `"104-002"` is 104 and ¼ of a 32nd. So formatted prices
        parse back exactly. A last digit `format_price` never writes reads as tenths.

        Args:
            text: The price as text, such as `"112-16"` or `"1,234.50"`.

        Returns:
            The price; None for empty text, and `nan` for text that isn't a price.
        """
        return price_to_number(text, self.price_format)


def _get_option_underlying_root(definition: openfeed_instrument_pb2.InstrumentDefinition) -> str | None:
    """An option's underlying root, from the definition's Barchart underlying symbol; None for anything else.

    An option is a definition with an option type; an equity option's root is its instrument group, never a treasury,
    so it's left out.
    """
    if definition.optionType == _OptionType.UNKNOWN_OPTION_TYPE:
        return None
    if definition.instrumentType == _InstrumentType.EQUITY_OPTION:
        return None
    match = _FUTURES_SYMBOL.match(definition.underlying)
    return match.group(1) if match else None


def _get_price_format(definition: openfeed_instrument_pb2.InstrumentDefinition, log: logging.Logger) -> PriceFormat:
    if not definition.HasField("priceFormat"):
        log.warning("Unknown price format for %s", ", ".join(s.symbol for s in definition.symbols))
        return PriceFormat(price_scaling=1)
    price_format = definition.priceFormat
    exponent = definition.priceScalingExponent
    # a denominator that can't be one: prices are written as plain numbers, as for an unknown format
    is_valid = price_format.denominator > 0
    if not is_valid:
        log.warning("Invalid price format denominator for %s: %s", definition.symbol, price_format.denominator)

    if not price_format.isFractional:
        price_scaling = 1.0
        if exponent < 0:
            price_scaling /= 10**-exponent
        elif exponent > 0:
            price_scaling *= 10**exponent
        # the decimals the denominator's unit needs: 100 → 2, and 8 → 3, since 1/8 is 0.125
        return PriceFormat(price_scaling=price_scaling, num_decimals=get_unit_decimals(1 / price_format.denominator) if is_valid else None)
    if not is_valid:
        return PriceFormat(price_scaling=1)

    # Anything finer than the base is written as parts of it: in 32nds, 64 is half 32nds; in 64ths, 128 is half 64ths.
    base = 64 if _get_option_underlying_root(definition) in _TREASURY_OPTION_ROOTS else 32
    denominator = price_format.denominator
    if denominator > base:
        # a float, as in JavaScript: 48ths give 1.5
        fraction = PriceFraction(denominator=base, subdenominator=denominator / base)
    else:
        fraction = PriceFraction(denominator=denominator, subdenominator=price_format.subDenominator or 1)
    return PriceFormat(price_scaling=1, fraction=fraction)


def create_instrument(definition: openfeed_instrument_pb2.InstrumentDefinition, log: logging.Logger) -> Instrument:
    price_denominator = definition.priceDenominator or 1
    quantity_denominator = definition.quantityDenominator or 1
    return Instrument(
        market_id=str(definition.marketId),
        symbol=definition.symbol,
        description=definition.description,
        instrument_type=definition.instrumentType,
        exchange_code=definition.exchangeCode,
        time_zone_name=definition.timeZoneName,
        book_depth=definition.bookDepth,
        minimum_price_increment=definition.minimumPriceIncrement,
        price_format=_get_price_format(definition, log),
        definition=definition,
        _price_denominator=price_denominator,
        _quantity_denominator=quantity_denominator,
    )
