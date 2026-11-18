#!/bin/sh
# Everything CI runs for the Python SDK, in order: lint, format, types, tests, and the build with its API reference
# (scripts/build.sh). The repository's own scripts (../scripts/) are checked here too, being Python. Run it from python/:
# `sh scripts/check.sh`.
set -eu

sh ../scripts/install-hooks.sh

uv run --locked ruff check . ../scripts
uv run --locked ruff format --check . ../scripts
uv run --locked mypy src tests examples scripts ../scripts
uv run --locked pytest
sh scripts/build.sh
