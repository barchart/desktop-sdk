"""QuoteRecord: apply_snapshot and apply_update.

Protobuf hands out a default instance for an unset message field, so optional message fields are checked with
`HasField`.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import NamedTuple, Protocol

from openfeed.generated import openfeed_pb2

from ._instrument import Instrument
from ._proto import to_time, to_trade_date


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One trading session, as Openfeed sends it: prices and quantities as numbers, with no fallbacks between fields.

    A field the feed hasn't sent is None; some are never sent for a session, such as the previous session's `close`.

    Prices with a time have two stamps: `…_time`, when it happened, as a UTC `datetime`; and `…_trade_date`, the session
    it belongs to, as a `date`. Which one to show is up to the application.
    """

    trade_date: date | None = None
    """The session's date."""

    open: float | None = None
    """The session's opening price."""
    high: float | None = None
    """The session's highest price so far."""
    low: float | None = None
    """The session's lowest price so far."""

    close: float | None = None
    """The session's closing price."""
    close_time: datetime | None = None
    """When the close was set."""
    close_trade_date: date | None = None
    """The session the close belongs to."""

    settle: float | None = None
    """The session's settlement price."""
    settle_time: datetime | None = None
    """When the settlement was set."""
    settle_trade_date: date | None = None
    """The session the settlement belongs to."""
    settle_preliminary: bool | None = None
    """Whether the settlement is preliminary, rather than final."""

    prev_settle: float | None = None
    """The previous session's settlement price, as the last snapshot sent it with this session. Updates change
    `previous_session.settle` instead, so read that one for the latest value.
    """
    prev_settle_time: datetime | None = None
    """When the previous session's settlement was set."""
    prev_settle_trade_date: date | None = None
    """The session the previous settlement belongs to."""

    last: float | None = None
    """The last price."""
    last_time: datetime | None = None
    """When the last price was set."""
    last_trade_date: date | None = None
    """The session the last price belongs to."""
    last_size: int | None = None
    """The quantity traded at the last price."""
    last_trade: float | None = None
    """The price of Openfeed's `lastTrade`, which has no time or date of its own here."""

    volume: int | None = None
    """The quantity traded in the session so far."""
    open_interest: int | None = None
    """The number of open contracts."""
    number_of_trades: int | None = None
    """How many trades the session has had so far."""


@dataclass(frozen=True, slots=True)
class BestBidOfferRecord:
    """The best bid and offer: the highest price a buyer is offering and the lowest a seller will take.

    When one side is empty, an update sets its fields to 0.
    """

    bid_price: float | None = None
    """The highest bid."""
    bid_size: int | None = None
    """The quantity bid at `bid_price`."""
    bid_order_count: int | None = None
    """How many orders make up `bid_size`, when the feed sends it."""
    ask_price: float | None = None
    """The lowest offer."""
    ask_size: int | None = None
    """The quantity offered at `ask_price`."""
    ask_order_count: int | None = None
    """How many orders make up `ask_size`, when the feed sends it."""


@dataclass(frozen=True, slots=True)
class StatusRecord:
    """The instrument's trading status, as Openfeed's `InstrumentStatus` sends it."""

    trading: int | None = None
    """The current status, such as open or halted: an Openfeed `InstrumentTradingStatus` value."""
    prior: int | None = None
    """The status before it."""
    trade_date: date | None = None
    """The status's session date; a snapshot without a status gives its own trade date."""
    time: datetime | None = None
    """When the status changed (status updates only)."""


@dataclass(frozen=True, slots=True)
class QuoteRecord:
    """A market's quote state, in the protocol's groups: the current and previous sessions, the best bid and offer, and
    the trading status.

    A group object is replaced only when a message changed it, so an unchanged group is the same object as before.
    """

    session: SessionRecord
    """The current session."""
    previous_session: SessionRecord
    """The session before it; every field None until the feed sends it."""
    best_bid_offer: BestBidOfferRecord
    """The best bid and offer."""
    status: StatusRecord
    """The trading status."""


# A session the snapshot doesn't carry: every field None.
_EMPTY_SESSION = SessionRecord()


class _Stamped(Protocol):
    """A protocol message with a price and when it happened: Close, Settlement, Last."""

    price: int
    transactionTime: int  # noqa: N815
    tradeDate: int  # noqa: N815


class _Stamp(NamedTuple):
    """A `_Stamped` message's values; every one None without a message."""

    price: float | None = None
    time: datetime | None = None
    trade_date: date | None = None


def _stamp(value: _Stamped | None, i: Instrument) -> _Stamp:
    if value is None:
        return _Stamp()
    return _Stamp(price=i.price(value.price), time=to_time(value.transactionTime), trade_date=to_trade_date(value.tradeDate))


