# Backups

Full SWD read-outs of the two Thingy:52 units. Each set has:

- `*_flash.bin`: all 512 KB of flash, 0x00000000–0x0007FFFF (MBR, SoftDevice, app, bootloader, settings)
- `*_uicr.bin`: UICR, 0x10001000–0x100013FF
- `*.hex` (when present): the same data as a restorable image, with all non-erased flash plus the programmed UICR words

Both units have the same UICR: BOOTLOADERADDR 0x75000, MBR params page 0x7E000, PSELRESET P0.21.

## thingy_E12A608A80C1_factory_fw2.1.0_*

**Good firmware, believed to be the factory image.** Read on 2026-11-03 from the Thingy that advertises as `Thingy` at E1:2A:60:8A:80:C1. It fades the LED blue while advertising. Over BLE its FW Version characteristic reports 2.1.0. The read-out was read-only, and the unit kept running its own firmware.

Restore (chip erase, then program and verify):

    .venv/bin/python tools/jlink.py flash backup/thingy_E12A608A80C1_factory_fw2.1.0.hex

The image holds no device-unique data (the BLE address comes from FICR, which is not in it), so it should also bring other units back to stock. This has not been tried yet.

## thingy_EABFEEF77621_BAD_FIRMWARE_*

**Bad firmware. Do not restore this as a working image.**

This is a full read-out of the second Thingy (FICR address EA:BF:EE:F7:76:21), taken on 2026-11-03 before that unit was erased and reflashed with our SoftDevice + bootloader. The user said the firmware on it at that time was bad. Keep it only for forensics.
