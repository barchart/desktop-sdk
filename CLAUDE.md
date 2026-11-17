# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository.

## What this is

The Barchart Desktop SDK: live market state on top of an Openfeed client. It subscribes, applies snapshots and updates, and keeps quotes, order books and cumulative volume per instrument. Open source (see `js/LICENSE`). Customers use it through the Barchart Desktop API.

- `js/`: `@barchart/desktop-sdk-js`, TypeScript, on npm, on top of `@openfeed/sdk-js`. `js/CLAUDE.md` has its details.

At the root: what the repository shares. `.github/` (CI, releases, code owners), `.githooks/` and `scripts/` (the git hooks and the commit check), and the release-please config.

## Commands

```shell
cd js && yarn install && yarn check              # everything CI runs for TypeScript; see js/CLAUDE.md
python3 scripts/check_commits.py --range origin/main..HEAD   # the branch's commit messages, as CI checks a pull request
```

`yarn check` and `yarn build` turn on the git hooks (`sh scripts/install-hooks.sh`: `git config core.hooksPath .githooks`). The package runs no install scripts, so `yarn install` doesn't. `commit-msg` checks the message with `scripts/check_commits.py` (Python's standard library only, commitlint's conventional rules). `pre-commit` lints and formats the staged files of each package with its own tools, and skips a package with nothing staged.

## Supply chain

The package's dependencies are pinned and held back a day (`js/.yarnrc.yml`), except our own Openfeed client; `js/CLAUDE.md` has the details. For the repository:

- **Workflows:** actions are pinned to commit SHAs, with the version in a comment; update the SHA and the comment together. Jobs get the fewest permissions they need, and checkouts don't keep the token (`persist-credentials: false`).
- **Code owners** (`.github/CODEOWNERS`): changes to `.github/` (the workflows, and the owners file itself), `.githooks/`, `scripts/` and `js/.yarnrc.yml` need a review from one of the owners.
- **Build and publish are separate jobs** (`release.yml`): `build` installs, checks and packs `package.tgz` with read-only access and no secrets; `publish` downloads that tarball and runs only `npm stage publish package.tgz --ignore-scripts`, with no checkout and no install, so dependency code never runs next to the npm credentials.

## Rules

- **Code comments say what the code does and why,** never its history.
- **Every change** passes its package's check, with tests for new behavior.
- **Commit messages are conventional commits** (`feat:`, `fix:`, `docs:`, `chore:`…; `feat!:` or a `BREAKING CHANGE:` footer for breaking changes). They set the next version of the package whose folder they touch, and become its changelog, so write them for users. With squash merges, the PR title is the commit.

## Releasing

release-please (`.github/workflows/release.yml`, `release-please-config.json`, `.release-please-manifest.json`) keeps a release PR open on `main`. It bumps `version` in `js/package.json` from the commits that touched `js/` since the last release (`fix` a patch, `feat` a minor; before 1.0 a breaking change is a minor too) and adds them to `js/CHANGELOG.md`. Merging it tags the release and creates the GitHub release; the same workflow then builds and checks the tarball (`build`) and publishes it to npm (`publish`), public and with provenance (`publishConfig`). Don't bump `version` by hand, and don't edit `CHANGELOG.md`.

Publishing uses npm's trusted publishing: npm trusts this repository's `release.yml`, in the `npm` environment, through OIDC. There's no npm token anywhere, and the package refuses publishes with one. One people-gate, outside GitHub: the job only stages the version (`npm stage publish`; the trusted publisher allows staging, not `npm publish`), with no approval on the `npm` environment. It goes public once a maintainer approves the staged version on npmjs.com, or with `npm stage approve`, with 2FA.
