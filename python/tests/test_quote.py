from datetime import UTC, date, datetime
from typing import Any

import pytest
from helpers import definition, message, quiet
from openfeed.generated import openfeed_pb2

from barchart_desktop_sdk import QuoteRecord
from barchart_desktop_sdk._store import InstrumentChange, InstrumentStore

# 2026-10-29T13:31:15.250999Z, with nanoseconds below the microsecond
NANOS = 1793280675250999999
TIME = datetime(2026, 10, 29, 13, 31, 15, 250999, tzinfo=UTC)


def setup(**snapshot: Any) -> InstrumentStore:
    """A store with a definition whose quantities are in hundredths, and a snapshot's fields applied."""
    store = InstrumentStore(quiet)
    store.apply(message(instrumentDefinition=definition(quantity_denominator=100)))
    store.apply(message(marketSnapshot={"marketId": 42, "tradeDate": 20261029, **snapshot}))
    return store


def update(store: InstrumentStore, **fields: Any) -> InstrumentChange:
    change = store.apply(message(marketUpdate={"marketId": 42, "symbol": "ESZ26", **fields}))
    assert change is not None
    return change


def quote(store: InstrumentStore) -> QuoteRecord:
    q = store.get_quote_by_id("42")
    assert q is not None
    return q


class TestQuantities:
    store = setup(
        bbo={"bidPrice": 10000, "bidQuantity": 250, "bidOrderCount": 3, "offerPrice": 10025, "offerQuantity": 350},
        last={"price": 10000, "quantity": 250, "transactionTime": NANOS, "tradeDate": 20261029},
        volume={"volume": 12345},
        openInterest={"volume": 999},
    )

    def test_keeps_the_best_bid_and_offers_sizes_and_a_snapshots_last_size_as_sent(self) -> None:
        q = quote(self.store)
        bbo = q.best_bid_offer
        assert [bbo.bid_size, bbo.ask_size, bbo.bid_order_count, bbo.ask_order_count] == [250, 350, 3, 0]
        assert q.session.last_size == 250

    def test_scales_volume_and_open_interest_dropping_the_fraction(self) -> None:
        session = quote(self.store).session
        assert [session.volume, session.open_interest] == [123, 9]

    def test_scales_a_last_updates_size_dropping_the_fraction(self) -> None:
        store = setup()
        update(store, last={"price": 10025, "quantity": 250})
        assert quote(store).session.last_size == 2


class TestSnapshots:
    def test_reads_times_to_the_microsecond_and_trade_dates_as_dates(self) -> None:
        session = quote(setup(last={"price": 10000, "quantity": 1, "transactionTime": NANOS, "tradeDate": 20261029})).session
        assert session.last_time == datetime(2026, 10, 29, 13, 31, 15, 250999, tzinfo=UTC)
        assert session.last_trade_date == date(2026, 10, 29)
        assert session.trade_date == date(2026, 10, 29)

    def test_dates_the_previous_sessions_settlement_with_that_sessions_date_when_it_has_none_and_the_current_ones_not_at_all(self) -> None:
        q = quote(
            setup(
                settlement={"price": 10075, "tradeDate": 0},
                previousSession={"tradeDate": 20261028, "settlement": {"price": 10050, "tradeDate": 0, "preliminarySettle": False}},
            )
        )
        previous = q.previous_session
        assert [previous.settle, previous.settle_trade_date, previous.settle_preliminary] == [100.5, date(2026, 10, 28), False]
        assert [q.session.settle, q.session.settle_trade_date] == [100.75, None]

    def test_leaves_every_field_of_a_session_the_snapshot_doesnt_carry_none(self) -> None:
        previous = quote(setup()).previous_session
        assert all(getattr(previous, name) is None for name in previous.__slots__)

    def test_takes_the_status_date_from_the_snapshot_only_when_it_has_no_status(self) -> None:
        own = quote(setup()).status
        assert [own.trading, own.prior, own.trade_date, own.time] == [None, None, date(2026, 10, 29), None]
        status = quote(setup(instrumentStatus={"tradingStatus": "OPEN", "tradeDate": 0})).status
        assert [status.trading, status.trade_date] == [openfeed_pb2.OPEN, None]

    def test_reads_the_last_trade_apart_from_the_last(self) -> None:
        session = quote(setup(last={"price": 10000, "quantity": 1}, lastTrade={"price": 10025, "quantity": 2})).session
        assert [session.last, session.last_trade] == [100, 100.25]


