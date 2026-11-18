"""Volume traded at each price today."""

import time

from barchart_desktop_sdk import Credentials, CumulativeVolumeEvent, DesktopSdk, Listener, SubscriptionError


def on_volume(event: CumulativeVolumeEvent) -> None:
    # On `initial` and `snapshot` the levels start over, so anything kept from before is gone.
    for level in event.changed:
        print(event.symbol, event.kind, level.price, level.volume)


def on_error(error: SubscriptionError) -> None:
    # `required_by` is set when what was refused is the ALL subscription that cumulative volume needs for its trades.
    print(error)


with DesktopSdk() as sdk:
    sdk.login(Credentials(url="ws://localhost:9000", username="your-username", password="your-password"))
    sdk.subscribe_cumulative_volume("ES*0", Listener(on_event=on_volume, on_error=on_error))
    time.sleep(60)
