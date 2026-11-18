# Contributing to the Python SDK

The repository's [CONTRIBUTING.md](../CONTRIBUTING.md) has what both SDKs share: commit messages and the git hooks.

## Setup

[uv](https://docs.astral.sh/uv/) manages the Python version, the environment and the lockfile. From `python/`:

```sh
uv sync                               # the environment, with the dev tools (exactly uv.lock)
sh scripts/check.sh                   # everything CI runs: lint, format, types, tests, API docs, build; turns on the git hooks
```

## API documentation

Every public class, method, property and field needs a docstring: users read it on hover and in `help()`, and it becomes the API reference that the package ships in `barchart_desktop_sdk/docs/api/`. Write it for people using the SDK, with Google-style `Args:`, `Returns:`, `Raises:` and, for the main methods, an `Example:`. A field's docstring is the string under it.

`docs/api/` is generated from the docstrings by `scripts/docs_api.py` and isn't committed (`.gitignore`). `scripts/build.sh` generates it before building, so every wheel and sdist has it; build with that rather than `uv build` alone. To read it locally:

```sh
uv run python scripts/docs_api.py
```

The tests fail when a public item has no docstring, and name it.

## Git hooks

Before each commit, the git hooks fix the staged files under `python/` with ruff and stage them again; a file that also has unstaged changes is only checked, since staging the fix would stage those too. Git doesn't run a repository's hooks on its own: `sh scripts/check.sh` turns them on.

## Working on the SDK inside an application

Install this repository into the application's environment as editable, so it runs your changes without a rebuild:

```sh
uv add --editable <path to python/>   # an application on uv
pip install --editable <path to python/>
```
