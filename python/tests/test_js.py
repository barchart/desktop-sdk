"""JavaScript's number semantics, where Python's own differ; the expected values are what Node prints."""

import math

import pytest

from barchart_desktop_sdk._js import js_round, number_to_string, parse_float, parse_int, to_fixed


@pytest.mark.parametrize(
    ("x", "digits", "expected"),
    [
        (0.125, 2, "0.13"),  # an exact tie, away from zero; Python's format gives "0.12"
        (-0.125, 2, "-0.13"),
        (2.5, 0, "3"),
        (1.005, 2, "1.00"),  # 1.005 is just below the tie as a double
        (-0.001, 2, "-0.00"),
        (-0.0, 2, "0.00"),
        (1234.5, 2.9, "1234.50"),  # digits are truncated
        (1e21, 2, "1e+21"),
    ],
)
def test_to_fixed(x: float, digits: float, expected: str) -> None:
    assert to_fixed(x, digits) == expected


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        (100, "100"),
        (100.0, "100"),
        (100.5, "100.5"),
        (-0.0, "0"),
        (0.1 + 0.2, "0.30000000000000004"),
        (123456789012345680000, "123456789012345680000"),
        (1e21, "1e+21"),
        (1.5e-7, "1.5e-7"),
        (0.000001, "0.000001"),
        (math.inf, "Infinity"),
    ],
)
def test_number_to_string(x: float, expected: str) -> None:
    assert number_to_string(x) == expected


def test_js_round_takes_halves_up() -> None:
    assert [js_round(2.5), js_round(-2.5), js_round(0.49999999999999994), js_round(-0.5)] == [3, -2, 0, 0]


def test_parse_float_and_parse_int_read_the_number_at_the_start() -> None:
    assert parse_float(" 12.5abc") == 12.5
    assert parse_float("1.") == 1.0
    assert math.isnan(parse_float("abc"))
    assert parse_int("016-") == 16
    assert parse_int("-") is None
