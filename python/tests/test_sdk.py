"""The SDK as a whole, with a fake connection: subscriptions, events, state, coalescing and threads."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from fakes import CREDENTIALS, FakeClient, flush, get_client, wait_until
from helpers import definition, level, pairs, recording_logger
from openfeed.generated import openfeed_api_pb2 as api
from openfeed.generated import openfeed_pb2 as proto

from barchart_desktop_sdk import BookEvent, CumulativeVolumeEvent, DesktopSdk, Listener, QuoteEvent, RawListener, SubscriptionError

CreateSdk = Callable[..., DesktopSdk]


def respond(client: FakeClient, subscription_type: Any, *, symbol: str = "ESZ26", market_id: int = 42) -> None:
    """The server's answer to the last subscription sent for the symbol and type, with its correlation id, as the server
    echoes it.
    """
    wanted = api.SubscriptionType.Value(subscription_type) if isinstance(subscription_type, str) else subscription_type
    sent = [s for s in client.subscriptions if not s.unsubscribe and s.requests[0].symbol == symbol and s.requests[0].subscriptionType[0] == wanted]
    client.receive(
        subscriptionResponse={
            "correlationId": sent[-1].correlationId if sent else 0,
            "symbol": symbol,
            "subscriptionType": subscription_type,
            "marketId": market_id,
            "status": {"result": "SUCCESS"},
        }
    )


def snapshot(client: FakeClient) -> None:
    client.receive(
        marketSnapshot={
            "marketId": 42,
            "priceLevels": [level("BID", 1, 10000, 5)],
            "volumeAtPrice": {"priceVolumes": [{"price": 10000, "volume": 10}]},
        }
    )


def update(client: FakeClient, **fields: Any) -> None:
    client.receive(marketUpdate={"marketId": 42, "symbol": "ESZ26", **fields})


def bbo(client: FakeClient, bid: int) -> None:
    update(client, bbo={"bidPrice": bid, "bidQuantity": 1, "offerPrice": bid + 25, "offerQuantity": 1})


def trade(client: FakeClient, price: int, quantity: int) -> None:
    update(
        client,
        session={"last": {"price": price, "quantity": quantity}},
        trades={"trades": [{"trade": {"price": price, "quantity": quantity}}]},
    )


def depth(client: FakeClient) -> None:
    update(client, depthPriceLevel={"levels": [{"modifyPriceLevel": level("BID", 1, 10000, 9)}]})


@dataclass
class Got:
    quote: list[QuoteEvent] = field(default_factory=list)
    book: list[BookEvent] = field(default_factory=list)
    volume: list[CumulativeVolumeEvent] = field(default_factory=list)
    raw: list[Any] = field(default_factory=list)


def setup(create_sdk: CreateSdk, **options: Any) -> tuple[DesktopSdk, FakeClient, Got]:
    """A logged-in SDK with a quote, depth and cumulative volume subscription on ESZ26, and its definition."""
    sdk = create_sdk(**options)
    got = Got()
    sdk.login(CREDENTIALS)
    sdk.subscribe_quote("ESZ26", Listener(on_event=got.quote.append, on_message=lambda message, _: got.raw.append(message)))
    sdk.subscribe_depth("ESZ26", Listener(on_event=got.book.append))
    sdk.subscribe_cumulative_volume("ESZ26", Listener(on_event=got.volume.append))
    flush(sdk)
    client = get_client()
    client.log_in()
    respond(client, api.ALL)
    respond(client, api.DEPTH_PRICE)
    respond(client, api.CUMULATIVE_VOLUME)
    client.receive(instrumentDefinition=definition(book_depth=2))
    flush(sdk)
    return sdk, client, got


def block_delivery(sdk: DesktopSdk, release: threading.Event) -> None:
    """Keeps the delivery thread busy until `release` is set, so what's queued meanwhile is seen together."""

    def wait() -> None:
        release.wait(5)

    sdk._schedule(wait)


