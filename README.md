# Thingy:52 button light + OTA

Custom firmware for the Nordic Thingy:52. The lightwell LED stays on (white) while the button is held, and goes back to its normal pattern when it is released. All sensors stream over BLE: environment (temperature, pressure, humidity, air quality, colour), battery, and motion. `tools/dashboard.py` shows them live in the terminal, for several Thingys at once, and `tools/sensors.py` prints and logs them. The firmware can be updated over BLE (Secure DFU). Updates are signed with our own key and checked by our own bootloader.

It is Nordic's official Thingy firmware with a small patch on top. The base is [Nordic-Thingy52-FW](https://github.com/NordicSemiconductor/Nordic-Thingy52-FW) at `6ada0a9` (v2.2.0, nRF5 SDK 13, S132 4.0.2). Nordic's tree is not stored here: `tools/build.sh` fetches it at that commit and applies `patches/thingy-fw.patch`. Everything else is kept: the BLE services, the Thingy app support, sleep, and buttonless DFU.

## Devices

| Unit | BLE address | Running |
|---|---|---|
| A | E1:2A:60:8A:80:C1 | Our SoftDevice + bootloader, app **version 4**: button light plus all sensors including motion (FW Version `255.255.4`). Its factory FW 2.1.0 is in `backup/`. |
| B | EA:BF:EE:F7:76:21 | Our SoftDevice + bootloader, app **version 2**: button light, no motion (FW Version `255.255.2`). Its previous firmware was bad; that copy is in `backup/`. |

Our app sleeps after 3 minutes of advertising without a connection; press the button to wake it.

## Layout