def _with_close(session: SessionRecord, close: openfeed_pb2.Close, i: Instrument) -> SessionRecord:
    stamp = _stamp(close, i)
    return replace(session, close=stamp.price, close_time=stamp.time, close_trade_date=stamp.trade_date)


def _with_settle(session: SessionRecord, settlement: openfeed_pb2.Settlement, i: Instrument) -> SessionRecord:
    stamp = _stamp(settlement, i)
    return replace(session, settle=stamp.price, settle_time=stamp.time, settle_trade_date=stamp.trade_date)


def _with_last(session: SessionRecord, last: openfeed_pb2.Last, i: Instrument) -> SessionRecord:
    stamp = _stamp(last, i)
    return replace(session, last=stamp.price, last_time=stamp.time, last_trade_date=stamp.trade_date)


def _bbo(b: openfeed_pb2.BestBidOffer, i: Instrument) -> BestBidOfferRecord:
    return BestBidOfferRecord(
        bid_price=i.price(b.bidPrice),
        ask_price=i.price(b.offerPrice),
        # the sizes as sent: not scaled
        bid_size=b.bidQuantity,
        ask_size=b.offerQuantity,
        bid_order_count=b.bidOrderCount,
        ask_order_count=b.offerOrderCount,
    )


def _read_session(
    s: openfeed_pb2.MarketSnapshot | openfeed_pb2.MarketSession,
    i: Instrument,
    *,
    close: openfeed_pb2.Close | None = None,
    prev_settlement: openfeed_pb2.Settlement | None = None,
    settle_date_from_session: bool = False,
) -> SessionRecord:
    """One session from a snapshot: its top level (the current session, which also carries `close`) or a MarketSession
    (the previous one, which also carries `prevSettlement`).

    `settle_date_from_session`: a settlement without a date takes the session's date, which has only ever applied to
    the previous session.
    """
    settlement = s.settlement if s.HasField("settlement") else None
    settle = _stamp(settlement, i)
    settle_trade_date = settle.trade_date
    if settlement is not None and settle_date_from_session and settle_trade_date is None:
        settle_trade_date = to_trade_date(s.tradeDate)
    close_stamp = _stamp(close, i)
    prev_settle = _stamp(prev_settlement, i)
    last = s.last if s.HasField("last") else None
    last_stamp = _stamp(last, i)
    return SessionRecord(
        trade_date=to_trade_date(s.tradeDate),
        open=i.price(s.open.price) if s.HasField("open") else None,
        high=i.price(s.high.price) if s.HasField("high") else None,
        low=i.price(s.low.price) if s.HasField("low") else None,
        close=close_stamp.price,
        close_time=close_stamp.time,
        close_trade_date=close_stamp.trade_date,
        settle=settle.price,
        settle_time=settle.time,
        settle_trade_date=settle_trade_date,
        settle_preliminary=settlement.preliminarySettle if settlement is not None else None,
        prev_settle=prev_settle.price,
        prev_settle_time=prev_settle.time,
        prev_settle_trade_date=prev_settle.trade_date,
        last=last_stamp.price,
        last_time=last_stamp.time,
        last_trade_date=last_stamp.trade_date,
        # as sent: not scaled in snapshots
        last_size=last.quantity if last is not None else None,
        last_trade=i.price(s.lastTrade.price) if s.HasField("lastTrade") else None,
        volume=i.quantity(s.volume.volume) if s.HasField("volume") else None,
        open_interest=i.quantity(s.openInterest.volume) if s.HasField("openInterest") else None,
        number_of_trades=s.numberOfTrades.numberTrades if s.HasField("numberOfTrades") else None,
    )


def apply_snapshot(snapshot: openfeed_pb2.MarketSnapshot, i: Instrument) -> QuoteRecord:
    """A snapshot replaces every field; anything the snapshot doesn't carry becomes None."""
    previous = snapshot.previousSession
    status = snapshot.instrumentStatus if snapshot.HasField("instrumentStatus") else None
    return QuoteRecord(
        session=_read_session(snapshot, i, close=snapshot.close if snapshot.HasField("close") else None),
        previous_session=(
            _read_session(
                previous,
                i,
                prev_settlement=previous.prevSettlement if previous.HasField("prevSettlement") else None,
                settle_date_from_session=True,
            )
            if snapshot.HasField("previousSession")
            else _EMPTY_SESSION
        ),
        best_bid_offer=_bbo(snapshot.bbo, i) if snapshot.HasField("bbo") else BestBidOfferRecord(),
        status=StatusRecord(
            trading=status.tradingStatus if status is not None else None,
            prior=status.priorTradingStatus if status is not None else None,
            # the snapshot's own date only when it has no instrumentStatus; an unset (0) status date gives None
            trade_date=to_trade_date(status.tradeDate if status is not None else snapshot.tradeDate),
            # not read from snapshots
            time=None,
        ),
    )


