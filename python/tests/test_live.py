"""The SDK on the real Openfeed client and websocket-client, against a real local socket speaking just enough of the protocol.

Mypy can't check the SDK's calls into the client, which has no annotations, so these do. Real Openfeed behavior isn't tested
here: the server below only does what these tests tell it to.
"""

import base64
import hashlib
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fakes import flush, wait_until
from helpers import definition, level, quiet
from openfeed.generated import openfeed_api_pb2 as api

from barchart_desktop_sdk import BookEvent, Credentials, DesktopSdk, Listener, QuoteEvent
from barchart_desktop_sdk import _connection as connection_module

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_CLOSE_OPCODE = 8


def _read_exactly(connection: socket.socket, length: int) -> bytes:
    data = b""
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise ConnectionError
        data += chunk
    return data


@dataclass(eq=False)
class ServerConnection:
    """One client's socket on the server's side."""

    socket: socket.socket
    requests: list[api.OpenfeedGatewayRequest] = field(default_factory=list)
    ended: threading.Event = field(default_factory=threading.Event)

    def send(self, **fields: Any) -> None:
        body = api.OpenfeedGatewayMessage(**fields).SerializeToString()
        payload = struct.pack(">h", len(body)) + body
        length = bytes([len(payload)]) if len(payload) < 126 else bytes([126]) + struct.pack(">H", len(payload))
        self.socket.sendall(bytes([0x82]) + length + payload)

    def read_frame(self) -> tuple[int, bytes]:
        head = _read_exactly(self.socket, 2)
        length = head[1] & 127
        if length == 126:
            length = struct.unpack(">H", _read_exactly(self.socket, 2))[0]
        mask = _read_exactly(self.socket, 4)
        data = _read_exactly(self.socket, length)
        return head[0] & 15, bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))

    def subscriptions(self) -> list[api.SubscriptionRequest]:
        return [request.subscriptionRequest for request in self.requests if request.WhichOneof("data") == "subscriptionRequest"]


class Server:
    """Answers every login with `login_response`, and keeps what it's sent."""

    def __init__(self, login_response: dict[str, Any]) -> None:
        self.login_response = login_response
        self.connections: list[ServerConnection] = []
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen()
        self.url = f"ws://127.0.0.1:{self._socket.getsockname()[1]}/ws"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        peer = ServerConnection(connection)
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                data += connection.recv(4096)
            key = next(line.split(b": ")[1] for line in data.split(b"\r\n") if line.lower().startswith(b"sec-websocket-key"))
            accept = base64.b64encode(hashlib.sha1(key + _GUID).digest())  # noqa: S324
            connection.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            self.connections.append(peer)
            while True:
                opcode, payload = peer.read_frame()
                if opcode == _CLOSE_OPCODE:
                    connection.sendall(bytes([0x88, 0]))
                    return
                request = api.OpenfeedGatewayRequest()
                request.ParseFromString(payload)
                peer.requests.append(request)
                if request.WhichOneof("data") == "loginRequest":
                    peer.send(loginResponse=self.login_response)
        except OSError:
            pass
        finally:
            peer.ended.set()
            connection.close()

    def close(self) -> None:
        self._socket.close()
        for peer in self.connections:
            peer.socket.close()


SUCCESS = {"token": "live-token", "status": {"result": "SUCCESS"}}


@pytest.fixture
def start() -> Iterator[Callable[..., tuple[DesktopSdk, Server]]]:
    servers: list[Server] = []
    sdks: list[DesktopSdk] = []

    def create(login_response: dict[str, Any] = SUCCESS, **options: Any) -> tuple[DesktopSdk, Server]:
        server = Server(login_response)
        sdk = DesktopSdk(logger=quiet, **options)
        servers.append(server)
        sdks.append(sdk)
        return sdk, server

    yield create
    for sdk in sdks:
        sdk.dispose()
    for server in servers:
        server.close()


def credentials(server: Server) -> Credentials:
    return Credentials(url=server.url, username="user", password="secret")


