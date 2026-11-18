"""One login's connection, with what the Openfeed client doesn't do itself.

Reconnecting, sending every subscription again after each login, dropping a silent connection, unsubscribing, instrument
lookups and message metadata. The only module that touches `OpenfeedClient`, which it uses for one connection at a time: a
new one per attempt, so what the client keeps about instruments goes with its connection.

Two threads, and neither runs a listener of the application's:
- the connection thread decides when to connect and when to give up, and is the only sender of subscription requests;
- the socket thread runs the client, and hands each message on as it arrives.
"""

from __future__ import annotations

import contextlib
import itertools
import random
import threading
import time
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Never, TypeVar

import websocket
from openfeed.generated import openfeed_api_pb2, openfeed_instrument_pb2, openfeed_pb2
from openfeed.openfeed_client import OpenfeedClient

from ._metadata import MetadataTracker
from ._types import Credentials, InstrumentLookupError, MessageMetadata, SendableRequest

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

    from ._types import Service, SubscriptionType

_T = TypeVar("_T")

RECONNECT_SECONDS = 5.0
"""Reconnects back off exponentially from this, up to `RECONNECT_MAX_SECONDS` (`ReconnectBackoff`)."""
RECONNECT_MAX_SECONDS = 60.0
CONNECTION_TIMEOUT_SECONDS = 20.0
"""A connection silent for this long is dropped. Fixed: the backoff spreads reconnects out."""
STOP_WAIT_SECONDS = 5.0
"""How long to wait for a stopped client's socket thread. One stuck connecting is left behind: it stops itself if it connects."""

_REJECTED_LOGINS = (openfeed_api_pb2.AUTHENTICATION_REQUIRED, openfeed_api_pb2.INVALID_CREDENTIALS, openfeed_api_pb2.INSUFFICIENT_PRIVILEGES)

_NOT_CONNECTED = "The Desktop SDK is not connected"
_REQUEST_NOT_SENDABLE = "Expected an exchange, instrument, instrument reference, list subscriptions or subscription request"


def get_request_field(request: SendableRequest) -> str:
    """The gateway request's field a request goes in. A type checker refuses any other kind; a caller that doesn't use
    one gets a `TypeError`.
    """
    match request:
        case openfeed_api_pb2.ExchangeRequest():
            return "exchangeRequest"
        case openfeed_api_pb2.InstrumentRequest():
            return "instrumentRequest"
        case openfeed_api_pb2.InstrumentReferenceRequest():
            return "instrumentReferenceRequest"
        case openfeed_api_pb2.ListSubscriptionsRequest():
            return "listSubscriptionsRequest"
        case openfeed_api_pb2.SubscriptionRequest():
            return "subscriptionRequest"
        case _:
            # mypy fails here if SendableRequest has a kind without its case above
            _: Never = request
            message = f"{_REQUEST_NOT_SENDABLE}, not {type(request).__name__}"
            raise TypeError(message)


class ReconnectBackoff:
    """Exponential backoff with jitter for reconnects: each attempt doubles the delay, up to the maximum, and waits a
    random time in its upper half, so clients dropped together don't all come back at once.
    """

    def __init__(self) -> None:
        self._delay = RECONNECT_SECONDS

    def next_delay(self) -> float:
        """The next wait: 5-10s, then 10-20s, 20-40s, and 30-60s from then on, until reset."""
        self._delay = min(self._delay * 2, RECONNECT_MAX_SECONDS)
        half = self._delay / 2
        return half + random.random() * half  # noqa: S311

    def reset(self) -> None:
        """Called once logged in, so the next disconnect starts again from the shortest wait."""
        self._delay = RECONNECT_SECONDS


def settle_result(future: Future[_T], result: _T) -> None:
    """Sets a future's result, unless its holder cancelled it."""
    with contextlib.suppress(InvalidStateError):
        future.set_result(result)


def settle_exception(future: Future[_T], error: BaseException) -> None:
    """Sets a future's exception, unless its holder cancelled it."""
    with contextlib.suppress(InvalidStateError):
        future.set_exception(error)


