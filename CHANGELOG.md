# Changelog

## [7.11.2](https://github.com/OpenDisplay/py-opendisplay/compare/v7.11.1...v7.11.2) (2026-07-06)


### Bug Fixes

* actually lock epaper-dithering 5.0.9 (stale uv index cache) ([a906119](https://github.com/OpenDisplay/py-opendisplay/commit/a9061199da180b19909938be8e151c81bd033ac0))
* bump epaper-dithering to 5.0.9 (FFI validation hardening) ([a13fe74](https://github.com/OpenDisplay/py-opendisplay/commit/a13fe74635065489dfd97780fef9fdb0c3cf29c2))
* bump epaper-dithering to 5.0.9 (FFI validation hardening) ([e1a29ec](https://github.com/OpenDisplay/py-opendisplay/commit/e1a29ec767935cff29733f9d74a8caa252726085))

## [7.11.1](https://github.com/OpenDisplay/py-opendisplay/compare/v7.11.0...v7.11.1) (2026-07-06)


### Bug Fixes

* bump epaper-dithering to 5.0.8 (tone=auto NaN collapse) ([4981bb7](https://github.com/OpenDisplay/py-opendisplay/commit/4981bb78dc56ce46a570c8e921389562d8c69e4c))
* bump epaper-dithering to 5.0.8 (tone=auto NaN collapse) ([b229c36](https://github.com/OpenDisplay/py-opendisplay/commit/b229c36a4e762b2f8e8ce2d43b7160f19547ba41))

## [7.11.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.10.0...v7.11.0) (2026-07-06)


### Features

* export PartialState and document partial updates ([dbeb369](https://github.com/OpenDisplay/py-opendisplay/commit/dbeb369700abbc3192f8aea108ec3eed8510c8e6))
* export PartialState and document partial updates ([1bf6ecf](https://github.com/OpenDisplay/py-opendisplay/commit/1bf6ecfdc4d703f49860f609610f4e209f45abfa))
* honor partial_update_support=2 (full-frame partial stream) ([8b89f33](https://github.com/OpenDisplay/py-opendisplay/commit/8b89f3307523eda5101b3db7fa5e143928ff392d))
* honor partial_update_support=2 (full-frame partial stream) ([8d3d4a2](https://github.com/OpenDisplay/py-opendisplay/commit/8d3d4a2f5581c6cd01a0789a3576e09091ddfbd5))
* warn about firmware upload limitations for BWR/BWY and non-aligned widths (C1/C2) ([84bf22c](https://github.com/OpenDisplay/py-opendisplay/commit/84bf22c04b1a3c007986168c0ca02bd46f115d5b))
* warn about firmware upload limitations for BWR/BWY and non-aligned widths (C1/C2) ([8c130fe](https://github.com/OpenDisplay/py-opendisplay/commit/8c130feb14e930473d32a6a3185833698dd77b9f))


### Bug Fixes

* always compress image uploads with a 9-bit zlib window (C3) ([7e38eae](https://github.com/OpenDisplay/py-opendisplay/commit/7e38eae7a92bdc46e80d3abe803c4528c0fd869c))
* always compress image uploads with a 9-bit zlib window (C3) ([c56e003](https://github.com/OpenDisplay/py-opendisplay/commit/c56e003d4d94750edef70d0c6ab235db953c4f0a))
* apply the streaming-decompression gate to partial stream compression too ([d8cab8a](https://github.com/OpenDisplay/py-opendisplay/commit/d8cab8ac6a2122624054e14fe12b5a207e46acfc))
* BWR red sets both bitplanes to match firmware and website ([fda1f8e](https://github.com/OpenDisplay/py-opendisplay/commit/fda1f8e1ca8310ea051c53caa580bb6c414803ae))
* BWR red sets both bitplanes to match firmware and website ([ab2fe6a](https://github.com/OpenDisplay/py-opendisplay/commit/ab2fe6a84707ddcdea8295727a0e68016f56d59d))
* carry across octets when computing the nRF DFU MAC (+1) (§4) ([88ae351](https://github.com/OpenDisplay/py-opendisplay/commit/88ae3513fe347379929f578318b1c0ce68842a00))
* carry across octets when computing the nRF DFU MAC (+1) (§4) ([48b8b9c](https://github.com/OpenDisplay/py-opendisplay/commit/48b8b9cdc69abb5e78e220227fe6c07c2d9bb886))
* compress uploads for streaming-decompression-only configs; rename ZIPXL ([97a54e6](https://github.com/OpenDisplay/py-opendisplay/commit/97a54e68d8842c3a4ef54100be78e782f1b4d236))
* compress uploads for streaming-decompression-only configs; rename ZIPXL ([446641b](https://github.com/OpenDisplay/py-opendisplay/commit/446641b7856cab49d45dad6670f74804823b0d4b))
* config serialization correctness — padding, mfr metadata, sizes, tx_power ([5ff94a9](https://github.com/OpenDisplay/py-opendisplay/commit/5ff94a90f50ccd4050efccb95a1e093b074e254c))
* config serialization correctness — padding, mfr metadata, sizes, tx_power (C7/C8/M5/M6) ([8bd3504](https://github.com/OpenDisplay/py-opendisplay/commit/8bd3504162c0adbb155c64a38c868307d7277250))
* drain stale notifications and recover on read timeout (C6) ([e6fc38b](https://github.com/OpenDisplay/py-opendisplay/commit/e6fc38bc53b22dc59340c6039049d5a11baa2201))
* drain stale notifications and recover on read timeout (C6) ([d5467ba](https://github.com/OpenDisplay/py-opendisplay/commit/d5467bab28c95e660f034e27fb0fd3a0a161dffc))
* emit NFC config packets last so firmware keeps flash/data_extended (M4) ([f22dae1](https://github.com/OpenDisplay/py-opendisplay/commit/f22dae157ef14b3dda81a4c7a70bf1795998d755))
* emit NFC config packets last so firmware keeps flash/data_extended (M4) ([294f47b](https://github.com/OpenDisplay/py-opendisplay/commit/294f47be0139fab0e10a08ab2cf0b38356c0c212))
* minor model/encoding validation (§4) ([5f4bcd1](https://github.com/OpenDisplay/py-opendisplay/commit/5f4bcd1e6250985f6347852ab91439495cb19e0b))
* minor model/encoding validation (§4) ([8184ae5](https://github.com/OpenDisplay/py-opendisplay/commit/8184ae52686b0bb2265073d64c1aed83971a8927))
* **ota:** align nRF DFU MAC increment to nrf-ota no-carry convention + add discovery fallback ([0ed1128](https://github.com/OpenDisplay/py-opendisplay/commit/0ed1128cafb82624fcf02ea6ef90e4e7de7b4cf3))
* **ota:** align nRF DFU MAC increment to nrf-ota no-carry convention + add discovery fallback ([356575e](https://github.com/OpenDisplay/py-opendisplay/commit/356575e99a7e3068eb84edf9d1674dbaee81ff04))
* **ota:** drop name-based selection in find_nrf_dfu_device (match nrf-ota [#7](https://github.com/OpenDisplay/py-opendisplay/issues/7)) ([5d1dde5](https://github.com/OpenDisplay/py-opendisplay/commit/5d1dde53fe4230bc056aa683e70e414fc514bb0b))
* partial-upload guards — MONO-only, 8-align, NACK fallback, etag commit (M1/M2/M10) ([ce931ed](https://github.com/OpenDisplay/py-opendisplay/commit/ce931ede33d28dfe31b96c71c754f9a92b7f0c0e))
* partial-upload guards — MONO-only, 8-align, NACK fallback, etag commit (M1/M2/M10) ([63730ee](https://github.com/OpenDisplay/py-opendisplay/commit/63730ee8b1aaa009e9cdc9152182b58dba9ca3f3))
* per-panel BWRY code table + 4-gray table additions (M3) ([68ef815](https://github.com/OpenDisplay/py-opendisplay/commit/68ef8155eb3f69382e5644cdc5a99403b72ff91e))
* per-panel BWRY code table + 4-gray table additions (M3) ([c47608c](https://github.com/OpenDisplay/py-opendisplay/commit/c47608c30d97dc9f9534adfb44e63697dc6e0183))
* preserve real config data through JSON export/import (M7) ([59f4160](https://github.com/OpenDisplay/py-opendisplay/commit/59f41605e758372663473169b60c45973f939c17))
* preserve real config data through JSON export/import (M7) ([2c86017](https://github.com/OpenDisplay/py-opendisplay/commit/2c86017e9ff4cac0346c4e0ff92d9a386895141b))
* Raise IntegrityCheckError on decrypt/integrity-failure frame ([742adad](https://github.com/OpenDisplay/py-opendisplay/commit/742adad9644ba178cff5e78fa1665ef7724bd843))
* serialize device commands and clear session on disconnect (C5, M9) ([4b99477](https://github.com/OpenDisplay/py-opendisplay/commit/4b99477918061e4e3a8af22aa0167659c7c23198))
* serialize device commands and clear session on disconnect (C5, M9) ([bf72bd7](https://github.com/OpenDisplay/py-opendisplay/commit/bf72bd7cf7ae2d20dbda60a40f877c3ec501d627))
* surface device error frames with typed exceptions (§4) ([376d118](https://github.com/OpenDisplay/py-opendisplay/commit/376d1189f7b799d65ed1625f116774efd90e0cbc))
* surface device error frames with typed exceptions (§4) ([63a28f5](https://github.com/OpenDisplay/py-opendisplay/commit/63a28f5b6bdfd9ad4d80e3d7c0c3e8693ad0f3d7))
* undefined DEFAULT_ZLIB_WINDOW_BITS in deferred compression path ([45d21fa](https://github.com/OpenDisplay/py-opendisplay/commit/45d21fa333aba5235e179ee1b4d15f8c4d1f476f))
* undefined DEFAULT_ZLIB_WINDOW_BITS in deferred compression path ([abacf4c](https://github.com/OpenDisplay/py-opendisplay/commit/abacf4cb42e6736516e0d12ea46afee4fb365a27))
* use CRC-16/CCITT for config CRC to match firmware/toolbox ([aad11ec](https://github.com/OpenDisplay/py-opendisplay/commit/aad11ec381cc0e308035a70a6540b60022489e32))
* validate LED group_repeats 1-254 and tolerate raw 0xFF on parse (M11) ([299c18c](https://github.com/OpenDisplay/py-opendisplay/commit/299c18c1e50e8def412fcea4e831814d9a5c14ce))
* validate LED group_repeats 1-254 and tolerate raw 0xFF on parse (M11) ([9d7134e](https://github.com/OpenDisplay/py-opendisplay/commit/9d7134e70a1cbbd6db5537877aae6367b6c2b3c3))
* verify the device's mutual-auth server proof (M8) ([ce6ac6f](https://github.com/OpenDisplay/py-opendisplay/commit/ce6ac6ff6de836f7e0d8987019b76af1e05c45e8))
* verify the device's mutual-auth server proof (M8) ([ba89249](https://github.com/OpenDisplay/py-opendisplay/commit/ba89249250b5d2d56ea6f9be7a934422df78445d))
* write_config first chunk must carry 200 data bytes (C4) ([388a372](https://github.com/OpenDisplay/py-opendisplay/commit/388a372e3a1696f6b2776c1875165b0745cee136))
* write_config first chunk must carry 200 data bytes (C4) ([40835a9](https://github.com/OpenDisplay/py-opendisplay/commit/40835a9328f67d674bb9f244f8d77f93c49be2f0))


### Performance Improvements

* defer full-frame compression when a partial upload may succeed ([c8b398a](https://github.com/OpenDisplay/py-opendisplay/commit/c8b398a26ede48ef94a31880913e408cc7261886))
* defer full-frame compression when a partial upload may succeed ([fbbf95e](https://github.com/OpenDisplay/py-opendisplay/commit/fbbf95ee802d42cbd3b55038c279820187f9bd0f))
* vectorize image and bitplane encoders ([f9a39fe](https://github.com/OpenDisplay/py-opendisplay/commit/f9a39fe0c7330cfd08fa10c4746ea9585bbfe477))
* vectorize image and bitplane encoders ([0a16768](https://github.com/OpenDisplay/py-opendisplay/commit/0a16768ecae90dc2ff03433c4f1f3b67daaf8fd8))
* vectorize partial-update bounding rect and segment encoder ([068f7f6](https://github.com/OpenDisplay/py-opendisplay/commit/068f7f664cb15c129e656001f1ca592f5b4ec727))
* vectorize partial-update bounding rect and segment encoder ([5483ad6](https://github.com/OpenDisplay/py-opendisplay/commit/5483ad66e0fc52638cd930f6a38d9fbaf3186c08))

## [7.10.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.9.0...v7.10.0) (2026-07-03)


### Features

* Add adc_ladder() factory for ADC resistor-ladder binary inputs ([dab93b1](https://github.com/OpenDisplay/py-opendisplay/commit/dab93b19b5214b894e1866ba876371a33ca99064))
* add Seeed board types 9-13 from live config tool ([bbbfa49](https://github.com/OpenDisplay/py-opendisplay/commit/bbbfa49fa6010942033f54173e1696ce26e6af80))
* parse data_extended (0x2c) identity packet ([1116efe](https://github.com/OpenDisplay/py-opendisplay/commit/1116efee00d27be69d20e3cd0fe08160abbbd2bf))

## [7.9.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.8.0...v7.9.0) (2026-06-07)


### Features

* Introduce 512-byte compression window for ZIPXL ([5afed09](https://github.com/OpenDisplay/py-opendisplay/commit/5afed09bc8aeb920fb6cc72b215c53d5748be28f))

## [7.8.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.7.0...v7.8.0) (2026-06-03)


### Features

* add landing_url() device deep-link encoder ([72a8637](https://github.com/OpenDisplay/py-opendisplay/commit/72a8637d3a028d8fd571336770e222881acb5b94))

## [7.7.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.6.0...v7.7.0) (2026-06-03)


### Features

* parse nfc_config (0x2a) and flash_config (0x2b) packets ([4807bf0](https://github.com/OpenDisplay/py-opendisplay/commit/4807bf0eb0ce2bf88f2e112ac01b454ca8052154))

## [7.6.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.5.0...v7.6.0) (2026-06-02)


### Features

* sync Solum/OpenDisplay board types with config tool ([7002029](https://github.com/OpenDisplay/py-opendisplay/commit/700202998dd44a7ff495c0f62b39f3d4918d6a5c))
* sync Solum/OpenDisplay board types with config tool ([1872016](https://github.com/OpenDisplay/py-opendisplay/commit/18720167c1582c4ca292ac1f2caf1db6d3bea3ac))

## [7.5.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.4.1...v7.5.0) (2026-06-02)


### Features

* BLE OTA firmware updates over Bluetooth proxies ([71c9062](https://github.com/OpenDisplay/py-opendisplay/commit/71c90627f7908a62e62184196c139f58b6eb8cb5))

## [7.4.1](https://github.com/OpenDisplay/py-opendisplay/compare/v7.4.0...v7.4.1) (2026-05-30)


### Bug Fixes

* Allow uncompressed transport for 4-gray uploads ([d3470c6](https://github.com/OpenDisplay/py-opendisplay/commit/d3470c6d9033ad3e0e9f940919fa6e7739f62930))
* serialize_display_config dropping full_update_mC ([09bbc0e](https://github.com/OpenDisplay/py-opendisplay/commit/09bbc0e87feee8fb443968789a9666f174eb718f))

## [7.4.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.3.2...v7.4.0) (2026-05-29)


### Features

* add BLE OTA firmware update support for nRF devices ([40db8d7](https://github.com/OpenDisplay/py-opendisplay/commit/40db8d7b96db52ea2652768f4c654e886d64e142))
* add Silabs EFR32BG22 OTA support and document firmware update paths ([271742a](https://github.com/OpenDisplay/py-opendisplay/commit/271742a42f545233d1986d75f42e61a45fbfdc1f))
* encode GRAYSCALE_4 as two 1-bit planes host-side ([12b7e0e](https://github.com/OpenDisplay/py-opendisplay/commit/12b7e0ef61e03cb871de9a52eb6b96f085949123))


### Documentation

* add macOS OTA limitation note to README ([f6ff268](https://github.com/OpenDisplay/py-opendisplay/commit/f6ff268794b2943321752083ad97cf6f6e8b2728))

## [7.3.2](https://github.com/OpenDisplay/py-opendisplay/compare/v7.3.1...v7.3.2) (2026-05-25)


### Bug Fixes

* change buzzer command ID from 0x0075 to 0x0077, remove firmware version guard ([2695351](https://github.com/OpenDisplay/py-opendisplay/commit/26953516e2c52affbaf62313ce95e653ca9ab148))

## [7.3.1](https://github.com/OpenDisplay/py-opendisplay/compare/v7.3.0...v7.3.1) (2026-05-25)


### Bug Fixes

* increase uncompressed END ACK timeout to 90s ([0bf6868](https://github.com/OpenDisplay/py-opendisplay/commit/0bf6868b2527332a9ed2af815cb3e279fbf55a2f))
* update test to expect TIMEOUT_UNCOMPRESSED_END_ACK for end ACK ([229bd3e](https://github.com/OpenDisplay/py-opendisplay/commit/229bd3ec3e25d34d98ddbbfecb683c26b7c7bb3e))

## [7.3.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.2.5...v7.3.0) (2026-05-25)


### Features

* add BuzzerActivateConfig for command 0x0075 ([a67a07c](https://github.com/OpenDisplay/py-opendisplay/commit/a67a07c0fc19c50c08f38c62f5c51f75b1710cd2))
* add EFR32BG22 to ICType enum ([6eb020c](https://github.com/OpenDisplay/py-opendisplay/commit/6eb020ceb95f1dc9155f3b3654e94a8bf02da90e))
* add firmware_release_repo() mapping by IC type ([a5a0454](https://github.com/OpenDisplay/py-opendisplay/commit/a5a045432c7b7d87d4718c4a8bc18ea7b32455b7))
* implement activate_buzzer() BLE command (0x0075) ([b809423](https://github.com/OpenDisplay/py-opendisplay/commit/b809423f132089f4c3a80ceff8d08cf0545582af))

## [7.2.5](https://github.com/OpenDisplay/py-opendisplay/compare/v7.2.4...v7.2.5) (2026-05-23)


### Bug Fixes

* **ci:** drop release-please config file, use inline release-type only ([8bd98d1](https://github.com/OpenDisplay/py-opendisplay/commit/8bd98d1da9d5113c816ad2dc1bb917fffbda4fca))
* **ci:** remove package-name from release-please config to preserve v-prefix tags ([3eac811](https://github.com/OpenDisplay/py-opendisplay/commit/3eac811de005040a64e95fe56db0356aeb3bc38d))
* **ci:** scope uv cache key per Python version to avoid matrix race ([40aa266](https://github.com/OpenDisplay/py-opendisplay/commit/40aa26623e56405febf383153cbeff46434e5385))

## [7.2.4](https://github.com/OpenDisplay/py-opendisplay/compare/v7.2.3...v7.2.4) (2026-05-23)


### Bug Fixes

* **battery:** update voltage tables for CR2450 and 2s supercap packs with PMIC ([d2f231a](https://github.com/OpenDisplay/py-opendisplay/commit/d2f231aa7d5f4bba13c0693bc5615f97300be1a3))
* correct test voltage values and typo for new battery tables ([5971081](https://github.com/OpenDisplay/py-opendisplay/commit/59710819dbf43b9af908d217cee044bc6f3ac553))

## [7.2.3](https://github.com/OpenDisplay/py-opendisplay/compare/v7.2.2...v7.2.3) (2026-05-21)


### Bug Fixes

* bump epaper-dithering to 5.0.6 (fixes missing manylinux x86_64 wheels) ([d6a43b9](https://github.com/OpenDisplay/py-opendisplay/commit/d6a43b90bbe4d9ca5c153c049d7d633ae4cbede9))

## [7.2.2](https://github.com/OpenDisplay/py-opendisplay/compare/v7.2.1...v7.2.2) (2026-05-21)


### Bug Fixes

* bump epaper-dithering to 5.0.5 (adds musllinux aarch64 wheels) ([4c6d87c](https://github.com/OpenDisplay/py-opendisplay/commit/4c6d87c3161476e081a9d95d0efd7a2ea5fa2de4))

## [7.2.1](https://github.com/OpenDisplay/py-opendisplay/compare/v7.2.0...v7.2.1) (2026-05-21)


### Bug Fixes

* bump epaper-dithering to 5.0.4 (adds musllinux x86_64 wheels) ([b298c87](https://github.com/OpenDisplay/py-opendisplay/commit/b298c8749940262b4df4ec4abbd89742fe725385))
* remove pyrefly from runtime dependencies ([0ec0e0d](https://github.com/OpenDisplay/py-opendisplay/commit/0ec0e0d29c80b2091eea6e4cb7c5086ab74e319e))

## [7.2.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.1.0...v7.2.0) (2026-05-19)


### Features

* Add touch event parsing and TouchTracker to advertisement module ([64f1580](https://github.com/OpenDisplay/py-opendisplay/commit/64f158040431ef3d5d8015ea2c9fbcbe36f06602))

## [7.1.0](https://github.com/OpenDisplay/py-opendisplay/compare/v7.0.0...v7.1.0) (2026-05-19)


### Features

* add partial update support ([449e52f](https://github.com/OpenDisplay/py-opendisplay/commit/449e52f1475bd44b4967d4c73dba5c1ccb8a67cf))

## [7.0.0](https://github.com/OpenDisplay/py-opendisplay/compare/v6.1.1...v7.0.0) (2026-05-18)


### ⚠ BREAKING CHANGES

* depends on epaper-dithering 5.0.0 which swapped GRAYSCALE_8 and GRAYSCALE_16 firmware values to match the actual firmware convention (GRAYSCALE_16=6, GRAYSCALE_8=7 reserved).

### Bug Fixes

* bump epaper-dithering to 5.0.0 and fix GRAYSCALE_16 encoding ([b964696](https://github.com/OpenDisplay/py-opendisplay/commit/b96469667861386bbf749881dda025151f718e97))

## [6.1.1](https://github.com/OpenDisplay/py-opendisplay/compare/v6.1.0...v6.1.1) (2026-05-18)


### Bug Fixes

* add missing ColorScheme.GRAYSCALE_8 encoding ([ac4bf3d](https://github.com/OpenDisplay/py-opendisplay/commit/ac4bf3dc5e78ea81c18c49e2a35714b41f913869))

## [6.1.0](https://github.com/OpenDisplay/py-opendisplay/compare/v6.0.0...v6.1.0) (2026-05-06)


### Features

* pin epaper dithering 4.1 ([3fe27d8](https://github.com/OpenDisplay/py-opendisplay/commit/3fe27d8179145e596eb2f8058c2340b1432269b5))

## [6.0.0](https://github.com/OpenDisplay/py-opendisplay/compare/v5.9.0...v6.0.0) (2026-05-03)


### ⚠ BREAKING CHANGES

* migrate to epaper-dithering v4 API

### Features

* migrate to epaper-dithering v4 API ([91d3c23](https://github.com/OpenDisplay/py-opendisplay/commit/91d3c235ea9fc66a9fd658506f515afe41f920b1))


### Bug Fixes

* increase uncompressed data chunk ACK timeout for Spectra/ACeP displays ([8892ec2](https://github.com/OpenDisplay/py-opendisplay/commit/8892ec23f2f16389adc5aae40b20947c169561bd))
* reliable upload across encrypted/slow-display device types ([6740626](https://github.com/OpenDisplay/py-opendisplay/commit/6740626bd8d941f7f1733fc4798d53796e3148b3))
* reliable upload across encrypted/slow-display device types ([ab1b636](https://github.com/OpenDisplay/py-opendisplay/commit/ab1b636243d18c735ca0ebae7dbe4ad91666b2c8))

## [5.9.0](https://github.com/OpenDisplay/py-opendisplay/compare/v5.8.2...v5.9.0) (2026-04-02)


### Features

* add GRAYSCALE_16 (4bpp) encoding support ([9b5983b](https://github.com/OpenDisplay/py-opendisplay/commit/9b5983ba01ed064325828e97b37efd1b0eebff6e))

## [5.8.2](https://github.com/OpenDisplay/py-opendisplay/compare/v5.8.1...v5.8.2) (2026-04-02)


### Bug Fixes

* bump epaper-dithering version to support GRAYSCALE_8 and GRAYSCALE_16 ([eb80b94](https://github.com/OpenDisplay/py-opendisplay/commit/eb80b94a0c4e0b5418546cd35e435f020751caf2))

## [5.8.1](https://github.com/OpenDisplay/py-opendisplay/compare/v5.8.0...v5.8.1) (2026-04-02)


### Bug Fixes

* add refresh display state ([947686e](https://github.com/OpenDisplay/py-opendisplay/commit/947686e81d2051b2d596d76894cdca68b6046ad1))
* apply device config rotation additively during image upload ([6c1363f](https://github.com/OpenDisplay/py-opendisplay/commit/6c1363fce2ea364c77b7ff153b30a2ee6eb6cadb))
* fix encryption ([fd2ed20](https://github.com/OpenDisplay/py-opendisplay/commit/fd2ed207a2723fccbe088569f482a343764c2b77))
* progress bar starts at left edge ([d580f17](https://github.com/OpenDisplay/py-opendisplay/commit/d580f17c6112c027c784fc433662052ec531b2c2))
* show rotation degrees correctly ([1f67c5b](https://github.com/OpenDisplay/py-opendisplay/commit/1f67c5b01b4fa10467c784c90e6fcec4126f0080))

## [5.8.0](https://github.com/OpenDisplay/py-opendisplay/compare/v5.7.0...v5.8.0) (2026-04-01)


### Features

* add cli ([5178320](https://github.com/OpenDisplay/py-opendisplay/commit/517832062a29fec0fe5c038bffe117a4fa569057))
* add default file path to export-config cli command ([e1d52ca](https://github.com/OpenDisplay/py-opendisplay/commit/e1d52caef0a4f220711847f55b1f8f4c7edc74db))
* protocol 1.2 support ([866bfce](https://github.com/OpenDisplay/py-opendisplay/commit/866bfcea3ba75f7b2b35b16580085dcfdff57f2b))

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