def _with_trades_session(record: SessionRecord, s: openfeed_pb2.MarketSession, i: Instrument, trades_last_size: int) -> SessionRecord:
    """The current session with the fields a trades update carries; the same object when it carries none."""
    if s.HasField("open"):
        record = replace(record, open=i.price(s.open.price))
    if s.HasField("high"):
        record = replace(record, high=i.price(s.high.price))
    if s.HasField("low"):
        record = replace(record, low=i.price(s.low.price))
    if s.HasField("last"):
        record = replace(_with_last(record, s.last, i), last_size=trades_last_size)
    if s.HasField("lastTrade"):
        record = replace(record, last_trade=i.price(s.lastTrade.price))
    if s.HasField("settlement"):
        record = replace(_with_settle(record, s.settlement, i), settle_preliminary=s.settlement.preliminarySettle)
    if s.HasField("openInterest"):
        record = replace(record, open_interest=i.quantity(s.openInterest.volume))
    if s.HasField("numberOfTrades"):
        record = replace(record, number_of_trades=s.numberOfTrades.numberTrades)
    if s.HasField("volume"):
        record = replace(record, volume=i.quantity(s.volume.volume))
    return record


def _with_trades_previous_session(
    record: SessionRecord, session: openfeed_pb2.MarketSession, previous: openfeed_pb2.MarketSession, i: Instrument
) -> SessionRecord:
    """The previous session with the fields a trades update carries; the same object when it carries none.

    The current session's `prevSettlement` is the previous session's settlement: one field, and the previous session's
    own wins.
    """
    if session.HasField("prevSettlement"):
        record = _with_settle(record, session.prevSettlement, i)
    if previous.HasField("last"):
        record = _with_last(record, previous.last, i)
    if previous.HasField("settlement"):
        record = _with_settle(record, previous.settlement, i)
    if previous.HasField("open"):
        record = replace(record, open=i.price(previous.open.price))
    if previous.HasField("high"):
        record = replace(record, high=i.price(previous.high.price))
    if previous.HasField("low"):
        record = replace(record, low=i.price(previous.low.price))
    if previous.HasField("volume"):
        record = replace(record, volume=i.quantity(previous.volume.volume))
    if previous.HasField("openInterest"):
        record = replace(record, open_interest=i.quantity(previous.openInterest.volume))
    return record


def _apply_trades_update(q: QuoteRecord, u: openfeed_pb2.MarketUpdate, i: Instrument, trades_last_size: int) -> QuoteRecord:
    """A session group is a new object only when the update changed one of its fields."""
    session = _with_trades_session(q.session, u.session, i, trades_last_size)
    previous = _with_trades_previous_session(q.previous_session, u.session, u.previousSession, i)
    if session is q.session and previous is q.previous_session:
        return q
    return replace(q, session=session, previous_session=previous)


def apply_update(q: QuoteRecord, u: openfeed_pb2.MarketUpdate, i: Instrument, trades_last_size: int = 0) -> QuoteRecord:  # noqa: PLR0911, PLR0912
    """MarketUpdate.data is a proto oneof, so exactly one branch applies. Only fields the update carries are written.

    `trades_last_size` is the size traded at `last` in this trades update, computed by the cumulative-volume reducer.
    The single-field updates other than `bbo` and `instrumentStatus` are the current session's.
    """
    match u.WhichOneof("data"):
        case "trades":
            return _apply_trades_update(q, u, i, trades_last_size)
        case "bbo":
            return replace(q, best_bid_offer=_bbo(u.bbo, i))
        case "open":
            return replace(q, session=replace(q.session, open=i.price(u.open.price)))
        case "high":
            return replace(q, session=replace(q.session, high=i.price(u.high.price)))
        case "low":
            return replace(q, session=replace(q.session, low=i.price(u.low.price)))
        case "close":
            return replace(q, session=_with_close(q.session, u.close, i))
        case "settlement":
            return replace(q, session=replace(_with_settle(q.session, u.settlement, i), settle_preliminary=u.settlement.preliminarySettle))
        case "openInterest":
            return replace(q, session=replace(q.session, open_interest=i.quantity(u.openInterest.volume)))
        case "numberOfTrades":
            return replace(q, session=replace(q.session, number_of_trades=u.numberOfTrades.numberTrades))
        case "volume":
            return replace(q, session=replace(q.session, volume=i.quantity(u.volume.volume)))
        case "last":
            return replace(q, session=replace(_with_last(q.session, u.last, i), last_size=i.quantity(u.last.quantity)))
        case "instrumentStatus":
            status = u.instrumentStatus
            return replace(
                q,
                status=StatusRecord(
                    trading=status.tradingStatus,
                    prior=status.priorTradingStatus,
                    trade_date=to_trade_date(status.tradeDate),
                    time=to_time(status.transactionTime),
                ),
            )
        case _:
            return q
