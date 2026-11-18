# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository.

## What this is

The Barchart Desktop SDK: live market state on top of an Openfeed client. It subscribes, applies snapshots and updates, and keeps quotes, order books and cumulative volume per instrument. Open source (see `LICENSE`). Customers use it through the Barchart Desktop API. One SDK per language, each in its own folder, released on its own:

- `js/`: `@barchart/desktop-sdk-js`, TypeScript, on npm, on top of `@openfeed/sdk-js`. `js/CLAUDE.md` has its details.
- `python/`: `barchart-desktop-sdk`, Python, on PyPI, on top of `openfeed`. `python/CLAUDE.md` has its details.

At the root: what both share. `.github/` (CI, releases, code owners), `.githooks/` and `scripts/` (the git hooks and the commit check), the release-please config, and `LICENSE`, which each package ships a copy of (CI checks they're the same).

The Openfeed clients are separate repositories, and not part of this one.

## One SDK, several languages

The SDKs behave the same: the same events, state, connection handling, errors and public API, each in its own language's style. A change to what users see goes into every SDK in the same pull request, each with its test. If one can't follow yet, say so in the pull request and leave a note for it.

`js/` is the reference: `js/docs/reference.md` describes the behavior and the delivery guarantees, and `js/docs/design.md` the internals, for contributors and ports. `python/CLAUDE.md` says how its names and threads map onto them.

## Commands

Each package has its own toolchain, run from its folder:

```shell
cd js && yarn install && yarn check              # everything CI runs for TypeScript; see js/CLAUDE.md
cd python && uv sync && sh scripts/check.sh      # everything CI runs for Python; see python/CLAUDE.md
python3 scripts/check_commits.py --range origin/main..HEAD   # the branch's commit messages, as CI checks a pull request
```

Either package's check, and `yarn build`, turn on the git hooks (`sh scripts/install-hooks.sh`: `git config core.hooksPath .githooks`). Neither package runs install scripts, so `yarn install` and `uv sync` don't. `commit-msg` checks the message with `scripts/check_commits.py` (Python's standard library only, commitlint's conventional rules). `pre-commit` lints and formats the staged files of each package with its own tools, and skips a package with nothing staged, so a contributor to one needn't install the other.

## Supply chain

Each package's dependencies are pinned and held back a day (`js/.yarnrc.yml`, `python/uv.toml`), except our own Openfeed client in each; the package's `CLAUDE.md` has the details. For the repository:

- **Workflows:** actions are pinned to commit SHAs, with the version in a comment; update the SHA and the comment together. Jobs get the fewest permissions they need, checkouts don't keep the token (`persist-credentials: false`), and nothing is cached, since a pull request could fill a cache for `main`.
- **Code owners** (`.github/CODEOWNERS`): changes to `.github/` (the workflows, and the owners file itself), `.githooks/`, `scripts/`, `js/.yarnrc.yml` and `python/uv.toml` need a review from one of the owners.
- **Building and publishing are separate jobs** (`release.yml`): the build jobs install, check and build with read-only access and no secrets; the publish and attach jobs only download what was built, with no checkout and no install, so dependency code never runs next to write access or a registry's credentials.

## Rules

- **Code comments say what the code does and why,** never its history.
- **Every change** passes its package's check, with tests for new behavior.
- **Commit messages are conventional commits** (`feat:`, `fix:`, `docs:`, `chore:`…; `feat!:` or a `BREAKING CHANGE:` footer for breaking changes). They set the next version of each package whose folder they touch, and become its changelog, so write them for users. A commit touching both packages releases both, so keep a change to one package out of a commit for the other. Scope a package's commits with its folder (`fix(js): …`, `feat(python): …`), and leave repository-wide ones unscoped (`ci: …`); release-please goes by the files a commit touches, so the scope is for readers. With squash merges, the PR title is the commit.

## Releasing

release-please (`.github/workflows/release.yml`, `release-please-config.json`, `.release-please-manifest.json`) keeps a release PR open on `main` for the packages with releasable commits since their last release. It bumps each package's version from the commits that touched its folder (`fix` a patch, `feat` a minor; before 1.0 a breaking change is a minor too), and adds them to the package's `CHANGELOG.md`. Merging it tags each release (`js-v0.4.0`, `python-v0.1.0`) and creates its GitHub release; the same workflow then builds and publishes it. Don't bump versions by hand, and don't edit a `CHANGELOG.md`.

- **`js/`, on npm:** trusted publishing through OIDC, in the `npm` environment; there's no npm token anywhere, and the package refuses publishes with one. The job only stages the version (`npm stage publish`; the trusted publisher allows staging, not `npm publish`), with no approval on the `npm` environment. It goes public once a maintainer approves the staged version on npmjs.com, or with `npm stage approve`, with 2FA.
- **`python/`, on PyPI:** the wheel and sdist are attached to the GitHub release, then published to PyPI through trusted publishing (OIDC), in the `pypi` environment; there's no PyPI token anywhere. PyPI's trusted publisher for `barchart-desktop-sdk` names this repository, `release.yml` and the environment `pypi`, so renaming any of them needs it updated too. The environment's required reviewers approve each release, the one manual step.