@dataclass(frozen=True, slots=True)
class ConnectionListeners:
    """What a connection reports, called on its own threads with no lock of its own held. Each is given the connection,
    so the SDK can ignore one it has moved on from.
    """

    on_connected: Callable[[Connection], None]
    on_disconnected: Callable[[Connection], None]
    on_rejected: Callable[[Connection], None]
    on_heartbeat: Callable[[Connection, openfeed_pb2.HeartBeat], None]
    on_message: Callable[[Connection, openfeed_api_pb2.OpenfeedGatewayMessage, MessageMetadata], None]
    schedule: Callable[[Callable[[], None]], None]
    """Runs a call where the application's callbacks run: lookups' results are settled there."""


@dataclass(eq=False, slots=True)
class _Attempt:
    """One client, from connecting until its socket thread ends. Flags are set under the connection's lock."""

    client: OpenfeedClient
    metadata: MetadataTracker = field(default_factory=MetadataTracker)
    logged_in: bool = False
    rejected: bool = False
    stopping: bool = False
    ended: bool = False


@dataclass(eq=False, slots=True)
class _Lookup:
    future: Future[list[openfeed_instrument_pb2.InstrumentDefinition]]
    definitions: list[openfeed_instrument_pb2.InstrumentDefinition] = field(default_factory=list)


