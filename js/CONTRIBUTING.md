# Contributing to the TypeScript SDK

The repository's [CONTRIBUTING.md](../CONTRIBUTING.md) has what both SDKs share: commit messages and the git hooks.

## Setup

From `js/`:

```sh
corepack enable      # once per machine: Yarn's version comes from packageManager in package.json
yarn install
yarn check           # everything CI runs: typecheck, lint, format, tests, API docs, build, package checks
```

## API documentation

Every public export and member needs a TSDoc comment (`/** … */`): users read it on hover, and it becomes the API reference that the npm package ships in `docs/api/`. Write it for people using the SDK, with `@param`, `@returns` and, for the main methods, an `@example`.

`docs/api/` is generated from those comments by TypeDoc (`typedoc.json`) and isn't committed (`.gitignore`): `yarn pack` generates it first (the `prepack` script), so every package has it. To read it locally:

```sh
yarn docs:api
```

`yarn check` (in CI too) runs it, and fails when a public item has no comment or a `{@link}` is broken; it names the item.

Before each commit, the git hooks lint and format the staged files under `js/` with ESLint and Prettier (`lint-staged.config.js`). They aren't turned on by `yarn install`, since this package runs no install scripts, its dependencies' included (`enableScripts: false` in `.yarnrc.yml`). `yarn build`, `yarn build:watch` and `yarn check` turn them on instead; `yarn hooks:install` does it on its own.

## Working on the SDK inside an application

To run your changes in an application, link `js/` into it and keep a build running there:

```sh
yarn build:watch   # rebuilds dist/ on every change, with source maps into src/
```

Linking depends on the application's package manager:

- **Yarn 2 or later:** run `yarn link <path to js/>` in the application.
- **npm:** run `npm link` in `js/`, then `npm link @barchart/desktop-sdk-js` in the application.

**The application must use its own `@openfeed/sdk-js` and `long`.** A linked package resolves its imports from `js/node_modules`, which has its own copies (for the tests). Two copies mean two Openfeed clients and two sets of protocol types and enums. With Vite:

```ts
resolve: { dedupe: ["@openfeed/sdk-js", "long"] },
optimizeDeps: { exclude: ["@barchart/desktop-sdk-js"] }, // picks up the rebuilds
```

The source maps `build:watch` writes point into `src/`, so the debugger and "Go to definition" open the TypeScript sources.
