"""Book: book_from_snapshot and apply_depth."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from openfeed.generated import openfeed_pb2

from ._instrument import Instrument

_Change = openfeed_pb2.AddPriceLevel | openfeed_pb2.ModifyPriceLevel | openfeed_pb2.DeletePriceLevel


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """One level of an order book. An empty level has price and size 0."""

    price: float
    """The level's price."""
    size: int
    """The quantity at this price."""
    order_count: int | None = None
    """How many orders make up `size`, when the feed sends it."""


@dataclass(frozen=True, slots=True)
class Book:
    """An order book by price level.

    Both sides always have the instrument's `book_depth` levels, best price first; empty levels are
    `PriceLevel(price=0, size=0)`.
    """

    bids: tuple[PriceLevel, ...]
    """The buy side, highest price first."""
    asks: tuple[PriceLevel, ...]
    """The sell side, lowest price first."""


_EMPTY_LEVEL = PriceLevel(price=0.0, size=0)


def _to_level(level: openfeed_pb2.AddPriceLevel | openfeed_pb2.ModifyPriceLevel, i: Instrument) -> PriceLevel:
    return PriceLevel(price=i.price(level.price), size=i.quantity(level.quantity), order_count=level.orderCount or None)


def book_from_snapshot(levels: Sequence[openfeed_pb2.AddPriceLevel], i: Instrument, log: logging.Logger) -> Book | None:
    """No book unless the snapshot has price levels. Levels outside the book's depth are skipped, with an error."""
    if not levels:
        return None

    bids = [_EMPTY_LEVEL] * i.book_depth
    asks = [_EMPTY_LEVEL] * i.book_depth
    sides = {openfeed_pb2.BID: bids, openfeed_pb2.OFFER: asks}

    for level in levels:
        side = sides.get(level.side)
        if side is not None and not 1 <= level.level <= len(side):
            log.error("Snapshot book level out of range, skipped: %s %s", i.symbol, level.level)
        elif side is not None:
            side[level.level - 1] = _to_level(level, i)

    return Book(bids=tuple(bids), asks=tuple(asks))


def _get_change(entry: openfeed_pb2.DepthPriceLevel.Entry) -> _Change | None:
    """The change an entry's oneof carries; None when it has none."""
    if entry.HasField("addPriceLevel"):
        return entry.addPriceLevel
    if entry.HasField("modifyPriceLevel"):
        return entry.modifyPriceLevel
    if entry.HasField("deletePriceLevel"):
        return entry.deletePriceLevel
    return None


def _apply_change(
    elements: tuple[PriceLevel, ...], change: _Change, i: Instrument, symbol: str, log: logging.Logger
) -> tuple[PriceLevel, ...] | None:
    """One change applied to its side's levels; None, with an error, when its level is outside the side: every
    operation keeps the side `book_depth` long.
    """
    index = change.level - 1
    if not 0 <= index < len(elements):
        log.error("Book level out of range, skipped: %s %s", symbol, index + 1)
        return None
    if isinstance(change, openfeed_pb2.DeletePriceLevel):
        return (*elements[:index], *elements[index + 1 :], _EMPTY_LEVEL)
    if isinstance(change, openfeed_pb2.AddPriceLevel):
        return (*elements[:index], _to_level(change, i), *elements[index:-1])
    return (*elements[:index], _to_level(change, i), *elements[index + 1 :])


def apply_depth(book: Book, depth: openfeed_pb2.DepthPriceLevel, i: Instrument, symbol: str, log: logging.Logger) -> Book:
    """Every entry keeps each side's length; levels outside the book are skipped with an error."""
    bids, asks = book.bids, book.asks

    for entry in depth.levels:
        change = _get_change(entry)
        if change is not None and change.level:
            if change.side == openfeed_pb2.BID:
                bids = _apply_change(bids, change, i, symbol, log) or bids
            elif change.side:
                asks = _apply_change(asks, change, i, symbol, log) or asks

    return Book(bids=bids, asks=asks)
