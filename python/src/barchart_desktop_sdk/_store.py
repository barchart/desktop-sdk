"""State per instrument, keyed by marketId: one listed instrument on its exchange."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from openfeed.generated import openfeed_api_pb2, openfeed_instrument_pb2, openfeed_pb2

from ._book import Book, apply_depth, book_from_snapshot
from ._cumulative import CumulativeVolumeStep, last_steps, seed_steps, trade_steps
from ._formatted import FormattedQuote
from ._instrument import Instrument, create_instrument
from ._quote import QuoteRecord, apply_snapshot, apply_update


@dataclass(frozen=True, slots=True)
class PriceVolume:
    """The volume traded at one price."""

    price: float
    """The price traded at."""
    volume: int
    """The total traded at `price` so far."""


@dataclass(frozen=True, slots=True)
class QuotePart:
    quote: QuoteRecord
    formatted: FormattedQuote


@dataclass(frozen=True, slots=True)
class DepthPart:
    book: Book


@dataclass(frozen=True, slots=True)
class CumulativeVolumePart:
    levels: tuple[PriceVolume, ...]
    """Every level, ascending by price."""
    changed: tuple[PriceVolume, ...]
    """The levels a message changed, each price once with its new volume, ascending; every level after a snapshot."""


@dataclass(frozen=True, slots=True)
class StateParts:
    """An instrument's state, by the subscription method that gets it, shaped as that method's event carries it. In a
    change, only the parts the message changed are set; a first delivery has every part there is.
    """

    quote: QuotePart | None = None
    depth: DepthPart | None = None
    cumulative_volume: CumulativeVolumePart | None = None


@dataclass(frozen=True, slots=True)
class InstrumentChange:
    """What one message changed in one instrument."""

    market_id: str
    kind: Literal["snapshot", "update"]
    parts: StateParts


@dataclass(frozen=True, slots=True)
class LastMessages:
    """What `diagnostics.get_last_messages` returns: the market's last messages, as they came."""

    snapshot: openfeed_pb2.MarketSnapshot | None
    """The last snapshot applied; None before the first."""
    update: openfeed_pb2.MarketUpdate | None
    """The last update applied; None before the first."""


@dataclass(slots=True)
class _InstrumentState:
    instrument: Instrument
    quote: QuoteRecord | None = None
    book: Book | None = None
    # None until a snapshot brings volume at price; trades don't start it
    volumes: dict[float, int] | None = None
    # `volumes` as subscribers get them: ascending by price; rebuilt when they change
    levels: tuple[PriceVolume, ...] | None = None
    last_snapshot: openfeed_api_pb2.OpenfeedGatewayMessage | None = None
    last_update: openfeed_pb2.MarketUpdate | None = None


def _apply_steps(volumes: dict[float, int], steps: Sequence[CumulativeVolumeStep]) -> None:
    """Applies steps in order; each adds its delta to the total at its price."""
    for step in steps:
        volumes[step.price] = volumes.get(step.price, 0) + step.delta


def _to_levels(volumes: dict[float, int]) -> tuple[PriceVolume, ...]:
    return tuple(PriceVolume(price=price, volume=volume) for price, volume in sorted(volumes.items()))


def _to_changed(volumes: dict[float, int], steps: Sequence[CumulativeVolumeStep]) -> tuple[PriceVolume, ...]:
    """The levels the steps touched, each price once with its volume after them, ascending by price."""
    return tuple(PriceVolume(price=price, volume=volumes.get(price, 0)) for price in sorted({step.price for step in steps}))


def _update_levels(market: _InstrumentState, steps: Sequence[CumulativeVolumeStep]) -> CumulativeVolumePart | None:
    """Applies steps to a market's levels; their part of the change, or None if the market has none or there are no steps."""
    if market.volumes is None or not steps:
        return None
    _apply_steps(market.volumes, steps)
    market.levels = _to_levels(market.volumes)
    return CumulativeVolumePart(levels=market.levels, changed=_to_changed(market.volumes, steps))


