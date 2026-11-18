from typing import Any

from helpers import definition, message, pairs, quiet

from barchart_desktop_sdk._store import InstrumentChange, InstrumentStore, StateParts


def setup() -> InstrumentStore:
    """ESZ26 with volume at price: 100 → 10, 100.25 → 4."""
    store = InstrumentStore(quiet)
    store.apply(message(instrumentDefinition=definition()))
    store.apply(
        message(
            marketSnapshot={
                "marketId": 42,
                "tradeDate": 0,
                "volumeAtPrice": {"priceVolumes": [{"price": 10000, "volume": 10}, {"price": 10025, "volume": 4}]},
            }
        )
    )
    return store


def trades(store: InstrumentStore, entries: list[dict[str, Any]], last: int | None = None) -> InstrumentChange:
    """A trades update; `last` is the price of the session's last, if it carries one."""
    update: dict[str, Any] = {"marketId": 42, "symbol": "ESZ26", "trades": {"trades": entries}}
    if last is not None:
        update["session"] = {"last": {"price": last}}
    change = store.apply(message(marketUpdate=update))
    assert change is not None
    return change


def trade(price: int, quantity: int, **flags: Any) -> dict[str, Any]:
    return {"trade": {"price": price, "quantity": quantity, **flags}}


def last_size(change: InstrumentChange) -> int | None:
    assert change.parts.quote is not None
    return change.parts.quote.quote.session.last_size


def test_takes_back_a_cancels_and_a_corrections_original_volume_and_adds_the_corrections_new_trade() -> None:
    change = trades(
        setup(),
        [
            {"tradeCancel": {"correctedTradePrice": 10025, "correctedTradeQuantity": 1}},
            {"tradeCorrection": {"originalTradePrice": 10025, "originalTradeQuantity": 1, "price": 10050, "quantity": 1}},
            # corrected to the price at last: counted in last_size
            {"tradeCorrection": {"originalTradePrice": 10000, "originalTradeQuantity": 2, "price": 10075, "quantity": 3}},
        ],
        10075,
    )
    cv = change.parts.cumulative_volume
    assert cv is not None
    assert pairs(cv.levels) == [[100, 8], [100.25, 2], [100.5, 1], [100.75, 3]]
    assert pairs(cv.changed) == pairs(cv.levels)
    assert last_size(change) == 3


def test_leaves_out_block_trades_t_session_trades_and_trades_that_dont_update_volume_or_last() -> None:
    change = trades(
        setup(),
        [
            trade(10050, 1, blockTrade=True),
            trade(10050, 2, session="T"),
            trade(10025, 4, doesNotUpdateVolume=True),
            trade(10000, 5, doesNotUpdateLast=True),
            trade(10050, 6),
        ],
        10050,
    )
    cv = change.parts.cumulative_volume
    assert cv is not None
    assert pairs(cv.levels) == [[100, 10], [100.25, 4], [100.5, 6]]
    assert pairs(cv.changed) == [[100.5, 6]]
    assert last_size(change) == 6


def test_adds_a_level_at_last_even_when_no_trade_was_at_last_with_what_traded_there_nothing() -> None:
    change = trades(setup(), [trade(10025, 1)], 10050)
    assert change.parts.cumulative_volume is not None
    assert pairs(change.parts.cumulative_volume.changed) == [[100.25, 5], [100.5, 0]]
    assert last_size(change) == 0


def test_doesnt_apply_trades_in_an_update_without_the_sessions_last() -> None:
    store = setup()
    levels = store.get_cumulative_volume_by_id("42")
    change = trades(store, [trade(10050, 1)])
    assert change.parts == StateParts()
    assert store.get_cumulative_volume_by_id("42") is levels


def test_adds_a_last_updates_size_at_its_price() -> None:
    store = setup()
    change = store.apply(message(marketUpdate={"marketId": 42, "symbol": "ESZ26", "last": {"price": 10050, "quantity": 2}}))
    assert change is not None
    assert change.parts.cumulative_volume is not None
    assert pairs(change.parts.cumulative_volume.changed) == [[100.5, 2]]
    assert last_size(change) == 2
