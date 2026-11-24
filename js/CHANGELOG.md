# Changelog

## [0.3.2](https://github.com/barchart/desktop-sdk/compare/js-v0.3.1...js-v0.3.2) (2026-11-24)


### Bug Fixes

* add subscription responses by correlation id ([19d70d7](https://github.com/barchart/desktop-sdk/commit/19d70d7f9a647fe0cd0699870e15b4b3ef057c0e))
* format prices for any denominator, and reject whenConnected() after rejected credentials ([756bf48](https://github.com/barchart/desktop-sdk/commit/756bf488c61645fd3e643c1d5cbaa2cb48c83d0b))
* keep coalesced cumulative volume and exchange-moved events complete ([6e98fc4](https://github.com/barchart/desktop-sdk/commit/6e98fc426cee73adf7db75f324af6d90f964b910))
* keep the order book in step with depth subscriptions and ClearBook ([ab1e134](https://github.com/barchart/desktop-sdk/commit/ab1e134f9397cc3082b9fc19b575a194cd64db1b))

## [0.3.1](https://github.com/barchart/desktop-sdk/compare/js-v0.3.0...js-v0.3.1) (2026-11-18)


### Bug Fixes

* **js:** bump openfeed sdk version ([44de2f2](https://github.com/barchart/desktop-sdk/commit/44de2f2d9fbbfbe222c5b6f161c748f84f0af2ce))

## [0.3.0](https://github.com/barchart/desktop-sdk-js/compare/v0.2.2...v0.3.0) (2026-11-12)


### Features

* move name tracking to registry only, deliver named Instruments to the outside ([1e0e433](https://github.com/barchart/desktop-sdk-js/commit/1e0e4339e8ff7b0ac3706b809e3ae227ab7bdaca))

## [0.2.2](https://github.com/barchart/desktop-sdk-js/compare/v0.2.1...v0.2.2) (2026-11-09)


### Bug Fixes

* simplify tresury option formatting ([9ae19ee](https://github.com/barchart/desktop-sdk-js/commit/9ae19ee0439a73b56dc4da4f97b10a5a8238c8d2))

## [0.2.1](https://github.com/barchart/desktop-sdk-js/compare/v0.2.0...v0.2.1) (2026-11-06)


### Bug Fixes

* update of sdk to fix lookup and tweak docs ([a3be445](https://github.com/barchart/desktop-sdk-js/commit/a3be44560c174bfc195d5d93212633521b605810))

## [0.2.0](https://github.com/barchart/desktop-sdk-js/compare/v0.1.1...v0.2.0) (2026-11-04)


### Features

* improve error reporting and involve it in tests/examples/docs ([1872ddb](https://github.com/barchart/desktop-sdk-js/commit/1872ddb9fc2063d72b3c859271f01dcb19111f90))


### Bug Fixes

* update sdk-js and fix related type errors ([70591d8](https://github.com/barchart/desktop-sdk-js/commit/70591d83213700a4975fc77f281fb1e7eb5bb08e))

## [0.1.1](https://github.com/barchart/desktop-sdk-js/compare/v0.1.0...v0.1.1) (2026-11-02)


### Bug Fixes

* parse a fraction's last digit as the sub-fraction formatPrice writes ([e6cef45](https://github.com/barchart/desktop-sdk-js/commit/e6cef45500bdde68b9175b7359a67f071f71f2cf))
* skip book levels outside the book's depth, logging an error ([7966533](https://github.com/barchart/desktop-sdk-js/commit/79665339cc3ed5577d4ea8ecc9d2c86b77407b1b))

## 0.1.0 (2026-10-28)


### Features

* initial release ([335e462](https://github.com/barchart/desktop-sdk-js/commit/335e462d7f3438830d0329f92670d1a2fb7407fe))
