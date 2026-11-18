# Barchart Desktop SDK

Live quotes, order books and cumulative volume from [Openfeed](https://openfeed.com/), kept up to date for you. The SDK subscribes, applies snapshots and updates, keeps market state per instrument, and tells you when it changes. Point it at the **Barchart Desktop API** on your machine, signed in with your Barchart Desktop credentials, or at an Openfeed gateway directly.

One SDK per language, with the same behavior, each in its own language's style:

| Language   | Package                                                                              | Docs                                 |
| ---------- | ------------------------------------------------------------------------------------ | ------------------------------------ |
| TypeScript | [`@barchart/desktop-sdk-js`](https://www.npmjs.com/package/@barchart/desktop-sdk-js) | [js/README.md](js/README.md)         |
| Python     | `barchart-desktop-sdk` (early release, installed from this repository)               | [python/README.md](python/README.md) |

The [reference](js/docs/reference.md) describes every method and the delivery guarantees, in the TypeScript SDK's names; the Python SDK's are the same in snake_case.

[CONTRIBUTING.md](CONTRIBUTING.md) says how to work on them. See [LICENSE](LICENSE) for the license.