class Connection:
    """Safe to call from any thread. `subscribe` and `unsubscribe` only queue, so they never wait on the network."""

    def __init__(self, credentials: Credentials, listeners: ConnectionListeners, log: logging.Logger) -> None:
        self._credentials = credentials
        self._listeners = listeners
        self._log = log
        self._lock = threading.Condition(threading.Lock())
        self._ids = itertools.count(1)
        self._closed = False
        self._attempt: _Attempt | None = None
        self._token: str | None = None
        self._last_message = 0.0
        # every open subscription, sent again after each login
        self._subscriptions: dict[int, openfeed_api_pb2.SubscriptionRequest] = {}
        self._outbox: list[openfeed_api_pb2.SubscriptionRequest] = []
        # subscriptions sent on this login whose answer hasn't come, and the unsubscribes waiting for those answers: a
        # subscription is unsubscribed once the server has answered it, so the unsubscribe can't overtake it
        self._unanswered: set[int] = set()
        self._closing: dict[int, openfeed_api_pb2.SubscriptionRequest] = {}
        self._lookups: dict[int, _Lookup] = {}
        self._backoff = ReconnectBackoff()

    @property
    def token(self) -> str | None:
        """The gateway's session token; None while not logged in."""
        with self._lock:
            return self._token

    def start(self) -> None:
        threading.Thread(target=self._run, name="barchart-desktop-sdk-connection", daemon=True).start()

    def close(self) -> None:
        """Stops the connection, without waiting for it. Nothing is reported after this that the SDK needs to see."""
        with self._lock:
            self._closed = True
            self._lock.notify_all()

    def subscribe(self, service: Service, subscription_type: SubscriptionType, symbol: str) -> int:
        with self._lock:
            subscription_id = next(self._ids)
            request = openfeed_api_pb2.SubscriptionRequest(
                correlationId=subscription_id,
                service=service,
                token=self._token,
                requests=[openfeed_api_pb2.SubscriptionRequest.Request(symbol=symbol, subscriptionType=[subscription_type])],
            )
            self._subscriptions[subscription_id] = request
            if self._token is not None:
                self._outbox.append(request)
                self._lock.notify_all()
            return subscription_id

    def unsubscribe(self, subscription_id: int) -> None:
        """Forgets the subscription. One not sent yet never reaches the wire; one sent but not answered yet is unsubscribed
        once its answer comes.
        """
        with self._lock:
            request = self._subscriptions.pop(subscription_id)
            if self._token is None:
                return
            for queued in self._outbox:
                if queued is request:
                    self._outbox.remove(queued)
                    return
            unsubscribe = openfeed_api_pb2.SubscriptionRequest(
                correlationId=request.correlationId, token=self._token, service=request.service, unsubscribe=True, requests=request.requests
            )
            if request.correlationId in self._unanswered:
                self._closing[request.correlationId] = unsubscribe
                return
            self._outbox.append(unsubscribe)
            self._lock.notify_all()

    def send(self, request: SendableRequest, correlation_id: int | None = None) -> int | None:
        """Sends a request in a gateway request, with this connection's token. Returns its correlation id, or None when not
        connected.
        """
        kind = get_request_field(request)
        with self._lock:
            if self._token is None:
                return None
            token = self._token
            sent_id = next(self._ids) if correlation_id is None else correlation_id

        filled = openfeed_api_pb2.OpenfeedGatewayRequest()
        body = getattr(filled, kind)
        body.CopyFrom(request)
        body.correlationId = sent_id
        body.token = token
        return sent_id if self._send(filled) else None

    def get_instrument(self, request: openfeed_api_pb2.InstrumentRequest) -> Future[list[openfeed_instrument_pb2.InstrumentDefinition]]:
        """The gateway's instrument lookup. Fails with `InstrumentLookupError`, or `ConnectionError` if the connection goes first."""
        future: Future[list[openfeed_instrument_pb2.InstrumentDefinition]] = Future()
        with self._lock:
            token = self._token
            if token is None:
                future.set_exception(ConnectionError(_NOT_CONNECTED))
                return future
            correlation_id = next(self._ids)
            self._lookups[correlation_id] = _Lookup(future)

        filled = openfeed_api_pb2.InstrumentRequest()
        filled.CopyFrom(request)
        filled.correlationId = correlation_id
        filled.token = token
        # version 1 is what makes the gateway send the definitions in its responses
        filled.version = 1
        if not self._send(openfeed_api_pb2.OpenfeedGatewayRequest(instrumentRequest=filled)):
            with self._lock:
                self._lookups.pop(correlation_id, None)
            future.set_exception(ConnectionError(_NOT_CONNECTED))
        return future

    def _send(self, request: openfeed_api_pb2.OpenfeedGatewayRequest) -> bool:
        with self._lock:
            attempt = self._attempt
        if attempt is None:
            return False
        try:
            attempt.client.send(request)
        except (OSError, websocket.WebSocketException) as error:
            self._log.warning("OF SDK > Sending failed: %s", error)
            self._stop_attempt(attempt)
            return False
        return True

    # ---- the connection thread ----

    def _run(self) -> None:
        while True:
            attempt = self._run_attempt()
            if attempt.rejected:
                self._listeners.on_rejected(self)
                return
            with self._lock:
                delay = self._backoff.next_delay()
            self._log.info("OF SDK > Reconnecting in %.1fs", delay)
            with self._lock:
                if self._lock.wait_for(lambda: self._closed, timeout=delay):
                    return

    def _run_attempt(self) -> _Attempt:
        """One client, until it ends or is given up on. Returns once it's safe to start another."""
        credentials = self._credentials
        attempt = _Attempt(client=OpenfeedClient(credentials.username, credentials.password, server=credentials.url))
        attempt.client.on_message = partial(self._on_message, attempt)
        attempt.client.on_connected = partial(self._on_open, attempt)
        attempt.client.on_error = self._on_client_error
        timeout = CONNECTION_TIMEOUT_SECONDS

        with self._lock:
            self._attempt = attempt
            self._last_message = time.monotonic()
        thread = threading.Thread(target=self._run_socket, args=(attempt,), name="barchart-desktop-sdk-socket", daemon=True)
        thread.start()

        self._serve(attempt, timeout)

        self._stop_attempt(attempt)
        attempt.client.stop()
        thread.join(STOP_WAIT_SECONDS)

        with self._lock:
            self._attempt = None
            self._token = None
            self._outbox.clear()
            # the next login sends only what's still subscribed, so what was waiting to be unsubscribed goes too
            self._unanswered.clear()
            self._closing.clear()
            lookups = list(self._lookups.values())
            self._lookups.clear()
        for lookup in lookups:
            self._listeners.schedule(partial(settle_exception, lookup.future, ConnectionError(_NOT_CONNECTED)))
        if attempt.logged_in:
            self._listeners.on_disconnected(self)
        return attempt

    def _serve(self, attempt: _Attempt, timeout: float) -> None:
        """Sends what's queued until the attempt is over, or the server has been silent for `timeout`."""
        while True:
            with self._lock:
                self._lock.wait_for(
                    lambda: self._closed or attempt.stopping or attempt.ended or bool(self._outbox),
                    timeout=max(0.0, self._last_message + timeout - time.monotonic()),
                )
                if self._closed or attempt.stopping or attempt.ended:
                    return
                if time.monotonic() - self._last_message >= timeout:
                    self._log.warning("OF SDK > Connection silent, disconnecting")
                    return
                outbox = self._outbox
                self._outbox = []
                self._unanswered.update(request.correlationId for request in outbox if not request.unsubscribe)
            for request in outbox:
                if not self._send(openfeed_api_pb2.OpenfeedGatewayRequest(subscriptionRequest=request)):
                    return

    def _stop_attempt(self, attempt: _Attempt) -> None:
        with self._lock:
            attempt.stopping = True
            self._lock.notify_all()

    # ---- the socket thread ----

    def _run_socket(self, attempt: _Attempt) -> None:
        try:
            attempt.client.start(blocking=True)
        finally:
            with self._lock:
                attempt.ended = True
                self._lock.notify_all()

    def _on_open(self, attempt: _Attempt, _socket: object) -> None:
        """A stop that came before this thread reached `client.start` is cleared by it, so it's done again here."""
        if attempt.stopping:
            attempt.client.stop()

    def _on_client_error(self, error: object) -> None:
        # Refused logins and subscriptions land here too, and are reported from their messages; the rest show up as the
        # socket ending.
        self._log.info("OF SDK > Openfeed client: %s", error)

    def _on_message(self, attempt: _Attempt, message: openfeed_api_pb2.OpenfeedGatewayMessage) -> None:
        try:
            self._handle_message(attempt, message)
        except Exception:
            self._log.exception("OF SDK > Handling a message failed")

    def _handle_message(self, attempt: _Attempt, message: openfeed_api_pb2.OpenfeedGatewayMessage) -> None:
        with self._lock:
            self._last_message = time.monotonic()
            if attempt.stopping or self._closed:
                # the rest of what it read isn't wanted
                attempt.stopping = True
                self._lock.notify_all()
                return

        kind = message.WhichOneof("data")
        if kind == "loginResponse":
            self._on_login_response(attempt, message.loginResponse)
        elif not attempt.logged_in:
            pass
        elif kind == "heartBeat":
            self._listeners.on_heartbeat(self, message.heartBeat)
        elif kind == "logoutResponse" and message.logoutResponse.status.result == openfeed_api_pb2.DUPLICATE_LOGIN:
            self._log.warning("OF SDK > Duplicate login, disconnecting")
            attempt.rejected = True
            self._stop_attempt(attempt)
        else:
            if kind == "instrumentResponse":
                self._on_instrument_response(message.instrumentResponse)
            elif kind == "subscriptionResponse" and not message.subscriptionResponse.unsubscribe:
                self._on_subscription_answered(message.subscriptionResponse.correlationId)
            self._listeners.on_message(self, message, attempt.metadata.get_metadata(message))

    def _on_login_response(self, attempt: _Attempt, response: openfeed_api_pb2.LoginResponse) -> None:
        if response.token:
            with self._lock:
                attempt.logged_in = True
                self._token = response.token
                self._backoff.reset()
                for request in self._subscriptions.values():
                    request.token = response.token
                self._outbox = list(self._subscriptions.values())
                self._lock.notify_all()
            self._listeners.on_connected(self)
        elif response.status.result in _REJECTED_LOGINS:
            self._log.warning("OF SDK > Authentication error, disconnecting")
            attempt.rejected = True
            self._stop_attempt(attempt)
        else:
            self._log.warning("OF SDK > Login failed, trying again: %s", response.status.message)
            self._stop_attempt(attempt)

    def _on_subscription_answered(self, correlation_id: int) -> None:
        """The server answered a subscription, whatever it said: an unsubscribe that waited for it can go."""
        with self._lock:
            self._unanswered.discard(correlation_id)
            unsubscribe = self._closing.pop(correlation_id, None)
            if unsubscribe is not None:
                self._outbox.append(unsubscribe)
                self._lock.notify_all()

    def _on_instrument_response(self, response: openfeed_api_pb2.InstrumentResponse) -> None:
        with self._lock:
            lookup = self._lookups.get(response.correlationId)
        # someone's own request, through `send`
        if lookup is None:
            return

        settle: Callable[[], None] | None = None
        if response.HasField("instrumentDefinition"):
            lookup.definitions.append(response.instrumentDefinition)
            if len(lookup.definitions) >= response.numberOfDefinitions:
                settle = partial(settle_result, lookup.future, lookup.definitions)
        elif response.status.result == openfeed_api_pb2.SUCCESS and response.numberOfDefinitions == 0:
            settle = partial(settle_result, lookup.future, lookup.definitions)
        else:
            settle = partial(settle_exception, lookup.future, InstrumentLookupError(response.status.result, response.status.message))

        if settle is not None:
            with self._lock:
                self._lookups.pop(response.correlationId, None)
            self._listeners.schedule(settle)
