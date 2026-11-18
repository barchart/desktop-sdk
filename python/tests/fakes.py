"""The Openfeed client replaced by one that keeps its callbacks, so tests can feed messages to the SDK from their own thread,
which stands in for the socket thread, and records what it's asked to send.
"""

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from helpers import message, quiet
from openfeed.generated import openfeed_api_pb2 as api

from barchart_desktop_sdk import Credentials, DesktopSdk
from barchart_desktop_sdk import _connection as connection_module

WAIT_SECONDS = 5.0
CREDENTIALS = Credentials(url="ws://test", username="u", password="p")


class FakeClient:
    instances: list["FakeClient"] = []  # noqa: RUF012

    def __init__(self, username: str, password: str, server: str = "") -> None:
        self.username, self.password, self.server = username, password, server
        self.on_message: Callable[[Any], None] = lambda _: None
        self.on_connected: Callable[[Any], None] = lambda _: None
        self.on_error: Callable[[Any], None] = lambda _: None
        self.sent: list[api.OpenfeedGatewayRequest] = []
        self.stopped = threading.Event()
        FakeClient.instances.append(self)

    def start(self, *, blocking: bool) -> None:
        assert blocking
        self.stopped.wait()

    def stop(self) -> None:
        self.stopped.set()

    def send(self, request: api.OpenfeedGatewayRequest) -> None:
        if self.stopped.is_set():
            raise ConnectionError
        self.sent.append(request)

    def receive(self, **fields: Any) -> None:
        self.on_message(message(**fields))

    def log_in(self, token: str = "t") -> None:
        self.receive(loginResponse={"token": token, "status": {"result": "SUCCESS"}})

    @property
    def subscriptions(self) -> list[api.SubscriptionRequest]:
        return [request.subscriptionRequest for request in self.sent if request.WhichOneof("data") == "subscriptionRequest"]


def wait_until(condition: Callable[[], Any]) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.001)


def flush(sdk: DesktopSdk) -> None:
    """Returns once the delivery thread has run everything queued so far, subscribes and closes first."""
    done = threading.Event()
    sdk._schedule(done.set)
    assert done.wait(WAIT_SECONDS)


def get_client(index: int = -1) -> FakeClient:
    wait_until(lambda: FakeClient.instances)
    return FakeClient.instances[index]


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeClient.instances = []
    monkeypatch.setattr(connection_module, "OpenfeedClient", FakeClient)
    monkeypatch.setattr(connection_module, "RECONNECT_SECONDS", 0.0)
    yield
    for client in FakeClient.instances:
        client.stop()


@pytest.fixture
def create_sdk(fake_client: None) -> Iterator[Callable[..., DesktopSdk]]:  # noqa: ARG001
    """Every SDK a test creates is disposed after it."""
    created: list[DesktopSdk] = []

    def create(**options: Any) -> DesktopSdk:
        sdk = DesktopSdk(logger=options.pop("logger", quiet), **options)
        created.append(sdk)
        return sdk

    yield create
    for sdk in created:
        sdk.dispose()
