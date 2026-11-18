"""Builders for protocol messages in tests: only the fields the SDK reads.

Protobuf's constructors take nested dicts and enum names, so messages are written as the protocol's JSON reads.
"""

import itertools
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from openfeed.generated import openfeed_api_pb2, openfeed_instrument_pb2

from barchart_desktop_sdk import PriceVolume

_ids = itertools.count()


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def recording_logger() -> tuple[logging.Logger, list[logging.LogRecord]]:
    """A logger of its own that keeps what it's given, for tests that check warnings."""
    logger = logging.getLogger(f"tests.{next(_ids)}")
    logger.propagate = False
    handler = _ListHandler()
    logger.addHandler(handler)
    return logger, handler.records


quiet, _ = recording_logger()


def message(**fields: Any) -> openfeed_api_pb2.OpenfeedGatewayMessage:
    return openfeed_api_pb2.OpenfeedGatewayMessage(**fields)


def decimal_format(denominator: int) -> dict[str, Any]:
    return {"isFractional": False, "denominator": denominator, "subDenominator": 0}


def fraction_format(denominator: int, sub_denominator: int = 0) -> dict[str, Any]:
    return {"isFractional": True, "denominator": denominator, "subDenominator": sub_denominator}


NO_PRICE_FORMAT: Mapping[str, Any] = {}
"""For `definition(price_format=...)`: a definition without a price format."""


def definition(  # noqa: PLR0913
    *,
    market_id: int = 42,
    symbol: str = "ESZ26",
    price_format: Mapping[str, Any] | None = None,
    price_denominator: int = 100,
    quantity_denominator: int = 1,
    book_depth: int = 5,
    instrument_type: str = "FUTURE",
    option_type: str = "UNKNOWN_OPTION_TYPE",
    underlying: str = "",
) -> openfeed_instrument_pb2.InstrumentDefinition:
    """A definition: ESZ26 in hundredths, five levels deep, unless told otherwise."""
    fields: dict[str, Any] = {
        "marketId": market_id,
        "symbol": symbol,
        "instrumentType": instrument_type,
        "optionType": option_type,
        "underlying": underlying,
        "priceDenominator": price_denominator,
        "quantityDenominator": quantity_denominator,
        "bookDepth": book_depth,
        "timeZoneName": "America/Chicago",
    }
    if price_format is not NO_PRICE_FORMAT:
        fields["priceFormat"] = price_format if price_format is not None else decimal_format(100)
    return openfeed_instrument_pb2.InstrumentDefinition(**fields)


def level(side: str, n: int, price: int, quantity: int) -> dict[str, Any]:
    return {"side": side, "level": n, "price": price, "quantity": quantity}


def pairs(levels: Iterable[PriceVolume]) -> list[list[float]]:
    """`[price, volume]` pairs, for comparing cumulative volume levels."""
    return [[pv.price, pv.volume] for pv in levels]
