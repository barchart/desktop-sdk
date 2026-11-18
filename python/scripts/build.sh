#!/bin/sh
# The wheel and the sdist, into dist/, with the API reference generated into the package first: uv_build has no hook to
# run it, so build with this rather than `uv build` alone.
set -eu

uv run --locked python scripts/docs_api.py
rm -rf dist
uv build --no-sources
