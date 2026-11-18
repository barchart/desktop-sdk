import pytest
from helpers import definition, fraction_format, quiet

from barchart_desktop_sdk._instrument import create_instrument
from barchart_desktop_sdk._price_format import PriceFraction

thirty_seconds = fraction_format(32)
sixty_fourths = fraction_format(64)
half_sixty_fourths = fraction_format(128)


def fraction(
    instrument_type: str, underlying: str, price_format: dict[str, object] | None = None, option_type: str | None = None
) -> PriceFraction | None:
    d = definition(
        instrument_type=instrument_type,
        underlying=underlying,
        price_format=price_format or thirty_seconds,
        price_denominator=1000000,
        option_type=option_type or ("UNKNOWN_OPTION_TYPE" if instrument_type == "FUTURE" else "CALL"),
    )
    return create_instrument(d, quiet).price_format.fraction


@pytest.mark.parametrize("underlying", ["ZNZ26", "ZBH7", "UDM26", "TNU26"])
def test_formats_these_options_in_64ths(underlying: str) -> None:
    assert fraction("OPTION", underlying, sixty_fourths) == PriceFraction(denominator=64, subdenominator=1)


@pytest.mark.parametrize("underlying", ["ZTZ26", "ZFZ26"])
def test_formats_these_options_in_half_64ths(underlying: str) -> None:
    assert fraction("OPTION", underlying, half_sixty_fourths) == PriceFraction(denominator=64, subdenominator=2)


def test_formats_futures_with_the_same_denominators_in_parts_of_a_32nd() -> None:
    assert fraction("FUTURE", "", sixty_fourths) == PriceFraction(denominator=32, subdenominator=2)
    assert fraction("FUTURE", "", half_sixty_fourths) == PriceFraction(denominator=32, subdenominator=4)
    assert fraction("EQUITY_OPTION", "ZNZ26", sixty_fourths) == PriceFraction(denominator=32, subdenominator=2)
    assert fraction("OPTION", "ZNZ26", sixty_fourths, "UNKNOWN_OPTION_TYPE") == PriceFraction(denominator=32, subdenominator=2)


def test_leaves_coarser_formats_to_the_definition() -> None:
    assert fraction("FUTURE", "") == PriceFraction(denominator=32, subdenominator=1)
    assert fraction("OPTION", "ZCZ26", fraction_format(8)) == PriceFraction(denominator=8, subdenominator=1)
    assert fraction("OPTION", "ZNZ26") == PriceFraction(denominator=32, subdenominator=1)
    assert fraction("FUTURE", "", fraction_format(32, 4)) == PriceFraction(denominator=32, subdenominator=4)


def test_reads_an_option_by_its_option_type() -> None:
    assert fraction("UNKNOWN_INSTRUMENT_TYPE", "ZTZ26", half_sixty_fourths, "PUT") == PriceFraction(denominator=64, subdenominator=2)


def test_formats_in_64ths() -> None:
    zn = create_instrument(definition(instrument_type="OPTION", option_type="CALL", underlying="ZNZ26", price_format=sixty_fourths), quiet)
    assert zn.format_price(1.515625) == "1-33"