- `patches/thingy-fw.patch`: our changes to Nordic's firmware
- `tools/build.sh`: fetches and patches `thingy-fw/` (first run), builds the bootloader and app, packages them into `out/`
- `tools/mkpkg.py`: makes signed DFU packages (replaces nrfutil, which no longer runs on current Python)
- `tools/secure_dfu.py`: Secure DFU client over BLE (BlueZ via bleak); handles buttonless entry
- `tools/jlink.py`: SWD backup / flash / verify through J-Link Commander (nrfjprog can't open our J-Link)
- `tools/dashboard.py`: terminal dashboard with the latest value of every sensor, one column per Thingy
- `tools/sensors.py`: streams every sensor over BLE, prints it and optionally logs it to CSV
- `tools/gatt_dump.py`, `tools/button_watch.py`: BLE inspection helpers
- `backup/`: SWD read-outs of both units (see `backup/README.md`)
- `keys/dfu_public_key.c`: the public key compiled into our bootloader
- `keys/private.pem`: **DFU signing key. Not in git; keep your own backup.** Without it, the next update needs the cable again.
- Not in git: `thingy-fw/` (fetched) and `out/` (build output)

## Setup

Needs `git`, `arm-none-eabi-gcc` (tested with 16.2), `mergehex`, SEGGER `JLinkExe` and BlueZ.

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

## Build

    tools/build.sh 5        # app version 5 -> out/thingy_app_v5.zip

The app version must be at least the version on the device. It also shows up as the patch number of the FW Version characteristic. The build is reproducible: from a fresh checkout it gives the same images that were flashed onto the units.

To change the firmware, edit files in `thingy-fw/`, then record the changes:

    git -C thingy-fw diff > patches/thingy-fw.patch

`build.sh` warns if `thingy-fw/` and the patch don't match.

## Update over the air

    .venv/bin/python tools/secure_dfu.py out/thingy_app_v5.zip --addr E1:2A:60:8A:80:C1 --prn 8

The client reboots the app into the bootloader (`ThingyDfu`, address + 1), sends the package and waits until the bootloader has validated it.

**Recovery:** hold the button while switching the Thingy on. It then stays in the bootloader and waits for a package.

## Reading the sensors

### Live dashboard

    .venv/bin/python tools/dashboard.py                                          # every Thingy nearby
    .venv/bin/python tools/dashboard.py --addr E1:2A:60:8A:80:C1=A --addr EA:BF:EE:F7:76:21=B

One column per Thingy, with the latest value of every sensor and a sparkline of the last 10 minutes of temperature. A value that has not changed for 3 s or more shows its age. Keys: `q` quits and disconnects, `m` turns motion off and on. Off also unsubscribes, so the Thingys power their motion sensor down.

A Thingy accepts one connection, and while connected it doesn't advertise. Close `sensors.py`, the nRF app or another dashboard first, or the dashboard won't find it. The firmware reboots on every disconnect. The dashboard notices, finds the Thingy again when it advertises and reconnects, which takes about 6 s. A Thingy asleep after 3 minutes without a connection reappears when its button is pressed.

### Streaming and logging

    .venv/bin/python tools/sensors.py --addr E1:2A:60:8A:80:C1                   # everything
    .venv/bin/python tools/sensors.py --only temperature,humidity --csv log.csv   # subset, logged

The tool prints at most one line per sensor per second (`--interval 0` prints everything) and appends every notification to the CSV, one row per field. `--temp-interval MS` changes the temperature interval, and the Thingy stores the setting. The air-quality sensor reports 400 ppm eCO2 / 0 ppb TVOC until it has warmed up (about 20 minutes).

`--only` takes a comma-separated list of these names. Without it, the tool subscribes to all of them. The rates are the firmware defaults.

| `--only` name | Values | Arrives |
|---|---|---|
| `temperature` | temperature (°C) | every 2 s |
| `pressure` | air pressure (hPa) | every 2 s |
| `humidity` | relative humidity (%) | every 2 s |
| `gas` | eCO2 (ppm), TVOC (ppb) | every 10 s |
| `color` | red, green, blue, clear (raw sensor counts) | every 1.5 s |
| `battery` | battery level (%) | on connect, then when it changes |
| `button` | pressed / released | on press and on release |
| `tap` | direction (x+, x-, y+, y-, z+, z-), number of taps | on a tap |
| `orientation` | portrait, landscape, reverse portrait, reverse landscape | when it changes |
| `pedometer` | steps, walking time (ms) | when the step count changes |
| `quaternion` | orientation quaternion w, x, y, z | 10 per second |
| `raw` | accelerometer x, y, z (g), gyroscope (°/s), compass (µT) | 10 per second |
| `euler` | roll, pitch, yaw (°) | 10 per second |
| `rotation` | 3×3 rotation matrix | 10 per second |
| `heading` | compass heading (°) | 10 per second |
| `gravity` | gravity vector x, y, z (m/s²) | 10 per second |

The last six come from the motion processing. While any of them is subscribed, the Thingy keeps its motion sensor running.

## Putting it on a new unit (once, by cable)

The factory bootloader only accepts updates signed with Nordic's private key, which is not public. So each unit first needs our SoftDevice + bootloader flashed over SWD ([Nordic: generating your own packages](https://nordicsemiconductor.github.io/Nordic-Thingy52-FW/documentation/dfu_generating.html)). The SWD connector is the 2×5 1.27 mm header next to the micro-USB port (standard Cortex 10-pin: 1 VTref, 2 SWDIO, 3/5 GND, 4 SWCLK, 10 nRESET). The Thingy must be switched on.

    tools/build.sh 5                                               # creates out/ if it doesn't exist yet
    .venv/bin/python tools/jlink.py backup backup/thingy_<addr>   # read-only, keep the old firmware
    .venv/bin/python tools/jlink.py flash out/thingy_sd_bl.hex     # erases the chip
    .venv/bin/python tools/secure_dfu.py out/thingy_app_v5.zip --name ThingyDfu

## What the patch changes

- `source/modules/m_ui.c`, `include/modules/m_ui.h`: on button press the lightwell is set to `UI_BUTTON_HOLD_COLOR`; on release the LED goes back to the connected or disconnected configuration. Button notifications are unchanged.
- Motion sensors use the MPL library bundled in Nordic's tree (`libs/eMD6/core/mpl/libmpllib.a`). Nordic's own GCC build used InvenSense's hard-float download instead, which is behind a login. The bundled library was compiled with IAR without an FPU (soft-float), so its float calls use a different calling convention from this hard-float firmware. `build.sh` renames the library's 11 float-by-value calls (`sqrtf`, `atan2`, …) to the AAPCS shims in `source/util/mpl_aapcs_shims.c`, which convert the arguments and call the real functions. Checked on unit A: quaternion length 1.0000, gravity 9.81 m/s², orthonormal rotation matrix, and Euler yaw matching the quaternion.
- Motion builds with current GCC: three new warnings in InvenSense/Nordic motion code are not errors. `COMPASS_BIAS` in `drv_motion.c` uses `2^16` (XOR), but the factory firmware ships with that value, and the intended `*65536` would not fit in a `long`.
- Builds with current GCC: `-Wno-error=array-bounds` (false positives on SDK linker-section arrays) and no `RTT_Syscalls_GCC.c` (it clashes with current newlib; nothing prints to stdout).
- `THINGY_FW_VERSION_PATCH` can be set from the build, so builds can be told apart over BLE.

Beyond the patch, `build.sh` builds the bootloader with our public key (written from `keys/private.pem`). It also pins micro-ecc to kmackay/micro-ecc `541b3a7`; Nordic's `setup_sdk.sh` would otherwise clone whatever is latest.
