# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in `python/`, the Python SDK. The repository's `CLAUDE.md` has what the SDKs share: keeping them in step, commits, the workflows and releasing.

## What this is

`barchart-desktop-sdk`: live market state on top of the Python Openfeed client, `openfeed` ([openfeed-org/sdk-python](https://github.com/openfeed-org/sdk-python)). It subscribes, applies snapshots and updates, and keeps quotes, order books and cumulative volume per instrument.

- `src/barchart_desktop_sdk/__init__.py` is the only public entry; modules starting with `_` are internal.
- `tests/`: pytest. `tests/helpers.py` builds protocol messages (protobuf constructors take nested dicts and enum names); `tests/fakes.py` replaces the Openfeed client with a fake.
- `examples/`: short programs, one task each, checked by mypy but not run. `examples/README.md` lists them.
- `llms.txt`: the summary and links that AI assistants read ([llmstxt.org](https://llmstxt.org/)). A wheel only carries the package, so `src/barchart_desktop_sdk/llms.txt` is a copy that an assistant finds in `site-packages`; `tests/test_docs.py` keeps the two the same, and checks the files it links to exist.

## Commands

```shell
uv sync                                          # environment and dev tools (exact versions, uv.lock)
sh scripts/check.sh                              # everything CI runs, in order: the commands below, then build.sh; also turns on the git hooks
uv run pytest
uv run ruff check && uv run ruff format --check  # every rule, minus those pyproject.toml turns off; 150 columns
uv run mypy src tests examples scripts           # strict
uv run python scripts/docs_api.py                # the API reference, from the docstrings, into src/barchart_desktop_sdk/docs/api/
sh scripts/build.sh                              # the wheel and the sdist, into dist/, with the API reference generated first
```

`src/barchart_desktop_sdk/docs/api/` is generated, not committed (`.gitignore`), and `uv_build` packs it anyway, so the wheel and the sdist ship it. `uv_build` has no hook to generate it, so build with `scripts/build.sh`, not `uv build` alone, which would pack whatever is in that folder, or nothing. The tests run the generator too, and fail on a public name it can't find.

`scripts/check.sh` also turns on the repository's git hooks (`../.githooks/`). Their `pre-commit` fixes the staged Python files with ruff and stages them again, except a file that also has unstaged changes: its staged version is only checked. It lints, types and tests the repository's `../scripts/` too, which are Python (`tests/test_check_commits.py`).

## Supply chain

- **Pinned versions:** dev dependencies are exact versions, and CI installs exactly `uv.lock` (`--locked`). uv holds back packages published less than a day ago (`exclude-newer = "1 day"` in `uv.toml`, uv's settings, kept apart from `pyproject.toml` so code owners can guard them).
- **Age gate exception:** `openfeed` skips the one-day wait (`exclude-newer-package = { openfeed = false }`). This is a deliberate decision and stays: it's our own client, released by us, so the SDK can pick up a new version the same day. Every other package, including the client's own dependencies, still waits.
- **Code owners** guard `uv.toml`; the workflows, the publishing jobs and the rest of the code owners are the repository's (its `CLAUDE.md`).

## From the TypeScript SDK

The TypeScript SDK in `../js/` is the reference: its `docs/reference.md` and `docs/design.md` describe what this package does, and a change to what users see goes into both (the repository's `CLAUDE.md`).

Bringing a change over from the TypeScript SDK: names in snake_case (`last_size` for `lastSize`), records as frozen slotted dataclasses, levels as tuples, `None` for `null`/`undefined`, UTC `datetime`s (to the microsecond) and `date`s. Where it formats or parses numbers with `toFixed`, `Math.round`, `String(number)`, `parseFloat` or `parseInt`, use `_js.py`, not Python's `round`, `format` or `str`, so the output matches. ts-proto leaves an unset message field `undefined`, where Python's protobuf hands out a default instance, so check presence as below.

## Code rules

- **Presence:** check optional message fields with `HasField` and oneofs with `HasField` or `WhichOneof`. A default instance's own `HasField` is false, so nested checks need no guard (`update.session.HasField("last")`).
- **Types:** protocol messages are typed by `openfeed`'s stubs (`openfeed_pb2.MarketUpdate`, say), so `src/` has no `Any`. Mypy can't check the field name given to `HasField` or `WhichOneof`, so tests have to cover each one. Write a record's fields out rather than spreading a dict, so mypy checks them.
- **The docs are for people using the SDK.** Keep `README.md`, `examples/` and both copies of `llms.txt` in step with changes to the public API or behavior.
- **Every public class, method, property and field has a docstring,** written for users: it's what they see on hover and in `help()`. Use Google-style `Args:`, `Returns:`, `Raises:` and, on the main methods, `Example:`. A field's docstring is the string under it. Contributor notes go in `#` comments. `tests/test_docs.py` checks every name in `__all__`, and `scripts/docs_api.py` turns them into the API reference.
- **Names:** `get_…` for lookups, `has_…`/`is_…` for predicates, `remove`/`clear` for removal, `try_…` for an action that may not happen, `on_…` for registering a listener.
- **One visible flow:** subscribing and closing go through the delivery thread's batches, coalesced events through its deadline, and connecting through the connection thread (see "Threads"). No other threads, timers or deferred calls.
- **Every change** passes `sh scripts/check.sh`, with tests for new behavior.

## Openfeed client

The SDK needs `openfeed` 1.5.0 or later, the first release with its current protos and type stubs. `uv.lock` pins the version the checks run against; `uv lock --upgrade-package openfeed` moves it. `openfeed` skips the one-day hold-back (`uv.toml`): it's our own client, so the SDK can pick up a new release the same day. A change that needs something newer raises the lower bound in `pyproject.toml`.

`openfeed.generated` is fully typed, but the client class isn't: mypy doesn't check calls into it (`untyped_calls_exclude`), so `tests/test_live.py` runs them against a real local socket. `_connection.py` is the only module that touches the client, and adds what it doesn't do: reconnecting with backoff, re-subscribing, the silent connection check, unsubscribing (once the server has answered the subscription, so the unsubscribe can't overtake it), lookups and message metadata. It doesn't use the client's own subscription API, which has no unsubscribe.

## Threads

`_sdk.py` and `_connection.py` run on these:

- **Socket thread:** applies each message to the state under the lock and queues what listeners should get. Never runs a listener.
- **Delivery thread:** runs every listener and settles every future, one at a time, and processes the queued subscribes and closes in batches, and delivers coalesced events.
- **Connection thread:** decides when to connect and give up, and is the only sender of subscription requests. Nothing under the SDK's lock waits on the network.

Never hold the lock while a listener runs, and never call a listener synchronously inside `subscribe`: callers rely on holding the handle first.

Threaded tests pass by luck easily. Tests wait with `flush` and `wait_until` rather than sleeping, and a test for a race has to be shown failing without its fix.

## Releasing

release-please, at the repository's root, bumps `version` in `pyproject.toml`, and the project's own entry in `uv.lock`, and writes `CHANGELOG.md`, from the commits that touched `python/`; don't bump it by hand. A release's wheel and sdist are attached to its GitHub release; PyPI waits for `openfeed` to be released there. The repository's `CLAUDE.md` has the details, and the steps to switch PyPI on.