def test_logs_in_subscribes_and_gets_quotes_and_a_book(start: Callable[..., tuple[DesktopSdk, Server]]) -> None:
    sdk, server = start()
    quotes: list[QuoteEvent] = []
    books: list[BookEvent] = []
    sdk.subscribe_quote("ESZ26", Listener(on_event=quotes.append))
    sdk.subscribe_depth("ESZ26", Listener(on_event=books.append))
    sdk.login(credentials(server))
    sdk.when_connected().result(5)
    assert sdk.diagnostics.get_connection_token() == "live-token"

    peer = server.connections[0]
    login = peer.requests[0].loginRequest
    assert [login.username, login.password] == ["user", "secret"]
    wait_until(lambda: len(peer.subscriptions()) == 2)
    assert {request.token for request in peer.subscriptions()} == {"live-token"}

    for subscription_type in ("ALL", "DEPTH_PRICE"):
        peer.send(subscriptionResponse={"symbol": "ESZ26", "subscriptionType": subscription_type, "marketId": 42, "status": {"result": "SUCCESS"}})
    peer.send(instrumentDefinition=definition(book_depth=2))
    peer.send(marketSnapshot={"marketId": 42, "priceLevels": [level("BID", 1, 10000, 5)]})
    peer.send(marketUpdate={"marketId": 42, "symbol": "ESZ26", "depthPriceLevel": {"levels": [{"addPriceLevel": level("BID", 1, 10025, 7)}]}})
    # types the client has no handler for, and a market it has no definition of, change nothing here
    peer.send(volumeAtPrice={"marketId": 42})
    peer.send(marketUpdate={"marketId": 99, "symbol": "NQZ26", "bbo": {"bidPrice": 1}})
    peer.send(marketUpdate={"marketId": 42, "symbol": "ESZ26", "bbo": {"bidPrice": 10050, "offerPrice": 10075}})

    wait_until(lambda: len(quotes) == 2 and len(books) == 2)
    assert [quote.kind for quote in quotes] == ["snapshot", "update"]
    assert quotes[-1].quote.best_bid_offer.bid_price == 100.5
    assert [level_.price for level_ in books[-1].book.bids] == [100.25, 100]
    assert sdk.get_book("ESZ26") is books[-1].book


def test_prints_nothing_for_messages_the_client_doesnt_handle(
    start: Callable[..., tuple[DesktopSdk, Server]], capsys: pytest.CaptureFixture[str]
) -> None:
    sdk, server = start()
    seen: list[Any] = []
    sdk.raw.on_message(lambda message, _: seen.append(message.WhichOneof("data")))
    sdk.login(credentials(server))
    sdk.when_connected().result(5)
    peer = server.connections[0]
    peer.send(volumeAtPrice={"marketId": 42})
    peer.send(subscriptionResponse={"symbol": "ESZ26", "subscriptionType": "ALL", "status": {"result": "INVALID_SYMBOL"}})
    wait_until(lambda: len(seen) == 2)
    assert capsys.readouterr().out == ""


def test_stops_after_rejected_credentials_and_closes_the_socket(start: Callable[..., tuple[DesktopSdk, Server]]) -> None:
    sdk, server = start({"status": {"result": "INVALID_CREDENTIALS"}})
    connected = sdk.when_connected()
    sdk.login(credentials(server))
    with pytest.raises(RuntimeError, match="rejected the credentials"):
        connected.result(5)
    assert sdk.connection_state == "credentials-rejected"
    wait_until(server.connections[0].ended.is_set)
    assert len(server.connections) == 1


