import dataclasses
import json

import pytest
from helpers import NO_PRICE_FORMAT, decimal_format, definition, fraction_format, quiet, recording_logger
from openfeed.generated import openfeed_pb2

from barchart_desktop_sdk._formatted import FormattedQuote
from barchart_desktop_sdk._instrument import create_instrument
from barchart_desktop_sdk._price_format import PriceFormat
from barchart_desktop_sdk._quote import apply_snapshot

es = create_instrument(definition(market_id=1, symbol="ESZ26", price_format=decimal_format(100)), quiet)
zn = create_instrument(definition(market_id=2, symbol="ZNZ26", price_format=fraction_format(32), price_denominator=1000000), quiet)
zt = create_instrument(definition(market_id=3, symbol="ZTZ26", price_format=fraction_format(32, 4), price_denominator=1000000), quiet)


class TestFormatPrice:
    def test_formats_decimals_with_thousands_separators(self) -> None:
        assert es.format_price(6812.25) == "6,812.25"
        assert es.format_price(6812.25, "decimal") == "6,812.25"

    def test_formats_fractions_natively_and_in_decimal_with_the_decimals_the_fraction_needs(self) -> None:
        assert zn.format_price(112.5) == "112-16"
        assert zn.format_price(112.5, "decimal") == "112.50000"
        assert zn.format_price(-0.03125, "decimal") == "-0.03125"
        assert zt.format_price(104.0078125, "decimal") == "104.0078125"

    def test_formats_no_price_as_an_empty_string(self) -> None:
        assert zn.format_price(None, "decimal") == ""


class TestFractions:
    def test_formats_a_negative_price_with_its_sign_before_the_whole_part(self) -> None:
        assert zn.format_price(-112.5) == "-112-16"
        assert zn.format_price(-0.03125) == "-0-01"

    def test_formats_quarters_of_a_32nd_as_their_first_decimal_digit_in_three_digits(self) -> None:
        assert zt.format_price(104.0078125) == "104-002"
        assert zt.format_price(104.015625) == "104-005"
        assert zt.format_price(104.0234375) == "104-007"

    def test_parses_what_it_formats_signs_included(self) -> None:
        assert zn.parse_price("112-16") == 112.5
        assert zn.parse_price("-112-16") == -112.5
        assert zn.parse_price("-0-01") == -0.03125
        assert zt.parse_price("104-005") == 104.015625

    def test_parses_the_last_digit_of_a_fraction_with_a_subdenominator_as_its_sub_fraction_so_prices_round_trip(self) -> None:
        assert zt.parse_price("104-002") == 104.0078125
        assert zt.parse_price("104-127") == 104 + 12.75 / 32
        for quarters in range(32 * 4 * 3):
            price = 103 + quarters / 128
            assert zt.parse_price(zt.format_price(price)) == price

    def test_reads_a_last_digit_the_formatter_never_writes_as_tenths(self) -> None:
        assert zt.parse_price("104-003") == (104 * 32 + 3 * 0.1) / 32
        assert zt.parse_price("104-129") == (104 * 32 + 129 * 0.1) / 32

    def test_parses_text_without_a_fraction_as_a_decimal_and_nothing_as_none(self) -> None:
        assert zn.parse_price("112.5") == 112.5
        assert zn.parse_price("") is None


class TestDecimals:
    def test_formats_with_thousands_separators_and_the_formats_decimals_and_parses_them_back(self) -> None:
        assert es.format_price(1234567.5) == "1,234,567.50"
        assert es.format_price(-1234.5) == "-1,234.50"
        assert es.parse_price("1,234,567.50") == 1234567.5


class TestWithoutPriceFormat:
    logger, warnings = recording_logger()
    plain = create_instrument(definition(price_format=NO_PRICE_FORMAT, price_denominator=1), logger)

    def test_warns_and_scales_prices_by_the_denominator_alone(self) -> None:
        assert len(self.warnings) == 1
        assert self.plain.price_format == PriceFormat(price_scaling=1)

    def test_formats_prices_as_javascript_prints_numbers_in_either_notation(self) -> None:
        assert self.plain.format_price(100) == "100"
        assert self.plain.format_price(100.5) == "100.5"
        assert self.plain.format_price(1234567) == "1234567"
        assert self.plain.format_price(100, "decimal") == "100"


