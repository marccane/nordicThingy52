#!/usr/bin/env python3
"""Back up or flash a Thingy:52 (nRF52832) over SWD with SEGGER J-Link Commander.

nrfjprog can't open the J-Link on this machine; JLinkExe can.

  jlink.py backup <prefix>  read-only; writes <prefix>_flash.bin, <prefix>_uicr.bin and a
                            restorable <prefix>.hex (flash + programmed UICR words)
  jlink.py flash <hex>      chip erase, program <hex>, verify by read-back, run
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile

from intelhex import IntelHex

FLASH_SIZE = 0x80000
FICR = 0x10000000
UICR = 0x10001000
INFO_SIZE = 0x400
ERRORS = ("Could not connect", "Error occurred", "****** Error")


def jlink(commands, serial=None):
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "commands.jlink")
        with open(script, "w") as f:
            f.write("\n".join(commands + ["exit"]) + "\n")
        args = ["JLinkExe", "-NoGui", "1", "-device", "NRF52832_XXAA", "-if", "SWD", "-speed", "4000",
                "-autoconnect", "1", "-CommandFile", script]
        if serial:
            args += ["-SelectEmuBySN", serial]
        out = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300).stdout
    if "Cortex-M4 identified" not in out or any(e in out for e in ERRORS):
        sys.exit("J-Link command failed:\n" + out[-2000:])
    return out


def read_chip(serial=None):
    """Returns (flash, uicr, ficr) without halting or modifying the target."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = {name: os.path.join(tmp, name + ".bin") for name in ("flash", "uicr", "ficr")}
        jlink([f"savebin {paths['flash']} 0x0 {FLASH_SIZE:#x}",
               f"savebin {paths['uicr']} {UICR:#x} {INFO_SIZE:#x}",
               f"savebin {paths['ficr']} {FICR:#x} {INFO_SIZE:#x}"], serial)
        data = {}
        for name, path in paths.items():
            with open(path, "rb") as f:
                data[name] = f.read()
    if len(data["flash"]) != FLASH_SIZE or len(data["uicr"]) != INFO_SIZE:
        sys.exit("short read from target")
    return data["flash"], data["uicr"], data["ficr"]


def ble_address(ficr):
    """The random static address the Thingy firmware advertises with (FICR DEVICEADDR, top bits set)."""
    low, high = struct.unpack_from("<II", ficr, 0xA4)
    addr = ((high & 0xFFFF) | 0xC000) << 32 | low
    return ":".join(f"{addr >> shift & 0xFF:02X}" for shift in range(40, -8, -8))


def backup(prefix, serial):
    flash, uicr, ficr = read_chip(serial)
    part = struct.unpack_from("<I", ficr, 0x100)[0]
    os.makedirs(os.path.dirname(prefix) or ".", exist_ok=True)
    with open(prefix + "_flash.bin", "wb") as f:
        f.write(flash)
    with open(prefix + "_uicr.bin", "wb") as f:
        f.write(uicr)

    image = IntelHex()
    for addr, byte in enumerate(flash):
        if byte != 0xFF:
            image[addr] = byte
    for offset in range(0, INFO_SIZE, 4):
        word = uicr[offset:offset + 4]
        if word != b"\xff\xff\xff\xff":
            for i, byte in enumerate(word):
                image[UICR + offset + i] = byte
    image.write_hex_file(prefix + ".hex")

    bootloader = struct.unpack_from("<I", uicr, 0x14)[0]
    print(f"part nRF{part:x}, BLE address {ble_address(ficr)}, bootloader at {bootloader:#x}")
    print(f"wrote {prefix}_flash.bin, {prefix}_uicr.bin, {prefix}.hex")


def flash(hex_path, serial):
    image = IntelHex(hex_path)
    jlink(["r", "h", "erase", f"loadfile {os.path.abspath(hex_path)}", "r", "g"], serial)
    chip_flash, chip_uicr, ficr = read_chip(serial)
    mismatches = 0
    for addr in image.addresses():
        if addr < FLASH_SIZE:
            mismatches += chip_flash[addr] != image[addr]
        elif UICR <= addr < UICR + INFO_SIZE:
            mismatches += chip_uicr[addr - UICR] != image[addr]
    if mismatches:
        sys.exit(f"verify failed: {mismatches} bytes differ")
    print(f"flashed and verified {len(image.addresses())} bytes on {ble_address(ficr)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", help="J-Link serial number (default: the only one attached)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup").add_argument("prefix")
    sub.add_parser("flash").add_argument("hex")
    args = parser.parse_args()
    if args.cmd == "backup":
        backup(args.prefix, args.serial)
    else:
        flash(args.hex, args.serial)


if __name__ == "__main__":
    main()
