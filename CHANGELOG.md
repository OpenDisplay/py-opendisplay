# Changelog

## [5.7.0](https://github.com/OpenDisplay/py-opendisplay/compare/v5.6.0...v5.7.0) (2026-03-24)


### Features

* add initial support for encryption ([28c24f9](https://github.com/OpenDisplay/py-opendisplay/commit/28c24f9622ee900ffca08f91197a79e69e1db5bc))
* support reauthentication ([3214ead](https://github.com/OpenDisplay/py-opendisplay/commit/3214ead07f4c4f48ede373764fb84f84993541c8))

## [5.6.0](https://github.com/OpenDisplay/py-opendisplay/compare/v5.5.0...v5.6.0) (2026-03-24)


### Features

* add python 14 to supported versions ([2d97f2f](https://github.com/OpenDisplay/py-opendisplay/commit/2d97f2f334fe76204ef1f0eb1a82d5a019c8147f))


### Bug Fixes

* respect device supports_zip when choosing upload protocol ([5320a5a](https://github.com/OpenDisplay/py-opendisplay/commit/5320a5a6a7a6ebbb34a78e8eac813cb3db8c3fc5))

## [5.5.0](https://github.com/OpenDisplay/py-opendisplay/compare/v5.4.0...v5.5.0) (2026-03-07)


### Features

* add py.typed marker and fix strict mypy errors ([959176b](https://github.com/OpenDisplay/py-opendisplay/commit/959176bcd275591f5d9bfbcc2922ae363f12ad3b))
* add py.typed marker and fix strict mypy errors and add prek ([c1cf3b4](https://github.com/OpenDisplay/py-opendisplay/commit/c1cf3b4aff940518c410236e3f0fec8ef44bdb4e))

## [5.4.0](https://github.com/OpenDisplay/py-opendisplay/compare/v5.3.0...v5.4.0) (2026-03-06)


### Features

* add voltage_to_percent function ([fd93f82](https://github.com/OpenDisplay/py-opendisplay/commit/fd93f8243e636e943080882b1a1d2ee7a035d5e1))

## [5.3.0](https://github.com/OpenDisplay/py-opendisplay/compare/v5.2.0...v5.3.0) (2026-03-06)


### Features

* **models:** update config models for firmware spec v1.1 ([5bfccea](https://github.com/OpenDisplay/py-opendisplay/commit/5bfccea1d4a528dfcea7839f7d0c35ac421a5cd4))

## [5.2.0](https://github.com/OpenDisplay/py-opendisplay/compare/v5.1.0...v5.2.0) (2026-03-06)


### Features

* Add calibration preset for panel_ic_type 55 ([2852550](https://github.com/OpenDisplay/py-opendisplay/commit/285255061ff1189ca7c399d310b33729f5a6cfb6))
* Add is_flex property to OpenDisplayDevice ([031a778](https://github.com/OpenDisplay/py-opendisplay/commit/031a7780616e9a475279851304aea88b48ab60d7))

## [5.1.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v5.0.0...v5.1.0) (2026-02-18)


### Features

* add way of calling prepare_image() without async context ([da53f7e](https://github.com/OpenDisplay-org/py-opendisplay/commit/da53f7eaba02f53e7ea5ffe992f9859d33a78d8c))

## [5.0.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v4.4.0...v5.0.0) (2026-02-15)


### ⚠ BREAKING CHANGES

* change upload rotation semantics to be clockwise

### Features

* change upload rotation semantics to be clockwise ([d350f02](https://github.com/OpenDisplay-org/py-opendisplay/commit/d350f0251c2bc473f53accd5a62073080fcb32ac))

## [4.4.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v4.3.0...v4.4.0) (2026-02-15)


### Features

* **upload:** add enum-based per-image rotation before fit in pipeline ([38a06bc](https://github.com/OpenDisplay-org/py-opendisplay/commit/38a06bcddc4d65a85c06d1f1790b4033a163fabb))

## [4.3.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v4.2.0...v4.3.0) (2026-02-14)


### Features

* **advertisements:** add v1 button tracker ([386b67a](https://github.com/OpenDisplay-org/py-opendisplay/commit/386b67a003fe0ddd12f8fcc57bc5f5c04c047f2a))
* **config:** expose binary input button_data_byte_index and align 0x25 layout ([5981cfd](https://github.com/OpenDisplay-org/py-opendisplay/commit/5981cfd8df4537ef0c236858a02b8cf05a6fa227))
* **config:** support wifi_config packet (0x26) ([8de1e96](https://github.com/OpenDisplay-org/py-opendisplay/commit/8de1e96e2b3e3f584098764c31459c2eb16e08e6))
* **protocol:** add typed LED activate API and handle legacy wifi config packet ([ed9676a](https://github.com/OpenDisplay-org/py-opendisplay/commit/ed9676aa2e172de6f10b1adcf352c183d0c51774))
* **protocol:** support firmware v1 config/advertisement updates ([9e929ed](https://github.com/OpenDisplay-org/py-opendisplay/commit/9e929eddb3f193dd36bed731d092970a8402382d))

## [4.2.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v4.1.0...v4.2.0) (2026-02-14)


### Features

* return processed preview image from upload_image ([54d6ddc](https://github.com/OpenDisplay-org/py-opendisplay/commit/54d6ddcf1736b6d151d70ce9103191d34715fad8))

## [4.1.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v4.0.0...v4.1.0) (2026-02-13)


### Features

* require at least one display in config parse and JSON import ([758d7a6](https://github.com/OpenDisplay-org/py-opendisplay/commit/758d7a6caa5d18043a65aa9480b622a775e7a3b0))


### Bug Fixes

* resolve display color scheme enum using from_value ([a2931db](https://github.com/OpenDisplay-org/py-opendisplay/commit/a2931db06f69e85528ca1ec40db3823d86d6779f))

## [4.0.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v3.2.0...v4.0.0) (2026-02-13)


### ⚠ BREAKING CHANGES

* make core config packets non-optional across models and serialization

### Features

* add typed board type mappings and lookup helpers ([2731ee1](https://github.com/OpenDisplay-org/py-opendisplay/commit/2731ee1bf3371af70329f0decef8407f41f11c4c))
* enforce required config packets for parse and write ([6f2d59d](https://github.com/OpenDisplay-org/py-opendisplay/commit/6f2d59d740cc14510e7109b84aa5621920b605b6))
* make core config packets non-optional across models and serialization ([3749da6](https://github.com/OpenDisplay-org/py-opendisplay/commit/3749da6baf94d1be2d41328d4226522a14f7d6ac))

## [3.2.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v3.1.0...v3.2.0) (2026-02-13)


### Features

* add display diagonal inches computed property ([538a604](https://github.com/OpenDisplay-org/py-opendisplay/commit/538a6041f5f4dd383204a7ebfac25879b554e581))

## [3.1.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v3.0.0...v3.1.0) (2026-02-13)


### Features

* add typed board manufacturer API and docs ([a79eb8d](https://github.com/OpenDisplay-org/py-opendisplay/commit/a79eb8d9d1c9c2ed440a77930115d6c7b90f84f8))

## [3.0.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v2.5.1...v3.0.0) (2026-02-11)


### ⚠ BREAKING CHANGES

* add FitMode image fitting strategies (contain, cover, crop, stretch)

### Features

* add FitMode image fitting strategies (contain, cover, crop, stretch) ([786c614](https://github.com/OpenDisplay-org/py-opendisplay/commit/786c6144f6524f75ddb90013fa15c0d06dd1ad7f))

## [2.5.1](https://github.com/OpenDisplay-org/py-opendisplay/compare/v2.5.0...v2.5.1) (2026-02-11)


### Bug Fixes

* bump epaper-dithering version ([2dfd846](https://github.com/OpenDisplay-org/py-opendisplay/commit/2dfd8461ae6e02fe3e01c3f5cbf3b22af8b12049))

## [2.5.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v2.4.0...v2.5.0) (2026-02-11)


### Features

* bump epaper-dithering version and expose tone compression option ([c7d3dd1](https://github.com/OpenDisplay-org/py-opendisplay/commit/c7d3dd1cde26c81883793686e019360741004039))

## [2.4.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v2.3.0...v2.4.0) (2026-02-09)


### Features

* bump epaper-dithering version to 0.5.1 ([9ff01ba](https://github.com/OpenDisplay-org/py-opendisplay/commit/9ff01bad1620937cac7d671a23037cbfa81451b9))

## [2.3.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v2.2.0...v2.3.0) (2026-02-04)


### Features

* **epaper-dithering:** bump version for corrected palette ([2a2d5ff](https://github.com/OpenDisplay-org/py-opendisplay/commit/2a2d5ff7a72b7cb49d54ab82581275af591df67f))

## [2.2.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v2.1.0...v2.2.0) (2026-02-03)


### Features

* **palettes:** add automatic measured palette selection ([ae35b26](https://github.com/OpenDisplay-org/py-opendisplay/commit/ae35b26fd83fcbc65ccd8444fc29b5a258b4e865))

## [2.1.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v2.0.0...v2.1.0) (2026-01-12)


### Features

* add device reboot command (0x000F) ([147371d](https://github.com/OpenDisplay-org/py-opendisplay/commit/147371d617e92e7cf3d425c86608641eeabeea7b))
* **config:** add device configuration writing with JSON import/export ([4665d98](https://github.com/OpenDisplay-org/py-opendisplay/commit/4665d98eccfdc90b419f092f43d0c03a34471860))

## [2.0.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v1.0.0...v2.0.0) (2026-01-11)


### ⚠ BREAKING CHANGES

* Dithering functionality moved to standalone epaper-dithering package

### Code Refactoring

* extract dithering to epaper-dithering package ([95aa3c1](https://github.com/OpenDisplay-org/py-opendisplay/commit/95aa3c1700cad438b36146443e01f1fb8bcb00f3))

## [1.0.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v0.3.0...v1.0.0) (2026-01-09)


### ⚠ BREAKING CHANGES

* **connection:** Removed get_device_lock from public API. The global per-device lock mechanism has been removed in favor of simpler single-instance usage pattern.

### Features

* **connection:** integrate bleak-retry-connector for reliable connections ([088c187](https://github.com/OpenDisplay-org/py-opendisplay/commit/088c187c32e6b6564ef5d12184dbe57976e60cf7))

## [0.3.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v0.2.1...v0.3.0) (2025-12-30)


### Features

* add sha to firmware version parsing ([a47d58e](https://github.com/OpenDisplay-org/py-opendisplay/commit/a47d58e07d20b232b65b56d74edef64477497a37))


### Bug Fixes

* fix compressed image upload with chunking ([5d1b48a](https://github.com/OpenDisplay-org/py-opendisplay/commit/5d1b48a3ce8e7fae27526fa1d7495f81ef0bd65d)), closes [#5](https://github.com/OpenDisplay-org/py-opendisplay/issues/5)


### Documentation

* add git commit SHA documentation ([0e2ef50](https://github.com/OpenDisplay-org/py-opendisplay/commit/0e2ef50b7b813e87aceaa7620e7759325dcce0e7))

## [0.2.1](https://github.com/OpenDisplay-org/py-opendisplay/compare/v0.2.0...v0.2.1) (2025-12-30)


### Bug Fixes

* correct advertisement data ([ec152ba](https://github.com/OpenDisplay-org/py-opendisplay/commit/ec152ba53ec7c543957db2b6f618f4485c927b68))


### Documentation

* improve README.md ([e90a612](https://github.com/OpenDisplay-org/py-opendisplay/commit/e90a6128fe20b1bbbfcbaa0b303c72e8ab5359d8))

## [0.2.0](https://github.com/OpenDisplay-org/py-opendisplay/compare/v0.1.1...v0.2.0) (2025-12-29)


### Features

* add more dithering algorithms ([1b2fc6a](https://github.com/OpenDisplay-org/py-opendisplay/commit/1b2fc6aeef3ef6c3b81e0c23855d38a61e00a62b))

## [0.1.1](https://github.com/OpenDisplay-org/py-opendisplay/compare/v0.1.0...v0.1.1) (2025-12-29)


### Bug Fixes

* add conftest ([673db99](https://github.com/OpenDisplay-org/py-opendisplay/commit/673db99bfa85608a2d5bcdea1a36d37b25e76b51))

## 0.1.0 (2025-12-29)


### Features

* add discovery function ([2760ef9](https://github.com/OpenDisplay-org/py-opendisplay/commit/2760ef913440b8689bdc6c39d09050fc5f757b64))

## 0.1.0 (2025-12-29)

### Features

* Initial release of py-opendisplay
* BLE device discovery with `discover_devices()` function
* Connect by device name or MAC address
* Automatic image upload with compression support
* Device interrogation and capability detection
* Image resize warnings for automatic resizing
* Support for multiple color schemes (BW, BWR, BWY, BWRY, BWGBRY, GRAYSCALE_4)
* Firmware version reading
* TLV config parsing for OpenDisplay protocol

### Documentation

* Quick start guide with examples
* API documentation
* Image resizing behavior documentation
