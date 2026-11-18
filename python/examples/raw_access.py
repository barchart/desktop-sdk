"""What the SDK doesn't model: raw subscriptions, one-off snapshots and instrument lookups."""

import time

from openfeed.generated import openfeed_api_pb2

from barchart_desktop_sdk import Credentials, DesktopSdk, MessageMetadata, RawListener, SubscriptionError


def on_message(message: openfeed_api_pb2.OpenfeedGatewayMessage, _metadata: MessageMetadata) -> None:
    if message.HasField("marketUpdate") and message.marketUpdate.HasField("trades"):
        for entry in message.marketUpdate.trades.trades:
            print("trade", entry.trade.price, entry.trade.quantity)


def on_error(error: SubscriptionError) -> None:
    print(error)


with DesktopSdk() as sdk:
    sdk.login(Credentials(url="ws://localhost:9000", username="your-username", password="your-password"))

    # Any Openfeed subscription, with its raw messages and no SDK state or events.
    trades = sdk.raw.subscribe("ES*0", openfeed_api_pb2.TRADES, RawListener(on_message=on_message, on_error=on_error))

    try:
        # Fails on rejected credentials; the snapshot below would wait for a connection that never comes.
        sdk.when_connected().result(timeout=30)

        # One snapshot, then the subscription closes. Raises a SubscriptionError if the server refuses it.
        snapshot = sdk.raw.request_snapshot("ZC*0").result(timeout=30)
        print(snapshot.instrument_definition, snapshot.market_snapshot.marketId)

        # The gateway's instrument lookup. Raises an InstrumentLookupError if the server refuses it or finds nothing.
        definitions = sdk.raw.lookup_instruments(openfeed_api_pb2.InstrumentRequest(symbol="ZC*0")).result(timeout=30)
        print([definition.symbol for definition in definitions])

        # Trades for a minute.
        time.sleep(60)
    except (RuntimeError, TimeoutError, SubscriptionError) as error:
        print(error)
    finally:
        trades.close()