class TestRawAccessAndLifetime:
    def test_subscribes_raw_on_real_time_and_snapshots_on_real_time_snapshot_by_default(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        sdk.raw.subscribe("ZC^UDS", api.ALL, RawListener(on_message=lambda *_: None))
        sdk.raw.request_snapshot("ZCZ26")
        flush(sdk)
        info = {(e.service, e.type, e.symbol, e.state) for e in sdk.diagnostics.get_subscription_info()}
        assert info == {(proto.REAL_TIME, api.ALL, "ZC^UDS", "pending"), (proto.REAL_TIME_SNAPSHOT, api.ALL, "ZCZ26", "pending")}
        assert sdk.diagnostics.get_connection_token() is None
        assert sdk.raw.send(api.ExchangeRequest()) is None
        assert sdk.diagnostics.get_last_messages("ZCZ26") is None

    def test_rejects_snapshot_requests_pending_at_dispose_and_any_after_it(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        pending = sdk.raw.request_snapshot("ZCZ26")
        flush(sdk)
        sdk.dispose()
        with pytest.raises(RuntimeError, match="The Desktop SDK was disposed"):
            pending.result(5)
        with pytest.raises(RuntimeError, match="The Desktop SDK was disposed"):
            sdk.raw.request_snapshot("ZCZ26").result(5)

    def test_refuses_new_subscriptions_and_logins_after_dispose(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        sdk.dispose()
        with pytest.raises(RuntimeError, match="disposed"):
            sdk.subscribe_quote("ESZ26", Listener())
        with pytest.raises(RuntimeError, match="disposed"):
            sdk.login(CREDENTIALS)
        with pytest.raises(RuntimeError, match="disposed"):
            sdk.when_connected().result(5)

    def test_gives_a_one_off_snapshot_with_its_definition_then_closes(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        sdk.login(CREDENTIALS)
        client = get_client()
        client.log_in()
        request = sdk.raw.request_snapshot("ESZ26")
        flush(sdk)
        wait_until(lambda: client.subscriptions)
        respond(client, api.ALL)
        client.receive(instrumentDefinition=definition())
        snapshot(client)

        result = request.result(5)
        assert result.market_snapshot.marketId == 42
        assert result.instrument_definition is not None
        assert result.instrument_definition.symbol == "ESZ26"
        flush(sdk)
        assert sdk.diagnostics.get_subscription_info() == []
        wait_until(lambda: [s for s in client.subscriptions if s.unsubscribe])

    def test_fails_a_one_off_snapshot_the_server_refuses_with_its_error(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        sdk.login(CREDENTIALS)
        client = get_client()
        client.log_in()
        request = sdk.raw.request_snapshot("ESZ26")
        flush(sdk)
        client.receive(subscriptionResponse={"symbol": "ESZ26", "subscriptionType": "ALL", "status": {"result": "INVALID_SYMBOL", "message": "nope"}})
        with pytest.raises(SubscriptionError, match="INVALID_SYMBOL"):
            request.result(5)

    def test_gives_raw_on_message_every_message_after_the_sdk_applied_it(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = setup(create_sdk)
        kinds: list[Any] = []
        quotes: list[Any] = []

        def on_message(message: api.OpenfeedGatewayMessage, _: Any) -> None:
            kinds.append(message.WhichOneof("data"))
            quotes.append(sdk.get_quote("ESZ26"))

        remove = sdk.raw.on_message(on_message)
        snapshot(client)
        flush(sdk)
        assert kinds == ["marketSnapshot"]
        assert quotes[0] is not None

        remove()
        snapshot(client)
        flush(sdk)
        assert kinds == ["marketSnapshot"]


class TestDelivery:
    def test_delivers_an_event_per_message_by_default(self, create_sdk: CreateSdk) -> None:
        sdk, client, got = setup(create_sdk)
        snapshot(client)
        bbo(client, 10000)
        bbo(client, 10025)
        trade(client, 10050, 2)
        flush(sdk)
        assert [len(got.quote), len(got.book), len(got.volume)] == [4, 1, 2]

    def test_delivers_nothing_inside_subscribe_and_the_current_state_afterwards_as_initial(self, create_sdk: CreateSdk) -> None:
        sdk, client, got = setup(create_sdk)
        snapshot(client)
        flush(sdk)

        calling = threading.get_ident()
        threads: list[int] = []
        events: list[QuoteEvent] = []

        def on_event(event: QuoteEvent) -> None:
            threads.append(threading.get_ident())
            events.append(event)

        handle = sdk.subscribe_quote("ESZ26", Listener(on_event=on_event))
        assert handle.state in {"pending", "active"}
        flush(sdk)
        assert [event.kind for event in events] == ["initial"]
        assert events[0].raw.WhichOneof("data") == "marketSnapshot"
        assert threads[0] != calling
        assert handle.state == "active"
        assert len(got.quote) == 1

    def test_gives_a_later_subscriber_a_stored_error(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        sdk.login(CREDENTIALS)
        client = get_client()
        client.log_in()
        first: list[SubscriptionError] = []
        sdk.subscribe_depth("ESZ26", Listener(on_error=first.append))
        flush(sdk)
        client.receive(
            subscriptionResponse={
                "symbol": "ESZ26",
                "subscriptionType": "DEPTH_PRICE",
                "status": {"result": "INSUFFICIENT_PRIVILEGES", "message": "no"},
            }
        )
        flush(sdk)

        later: list[SubscriptionError] = []
        handle = sdk.subscribe_depth("ESZ26", Listener(on_error=later.append))
        flush(sdk)
        assert later == first
        assert len(first) == 1
        assert handle.state == "error"
        assert handle.error is first[0]

    def test_a_listener_that_raises_is_logged_and_the_rest_still_run(self, create_sdk: CreateSdk) -> None:
        logger, records = recording_logger()
        sdk, client, got = setup(create_sdk, logger=logger)

        def on_event(_: QuoteEvent) -> None:
            message = "listener"
            raise ValueError(message)

        sdk.subscribe_quote("ESZ26", Listener(on_event=on_event))
        flush(sdk)
        snapshot(client)
        bbo(client, 10000)
        flush(sdk)
        assert len(got.quote) == 2
        assert [record for record in records if "listener threw" in record.getMessage()]

    def test_sends_nothing_for_a_subscribe_and_close_in_one_batch(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        sdk.login(CREDENTIALS)
        client = get_client()
        client.log_in()
        flush(sdk)
        # the delivery thread is busy until both are queued, so it sees them together
        release = threading.Event()
        block_delivery(sdk, release)
        flush_started = threading.Event()
        sdk._schedule(flush_started.set)
        got: list[QuoteEvent] = []
        sdk.subscribe_quote("ESZ26", Listener(on_event=got.append)).close()
        sdk.subscribe_quote("NQZ26", Listener())
        release.set()
        wait_until(flush_started.is_set)
        flush(sdk)
        wait_until(lambda: client.subscriptions)
        assert [request.requests[0].symbol for request in client.subscriptions] == ["NQZ26"]
        assert {entry.symbol for entry in sdk.diagnostics.get_subscription_info()} == {"NQZ26"}

    def test_a_remount_in_one_batch_keeps_the_subscription_on_the_wire(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        sdk.login(CREDENTIALS)
        client = get_client()
        client.log_in()
        old = sdk.subscribe_quote("ESZ26", Listener())
        flush(sdk)
        wait_until(lambda: len(client.subscriptions) == 1)
        release = threading.Event()
        block_delivery(sdk, release)
        new = sdk.subscribe_quote("ESZ26", Listener())
        old.close()
        release.set()
        flush(sdk)
        assert len(client.subscriptions) == 1
        assert new.state == "pending"
        assert [entry.subscribers for entry in sdk.diagnostics.get_subscription_info()] == [1]

    def test_reports_a_symbol_once_no_subscription_has_it(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = setup(create_sdk)
        removed: list[str] = []
        sdk.on_removed(removed.append)
        depth_only = sdk.subscribe_depth("NQZ26", Listener())
        flush(sdk)
        respond(client, api.DEPTH_PRICE, symbol="NQZ26", market_id=7)
        assert sdk.is_subscribed("NQZ26")
        depth_only.close()
        flush(sdk)
        assert removed == ["NQZ26"]
        assert not sdk.is_subscribed("NQZ26")
        assert sdk.is_subscribed("ESZ26")


class TestThreads:
    def test_runs_listeners_one_at_a_time_on_one_thread_in_order(self, create_sdk: CreateSdk) -> None:
        sdk, client, got = setup(create_sdk)
        threads: set[int] = set()
        running = [0]
        overlaps = [0]
        prices: list[float | None] = []

        def on_event(event: QuoteEvent) -> None:
            threads.add(threading.get_ident())
            running[0] += 1
            overlaps[0] += running[0] > 1
            prices.append(event.quote.best_bid_offer.bid_price)
            time.sleep(0.001)
            running[0] -= 1

        sdk.subscribe_quote("ESZ26", Listener(on_event=on_event))
        sdk.subscribe_depth("ESZ26", Listener(on_event=lambda _: threads.add(threading.get_ident())))
        snapshot(client)
        flush(sdk)
        for bid in range(10000, 10020):
            bbo(client, bid)
        flush(sdk)
        depth(client)
        flush(sdk)
        assert len(threads) == 1
        assert threading.get_ident() not in threads
        assert overlaps == [0]
        assert prices[-20:] == [bid / 100 for bid in range(10000, 10020)]
        assert len(got.quote) == 21

    def test_a_slow_listener_doesnt_hold_up_the_state(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = setup(create_sdk)
        release = threading.Event()

        def on_event(_: QuoteEvent) -> None:
            release.wait(5)

        sdk.subscribe_quote("ESZ26", Listener(on_event=on_event))
        flush(sdk)
        snapshot(client)
        bbo(client, 10000)
        bbo(client, 10025)
        quote = sdk.get_quote("ESZ26")
        release.set()
        assert quote is not None
        assert quote.best_bid_offer.bid_price == 100.25

    def test_a_listener_can_call_the_sdk_including_closing_itself(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = setup(create_sdk)
        seen: list[Any] = []
        handles: list[Any] = []

        def on_event(event: QuoteEvent) -> None:
            seen.append((sdk.get_quote(event.symbol) is not None, sdk.connection_state))
            handles[0].close()
            sdk.subscribe_depth("NQZ26", Listener())

        handles.append(sdk.subscribe_quote("ESZ26", Listener(on_event=on_event)))
        flush(sdk)
        snapshot(client)
        bbo(client, 10000)
        flush(sdk)
        flush(sdk)
        assert seen == [(True, "connected")]
        assert handles[0].state == "closed"
        assert "NQZ26" in {entry.symbol for entry in sdk.diagnostics.get_subscription_info()}

    def test_starts_no_callback_after_close_returns(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = setup(create_sdk)
        got: list[QuoteEvent] = []
        handle = sdk.subscribe_quote("ESZ26", Listener(on_event=got.append))
        flush(sdk)
        snapshot(client)
        bbo(client, 10000)
        handle.close()
        count = len(got)
        bbo(client, 10025)
        flush(sdk)
        assert len(got) == count
        assert handle.state == "closed"

    def test_stays_consistent_with_many_threads_subscribing_closing_and_reading(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = setup(create_sdk)
        errors: list[BaseException] = []
        snapshot(client)
        stop = threading.Event()

        def churn(symbol: str) -> None:
            try:
                while not stop.is_set():
                    handle = sdk.subscribe_quote(symbol, Listener(on_event=lambda _: None))
                    sdk.get_quote("ESZ26")
                    sdk.is_subscribed(symbol)
                    sdk.diagnostics.get_subscription_info()
                    handle.close()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=churn, args=(symbol,)) for symbol in ("ESZ26", "ESZ26", "NQZ26", "YMZ26")]
        for thread in threads:
            thread.start()
        for bid in range(10000, 10500):
            bbo(client, bid)
        stop.set()
        for thread in threads:
            thread.join(5)
        flush(sdk)

        assert errors == []
        quote = sdk.get_quote("ESZ26")
        assert quote is not None
        assert quote.best_bid_offer.bid_price == 104.99
        # only setup's subscriptions are left
        info = {(entry.symbol, entry.type): entry.subscribers for entry in sdk.diagnostics.get_subscription_info()}
        assert info == {("ESZ26", api.ALL): 1, ("ESZ26", api.DEPTH_PRICE): 1, ("ESZ26", api.CUMULATIVE_VOLUME): 1}


class TestCoalescing:
    def test_holds_events_for_the_interval_then_delivers_one_per_subscription_with_the_latest_state(self, create_sdk: CreateSdk) -> None:
        sdk, client, got = setup(create_sdk, coalesce_interval_ms=150)
        raw_before = len(got.raw)
        snapshot(client)
        bbo(client, 10000)
        bbo(client, 10025)
        trade(client, 10050, 2)
        depth(client)
        trade(client, 10075, 3)

        assert [len(got.quote), len(got.book), len(got.volume)] == [0, 0, 0]
        quote = sdk.get_quote("ESZ26")
        assert quote is not None
        # the state is current at once
        assert quote.best_bid_offer.bid_price == 100.25

        wait_until(lambda: got.quote and got.book and got.volume)
        flush(sdk)
        # raw messages aren't held
        assert len(got.raw) - raw_before == 6
        assert [len(got.quote), len(got.book), len(got.volume)] == [1, 1, 1]
        assert [got.quote[0].quote.best_bid_offer.bid_price, got.quote[0].quote.session.last] == [100.25, 100.75]
        assert [got.quote[0].kind, got.volume[0].kind] == ["snapshot", "snapshot"]
        assert got.book[0].book.bids[0].size == 9
        assert pairs(got.volume[0].levels) == [[100, 10], [100.5, 2], [100.75, 3]]
        # after a snapshot, every level
        assert pairs(got.volume[0].changed) == pairs(got.volume[0].levels)

    def test_merges_cumulative_volumes_changed_levels_across_an_interval_of_updates(self, create_sdk: CreateSdk) -> None:
        sdk, client, got = setup(create_sdk, coalesce_interval_ms=50)
        snapshot(client)
        wait_until(lambda: got.volume)
        trade(client, 10050, 1)
        bbo(client, 10050)
        trade(client, 10100, 4)
        wait_until(lambda: len(got.volume) == 2)
        flush(sdk)

        assert [len(got.quote), len(got.volume)] == [2, 2]
        assert got.quote[1].kind == "update"
        assert pairs(got.volume[1].changed) == [[100.5, 1], [101, 4]]
        # the last message behind it
        assert got.quote[1].raw.marketUpdate.HasField("trades")

    def test_drops_whats_held_at_logout(self, create_sdk: CreateSdk) -> None:
        sdk, client, got = setup(create_sdk, coalesce_interval_ms=30)
        snapshot(client)
        sdk.logout()
        time.sleep(0.06)
        flush(sdk)
        assert got.quote == []
