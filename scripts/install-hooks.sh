#!/bin/sh
# Turns on the repository's git hooks (.githooks/). Git doesn't run a repository's hooks on its own, and neither package
# runs install scripts, so each package's check or build runs this instead. Nothing to do outside a clone.
set -eu

if git rev-parse --git-dir >/dev/null 2>&1; then
    git config core.hooksPath .githooks
fi
