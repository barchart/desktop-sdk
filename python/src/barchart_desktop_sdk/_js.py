"""JavaScript's number semantics, which formatted and parsed prices follow.

Prices are formatted and read with the rules of JavaScript's `toFixed`, `Math.round`, `String(number)`, `parseFloat` and
`parseInt`. Python's equivalents differ at the edges (`f"{0.125:.2f}"` is "0.12", `toFixed` gives "0.13"; `str(100.0)` is
"100.0", `String(100)` is "100"), so these reproduce JavaScript's.
"""

import math
import re
from decimal import ROUND_HALF_UP, Context, Decimal

# enough digits for any double below 1e21 to 100 decimals; Decimal's default context has 28
_CONTEXT = Context(prec=200)

_FLOAT_PREFIX = re.compile(r"[+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", re.ASCII)
_INT_PREFIX = re.compile(r"[+-]?\d+", re.ASCII)


def js_round(x: float) -> int:
    """`Math.round`: to the nearest integer, halves towards +∞ (Python's `round` takes halves to even)."""
    floor = math.floor(x)
    # exact for doubles: x and its floor are within one unit of each other
    return floor + 1 if x - floor >= 0.5 else floor


def number_to_string(x: float) -> str:
    """`String(x)`: integers without ".0", exponents only from 1e21 and below 1e-6, as ECMAScript's Number::toString."""
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    # repr is the shortest string that reads back as the same double, as JavaScript's digits are
    digits_tuple = Decimal(repr(abs(float(x)))).normalize().as_tuple()
    digits = "".join(map(str, digits_tuple.digits))
    k = len(digits)
    n = k + int(digits_tuple.exponent)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = f"{digits[:n]}.{digits[n:]}"
    elif -6 < n <= 0:
        body = f"0.{'0' * -n}{digits}"
    else:
        e = n - 1
        mantissa = digits[0] + (f".{digits[1:]}" if k > 1 else "")
        body = f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"
    return sign + body


def to_fixed(x: float, digits: float) -> str:
    """`x.toFixed(digits)`: rounds the double's exact value, ties away from zero; `digits` is truncated to an integer."""
    if math.isnan(x) or math.isinf(x) or abs(x) >= 1e21:
        return number_to_string(x)
    # -0 prints as "0.00"
    exact = Decimal(x if x != 0 else 0.0)
    rounded = exact.quantize(Decimal(1).scaleb(-int(digits)), rounding=ROUND_HALF_UP, context=_CONTEXT)
    return format(rounded, "f")


def parse_float(text: str) -> float:
    """`parseFloat`: the longest number at the start of the text, after whitespace; NaN without one."""
    match = _FLOAT_PREFIX.match(text.lstrip())
    return float(match.group()) if match else math.nan


def parse_int(text: str) -> int | None:
    """`parseInt(text, 10)`: the digits at the start of the text, after whitespace; None where JavaScript gives NaN."""
    match = _INT_PREFIX.match(text.lstrip())
    return int(match.group()) if match else None
