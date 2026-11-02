# Thingy:52 button light + OTA

Custom firmware for the Nordic Thingy:52. The lightwell LED stays on (white) while the button is held, and goes back to its normal pattern when it is released. The firmware can be updated over BLE (Secure DFU). Updates are signed with our own key and checked by our own bootloader.

It is Nordic's official Thingy firmware with a small patch on top. The base is [Nordic-Thingy52-FW](https://github.com/NordicSemiconductor/Nordic-Thingy52-FW) at `6ada0a9` (v2.2.0, nRF5 SDK 13, S132 4.0.2). Nordic's tree is not stored here: `tools/build.sh` fetches it at that commit and applies `patches/thingy-fw.patch`. Everything else is kept: the BLE services, the Thingy app support, sleep, and buttonless DFU.

## Devices

| Unit | BLE address | Running |
|---|---|---|
| A | E1:2A:60:8A:80:C1 | Factory FW 2.1.0 with Nordic's bootloader. Backed up in `backup/`. |
| B | EA:BF:EE:F7:76:21 | Our SoftDevice + bootloader, app **version 2** (FW Version characteristic `255.255.2`). Its previous firmware was bad; that copy is in `backup/`. |

## Layout

- `patches/thingy-fw.patch`: our changes to Nordic's firmware
- `tools/build.sh`: fetches and patches `thingy-fw/` (first run), builds the bootloader and app, packages them into `out/`
- `tools/mkpkg.py`: makes signed DFU packages (replaces nrfutil, which no longer runs on current Python)
- `tools/secure_dfu.py`: Secure DFU client over BLE (BlueZ via bleak); handles buttonless entry
- `tools/jlink.py`: SWD backup / flash / verify through J-Link Commander (nrfjprog can't open our J-Link)
- `tools/gatt_dump.py`, `tools/button_watch.py`: BLE inspection helpers
- `backup/`: SWD read-outs of both units (see `backup/README.md`)
- `keys/dfu_public_key.c`: the public key compiled into our bootloader
- `keys/private.pem`: **DFU signing key. Not in git; keep your own backup.** Without it, the next update needs the cable again.
- Not in git: `thingy-fw/` (fetched) and `out/` (build output)

## Setup

Needs `git`, `arm-none-eabi-gcc` (tested with 16.2), `mergehex`, SEGGER `JLinkExe` and BlueZ.

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

## Build

    tools/build.sh 3        # app version 3 -> out/thingy_app_v3.zip

The app version must be at least the version on the device. It also shows up as the patch number of the FW Version characteristic. The build is reproducible: from a fresh checkout it gives the same images that were flashed onto unit B.

To change the firmware, edit files in `thingy-fw/`, then record the changes:

    git -C thingy-fw diff > patches/thingy-fw.patch

`build.sh` warns if `thingy-fw/` and the patch don't match.

## Update over the air

    .venv/bin/python tools/secure_dfu.py out/thingy_app_v3.zip --addr EA:BF:EE:F7:76:21 --prn 8

The client reboots the app into the bootloader (`ThingyDfu`, address + 1), sends the package and waits until the bootloader has validated it.

**Recovery:** hold the button while switching the Thingy on. It then stays in the bootloader and waits for a package.

## Putting it on a new unit (once, by cable)

The factory bootloader only accepts updates signed with Nordic's private key, which is not public. So each unit first needs our SoftDevice + bootloader flashed over SWD ([Nordic: generating your own packages](https://nordicsemiconductor.github.io/Nordic-Thingy52-FW/documentation/dfu_generating.html)). The SWD connector is the 2×5 1.27 mm header next to the micro-USB port (standard Cortex 10-pin: 1 VTref, 2 SWDIO, 3/5 GND, 4 SWCLK, 10 nRESET). The Thingy must be switched on.

    .venv/bin/python tools/jlink.py backup backup/thingy_<addr>   # read-only, keep the old firmware
    .venv/bin/python tools/jlink.py flash out/thingy_sd_bl.hex     # erases the chip
    .venv/bin/python tools/secure_dfu.py out/thingy_app_v3.zip --name ThingyDfu

## What the patch changes

- `source/modules/m_ui.c`, `include/modules/m_ui.h`: on button press the lightwell is set to `UI_BUTTON_HOLD_COLOR`; on release the LED goes back to the connected or disconnected configuration. Button notifications are unchanged.
- Motion service compiled out: it needs InvenSense's proprietary MPL library (download behind a login). The Makefile turns motion back on automatically if `libs/liblibmplmpu_m4_hardfp/liblibmplmpu.a` is present.
- Builds with current GCC: `-Wno-error=array-bounds` (false positives on SDK linker-section arrays) and no `RTT_Syscalls_GCC.c` (it clashes with current newlib; nothing prints to stdout).
- `THINGY_FW_VERSION_PATCH` can be set from the build, so builds can be told apart over BLE.

Beyond the patch, `build.sh` builds the bootloader with our public key (written from `keys/private.pem`). It also pins micro-ecc to kmackay/micro-ecc `541b3a7`; Nordic's `setup_sdk.sh` would otherwise clone whatever is latest.
