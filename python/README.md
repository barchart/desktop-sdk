# Barchart Desktop SDK for Python

Live quotes, order books and cumulative volume from [Openfeed](https://openfeed.com/), kept up to date for you. The Python port of [`@barchart/desktop-sdk-js`](https://github.com/barchart/desktop-sdk/tree/main/js), which is the reference implementation: its [reference](https://github.com/barchart/desktop-sdk/blob/main/js/docs/reference.md) and [design](https://github.com/barchart/desktop-sdk/blob/main/js/docs/design.md) describe what this package does.

> **Early release.** The API follows the TypeScript SDK.

## Installing

Python 3.11 or later:

```shell
pip install barchart-desktop-sdk
# or
uv add barchart-desktop-sdk
```

## Quick start

```python
from barchart_desktop_sdk import Credentials, DesktopSdk, Listener, QuoteEvent, SubscriptionError


def on_quote(event: QuoteEvent) -> None:
    print(event.symbol, event.formatted.session.last)


def on_error(error: SubscriptionError) -> None:
    print(error)


with DesktopSdk() as sdk:
    sdk.login(Credentials(url="ws://localhost:9000", username="your-username", password="your-password"))
    subscription = sdk.subscribe_quote("ES*0", Listener(on_event=on_quote, on_error=on_error))
    ...
```

`ws://localhost:9000` is the Barchart Desktop API, which takes Barchart Desktop's credentials. An Openfeed gateway works the same way, with its own URL.

Always pass `on_error`: nothing else tells you when the server refuses a subscription, such as one you don't have permission for.

Not every Openfeed server sends volume at price: on one that doesn't, the subscription goes `active`, but no events come and `get_cumulative_volume` stays `None`.

A failed lookup raises: `raw.lookup_instruments` gives an `InstrumentLookupError` when the server refuses it or finds nothing.

More in [`examples/`](https://github.com/barchart/desktop-sdk/tree/main/python/examples). Every class, method and field has a docstring: your editor shows it on hover, `help(DesktopSdk.subscribe_quote)` prints it, and the installed package has them all as Markdown in `barchart_desktop_sdk/docs/api/`. The [TypeScript SDK's reference](https://github.com/barchart/desktop-sdk/blob/main/js/docs/reference.md) describes the same methods and the delivery guarantees, under the names below.

## Names

The same names as the TypeScript SDK, in Python's style: `session.lastSize` is `session.last_size`, `sdk.subscribeQuote` is `sdk.subscribe_quote`. Records are frozen dataclasses, levels are tuples, times are UTC `datetime`s (to the microsecond) and trade dates are `date`s. The logger is a standard `logging.Logger`, by default `logging.getLogger("barchart_desktop_sdk")`.

## Threads

Every method is safe to call from any thread, listeners included.

- **Listeners run one at a time, in order, on the SDK's delivery thread**, and never inside the call that registered them, so you always hold the handle first.
- **State is applied as messages arrive,** on another thread, so `get_quote` and the other getters are current even while a slow listener holds up the events behind it. Events wait in a queue with no limit: a listener that can't keep up should use `coalesce_interval_ms`.
- **A listener mustn't wait on a future the SDK settles,** such as `sdk.when_connected().result()`, since settling it needs the thread the listener is running on. `add_done_callback` is fine.
- **After `close()` returns, no new callback starts.** One already running on the delivery thread finishes.

The SDK's threads are daemon threads, so one you forget to `dispose()` doesn't keep the process alive.

In `asyncio`, await the futures the SDK returns (`when_connected`, `raw.request_snapshot`, `raw.lookup_instruments`) by wrapping them: `await asyncio.wrap_future(sdk.when_connected())`.

## Development

[uv](https://docs.astral.sh/uv/) manages the Python version, the environment and the lockfile; [CONTRIBUTING.md](https://github.com/barchart/desktop-sdk/blob/main/python/CONTRIBUTING.md) has the rest.

```shell
uv sync                                 # the environment, with the dev tools
sh scripts/check.sh                     # everything CI runs: lint, format, types, tests, API docs, build
```

It depends on [`openfeed`](https://github.com/openfeed-org/sdk-python) 1.5.0 or later, from PyPI. `uv.lock` pins the version the checks use; `uv lock --upgrade-package openfeed` moves it.
