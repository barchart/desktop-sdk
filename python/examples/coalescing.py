"""On a busy feed, at most one event per subscription every 100 ms, each with the latest state."""

import time

from openfeed.generated import openfeed_api_pb2

from barchart_desktop_sdk import Credentials, DesktopSdk, Listener, MessageMetadata, QuoteEvent, SubscriptionError


def on_quote(event: QuoteEvent) -> None:
    print(event.symbol, event.formatted.session.last)


# Raw messages aren't held: this runs for each message, and can come before the event that covers it.
def on_message(message: openfeed_api_pb2.OpenfeedGatewayMessage, metadata: MessageMetadata) -> None:
    if message.WhichOneof("data") == "marketUpdate" and message.marketUpdate.HasField("trades"):
        print(", ".join(metadata.symbol_names), "traded")


# Errors aren't held either.
def on_error(error: SubscriptionError) -> None:
    print(error)


with DesktopSdk(coalesce_interval_ms=100) as sdk:
    sdk.login(Credentials(url="ws://localhost:9000", username="your-username", password="your-password"))
    for symbol in ("ES*0", "NQ*0", "ZN*0", "CL*0"):
        sdk.subscribe_quote(symbol, Listener(on_event=on_quote, on_message=on_message, on_error=on_error))
    time.sleep(60)
