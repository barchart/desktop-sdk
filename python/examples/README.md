# Examples

Short programs, one task each. They connect to the Barchart Desktop API on `ws://localhost:9000`; put in your Barchart Desktop credentials to run them. They run for a minute or so, then close everything. Most print only when data arrives, so without a running Barchart Desktop they stay quiet.

- [quotes.py](quotes.py): a quote's last price and best bid and offer, as they change.
- [order_book.py](order_book.py): the order book, rendered on each change.
- [coalescing.py](coalescing.py): at most one event per subscription per interval, on a busy feed.
- [cumulative_volume.py](cumulative_volume.py): the volume-at-price levels that changed.
- [connection.py](connection.py): logging in and out, rejected credentials, heartbeats.
- [raw_access.py](raw_access.py): raw subscriptions, one-off snapshots and instrument lookups.

## Running one

In a clone of this repository, put your credentials in the example's `sdk.login(...)` call, then, from `python/`:

```shell
uv sync
uv run python examples/quotes.py
```

`uv sync` installs the SDK from `src/` in editable mode, so the examples pick up changes to it without a reinstall.

Every method and shape they use has a docstring, and the installed package has the API reference as Markdown in `barchart_desktop_sdk/docs/api/`. The [README](../README.md) covers names and threads.
