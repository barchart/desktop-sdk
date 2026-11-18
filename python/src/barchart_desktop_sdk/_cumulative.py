"""Cumulative volume as ordered steps: seed, trades, last."""

from dataclasses import dataclass

from openfeed.generated import openfeed_pb2

from ._instrument import Instrument


@dataclass(frozen=True, slots=True)
class CumulativeVolumeStep:
    """One change to the volume at a price, in the order it happens; the store applies them in that order."""

    price: float
    delta: int


def seed_steps(volume_at_price: openfeed_pb2.VolumeAtPrice, i: Instrument) -> list[CumulativeVolumeStep]:
    """Seed from the snapshot's volume at price, in ascending price order."""
    steps = [CumulativeVolumeStep(price=i.price(pv.price), delta=i.quantity(pv.volume)) for pv in volume_at_price.priceVolumes]
    return sorted(steps, key=lambda step: step.price)


def trade_steps(trades: openfeed_pb2.Trades, last_price: int, i: Instrument) -> tuple[int, list[CumulativeVolumeStep]]:
    """Steps for the trades of an update that carries `session.last`, at its price `last_price`, and the size traded at
    last; trades in updates without it are not applied. Trades and corrections at `last` are summed into one final
    step at `last`, and that sum is also the quote's `last_size`.
    """
    last = i.price(last_price)
    steps: list[CumulativeVolumeStep] = []
    current_price_size = 0

    for entry in trades.trades:
        if entry.HasField("trade"):
            trade = entry.trade
            counts = not trade.doesNotUpdateVolume and not trade.doesNotUpdateLast and trade.session != "T" and not trade.blockTrade
            trade_price = i.price(trade.price)
            if counts and trade_price == last:
                current_price_size += i.quantity(trade.quantity)
            elif counts:
                steps.append(CumulativeVolumeStep(price=trade_price, delta=i.quantity(trade.quantity)))
        elif entry.HasField("tradeCancel"):
            cancel = entry.tradeCancel
            steps.append(CumulativeVolumeStep(price=i.price(cancel.correctedTradePrice), delta=-i.quantity(cancel.correctedTradeQuantity)))
        elif entry.HasField("tradeCorrection"):
            correction = entry.tradeCorrection
            steps.append(CumulativeVolumeStep(price=i.price(correction.originalTradePrice), delta=-i.quantity(correction.originalTradeQuantity)))
            new_price = i.price(correction.price)
            if new_price == last:
                current_price_size += i.quantity(correction.quantity)
            else:
                steps.append(CumulativeVolumeStep(price=new_price, delta=i.quantity(correction.quantity)))

    steps.append(CumulativeVolumeStep(price=last, delta=current_price_size))
    return current_price_size, steps


def last_steps(last: float | None, last_size: int | None) -> list[CumulativeVolumeStep]:
    """A `last` update adds its size at its price."""
    return [CumulativeVolumeStep(price=last, delta=last_size)] if last is not None and last_size is not None else []
