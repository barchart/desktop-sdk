# Contributing

The Barchart Desktop SDK, one per language, each in its own folder with its own toolchain:

- [`js/`](js/): TypeScript, with Yarn. [js/CONTRIBUTING.md](js/CONTRIBUTING.md) has its setup.
- [`python/`](python/): Python, with uv. [python/CONTRIBUTING.md](python/CONTRIBUTING.md) has its setup.

You only need the toolchain of the SDK you're changing.

## Changing what users see

The SDKs behave the same. A change to what users see (events, state, connection handling, errors, the public API) goes into every SDK, each with its test, in the same pull request. If you can only make it in one, say so in the pull request.

## Commit messages

Commit messages are [conventional commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`…; `feat!:` or a `BREAKING CHANGE:` footer for a breaking change). They set the next version of each SDK whose folder they touch, and become its changelog, so write them for the SDK's users. Keep a change to one SDK out of a commit for the other, or both are released. Scope an SDK's commits with its folder, such as `fix(js): …` or `feat(python): …`, and leave changes to the repository as a whole, such as CI, unscoped.

## Git hooks

The git hooks (`.githooks/`) check the commit message with `scripts/check_commits.py`, which needs only `python3`, and lint and format the staged files of each SDK with its own tools; an SDK with nothing staged is skipped. Git doesn't run a repository's hooks on its own: each SDK's check turns them on, as does `sh scripts/install-hooks.sh`.