def test_reconnects_when_the_server_drops_it_and_subscribes_again(
    start: Callable[..., tuple[DesktopSdk, Server]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection_module, "RECONNECT_SECONDS", 0.0)
    sdk, server = start()
    states: list[str] = []
    sdk.on_connection_state(states.append)
    sdk.subscribe_quote("ESZ26", Listener())
    sdk.login(credentials(server))
    sdk.when_connected().result(5)
    first = server.connections[0]
    wait_until(first.subscriptions)

    first.socket.shutdown(socket.SHUT_RDWR)
    wait_until(lambda: len(server.connections) == 2 and server.connections[1].subscriptions())
    wait_until(lambda: sdk.connection_state == "connected")
    flush(sdk)
    assert states == ["connecting", "connected", "disconnected", "connected"]


def test_drops_a_silent_server(start: Callable[..., tuple[DesktopSdk, Server]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "RECONNECT_SECONDS", 0.0)
    monkeypatch.setattr(connection_module, "CONNECTION_TIMEOUT_SECONDS", 0.2)
    sdk, server = start()
    sdk.login(credentials(server))
    sdk.when_connected().result(5)
    wait_until(server.connections[0].ended.is_set)
    wait_until(lambda: len(server.connections) == 2)


def _get_socket_threads() -> set[threading.Thread]:
    return {thread for thread in threading.enumerate() if thread.name == "barchart-desktop-sdk-socket"}


def _wait_for_connections(server: Server, count: int) -> None:
    wait_until(lambda: len(server.connections) >= count)


def _wait_for_all_ended(server: Server) -> None:
    wait_until(lambda: all(peer.ended.is_set() for peer in list(server.connections)))


def test_leaves_no_socket_thread_or_connection_behind_however_stopping_races_the_reader(
    start: Callable[..., tuple[DesktopSdk, Server]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that loses the race strands the socket thread, which holds up the next attempt: so many attempts, quickly."""
    monkeypatch.setattr(connection_module, "RECONNECT_SECONDS", 0.0)
    before = _get_socket_threads()
    for timeout, attempts in ((0.005, 150), (0.01, 80), (0.02, 40)):
        monkeypatch.setattr(connection_module, "CONNECTION_TIMEOUT_SECONDS", timeout)
        sdk, server = start()
        sdk.login(credentials(server))
        _wait_for_connections(server, attempts)
        sdk.dispose()
        _wait_for_all_ended(server)
    wait_until(lambda: not _get_socket_threads() - before)


def test_closes_a_connection_stopped_before_its_socket_thread_got_going(
    start: Callable[..., tuple[DesktopSdk, Server]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client's `start` clears an earlier `stop`, so this order has to be caught when the socket connects."""
    run_socket = connection_module.Connection._run_socket

    def run_socket_late(connection: connection_module.Connection, attempt: Any) -> None:
        wait_until(lambda: attempt.stopping)
        run_socket(connection, attempt)

    monkeypatch.setattr(connection_module.Connection, "_run_socket", run_socket_late)
    sdk, server = start()
    sdk.login(credentials(server))
    sdk.logout()
    _wait_for_connections(server, 1)
    wait_until(server.connections[0].ended.is_set)


def test_logout_closes_the_socket_before_and_after_login(start: Callable[..., tuple[DesktopSdk, Server]]) -> None:
    sdk, server = start()
    sdk.login(credentials(server))
    sdk.when_connected().result(5)
    sdk.logout()
    assert sdk.connection_state == "logged-out"
    wait_until(server.connections[0].ended.is_set)


def test_a_lookup_and_a_raw_send_reach_the_server_with_the_token(start: Callable[..., tuple[DesktopSdk, Server]]) -> None:
    sdk, server = start()
    sdk.login(credentials(server))
    sdk.when_connected().result(5)
    peer = server.connections[0]

    correlation_id = sdk.raw.send(api.ExchangeRequest())
    wait_until(lambda: any(request.WhichOneof("data") == "exchangeRequest" for request in peer.requests))
    exchange = next(request.exchangeRequest for request in peer.requests if request.WhichOneof("data") == "exchangeRequest")
    assert [exchange.correlationId, exchange.token] == [correlation_id, "live-token"]

    lookup = sdk.raw.lookup_instruments(api.InstrumentRequest(symbol="ESZ26"))
    wait_until(lambda: any(request.WhichOneof("data") == "instrumentRequest" for request in peer.requests))
    sent = next(request.instrumentRequest for request in peer.requests if request.WhichOneof("data") == "instrumentRequest")
    peer.send(
        instrumentResponse={
            "correlationId": sent.correlationId,
            "status": {"result": "SUCCESS"},
            "numberOfDefinitions": 1,
            "instrumentDefinition": definition(),
        }
    )
    assert [found.symbol for found in lookup.result(5)] == ["ESZ26"]