class InstrumentStore:
    """State only, by market id. Which symbol names which market is the registry's, and so is which markets are kept: the
    SDK applies a message only for a market a subscription names, and calls remove_id once none does.
    """

    def __init__(self, log: logging.Logger) -> None:
        self._log = log
        self._instruments: dict[str, _InstrumentState] = {}

    def remove_id(self, market_id: str) -> None:
        self._instruments.pop(market_id, None)

    def moved(self, from_id: str, definition: openfeed_instrument_pb2.InstrumentDefinition) -> None:
        """`instrumentAction` EXCHANGE_MOVE: the state moves to the new definition's marketId, and the new definition
        replaces the instrument.
        """
        state = self._instruments.pop(from_id, None)
        if state is None:
            return
        self._instruments[str(definition.marketId)] = state
        self._define(definition)

    def clear(self) -> None:
        self._instruments.clear()

    # Getters by marketId; None in, None out, so a lookup can be passed straight through.

    def get_instrument_by_id(self, market_id: str | None) -> Instrument | None:
        market = self._get(market_id)
        return market.instrument if market else None

    def get_quote_by_id(self, market_id: str | None) -> QuoteRecord | None:
        market = self._get(market_id)
        return market.quote if market else None

    def get_book_by_id(self, market_id: str | None) -> Book | None:
        market = self._get(market_id)
        return market.book if market else None

    def get_cumulative_volume_by_id(self, market_id: str | None) -> tuple[PriceVolume, ...] | None:
        """Ascending by price."""
        market = self._get(market_id)
        return market.levels if market else None

    def get_state_parts_by_id(self, market_id: str | None) -> StateParts:
        """Every part of the current state, for a first delivery."""
        market = self._get(market_id)
        if market is None:
            return StateParts()
        return StateParts(
            quote=QuotePart(quote=market.quote, formatted=FormattedQuote(market.quote, market.instrument)) if market.quote else None,
            depth=DepthPart(book=market.book) if market.book else None,
            cumulative_volume=CumulativeVolumePart(levels=market.levels, changed=market.levels) if market.levels is not None else None,
        )

    def get_last_snapshot_message_by_id(self, market_id: str | None) -> openfeed_api_pb2.OpenfeedGatewayMessage | None:
        """The whole last snapshot message, the `raw` of `initial` events."""
        market = self._get(market_id)
        return market.last_snapshot if market else None

    def get_last_messages_by_id(self, market_id: str | None) -> LastMessages | None:
        market = self._get(market_id)
        if market is None:
            return None
        snapshot = market.last_snapshot.marketSnapshot if market.last_snapshot is not None else None
        return LastMessages(snapshot=snapshot, update=market.last_update)

    def apply(self, message: openfeed_api_pb2.OpenfeedGatewayMessage) -> InstrumentChange | None:
        """Applies one message; returns what changed, or None if it changed no market state."""
        if message.HasField("instrumentDefinition"):
            self._define(message.instrumentDefinition)
            return None
        if message.HasField("marketSnapshot"):
            return self._snapshot(message, message.marketSnapshot)
        if message.HasField("marketUpdate"):
            return self._update(message.marketUpdate)
        return None

    def _get(self, market_id: str | None) -> _InstrumentState | None:
        return None if market_id is None else self._instruments.get(market_id)

    def _define(self, definition: openfeed_instrument_pb2.InstrumentDefinition) -> None:
        market_id = str(definition.marketId)
        instrument = create_instrument(definition, self._log)
        market = self._instruments.get(market_id)
        if market is not None:
            market.instrument = instrument
        else:
            self._instruments[market_id] = _InstrumentState(instrument=instrument)

    def _snapshot(self, message: openfeed_api_pb2.OpenfeedGatewayMessage, snapshot: openfeed_pb2.MarketSnapshot) -> InstrumentChange | None:
        market_id = str(snapshot.marketId)
        market = self._instruments.get(market_id)
        # none after its last subscription went, or before its definition: skipped
        if market is None:
            return None
        instrument = market.instrument

        market.last_snapshot = message
        market.quote = apply_snapshot(snapshot, instrument)

        # no book unless the snapshot has price levels; an existing book stays
        book = book_from_snapshot(snapshot.priceLevels, instrument, self._log)
        if book is not None:
            market.book = book

        # volume at price starts the levels over
        has_volume_at_price = snapshot.HasField("volumeAtPrice")
        if has_volume_at_price:
            market.volumes = {}
            _apply_steps(market.volumes, seed_steps(snapshot.volumeAtPrice, instrument))
            market.levels = _to_levels(market.volumes)

        return InstrumentChange(
            market_id=market_id,
            kind="snapshot",
            parts=StateParts(
                quote=QuotePart(quote=market.quote, formatted=FormattedQuote(market.quote, instrument)),
                depth=DepthPart(book=book) if book is not None else None,
                cumulative_volume=(
                    CumulativeVolumePart(levels=market.levels, changed=market.levels) if has_volume_at_price and market.levels is not None else None
                ),
            ),
        )

    def _update(self, update: openfeed_pb2.MarketUpdate) -> InstrumentChange | None:
        market_id = str(update.marketId)
        market = self._instruments.get(market_id)
        # ignored until the first snapshot
        if market is None or market.quote is None:
            return None
        instrument = market.instrument
        market.last_update = update
        kind = update.WhichOneof("data")

        # trades only count towards CV (and last_size) when the update carries session.last
        trades = trade_steps(update.trades, update.session.last.price, instrument) if kind == "trades" and update.session.HasField("last") else None

        previous = market.quote
        next_quote = apply_update(previous, update, instrument, trades[0] if trades else 0)
        quote_changed = next_quote is not previous
        if quote_changed:
            market.quote = next_quote

        if trades is not None:
            steps = trades[1]
        elif kind == "last":
            steps = last_steps(next_quote.session.last, next_quote.session.last_size)
        else:
            steps = []

        return InstrumentChange(
            market_id=market_id,
            kind="update",
            parts=StateParts(
                quote=QuotePart(quote=next_quote, formatted=FormattedQuote(next_quote, instrument)) if quote_changed else None,
                depth=self._apply_depth(market, update) if kind == "depthPriceLevel" else None,
                cumulative_volume=_update_levels(market, steps),
            ),
        )

    def _apply_depth(self, market: _InstrumentState, update: openfeed_pb2.MarketUpdate) -> DepthPart | None:
        """Applies a depth update to the market's book; its part of the change, or None (with a warning) without a book."""
        if market.book is None:
            self._log.warning("No book found for %s", update.symbol or str(update.marketId))
            return None
        market.book = apply_depth(market.book, update.depthPriceLevel, market.instrument, update.symbol, self._log)
        return DepthPart(book=market.book)
