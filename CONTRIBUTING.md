# Contributing

The Barchart Desktop SDK, in its own folder with its own toolchain:

- [`js/`](js/): TypeScript, with Yarn. [js/CONTRIBUTING.md](js/CONTRIBUTING.md) has its setup.

## Commit messages

Commit messages are [conventional commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`…; `feat!:` or a `BREAKING CHANGE:` footer for a breaking change). They set the next version of the package whose folder they touch, and become its changelog, so write them for the SDK's users.

## Git hooks

The git hooks (`.githooks/`) check the commit message with `scripts/check_commits.py`, which needs only `python3`, and lint and format the staged files of each package with its own tools; a package with nothing staged is skipped. Git doesn't run a repository's hooks on its own: the package's check turns them on, as does `sh scripts/install-hooks.sh`.
