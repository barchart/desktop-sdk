"""The connection's lifetime: one SDK for the application, logins and logouts as states of it."""

import time
from datetime import datetime

from openfeed.generated import openfeed_pb2

from barchart_desktop_sdk import ConnectionState, Credentials, DesktopSdk, Listener, QuoteEvent, SubscriptionError

CREDENTIALS = Credentials(url="ws://localhost:9000", username="your-username", password="your-password")


def on_state(state: ConnectionState) -> None:
    # Rejected credentials are final until the next login; a lost connection is retried by itself.
    if state == "credentials-rejected":
        print("Sign in with the credentials Barchart Desktop is signed in with.")


def on_heartbeat(server_time: datetime | None, _raw: openfeed_pb2.HeartBeat) -> None:
    print("server time", server_time)


def on_quote(event: QuoteEvent) -> None:
    print(event.quote.session.last)


def on_error(error: SubscriptionError) -> None:
    print(error)


# Closes everything when the block ends.
with DesktopSdk() as sdk:
    sdk.on_connection_state(on_state)
    sdk.on_heartbeat(on_heartbeat)

    # Subscriptions made while logged out stay `pending`, and are sent at login.
    subscription = sdk.subscribe_quote("ES*0", Listener(on_event=on_quote, on_error=on_error))
    print(subscription.state)

    sdk.login(CREDENTIALS)
    try:
        sdk.when_connected().result(timeout=30)
    except (RuntimeError, TimeoutError) as error:
        # rejected credentials, the SDK disposed first, or no connection in time
        print(error)
    else:
        # Quotes and heartbeats for half a minute.
        time.sleep(30)

        # Logging out clears the market state but keeps the subscriptions, which are sent again at the next login.
        sdk.logout()
        sdk.login(CREDENTIALS)
        time.sleep(30)
