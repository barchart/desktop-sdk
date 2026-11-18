"""Price formats and formatting.

`format_price` and `format_decimal` are public, for prices shown in a format of the user's choosing; an instrument
formats in its own format with `Instrument.format_price`.
"""

import math
import re
from dataclasses import dataclass

from ._js import js_round, number_to_string, parse_float, parse_int, to_fixed

_THOUSANDS = re.compile(r"\B(?=(\d{3})+(?!\d))", re.ASCII)


@dataclass(frozen=True, slots=True)
class PriceFraction:
    """Fractional notation, `112-16` for 112 and 16/32."""

    denominator: int
    """The fraction's unit: 32 for 32nds."""
    subdenominator: float
    """Parts of each unit, written as one more digit: 2 for half 32nds, so 112 and 16.5/32 is `112-165`. 1 for none."""


@dataclass(frozen=True, slots=True)
class PriceFormat:
    """How an instrument's prices are written: a fixed number of decimals, or a fraction such as 32nds.

    An instrument's comes from its definition (`instrument.price_format`); `format_price` takes one you choose.
    """

    price_scaling: float
    """What the instrument's prices are multiplied by when the SDK reads them from the feed. Formatting doesn't use it:
    the prices it formats are already scaled.
    """
    num_decimals: float | None = None
    """Decimal notation with this many decimals. Takes precedence over `fraction`."""
    fraction: PriceFraction | None = None
    """Fractional notation, `112-16` for 112 and 16/32."""


def get_integer_part(value: float) -> int:
    return math.floor(value)


def _get_decimal_part(value: float) -> float:
    magnitude = abs(value)
    return magnitude - math.floor(magnitude)


def _to_fixed_number(num: float, digits: int) -> float:
    power = 10.0**digits
    return js_round(num * power) / power


def format_decimal(value: str, thousands_separator: str = ",", decimal_separator: str = ".") -> str:
    """Adds thousands separators to a decimal number's text.

    Args:
        value: The number as text, such as `f"{price:.2f}"`.
        thousands_separator: The separator to add, `","` by default.
        decimal_separator: The decimal separator `value` uses, `"."` by default. It's kept as is.

    Returns:
        The text with separators: `"1250.5"` becomes `"1,250.5"`.

    Example:
        ```python
        format_decimal("1234567.25")  # "1,234,567.25"
        format_decimal("1234567,25", ".", ",")  # "1.234.567,25"
        ```
    """
    parts = value.split(decimal_separator)
    integer_part = _THOUSANDS.sub(lambda _: thousands_separator, parts[0])
    decimal_part = parts[1] if len(parts) > 1 else ""
    return f"{integer_part}{decimal_separator if decimal_part else ''}{decimal_part}"


def format_price(price: float | None, price_format: PriceFormat) -> str:
    """Formats a price in a format you choose. To format in an instrument's own format, use its `format_price` instead.

    Args:
        price: The price. None gives `""`.
        price_format: A fixed number of decimals (`num_decimals`, with thousands separators) or a fraction (`fraction`,
            as in "112-16" for 112 and 16/32). With neither, the number as is.

    Returns:
        The formatted price.

    Example:
        ```python
        format_price(1234.5, PriceFormat(price_scaling=1, num_decimals=2))  # "1,234.50"
        format_price(112.5, PriceFormat(price_scaling=1, fraction=PriceFraction(denominator=32, subdenominator=1)))  # "112-16"
        format_price(112.515625, PriceFormat(price_scaling=1, fraction=PriceFraction(denominator=32, subdenominator=2)))  # "112-165"
        format_price(None, PriceFormat(price_scaling=1, num_decimals=2))  # ""
        ```
    """
    if price is None:
        return ""

    if price_format.num_decimals is not None:
        return format_decimal(to_fixed(price, price_format.num_decimals))
    if price_format.fraction is not None:
        denominator = price_format.fraction.denominator
        sub_denominator_shift = 1 if price_format.fraction.subdenominator > 1 else 0

        # of the absolute value: a negative value's floor is one lower
        integer_part = get_integer_part(abs(price))
        decimal_part = _get_decimal_part(price)

        formula_result = _to_fixed_number((decimal_part / (1 / denominator)) * 10.0**sub_denominator_shift, 1)

        format_digits = 3
        if denominator * price_format.fraction.subdenominator == 32 or denominator == 64:
            format_digits = 2
        elif denominator * price_format.fraction.subdenominator < 32:
            format_digits = 1

        formatted = str(get_integer_part(formula_result)).rjust(format_digits, "0")

        sign = "-" if price < 0 else ""
        return f"{sign}{integer_part}-{formatted}"

    return number_to_string(price)


def _get_decimal_count(price_format: PriceFormat) -> float | None:
    if price_format.num_decimals is not None:
        return price_format.num_decimals
    if price_format.fraction is None:
        return None
    unit = 1 / (price_format.fraction.denominator * price_format.fraction.subdenominator)
    decimals = 0
    while decimals < 12 and abs(unit * 10**decimals - js_round(unit * 10**decimals)) > 1e-9:
        decimals += 1
    return decimals


def format_price_decimal(price: float | None, price_format: PriceFormat) -> str:
    """Decimal notation, whatever the format's own: as many decimals as its increment needs (a fraction's smallest
    part exactly: 1/32 → 5, 1/64 → 6), with thousands separators. Unknown formats get the number as is.
    """
    if price is None:
        return ""
    decimals = _get_decimal_count(price_format)
    return number_to_string(price) if decimals is None else format_decimal(to_fixed(price, decimals))


def _get_sub_fraction(digit: int, subdenominator: float) -> float | None:
    """With a subdenominator, format_price writes a fraction's last digit as the first decimal digit of its sub-fraction
    (quarters of a 32nd: ¼ → 2, ½ → 5, ¾ → 7). The sub-fraction a digit stands for, or None for a digit it never writes.
    """
    k = 0
    while k < subdenominator:
        if math.floor((k / subdenominator) * 10) == digit:
            return k / subdenominator
        k += 1
    return None


def price_to_number(price: str, price_format: PriceFormat) -> float | None:
    """Parses a price in the format's notation; None for no text, NaN for text that isn't a number."""
    if not price:
        return None

    clean_price = price.replace(",", "")

    if price_format.num_decimals is not None:
        return parse_float(clean_price)
    if price_format.fraction is not None:
        is_negative = clean_price.startswith("-")
        price_value = clean_price[1:] if is_negative else clean_price
        sign = -1 if is_negative else 1
        denominator = price_format.fraction.denominator
        sub_denominator_shift = 1 if price_format.fraction.subdenominator > 1 else 0

        parts = price_value.split("-")
        parsed_integer_part = parse_int(parts[0])
        parsed_decimal_part = parse_int(parts[1]) if len(parts) > 1 else None
        if parsed_integer_part is None or parsed_decimal_part is None:
            return parse_float(clean_price)

        # the last digit as format_price writes it (2 is ¼ in quarter 32nds); a digit it never writes reads as tenths
        sub_fraction = (
            _get_sub_fraction(parsed_decimal_part % 10, price_format.fraction.subdenominator)
            if sub_denominator_shift > 0 and parsed_decimal_part >= 0
            else None
        )
        units = (
            parsed_decimal_part * (1 / 10.0**sub_denominator_shift) if sub_fraction is None else math.floor(parsed_decimal_part / 10) + sub_fraction
        )
        formula_result = (parsed_integer_part * denominator + units) / denominator

        return sign * formula_result
    return parse_float(clean_price)