class TestDecimalDenominator:
    def test_gives_the_decimals_its_unit_needs_so_a_power_of_2_keeps_every_digit(self) -> None:
        eighths = create_instrument(definition(price_format=decimal_format(8)), quiet)
        assert eighths.price_format.num_decimals == 3
        assert eighths.format_price(112.125) == "112.125"
        assert create_instrument(definition(price_format=decimal_format(1000)), quiet).price_format.num_decimals == 3
        assert create_instrument(definition(price_format=decimal_format(1)), quiet).price_format.num_decimals == 0

    @pytest.mark.parametrize("price_format", [decimal_format(0), decimal_format(-10), fraction_format(0)])
    def test_of_0_or_less_is_no_format_a_warning_and_prices_as_plain_numbers(self, price_format: dict[str, object]) -> None:
        logger, warnings = recording_logger()
        instrument = create_instrument(definition(price_format=price_format), logger)
        assert len(warnings) == 1
        assert (instrument.price_format.num_decimals, instrument.price_format.fraction) == (None, None)
        assert (instrument.format_price(100.5), instrument.format_price(100.5, "decimal")) == ("100.5", "100.5")


class TestFormattedQuote:
    empty = apply_snapshot(openfeed_pb2.MarketSnapshot(tradeDate=0), zn)
    quote = dataclasses.replace(
        empty,
        best_bid_offer=dataclasses.replace(empty.best_bid_offer, bid_price=112.484375, ask_price=112.5, bid_size=1500),
        session=dataclasses.replace(empty.session, last=112.5, volume=1204551),
        previous_session=dataclasses.replace(empty.previous_session, settle=112.03125, prev_settle=111.875),
    )
    f = FormattedQuote(quote, zn)

    def test_has_a_view_per_group_and_nothing_else(self) -> None:
        assert [name for name in dir(self.f) if not name.startswith("_")] == ["best_bid_offer", "previous_session", "session", "to_dict"]

    def test_formats_prices_natively_and_in_decimal_and_quantities_with_separators(self) -> None:
        f = self.f
        assert f.best_bid_offer.bid_price == "112-15"
        assert f.best_bid_offer.decimal.bid_price == "112.48438"
        assert f.best_bid_offer.bid_size == "1,500"
        assert f.session.last == "112-16"
        assert f.session.decimal.last == "112.50000"
        assert f.session.volume == "1,204,551"
        assert f.session.decimal.volume == "1,204,551"
        assert f.previous_session.settle == "112-01"
        assert f.previous_session.prev_settle == "111-28"
        assert f.session.settle == ""

    def test_builds_each_view_once(self) -> None:
        assert self.f.session is self.f.session
        assert self.f.best_bid_offer is self.f.best_bid_offer
        assert self.f.session.decimal is self.f.session.decimal

    def test_gives_every_field_as_a_read_only_mapping(self) -> None:
        f = self.f
        assert len(f.session.values) == 12
        assert sorted(f.best_bid_offer.values) == ["ask_order_count", "ask_price", "ask_size", "bid_order_count", "bid_price", "bid_size"]
        assert {**f.session.values}["last"] == "112-16"
        assert f.session.values is f.session.values
        with pytest.raises(TypeError):
            f.session.values["last"] = "1"  # type: ignore[index]
        assert not any(k in f.best_bid_offer.values or k in f.session.values for k in ("values", "decimal", "session"))

    def test_serializes_every_group_with_its_decimal_view_and_no_status(self) -> None:
        data = json.loads(json.dumps(self.f.to_dict()))
        assert data["session"]["last"] == "112-16"
        assert data["session"]["decimal"]["last"] == "112.50000"
        assert data["previous_session"]["settle"] == "112-01"
        assert data["best_bid_offer"]["bid_price"] == "112-15"
        assert data["best_bid_offer"]["decimal"]["ask_price"] == "112.50000"
        assert "decimal" not in data["session"]["decimal"]
        assert "status" not in data
