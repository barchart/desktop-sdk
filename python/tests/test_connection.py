"""Connecting, reconnecting and requests: the connection, on a fake Openfeed client."""

import logging
import random
import threading
from collections.abc import Callable
from typing import Any

import pytest
from fakes import CREDENTIALS, FakeClient, flush, get_client, wait_until
from helpers import definition, recording_logger
from openfeed.generated import openfeed_api_pb2 as api
from openfeed.generated import openfeed_pb2 as proto

from barchart_desktop_sdk import DesktopSdk, InstrumentLookupError, Listener, QuoteEvent, RawListener
from barchart_desktop_sdk import _connection as connection_module

CreateSdk = Callable[..., DesktopSdk]


def logged_in(create_sdk: CreateSdk) -> tuple[DesktopSdk, FakeClient, list[str]]:
    sdk = create_sdk()
    states: list[str] = []
    sdk.on_connection_state(states.append)
    sdk.login(CREDENTIALS)
    client = get_client()
    client.log_in()
    flush(sdk)
    return sdk, client, states


def symbols(client: FakeClient) -> list[str]:
    return [request.requests[0].symbol for request in client.subscriptions if not request.unsubscribe]


class TestLogin:
    def test_gives_the_client_the_credentials_and_reports_connecting_then_connected(self, create_sdk: CreateSdk) -> None:
        sdk, client, states = logged_in(create_sdk)
        assert [client.username, client.password, client.server] == ["u", "p", "ws://test"]
        assert states == ["connecting", "connected"]
        assert sdk.connection_state == "connected"
        assert sdk.diagnostics.get_connection_token() == "t"
        sdk.when_connected().result(5)

    def test_when_connected_waits_for_the_login(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        connected = sdk.when_connected()
        sdk.login(CREDENTIALS)
        client = get_client()
        assert not connected.done()
        client.log_in()
        connected.result(5)

    @pytest.mark.parametrize("result", ["AUTHENTICATION_REQUIRED", "INVALID_CREDENTIALS", "INSUFFICIENT_PRIVILEGES"])
    def test_a_rejected_login_stops_for_good(self, create_sdk: CreateSdk, result: str) -> None:
        sdk = create_sdk()
        states: list[str] = []
        sdk.on_connection_state(states.append)
        connected = sdk.when_connected()
        sdk.login(CREDENTIALS)
        client = get_client()
        client.receive(loginResponse={"status": {"result": result}})

        with pytest.raises(RuntimeError, match="rejected the credentials"):
            connected.result(5)
        assert client.stopped.is_set()
        flush(sdk)
        assert states == ["connecting", "credentials-rejected"]
        assert len(FakeClient.instances) == 1

    def test_a_duplicate_login_stops_for_good_after_disconnecting(self, create_sdk: CreateSdk) -> None:
        sdk, client, states = logged_in(create_sdk)
        client.receive(logoutResponse={"status": {"result": "DUPLICATE_LOGIN"}})
        wait_until(lambda: sdk.connection_state == "credentials-rejected")
        flush(sdk)
        assert states == ["connecting", "connected", "disconnected", "credentials-rejected"]
        assert len(FakeClient.instances) == 1

    def test_any_other_failed_login_tries_again(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        sdk.login(CREDENTIALS)
        client = get_client()
        client.receive(loginResponse={"status": {"result": "UNKNOWN_RESULT", "message": "busy"}})
        wait_until(lambda: len(FakeClient.instances) == 2)
        assert client.stopped.is_set()
        assert sdk.connection_state == "connecting"

    def test_logging_in_again_stops_the_old_client_and_ignores_it(self, create_sdk: CreateSdk) -> None:
        sdk, first, _ = logged_in(create_sdk)
        sdk.login(CREDENTIALS)
        wait_until(lambda: len(FakeClient.instances) == 2)
        wait_until(first.stopped.is_set)
        first.log_in("old")
        assert sdk.connection_state == "connecting"
        second = get_client()
        second.log_in("new")
        assert sdk.diagnostics.get_connection_token() == "new"


class TestReconnecting:
    def test_reconnects_when_the_socket_ends_and_sends_every_subscription_again(self, create_sdk: CreateSdk) -> None:
        sdk, first, states = logged_in(create_sdk)
        sdk.subscribe_quote("ESZ26", Listener())
        sdk.subscribe_depth("NQZ26", Listener())
        flush(sdk)
        wait_until(lambda: symbols(first) == ["ESZ26", "NQZ26"])

        first.stop()
        wait_until(lambda: len(FakeClient.instances) == 2)
        second = get_client()
        assert sdk.diagnostics.get_connection_token() is None
        second.log_in("again")
        wait_until(lambda: symbols(second) == ["ESZ26", "NQZ26"])
        flush(sdk)
        assert states == ["connecting", "connected", "disconnected", "connected"]
        assert {request.token for request in second.subscriptions} == {"again"}

    def test_keeps_a_subscription_made_while_disconnected_for_the_next_login(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        sdk.login(CREDENTIALS)
        client = get_client()
        sdk.subscribe_quote("ESZ26", Listener())
        flush(sdk)
        assert client.subscriptions == []
        client.log_in()
        wait_until(lambda: symbols(client) == ["ESZ26"])

    def test_keeps_market_state_across_a_reconnect_and_clears_it_at_logout(self, create_sdk: CreateSdk) -> None:
        sdk, first, _ = logged_in(create_sdk)
        handle = sdk.subscribe_quote("ESZ26", Listener())
        flush(sdk)
        first.receive(subscriptionResponse={"symbol": "ESZ26", "subscriptionType": "ALL", "marketId": 42, "status": {"result": "SUCCESS"}})
        first.receive(instrumentDefinition=definition())
        first.receive(marketSnapshot={"marketId": 42})
        first.stop()
        wait_until(lambda: len(FakeClient.instances) == 2)
        assert sdk.get_quote("ESZ26") is not None
        assert [handle.state] == ["active"]

        sdk.logout()
        assert sdk.get_quote("ESZ26") is None
        assert [handle.state] == ["pending"]

    def test_drops_a_silent_connection(self, create_sdk: CreateSdk, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(connection_module, "CONNECTION_TIMEOUT_SECONDS", 0.05)
        sdk, first, _ = logged_in(create_sdk)
        wait_until(first.stopped.is_set)
        wait_until(lambda: len(FakeClient.instances) == 2)
        assert sdk.connection_state == "disconnected"

    def test_keeps_a_connection_that_only_sends_heartbeats(self, create_sdk: CreateSdk, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(connection_module, "CONNECTION_TIMEOUT_SECONDS", 0.2)
        sdk, client, _ = logged_in(create_sdk)
        times: list[Any] = []
        sdk.on_heartbeat(lambda server_time, _: times.append(server_time))
        stop = threading.Event()
        while len(times) < 20 and not stop.wait(0.02):
            client.receive(heartBeat={"transactionTime": 1793280675250999999})
        assert not client.stopped.is_set()
        assert len(FakeClient.instances) == 1
        assert times[0].year == 2026

    def test_tries_again_after_a_send_fails(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        client.stopped.set()
        sdk.subscribe_quote("ESZ26", Listener())
        flush(sdk)
        wait_until(lambda: len(FakeClient.instances) == 2)


def answer(client: FakeClient, request: api.SubscriptionRequest, result: str = "SUCCESS") -> None:
    """The server's answer to a subscription, with its correlation id, as the server echoes it."""
    client.receive(
        subscriptionResponse={
            "correlationId": request.correlationId,
            "symbol": request.requests[0].symbol,
            "subscriptionType": request.requests[0].subscriptionType[0],
            "status": {"result": result},
        }
    )


def unsubscribes(client: FakeClient) -> list[api.SubscriptionRequest]:
    return [request for request in client.subscriptions if request.unsubscribe]


def send_after(sdk: DesktopSdk, client: FakeClient, symbol: str) -> None:
    """Subscribes to another symbol and waits for it on the wire: requests go out in order, so anything queued before
    it, such as an unsubscribe, is on the wire by then too.
    """
    sdk.subscribe_quote(symbol, Listener())
    flush(sdk)
    wait_until(lambda: symbol in symbols(client))


class TestWire:
    def test_sends_each_subscription_with_its_service_type_and_token(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        sdk.subscribe_cumulative_volume("ESZ26", Listener())
        flush(sdk)
        wait_until(lambda: len(client.subscriptions) == 2)
        sent = {(s.service, s.requests[0].subscriptionType[0], s.requests[0].symbol, s.token) for s in client.subscriptions}
        assert sent == {(proto.REAL_TIME, api.ALL, "ESZ26", "t"), (proto.REAL_TIME_SNAPSHOT, api.CUMULATIVE_VOLUME, "ESZ26", "t")}

    def test_unsubscribes_with_the_same_correlation_id(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        handle = sdk.subscribe_depth("ESZ26", Listener())
        flush(sdk)
        wait_until(lambda: client.subscriptions)
        answer(client, client.subscriptions[0])
        handle.close()
        flush(sdk)
        wait_until(lambda: len(client.subscriptions) == 2)
        [sub, unsub] = client.subscriptions
        assert (sub.unsubscribe, unsub.unsubscribe) == (False, True)
        assert sub.correlationId == unsub.correlationId
        assert unsub.requests[0].symbol == "ESZ26"
        assert unsub.token == "t"

    @pytest.mark.parametrize("result", ["SUCCESS", "INVALID_SYMBOL"])
    def test_unsubscribes_only_once_the_server_has_answered_the_subscription(self, create_sdk: CreateSdk, result: str) -> None:
        sdk, client, _ = logged_in(create_sdk)
        handle = sdk.subscribe_depth("ESZ26", Listener())
        flush(sdk)
        wait_until(lambda: client.subscriptions)
        [subscription] = client.subscriptions
        handle.close()
        flush(sdk)

        send_after(sdk, client, "NQZ26")
        assert unsubscribes(client) == []

        # any answer, a refusal too
        answer(client, subscription, result)
        wait_until(lambda: unsubscribes(client))
        [unsubscribe] = unsubscribes(client)
        assert unsubscribe.correlationId == subscription.correlationId

    def test_never_unsubscribes_what_wasnt_answered_before_the_connection_dropped(self, create_sdk: CreateSdk) -> None:
        sdk, first, _ = logged_in(create_sdk)
        handle = sdk.subscribe_depth("ESZ26", Listener())
        flush(sdk)
        wait_until(lambda: first.subscriptions)
        handle.close()
        flush(sdk)
        send_after(sdk, first, "YMZ26")
        assert unsubscribes(first) == []

        first.stop()
        wait_until(lambda: len(FakeClient.instances) == 2)
        second = get_client()
        second.log_in("again")
        send_after(sdk, second, "NQZ26")
        # the next login sends what's still subscribed, which ESZ26 isn't
        assert symbols(second) == ["YMZ26", "NQZ26"]
        assert unsubscribes(first) == unsubscribes(second) == []

    def test_raw_send_fills_in_the_token_and_a_correlation_id_and_leaves_the_request_alone(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        request = api.ExchangeRequest()
        correlation_id = sdk.raw.send(request)
        assert correlation_id is not None
        assert client.sent[-1].exchangeRequest.correlationId == correlation_id
        assert client.sent[-1].exchangeRequest.token == "t"
        assert request.token == ""
        assert sdk.raw.send(request, 77) == 77
        assert client.sent[-1].exchangeRequest.correlationId == 77

    @pytest.mark.parametrize("request_", [api.LoginRequest(), api.OpenfeedGatewayRequest(exchangeRequest={})], ids=["login", "wrapped"])
    @pytest.mark.parametrize("connected", [True, False])
    def test_raw_send_refuses_any_other_kind_connected_or_not(self, create_sdk: CreateSdk, request_: Any, *, connected: bool) -> None:
        sdk = logged_in(create_sdk)[0] if connected else create_sdk()
        # what a type checker refuses, from an untyped caller
        with pytest.raises(TypeError, match=r"Expected .*, not (LoginRequest|OpenfeedGatewayRequest)"):
            sdk.raw.send(request_)

    def test_raw_send_returns_none_when_the_socket_has_gone(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        client.stopped.set()
        assert sdk.raw.send(api.ExchangeRequest()) is None


class TestLookups:
    def request(self, sdk: DesktopSdk) -> Any:
        return sdk.raw.lookup_instruments(api.InstrumentRequest(symbol="ZC*1"))

    def test_asks_for_definitions_in_the_responses_and_collects_them_all(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        lookup = self.request(sdk)
        wait_until(lambda: client.sent)
        sent = client.sent[-1].instrumentRequest
        assert [sent.symbol, sent.version, sent.token] == ["ZC*1", 1, "t"]

        fields = {"correlationId": sent.correlationId, "status": {"result": "SUCCESS"}, "numberOfDefinitions": 2}
        client.receive(instrumentResponse={**fields, "instrumentDefinition": definition(symbol="ZCZ26")})
        flush(sdk)
        assert not lookup.done()
        client.receive(instrumentResponse={**fields, "instrumentDefinition": definition(symbol="ZCH27")})
        assert [d.symbol for d in lookup.result(5)] == ["ZCZ26", "ZCH27"]

    def test_waits_for_the_connection(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        lookup = self.request(sdk)
        sdk.login(CREDENTIALS)
        client = get_client()
        assert not lookup.done()
        client.log_in()
        wait_until(lambda: client.sent)
        client.receive(instrumentResponse={"correlationId": client.sent[-1].instrumentRequest.correlationId, "status": {"result": "SUCCESS"}})
        assert lookup.result(5) == []

    def test_fails_when_the_server_finds_nothing_rather_than_waiting_forever(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        lookup = self.request(sdk)
        wait_until(lambda: client.sent)
        client.receive(
            instrumentResponse={
                "correlationId": client.sent[-1].instrumentRequest.correlationId,
                "status": {"result": "INSTRUMENTS_NOT_FOUND", "message": "no"},
            }
        )
        with pytest.raises(InstrumentLookupError, match="INSTRUMENTS_NOT_FOUND") as raised:
            lookup.result(5)
        assert raised.value.result == api.INSTRUMENTS_NOT_FOUND
        assert raised.value.server_message == "no"

    def test_fails_when_the_connection_goes_first(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        lookup = self.request(sdk)
        wait_until(lambda: client.sent)
        client.stop()
        with pytest.raises(ConnectionError):
            lookup.result(5)

    def test_fails_on_dispose_while_waiting_for_the_connection(self, create_sdk: CreateSdk) -> None:
        sdk = create_sdk()
        lookup = self.request(sdk)
        sdk.dispose()
        with pytest.raises(RuntimeError, match="disposed"):
            lookup.result(5)

    def test_a_lookup_its_holder_cancelled_changes_nothing_when_its_answer_comes(self, create_sdk: CreateSdk) -> None:
        logger, records = recording_logger()
        sdk = create_sdk(logger=logger)
        sdk.login(CREDENTIALS)
        client = get_client()
        client.log_in()
        wait_until(lambda: sdk.connection_state == "connected")
        lookup = self.request(sdk)
        wait_until(lambda: client.sent)
        assert lookup.cancel()
        client.receive(instrumentResponse={"correlationId": client.sent[-1].instrumentRequest.correlationId, "status": {"result": "SUCCESS"}})
        flush(sdk)
        assert [record for record in records if record.levelno >= logging.ERROR] == []

    def test_settles_on_the_delivery_thread(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        threads: list[int] = []
        delivery: list[int] = []
        sdk.subscribe_quote("ESZ26", Listener(on_event=lambda _: None))
        sdk._schedule(lambda: delivery.append(threading.get_ident()))
        flush(sdk)
        lookup = self.request(sdk)
        lookup.add_done_callback(lambda _: threads.append(threading.get_ident()))
        wait_until(lambda: client.sent)
        client.receive(instrumentResponse={"correlationId": client.sent[-1].instrumentRequest.correlationId, "status": {"result": "SUCCESS"}})
        lookup.result(5)
        flush(sdk)
        assert threads == delivery


class TestMetadata:
    def test_gives_raw_listeners_the_symbols_and_the_definition(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        got: list[Any] = []
        sdk.raw.subscribe("ESZ26", api.ALL, RawListener(on_message=lambda message, metadata: got.append((message.WhichOneof("data"), metadata))))
        flush(sdk)
        wait_until(lambda: client.subscriptions)
        correlation_id = client.subscriptions[0].correlationId
        client.receive(
            subscriptionResponse={
                "correlationId": correlation_id,
                "symbol": "ESZ26",
                "subscriptionType": "ALL",
                "marketId": 42,
                "status": {"result": "SUCCESS"},
            }
        )
        client.receive(instrumentDefinition=definition())
        client.receive(marketSnapshot={"marketId": 42})
        flush(sdk)

        kinds = [kind for kind, _ in got]
        assert kinds == ["subscriptionResponse", "instrumentDefinition", "marketSnapshot"]
        assert got[0][1].symbol_names == ("ESZ26",)
        # a definition's own message reports what was known before it
        assert got[1][1].definition is None
        assert got[2][1].symbol_names == ("ESZ26",)
        assert got[2][1].definition.symbol == "ESZ26"

    def test_starts_over_on_each_connection(self, create_sdk: CreateSdk) -> None:
        sdk, first, _ = logged_in(create_sdk)
        got: list[Any] = []
        sdk.raw.on_message(lambda _, metadata: got.append(metadata))
        first.receive(instrumentDefinition=definition())
        first.stop()
        wait_until(lambda: len(FakeClient.instances) == 2)
        second = get_client()
        second.log_in()
        second.receive(marketSnapshot={"marketId": 42})
        flush(sdk)
        assert got[-1].definition is None

    def test_follows_an_alias_to_its_new_instrument_and_an_exchange_move(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        got: list[Any] = []
        sdk.raw.on_message(lambda _, metadata: got.append(metadata))
        client.receive(
            subscriptionResponse={"correlationId": 1, "symbol": "ZC*1", "subscriptionType": "ALL", "marketId": 1, "status": {"result": "SUCCESS"}}
        )
        client.receive(
            subscriptionResponse={"correlationId": 2, "symbol": "ZCZ26", "subscriptionType": "ALL", "marketId": 1, "status": {"result": "SUCCESS"}}
        )
        client.receive(
            instrumentAction={"action": "ALIAS_CHANGED", "oldAlias": "ZC*1", "instrument": {"marketId": 1}, "newInstrument": {"marketId": 2}}
        )
        client.receive(marketSnapshot={"marketId": 1})
        client.receive(instrumentAction={"action": "EXCHANGE_MOVE", "instrument": {"marketId": 1}, "newInstrument": {"marketId": 3}})
        client.receive(marketSnapshot={"marketId": 3})
        flush(sdk)
        assert got[3].symbol_names == ("ZCZ26",)
        assert got[-1].symbol_names == ("ZCZ26",)


class TestInstrumentActions:
    def test_an_alias_roll_moves_the_quote_to_the_new_instrument(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        got: list[QuoteEvent] = []
        sdk.subscribe_quote("ZC*1", Listener(on_event=got.append))
        flush(sdk)

        def name(market_id: int) -> None:
            client.receive(subscriptionResponse={"symbol": "ZC*1", "subscriptionType": "ALL", "marketId": market_id, "status": {"result": "SUCCESS"}})
            client.receive(instrumentDefinition=definition(market_id=market_id, symbol=f"ZC{market_id}"))
            client.receive(marketSnapshot={"marketId": market_id})

        name(1)
        instrument = sdk.get_instrument("ZC*1")
        assert instrument is not None
        assert (instrument.symbol, instrument.definition.symbol) == ("ZC*1", "ZC1")

        client.receive(instrumentAction={"action": "ALIAS_CHANGED", "oldAlias": "ZC*1", "instrument": {"marketId": 1}})
        assert sdk.get_quote("ZC*1") is None
        name(2)
        instrument = sdk.get_instrument("ZC*1")
        assert instrument is not None
        assert (instrument.symbol, instrument.definition.symbol) == ("ZC*1", "ZC2")
        flush(sdk)
        assert [event.instrument.definition.symbol for event in got] == ["ZC1", "ZC2"]

    def test_an_exchange_move_keeps_the_state_under_the_new_market(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        got: list[QuoteEvent] = []
        sdk.subscribe_quote("ESZ26", Listener(on_event=got.append))
        flush(sdk)
        client.receive(subscriptionResponse={"symbol": "ESZ26", "subscriptionType": "ALL", "marketId": 42, "status": {"result": "SUCCESS"}})
        client.receive(instrumentDefinition=definition())
        client.receive(marketSnapshot={"marketId": 42})
        client.receive(instrumentAction={"action": "EXCHANGE_MOVE", "instrument": {"marketId": 42}, "newInstrument": {"marketId": 43}})
        client.receive(marketUpdate={"marketId": 43, "symbol": "ESZ26", "bbo": {"bidPrice": 10000, "offerPrice": 10025}})
        flush(sdk)
        quote = sdk.get_quote("ESZ26")
        assert quote is not None
        assert quote.best_bid_offer.bid_price == 100
        assert [event.kind for event in got] == ["snapshot", "update"]


def test_a_problem_handling_a_message_is_logged_and_the_next_still_arrives(create_sdk: CreateSdk, monkeypatch: pytest.MonkeyPatch) -> None:
    logger, records = recording_logger()
    sdk = create_sdk(logger=logger)
    sdk.login(CREDENTIALS)
    client = get_client()
    client.log_in()
    sdk.subscribe_quote("ESZ26", Listener())
    flush(sdk)
    client.receive(subscriptionResponse={"symbol": "ESZ26", "subscriptionType": "ALL", "marketId": 42, "status": {"result": "SUCCESS"}})
    original = sdk._store.apply
    calls = [0]

    def apply(message: Any) -> Any:
        calls[0] += 1
        if calls[0] == 1:
            problem = "boom"
            raise ValueError(problem)
        return original(message)

    monkeypatch.setattr(sdk._store, "apply", apply)
    client.receive(instrumentDefinition=definition())
    client.receive(instrumentDefinition=definition())
    assert calls == [2]
    assert sdk.get_instrument("ESZ26") is not None
    assert [record for record in records if "Handling a message failed" in record.getMessage()]


class TestInstrumentSymbols:
    def test_an_instrument_is_named_by_the_symbol_it_s_reached_by_with_one_copy_per_name_until_the_next_definition(
        self, create_sdk: CreateSdk
    ) -> None:
        sdk, client, _ = logged_in(create_sdk)
        got: list[QuoteEvent] = []
        for symbol in ("ESZ26", "ES*1"):
            sdk.subscribe_quote(symbol, Listener(on_event=got.append))
        flush(sdk)
        for symbol in ("ESZ26", "ES*1"):
            client.receive(subscriptionResponse={"symbol": symbol, "subscriptionType": "ALL", "marketId": 42, "status": {"result": "SUCCESS"}})
        client.receive(instrumentDefinition=definition(symbol="ESZ6"))
        client.receive(marketSnapshot={"marketId": 42})
        client.receive(marketUpdate={"marketId": 42, "symbol": "ESZ6", "bbo": {"bidPrice": 10000, "offerPrice": 10025}})
        flush(sdk)

        assert [(event.symbol, event.instrument.symbol) for event in got] == [("ESZ26", "ESZ26"), ("ES*1", "ES*1")] * 2
        alias = sdk.get_instrument("ES*1")
        assert alias is not None
        assert got[1].instrument is got[3].instrument is alias
        assert alias.definition.symbol == "ESZ6"
        # asked for by the definition's own symbol, it's named by that
        own = sdk.get_instrument("ESZ6")
        assert own is not None
        assert own.symbol == "ESZ6"

        client.receive(instrumentDefinition=definition(symbol="ESZ6", book_depth=3))
        redefined = sdk.get_instrument("ES*1")
        assert redefined is not None
        assert redefined is not alias
        assert (redefined.symbol, redefined.book_depth) == ("ES*1", 3)


class TestNamedMarkets:
    def test_a_market_none_names_keeps_no_state_and_its_messages_reach_the_raw_listeners_only(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        raw: list[str | None] = []
        sdk.raw.on_message(lambda message, _: raw.append(message.WhichOneof("data")))
        client.receive(instrumentDefinition=definition(market_id=7, symbol="ZCZ6"))
        client.receive(marketSnapshot={"marketId": 7})
        flush(sdk)
        assert raw == ["instrumentDefinition", "marketSnapshot"]
        assert (sdk.get_instrument("ZCZ6"), sdk.get_quote("ZCZ6")) == (None, None)

    def test_a_root_subscription_s_instruments_are_found_by_their_definition_s_symbol_while_it_s_open(self, create_sdk: CreateSdk) -> None:
        sdk, client, _ = logged_in(create_sdk)
        removed: list[str] = []
        sdk.on_removed(removed.append)
        root = sdk.raw.subscribe("ZC^UDS", api.ALL, RawListener(on_message=lambda *_: None))
        flush(sdk)
        client.receive(subscriptionResponse={"symbol": "ZC^UDS", "subscriptionType": "ALL", "marketId": 7, "status": {"result": "SUCCESS"}})
        client.receive(instrumentDefinition=definition(market_id=7, symbol="ZCZ6"))
        client.receive(marketSnapshot={"marketId": 7})
        instrument = sdk.get_instrument("ZCZ6")
        assert instrument is not None
        assert instrument.symbol == "ZCZ6"
        assert sdk.get_quote("ZCZ6") is not None
        assert sdk.is_subscribed("ZCZ6")

        root.close()
        flush(sdk)
        assert sorted(removed) == ["ZCZ6", "ZC^UDS"]
        assert (sdk.is_subscribed("ZCZ6"), sdk.get_quote("ZCZ6")) == (False, None)

        # one still on its way after the close doesn't bring the market back
        client.receive(instrumentDefinition=definition(market_id=7, symbol="ZCZ6"))
        assert sdk.get_instrument("ZCZ6") is None


class TestReconnectBackoff:
    def test_waits_longer_after_each_failed_attempt_and_starts_over_once_logged_in(
        self, create_sdk: CreateSdk, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the real delays, from the shortest waits, recorded rather than waited for
        monkeypatch.setattr(connection_module, "RECONNECT_SECONDS", 5.0)
        monkeypatch.setattr(random, "random", lambda: 0.0)
        delays: list[float] = []

        class Recording(connection_module.ReconnectBackoff):
            def next_delay(self) -> float:
                delays.append(super().next_delay())
                return 0.0

        monkeypatch.setattr(connection_module, "ReconnectBackoff", Recording)
        sdk = create_sdk()
        sdk.login(CREDENTIALS)
        failed = {"status": {"result": "UNKNOWN_RESULT"}}
        get_client(0).receive(loginResponse=failed)
        wait_until(lambda: len(FakeClient.instances) == 2)
        get_client(1).receive(loginResponse=failed)
        wait_until(lambda: len(FakeClient.instances) == 3)
        assert delays == [5.0, 10.0]

        get_client(2).log_in()
        get_client(2).stop()
        wait_until(lambda: len(FakeClient.instances) == 4)
        assert delays == [5.0, 10.0, 5.0]

    @staticmethod
    def delays(backoff: connection_module.ReconnectBackoff, count: int) -> list[float]:
        return [backoff.next_delay() for _ in range(count)]

    def test_doubles_the_delay_up_to_the_maximum_waiting_in_its_upper_half(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backoff = connection_module.ReconnectBackoff()

        monkeypatch.setattr(random, "random", lambda: 0.0)
        assert self.delays(backoff, 5) == [5.0, 10.0, 20.0, 30.0, 30.0]

        backoff.reset()
        monkeypatch.setattr(random, "random", lambda: 0.999_999)
        assert self.delays(backoff, 5) == pytest.approx([10.0, 20.0, 40.0, 60.0, 60.0], abs=1e-3)

    def test_never_goes_past_the_maximum(self) -> None:
        backoff = connection_module.ReconnectBackoff()
        for delay in self.delays(backoff, 100):
            assert connection_module.RECONNECT_SECONDS <= delay < connection_module.RECONNECT_MAX_SECONDS

    def test_starts_again_from_the_shortest_wait_once_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backoff = connection_module.ReconnectBackoff()
        monkeypatch.setattr(random, "random", lambda: 0.0)
        self.delays(backoff, 4)

        backoff.reset()
        assert backoff.next_delay() == connection_module.RECONNECT_SECONDS
