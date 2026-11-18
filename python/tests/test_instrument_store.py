import dataclasses

import pytest
from helpers import definition, level, message, pairs, quiet

from barchart_desktop_sdk._store import InstrumentChange, InstrumentStore


def setup() -> tuple[InstrumentStore, InstrumentChange]:
    """A store with ESZ26's definition and a snapshot with a book and volume at price, and the snapshot's change."""
    store = InstrumentStore(quiet)
    store.apply(message(instrumentDefinition=definition()))
    snapshot = store.apply(
        message(
            marketSnapshot={
                "marketId": 42,
                "tradeDate": 0,
                "priceLevels": [level("BID", 1, 10000, 5), level("BID", 2, 9975, 7), level("OFFER", 1, 10025, 3)],
                "volumeAtPrice": {"priceVolumes": [{"price": 10025, "volume": 4}, {"price": 10000, "volume": 10}]},
            }
        )
    )
    assert snapshot is not None
    return store, snapshot


def test_builds_a_fixed_length_frozen_book_from_a_snapshot() -> None:
    _, snapshot = setup()
    assert snapshot.parts.depth is not None
    book = snapshot.parts.depth.book
    assert len(book.bids) == 5
    assert book.bids[2].size == 0
    assert book.asks[0].price == 100.25
    with pytest.raises(dataclasses.FrozenInstanceError):
        book.bids[0].size = 1  # type: ignore[misc]


def test_seeds_cumulative_volume_ascending_with_every_level_changed() -> None:
    _, snapshot = setup()
    cv = snapshot.parts.cumulative_volume
    assert cv is not None
    assert pairs(cv.levels) == [[100, 10], [100.25, 4]]
    assert cv.changed is cv.levels


def test_hands_out_the_same_objects_from_its_getters() -> None:
    store, snapshot = setup()
    assert snapshot.parts.depth is not None
    assert snapshot.parts.cumulative_volume is not None
    assert store.get_book_by_id("42") is snapshot.parts.depth.book
    assert store.get_cumulative_volume_by_id("42") is snapshot.parts.cumulative_volume.levels


def test_applies_a_depth_delete_shifting_levels_up_and_padding_the_end() -> None:
    store, _ = setup()
    change = store.apply(
        message(marketUpdate={"marketId": 42, "symbol": "ESZ26", "depthPriceLevel": {"levels": [{"deletePriceLevel": {"side": "BID", "level": 1}}]}})
    )
    assert change is not None
    assert change.parts.depth is not None
    bids = change.parts.depth.book.bids
    assert len(bids) == 5
    assert bids[0].price == 99.75
    assert bids[1].size == 0
    assert change.parts.cumulative_volume is None
    assert change.parts.quote is None


def test_builds_cumulative_volume_from_trades_and_the_trades_size_at_last_is_last_size() -> None:
    store, snapshot = setup()
    change = store.apply(
        message(
            marketUpdate={
                "marketId": 42,
                "symbol": "ESZ26",
                "session": {"last": {"price": 10025}},
                "trades": {"trades": [{"trade": {"price": 10050, "quantity": 1}}, {"trade": {"price": 10025, "quantity": 2}}]},
            }
        )
    )
    assert change is not None
    cv = change.parts.cumulative_volume
    assert cv is not None
    assert pairs(cv.levels) == [[100, 10], [100.25, 6], [100.5, 1]]
    assert pairs(cv.changed) == [[100.25, 6], [100.5, 1]]
    assert change.parts.quote is not None
    quote = change.parts.quote.quote
    assert [quote.session.last, quote.session.last_size] == [100.25, 2]
    assert snapshot.parts.quote is not None
    assert quote.previous_session is snapshot.parts.quote.quote.previous_session


def test_keeps_book_and_cumulative_volume_through_a_snapshot_without_them() -> None:
    store, _ = setup()
    levels = store.get_cumulative_volume_by_id("42")
    change = store.apply(message(marketSnapshot={"marketId": 42, "tradeDate": 0}))
    assert change is not None
    assert change.parts.cumulative_volume is None
    assert change.parts.depth is None
    assert store.get_cumulative_volume_by_id("42") is levels


def test_skips_snapshots_before_the_definition_and_updates_before_the_first_snapshot() -> None:
    store = InstrumentStore(quiet)
    assert store.apply(message(marketSnapshot={"marketId": 42, "tradeDate": 0})) is None
    store.apply(message(instrumentDefinition=definition()))
    assert store.apply(message(marketUpdate={"marketId": 42, "symbol": "ESZ26", "high": {"price": 10100}})) is None
