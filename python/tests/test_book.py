import logging
from collections.abc import Iterable
from typing import Any

from helpers import definition, level, message, recording_logger

from barchart_desktop_sdk import Book, PriceLevel
from barchart_desktop_sdk._store import InstrumentChange, InstrumentStore


def setup() -> tuple[InstrumentStore, list[logging.LogRecord]]:
    """ESZ26 with a book three levels deep: bids 100 x 5, 99.75 x 7; ask 100.25 x 3."""
    logger, records = recording_logger()
    store = InstrumentStore(logger)
    store.apply(message(instrumentDefinition=definition(book_depth=3)))
    store.apply(
        message(
            marketSnapshot={
                "marketId": 42,
                "tradeDate": 0,
                "priceLevels": [level("BID", 1, 10000, 5), level("BID", 2, 9975, 7), level("OFFER", 1, 10025, 3)],
            }
        )
    )
    return store, records


def depth(store: InstrumentStore, *entries: dict[str, Any]) -> InstrumentChange | None:
    return store.apply(message(marketUpdate={"marketId": 42, "symbol": "ESZ26", "depthPriceLevel": {"levels": list(entries)}}))


def add(side: str, n: int, price: int, quantity: int) -> dict[str, Any]:
    return {"addPriceLevel": level(side, n, price, quantity)}


def modify(side: str, n: int, price: int, quantity: int) -> dict[str, Any]:
    return {"modifyPriceLevel": level(side, n, price, quantity)}


def remove(side: str, n: int) -> dict[str, Any]:
    return {"deletePriceLevel": {"side": side, "level": n}}


def sizes(levels: Iterable[PriceLevel]) -> list[list[float]]:
    """`[price, size]` pairs."""
    return [[pl.price, pl.size] for pl in levels]


def book(store: InstrumentStore) -> Book:
    current = store.get_book_by_id("42")
    assert current is not None
    return current


def test_builds_both_sides_at_the_books_depth_with_empty_levels_where_the_snapshot_has_none() -> None:
    store, _ = setup()
    assert sizes(book(store).bids) == [[100, 5], [99.75, 7], [0, 0]]
    assert sizes(book(store).asks) == [[100.25, 3], [0, 0], [0, 0]]


def test_inserts_an_add_pushing_the_levels_below_it_down_and_the_last_one_off() -> None:
    store, _ = setup()
    depth(store, add("BID", 1, 10010, 1))
    assert sizes(book(store).bids) == [[100.1, 1], [100, 5], [99.75, 7]]


def test_removes_a_delete_moving_the_levels_below_it_up_and_adding_an_empty_one_at_the_end() -> None:
    store, _ = setup()
    depth(store, remove("BID", 1))
    assert sizes(book(store).bids) == [[99.75, 7], [0, 0], [0, 0]]


def test_replaces_a_modify_in_place_with_its_order_count() -> None:
    store, _ = setup()
    depth(store, modify("OFFER", 1, 10025, 9))
    assert sizes(book(store).asks) == [[100.25, 9], [0, 0], [0, 0]]
    assert book(store).asks[0].order_count is None
    depth(store, {"modifyPriceLevel": {**level("OFFER", 1, 10025, 9), "orderCount": 2}})
    assert book(store).asks[0].order_count == 2


def test_applies_a_messages_entries_in_order() -> None:
    store, _ = setup()
    depth(store, remove("BID", 1), add("BID", 2, 9950, 2), modify("OFFER", 1, 10050, 1))
    assert sizes(book(store).bids) == [[99.75, 7], [99.5, 2], [0, 0]]
    assert book(store).asks[0].price == 100.5


def test_skips_an_add_a_modify_or_a_delete_outside_the_book_with_an_error_and_applies_the_messages_other_entries() -> None:
    store, records = setup()
    before = book(store)
    depth(store, add("BID", 4, 9950, 2), modify("BID", 4, 9950, 2), remove("OFFER", 4), modify("OFFER", 1, 10025, 9))
    assert book(store).bids == before.bids
    assert sizes(book(store).asks) == [[100.25, 9], [0, 0], [0, 0]]
    assert [r.levelname for r in records] == ["ERROR"] * 3


def test_skips_a_snapshots_levels_outside_the_book_with_an_error() -> None:
    logger, records = recording_logger()
    store = InstrumentStore(logger)
    store.apply(message(instrumentDefinition=definition(book_depth=2)))
    store.apply(
        message(
            marketSnapshot={
                "marketId": 42,
                "tradeDate": 0,
                "priceLevels": [level("BID", 1, 10000, 5), level("BID", 3, 9950, 1), level("OFFER", 0, 10025, 3)],
            }
        )
    )
    assert sizes(book(store).bids) == [[100, 5], [0, 0]]
    assert sizes(book(store).asks) == [[0, 0], [0, 0]]
    assert [r.levelname for r in records] == ["ERROR"] * 2


def test_starts_depth_without_a_book_from_an_empty_one_as_after_a_snapshot_of_an_empty_book() -> None:
    logger, warnings = recording_logger()
    store = InstrumentStore(logger)
    store.apply(message(instrumentDefinition=definition(book_depth=2)))
    store.apply(message(marketSnapshot={"marketId": 42, "tradeDate": 0}))
    assert store.get_book_by_id("42") is None

    change = depth(store, add("BID", 1, 10000, 1))
    assert change is not None
    assert change.parts.depth is not None
    assert store.get_book_by_id("42") is change.parts.depth.book
    assert sizes(book(store).bids) == [[100, 1], [0, 0]]
    assert sizes(book(store).asks) == [[0, 0], [0, 0]]
    assert warnings == []


def test_clear_book_empties_the_book_keeping_its_depth_and_the_next_depth_update_builds_on_it() -> None:
    store, _ = setup()
    change = store.apply(message(marketUpdate={"marketId": 42, "symbol": "ESZ26", "clearBook": {"transactionTime": 0}}))
    assert change is not None
    assert change.parts.depth is not None
    assert store.get_book_by_id("42") is change.parts.depth.book
    assert sizes(book(store).bids) == [[0, 0]] * 3
    assert sizes(book(store).asks) == [[0, 0]] * 3

    depth(store, add("OFFER", 1, 10050, 2))
    assert sizes(book(store).asks)[0] == [100.5, 2]
    assert sizes(book(store).bids)[0] == [0, 0]


def test_remove_book_drops_the_book_only() -> None:
    store, _ = setup()
    store.remove_book("42")
    assert store.get_book_by_id("42") is None
    assert store.get_quote_by_id("42") is not None
