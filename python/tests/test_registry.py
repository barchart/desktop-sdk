"""The registry: entries, their dependencies, responses, errors and definition symbols."""

import threading
from dataclasses import dataclass, field
from typing import Any, cast

from helpers import quiet
from openfeed.generated import openfeed_api_pb2 as api
from openfeed.generated import openfeed_instrument_pb2
from openfeed.generated import openfeed_pb2 as proto

from barchart_desktop_sdk import Listener, SubscriptionError
from barchart_desktop_sdk._registry import Registry
from barchart_desktop_sdk._subscriber import Entry, EntryKey, Subscriber, get_subscriber_error, get_subscriber_state


def key(service: int, subscription_type: int, symbol: str = "ESZ26") -> str:
    return f"+{service}|{subscription_type}|{symbol}"


@dataclass
class FakeClient:
    """Records what goes on the wire."""

    wire: list[str] = field(default_factory=list)

    def subscribe(self, service: Any, subscription_type: Any, symbol: str) -> int:
        self.wire.append(key(service, subscription_type, symbol))
        return len(self.wire)

    def unsubscribe(self, subscription_id: int) -> None:
        self.wire.append(f"-{subscription_id}")


def setup() -> tuple[Registry, list[str]]:
    registry = Registry()
    client = FakeClient()
    registry.attach(client)
    return registry, client.wire


@dataclass
class Subscribed:
    subscriber: Subscriber
    entry: Entry
    errors: list[int]
    received: list[SubscriptionError]


def subscribe(registry: Registry, service: Any, subscription_type: Any, method: Any) -> Subscribed:
    errors: list[int] = []
    received: list[SubscriptionError] = []

    def on_error(error: SubscriptionError) -> None:
        errors.append(error.type)
        received.append(error)

    subscriber = Subscriber(
        EntryKey(service, subscription_type, "ESZ26"),
        method,
        Listener(on_error=on_error),
        quiet,
        threading.Condition(threading.Lock()),
        lambda _: None,
    )
    subscriber.entry = registry.acquire(service, subscription_type, "ESZ26", [])
    subscriber.entry.subscribers[subscriber] = None
    return Subscribed(subscriber, subscriber.entry, errors, received)


def respond(registry: Registry, response: api.SubscriptionResponse) -> None:
    """A response, with its errors given to their subscribers, which the SDK does on its delivery thread."""
    for subscriber, error in registry.on_response(response).failures:
        subscriber.fail(error)


def response(subscription_type: Any, ok: bool) -> api.SubscriptionResponse:
    return api.SubscriptionResponse(
        symbol="ESZ26",
        subscriptionType=subscription_type,
        marketId=5 if ok else 0,
        status={"result": api.SUCCESS if ok else api.INVALID_SYMBOL, "message": "" if ok else "nope"},
    )


class TestCumulativeVolumesAllDependency:
    def test_subscribes_all_with_a_cumulative_volume_alone_and_a_later_quote_shares_it(self) -> None:
        registry, wire = setup()
        registry.acquire(proto.REAL_TIME_SNAPSHOT, api.CUMULATIVE_VOLUME, "ESZ26", [])
        assert wire == [key(proto.REAL_TIME, api.ALL), key(proto.REAL_TIME_SNAPSHOT, api.CUMULATIVE_VOLUME)]
        registry.acquire(proto.REAL_TIME, api.ALL, "ESZ26", [])
        assert len(wire) == 2

    def test_shares_the_quotes_all_when_the_quote_comes_first(self) -> None:
        registry, wire = setup()
        registry.acquire(proto.REAL_TIME, api.ALL, "ESZ26", [])
        registry.acquire(proto.REAL_TIME_SNAPSHOT, api.CUMULATIVE_VOLUME, "ESZ26", [])
        assert wire == [key(proto.REAL_TIME, api.ALL), key(proto.REAL_TIME_SNAPSHOT, api.CUMULATIVE_VOLUME)]

    def test_keeps_all_while_the_cumulative_volume_holds_it_and_removes_both_with_it(self) -> None:
        registry, wire = setup()
        quote = registry.acquire(proto.REAL_TIME, api.ALL, "ESZ26", [])
        volume = registry.acquire(proto.REAL_TIME_SNAPSHOT, api.CUMULATIVE_VOLUME, "ESZ26", [])
        registry.remove(quote)
        assert [w for w in wire if w.startswith("-")] == []
        registry.remove(volume)
        assert len([w for w in wire if w.startswith("-")]) == 2


