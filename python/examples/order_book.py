"""Renders the order book on each change, with every price in the instrument's own notation."""

import time

from barchart_desktop_sdk import Book, BookEvent, Credentials, DesktopSdk, Instrument, Listener, SubscriptionError


def render(book: Book, instrument: Instrument) -> None:
    # Each side is `book_depth` long; an empty level has price 0 and size 0.
    rows = []
    for bid, ask in zip(book.bids, book.asks, strict=True):
        left = f"{bid.size} @ {instrument.format_price(bid.price)}" if bid.size > 0 else ""
        right = f"{instrument.format_price(ask.price)} @ {ask.size}" if ask.size > 0 else ""
        rows.append(f"{left} | {right}")
    print("\n".join(rows))


# The event carries the whole book, so a render needs nothing else.
def on_book(event: BookEvent) -> None:
    render(event.book, event.instrument)


# Without permission for the symbol's depth, the server refuses the subscription and this is the only call.
def on_error(error: SubscriptionError) -> None:
    print(error)


with DesktopSdk() as sdk:
    sdk.login(Credentials(url="ws://localhost:9000", username="your-username", password="your-password"))
    subscription = sdk.subscribe_depth("ZN*0", Listener(on_event=on_book, on_error=on_error))
    time.sleep(60)
    subscription.close()