class TestUpdates:
    def test_applies_every_field_of_a_trades_updates_session_to_the_current_session(self) -> None:
        store = setup()
        update(
            store,
            trades={},
            session={
                "open": {"price": 10000},
                "high": {"price": 10100},
                "low": {"price": 9900},
                "last": {"price": 10050, "quantity": 7, "transactionTime": NANOS, "tradeDate": 20261029},
                "lastTrade": {"price": 10025},
                "settlement": {"price": 10075, "preliminarySettle": True, "transactionTime": NANOS, "tradeDate": 20261028},
                "openInterest": {"volume": 900},
                "numberOfTrades": {"numberTrades": 12},
                "volume": {"volume": 5000},
            },
        )
        s = quote(store).session
        assert [s.open, s.high, s.low, s.last_trade, s.open_interest, s.number_of_trades, s.volume] == [100, 101, 99, 100.25, 9, 12, 50]
        # no trades in the update, so none at `last`
        assert [s.last, s.last_time, s.last_trade_date, s.last_size] == [100.5, TIME, date(2026, 10, 29), 0]
        assert [s.settle, s.settle_time, s.settle_trade_date, s.settle_preliminary] == [100.75, TIME, date(2026, 10, 28), True]

    def test_applies_every_field_of_a_trades_updates_previous_session_and_its_settlement_wins(self) -> None:
        store = setup()
        update(
            store,
            trades={},
            session={"prevSettlement": {"price": 9000, "tradeDate": 20261027}},
            previousSession={
                "open": {"price": 10000},
                "high": {"price": 10100},
                "low": {"price": 9900},
                "last": {"price": 10050, "transactionTime": NANOS, "tradeDate": 20261028},
                "settlement": {"price": 10075, "tradeDate": 20261028},
                "openInterest": {"volume": 900},
                "volume": {"volume": 5000},
            },
        )
        p = quote(store).previous_session
        assert [p.open, p.high, p.low, p.open_interest, p.volume] == [100, 101, 99, 9, 50]
        assert [p.last, p.last_time, p.last_trade_date] == [100.5, TIME, date(2026, 10, 28)]
        assert [p.settle, p.settle_trade_date] == [100.75, date(2026, 10, 28)]

    @pytest.mark.parametrize("field", ["open", "high", "low"])
    def test_applies_a_single_price_update_to_its_own_field(self, field: str) -> None:
        store = setup(open={"price": 1}, high={"price": 1}, low={"price": 1})
        update(store, **{field: {"price": 10100}})
        session = quote(store).session
        assert {name: getattr(session, name) for name in ("open", "high", "low")} == {"open": 0.01, "high": 0.01, "low": 0.01, field: 101}

    def test_applies_a_close_update_with_its_time_and_date(self) -> None:
        store = setup()
        update(store, close={"price": 10100, "transactionTime": NANOS, "tradeDate": 20261029})
        session = quote(store).session
        assert [session.close, session.close_time, session.close_trade_date] == [101, TIME, date(2026, 10, 29)]

    def test_applies_a_last_update_scaling_its_size(self) -> None:
        store = setup()
        update(store, last={"price": 10100, "quantity": 700, "transactionTime": NANOS, "tradeDate": 20261029})
        session = quote(store).session
        assert [session.last, session.last_size, session.last_time, session.last_trade_date] == [101, 7, TIME, date(2026, 10, 29)]

    def test_applies_single_quantity_updates_to_their_own_fields(self) -> None:
        store = setup()
        update(store, volume={"volume": 5000})
        update(store, openInterest={"volume": 900})
        update(store, numberOfTrades={"numberTrades": 12})
        session = quote(store).session
        assert [session.volume, session.open_interest, session.number_of_trades] == [50, 9, 12]

    def test_applies_a_trades_updates_last_trade_without_touching_the_last(self) -> None:
        store = setup(last={"price": 10000, "quantity": 1})
        update(store, session={"lastTrade": {"price": 10025, "quantity": 2}}, trades={})
        session = quote(store).session
        assert [session.last, session.last_trade] == [100, 100.25]

    def test_changes_only_the_group_an_update_is_about_and_only_its_fields(self) -> None:
        store = setup(high={"price": 10100})
        before = quote(store)
        update(store, low={"price": 9900})
        after = quote(store)
        assert [after.session.low, after.session.high] == [99, 101]
        assert after.previous_session is before.previous_session
        assert after.best_bid_offer is before.best_bid_offer

    def test_applies_a_settlement_to_the_current_session_preliminary_or_not(self) -> None:
        store = setup()
        update(store, settlement={"price": 10100, "preliminarySettle": True, "tradeDate": 20261029})
        session = quote(store).session
        assert [session.settle, session.settle_preliminary, session.settle_trade_date] == [101, True, date(2026, 10, 29)]

    def test_applies_a_trades_updates_previous_settlement_to_the_previous_session(self) -> None:
        store = setup()
        update(store, session={"prevSettlement": {"price": 10050, "tradeDate": 20261028}}, trades={})
        q = quote(store)
        assert [q.previous_session.settle, q.previous_session.settle_trade_date] == [100.5, date(2026, 10, 28)]
        assert q.session.prev_settle is None

    def test_replaces_the_status_with_the_time_it_changed(self) -> None:
        store = setup()
        update(store, instrumentStatus={"tradingStatus": "CLOSE", "priorTradingStatus": "OPEN", "tradeDate": 20261029, "transactionTime": NANOS})
        status = quote(store).status
        assert [status.trading, status.prior, status.trade_date, status.time] == [
            openfeed_pb2.CLOSE,
            openfeed_pb2.OPEN,
            date(2026, 10, 29),
            datetime(2026, 10, 29, 13, 31, 15, 250999, tzinfo=UTC),
        ]

    def test_keeps_the_same_quote_for_an_update_it_doesnt_model(self) -> None:
        store = setup()
        before = quote(store)
        change = update(store, yearHigh={"price": 12000})
        assert change.parts.quote is None
        assert quote(store) is before