class TestASubscriptionError:
    def test_says_what_was_refused_and_keeps_the_servers_text(self) -> None:
        registry, _ = setup()
        depth = subscribe(registry, proto.REAL_TIME, api.DEPTH_PRICE, "depth")
        respond(
            registry,
            api.SubscriptionResponse(
                symbol="ESZ26",
                subscriptionType=api.DEPTH_PRICE,
                status={"result": api.INSUFFICIENT_PRIVILEGES, "message": "unknown_service is not allowed"},
            ),
        )

        error = depth.subscriber.handle.error
        assert error is not None
        assert error.message == 'INSUFFICIENT_PRIVILEGES: DEPTH_PRICE on ESZ26 (REAL_TIME) was refused. Server said: "unknown_service is not allowed"'
        assert error.result == api.INSUFFICIENT_PRIVILEGES
        assert error.service == proto.REAL_TIME
        assert error.server_message == "unknown_service is not allowed"
        assert error.required_by is None

    def test_leaves_out_a_server_text_thats_empty_and_shows_a_result_it_cant_name_as_its_number(self) -> None:
        error = SubscriptionError(cast("Any", 999), "ESZ26", api.ALL, proto.DELAYED, "")
        assert error.message == "999: ALL on ESZ26 (DELAYED) was refused."

    def test_names_the_cumulative_volume_that_needed_a_refused_all_only_for_its_subscribers(self) -> None:
        registry, _ = setup()
        quote = subscribe(registry, proto.REAL_TIME, api.ALL, "quote")
        volume = subscribe(registry, proto.REAL_TIME_SNAPSHOT, api.CUMULATIVE_VOLUME, "cumulative_volume")
        respond(registry, response(api.ALL, ok=False))

        assert quote.received[0].message == 'INVALID_SYMBOL: ALL on ESZ26 (REAL_TIME) was refused. Server said: "nope"'
        assert (
            volume.received[0].message == 'INVALID_SYMBOL: ALL on ESZ26 (REAL_TIME), which CUMULATIVE_VOLUME needs, was refused. Server said: "nope"'
        )
        assert volume.received[0].required_by == api.CUMULATIVE_VOLUME
        # the same object however it's reached: a later subscriber's first delivery comes from get_subscriber_error
        assert volume.subscriber.handle.error is volume.received[0]
        assert get_subscriber_error(volume.entry) is volume.received[0]
        # diagnostics show the entry as the server answered it
        assert volume.entry.error is None


class TestAFailedDependency:
    def test_reaches_the_quote_and_the_cumulative_volume_subscribers_with_alls_error(self) -> None:
        registry, _ = setup()
        quote = subscribe(registry, proto.REAL_TIME, api.ALL, "quote")
        volume = subscribe(registry, proto.REAL_TIME_SNAPSHOT, api.CUMULATIVE_VOLUME, "cumulative_volume")
        respond(registry, response(api.CUMULATIVE_VOLUME, ok=True))
        respond(registry, response(api.ALL, ok=False))

        assert quote.errors == [api.ALL]
        assert volume.errors == [api.ALL]
        assert get_subscriber_state(volume.entry) == "error"
        error = volume.subscriber.handle.error
        assert error is not None
        assert error.type == api.ALL
        # diagnostics show the entry as the server answered it
        assert volume.entry.status == "active"

        respond(registry, response(api.ALL, ok=True))
        assert get_subscriber_state(volume.entry) == "active"

    def test_isnt_reported_twice_to_a_cumulative_volume_that_failed_on_its_own(self) -> None:
        registry, _ = setup()
        volume = subscribe(registry, proto.REAL_TIME_SNAPSHOT, api.CUMULATIVE_VOLUME, "cumulative_volume")
        respond(registry, response(api.CUMULATIVE_VOLUME, ok=False))
        respond(registry, response(api.ALL, ok=False))
        assert volume.errors == [api.CUMULATIVE_VOLUME]

    def test_keeps_the_cumulative_volume_in_error_until_logout_clears_it(self) -> None:
        registry, _ = setup()
        volume = subscribe(registry, proto.REAL_TIME_SNAPSHOT, api.CUMULATIVE_VOLUME, "cumulative_volume")
        respond(registry, response(api.ALL, ok=False))
        respond(registry, response(api.CUMULATIVE_VOLUME, ok=True))
        assert get_subscriber_state(volume.entry) == "error"

        registry.detach()
        assert get_subscriber_state(volume.entry) == "pending"
        assert get_subscriber_error(volume.entry) is None


class TestDefinitionSymbols:
    @staticmethod
    def naming(registry: Registry, symbol: str, market_id: int) -> Entry:
        """An entry on `symbol` whose response names `market_id`."""
        entry = registry.acquire(proto.REAL_TIME, api.ALL, symbol, [])
        registry.on_response(api.SubscriptionResponse(symbol=symbol, subscriptionType=api.ALL, marketId=market_id, status={"result": "SUCCESS"}))
        return entry

    @staticmethod
    def define(registry: Registry, market_id: int, symbol: str) -> None:
        registry.on_definition(openfeed_instrument_pb2.InstrumentDefinition(marketId=market_id, symbol=symbol))

    def test_find_a_named_market_until_the_last_entry_naming_it_goes(self) -> None:
        registry, _ = setup()
        root = self.naming(registry, "ZC^UDS", 7)
        quote = self.naming(registry, "ZCZ26", 7)
        self.define(registry, 7, "ZCZ6")
        assert registry.get_market_id("ZCZ6") == "7"

        registry.remove(root)
        assert registry.get_market_id("ZCZ6") == "7"
        registry.remove(quote)
        assert registry.get_market_id("ZCZ6") is None

    def test_aren_t_kept_for_a_market_no_entry_names(self) -> None:
        registry, _ = setup()
        self.define(registry, 9, "ZCH7")
        assert registry.get_market_id("ZCH7") is None

    def test_follow_an_exchange_move_and_a_new_definition(self) -> None:
        registry, _ = setup()
        self.naming(registry, "ZC^UDS", 7)
        self.define(registry, 7, "ZCZ6")
        registry.moved("7", "8")
        assert registry.get_market_id("ZCZ6") == "8"

        self.define(registry, 8, "ZCZ26")
        assert (registry.get_market_id("ZCZ6"), registry.get_market_id("ZCZ26")) == (None, "8")

    def test_stay_with_the_market_that_took_one_last_when_the_other_one_goes(self) -> None:
        registry, _ = setup()
        first = self.naming(registry, "ZC^UDS", 7)
        self.naming(registry, "ZW^UDS", 8)
        self.define(registry, 7, "XZ6")
        self.define(registry, 8, "XZ6")

        registry.remove(first)
        assert registry.get_market_id("XZ6") == "8"
