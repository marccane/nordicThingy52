#!/usr/bin/env python3
"""Nordic Secure DFU client over BLE (bleak), for the nRF5 SDK 13 bootloader used by Thingy:52.

Usage: secure_dfu.py <pkg.zip> [--addr AA:BB:CC:DD:EE:FF] [--name Thingy] [--prn N]

Flow:
  1. Find the device. If it runs the application (buttonless DFU char 8ec90003), enable
     indications, write 0x01 and wait for the 20-01-01 indication; the device then resets
     into the bootloader.
  2. The bootloader advertises as "ThingyDfu" at the app address with the last octet +1
     (nrf_ble_dfu.c: addr.addr[0] += 1, a uint8 increment, so it wraps without carry).
  3. Send the init packet (.dat) as a command object and the image (.bin) as data objects
     over the control point (8ec90001) and packet (8ec90002) characteristics.

Protocol details taken from thingy-fw/external/sdk13/components/libraries/bootloader/ble_dfu/
nrf_ble_dfu.c and thingy-fw/project/dfu_req_handling/dfu_req_handling.c:
  - The bootloader replies to MTU exchange with 23, so packet writes are 20 bytes.
  - Command object max size 256 (INIT_COMMAND_MAX_SIZE), data object max size 4096 (one page);
    both are also reported by the Select response.
  - CRC is CRC-32 (zlib) of everything written so far for that object type; for data it is
    cumulative across objects.
  - Packet receipt notifications arrive as a CRC response (60 03 01 <offset> <crc>).
"""
import argparse
import asyncio
import json
import struct
import subprocess
import sys
import zipfile
import zlib

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

BUTTONLESS_UUID = "8ec90003-f315-4f60-9fb8-838830daea50"
CTRL_PT_UUID = "8ec90001-f315-4f60-9fb8-838830daea50"
PACKET_UUID = "8ec90002-f315-4f60-9fb8-838830daea50"

DFU_ADV_NAME = "ThingyDfu"
PACKET_LEN = 20  # ATT MTU is fixed at 23 by the bootloader

# Buttonless DFU (ble_dfu.c)
BTNLESS_ENTER_BOOTLOADER = 0x01
BTNLESS_RESPONSE = 0x20
BTNLESS_SUCCESS = 0x01

# Control point opcodes (nrf_ble_dfu.h)
OP_CREATE = 0x01
OP_SET_PRN = 0x02
OP_CRC = 0x03
OP_EXECUTE = 0x04
OP_SELECT = 0x06
OP_RESPONSE = 0x60

OBJ_COMMAND = 0x01
OBJ_DATA = 0x02

RES_SUCCESS = 0x01
RES_EXT_ERROR = 0x0B

OP_NAMES = {OP_CREATE: "create", OP_SET_PRN: "set PRN", OP_CRC: "CRC",
            OP_EXECUTE: "execute", OP_SELECT: "select"}

RES_NAMES = {
    0x00: "invalid opcode",
    0x02: "opcode not supported",
    0x03: "invalid parameter",
    0x04: "insufficient resources",
    0x05: "invalid object (bad signature, or fw/hw requirements not met)",
    0x07: "unsupported object type",
    0x08: "operation not permitted in this state",
    0x0A: "operation failed",
}

EXT_ERROR_NAMES = {
    0x00: "no error",
    0x01: "invalid error code",
    0x02: "wrong command format",
    0x03: "unknown command",
    0x04: "init command invalid",
    0x05: "firmware version too low",
    0x06: "hardware version mismatch",
    0x07: "SoftDevice version not in sd_req",
    0x08: "signature missing",
    0x09: "wrong hash type",
    0x0A: "hash failed",
    0x0B: "wrong signature type",
    0x0C: "verification failed (image hash mismatch)",
    0x0D: "insufficient space",
}

CONNECT_RETRIES = 3
OBJECT_RETRIES = 3


class DfuError(Exception):
    pass


class DfuDisconnected(DfuError):
    pass


class CrcMismatch(DfuError):
    pass


def load_package(path):
    """Return (init_packet, firmware) from an nrfutil DFU zip containing one image."""
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))["manifest"]
        if "application" in manifest:
            key = "application"
        elif len(manifest) == 1:
            key = next(iter(manifest))
        else:
            raise DfuError(f"package has several images ({', '.join(manifest)}); "
                           "only single-image packages are supported")
        image = manifest[key]
        dat = zf.read(image["dat_file"])
        bin_ = zf.read(image["bin_file"])
    print(f"package: {key}, init packet {len(dat)} B, image {len(bin_)} B")
    return dat, bin_


def dfu_address(app_addr, delta=1):
    """Bootloader address: last octet +1, wrapping without carry like the bootloader does."""
    octets = app_addr.upper().split(":")
    octets[-1] = f"{(int(octets[-1], 16) + delta) & 0xFF:02X}"
    return ":".join(octets)


def bluez_forget(addr):
    """Drop BlueZ's cached device (and GATT db) so services are rediscovered."""
    subprocess.run(["bluetoothctl", "remove", addr], capture_output=True, timeout=10, check=False)


async def find_device(addr=None, name=None, timeout=20.0):
    def match(dev, adv):
        if addr and dev.address.upper() == addr.upper():
            return True
        return bool(name) and name in (adv.local_name, dev.name)

    what = " or ".join(x for x in (addr, name and f"name {name!r}") if x)
    print(f"scanning for {what} ...")
    dev = await BleakScanner.find_device_by_filter(match, timeout=timeout)
    if dev is None:
        raise DfuError(f"{what} not found within {timeout:.0f} s")
    print(f"found {dev.address} ({dev.name})")
    return dev


async def connect(addr, name, on_disconnect):
    """Scan freshly and connect, retrying; returns a connected BleakClient."""
    last_err = None
    for attempt in range(1, CONNECT_RETRIES + 1):
        try:
            dev = await find_device(addr, name)
            client = BleakClient(dev, disconnected_callback=on_disconnect, timeout=20)
            await client.connect()
            return client
        except (BleakError, asyncio.TimeoutError, EOFError, DfuError) as e:
            last_err = e
            print(f"connect attempt {attempt}/{CONNECT_RETRIES} failed: {e}")
            await asyncio.sleep(2)
    raise DfuError(f"could not connect: {last_err}")


def has_char(client, uuid):
    return client.services.get_characteristic(uuid) is not None


async def enter_bootloader(client, disconnected):
    """Trigger buttonless DFU and wait for the device to reset into the bootloader."""
    indications = asyncio.Queue()
    await client.start_notify(BUTTONLESS_UUID, lambda _c, d: indications.put_nowait(bytes(d)))
    print("requesting bootloader via buttonless DFU")
    await client.write_gatt_char(BUTTONLESS_UUID, bytes([BTNLESS_ENTER_BOOTLOADER]), response=True)
    try:
        rsp = await asyncio.wait_for(indications.get(), 10)
    except asyncio.TimeoutError:
        raise DfuError("no response from buttonless DFU characteristic") from None
    if rsp[:2] != bytes([BTNLESS_RESPONSE, BTNLESS_ENTER_BOOTLOADER]) or len(rsp) < 3:
        raise DfuError(f"unexpected buttonless response {rsp.hex()}")
    if rsp[2] != BTNLESS_SUCCESS:
        raise DfuError(f"buttonless DFU refused, response code 0x{rsp[2]:02x}")
    # The app resets once the indication is confirmed, which BlueZ does automatically.
    try:
        await asyncio.wait_for(disconnected.wait(), 15)
    except asyncio.TimeoutError:
        await client.disconnect()
    print("device reset into bootloader")


class DfuTransfer:
    """Drives the SDK 13 Secure DFU control point protocol on a connected client."""

    def __init__(self, client, prn=0):
        self.client = client
        self.prn = prn
        self.responses = asyncio.Queue()

    def on_disconnect(self):
        self.responses.put_nowait(None)

    def _on_notify(self, _char, data):
        self.responses.put_nowait(bytes(data))

    async def start(self):
        await self.client.start_notify(CTRL_PT_UUID, self._on_notify)
        await self.request(bytes([OP_SET_PRN]) + struct.pack("<H", self.prn))

    async def _wait_response(self, opcode, timeout):
        while True:
            try:
                data = await asyncio.wait_for(self.responses.get(), timeout)
            except asyncio.TimeoutError:
                raise DfuError(f"timeout waiting for {OP_NAMES[opcode]} response") from None
            if data is None:
                raise DfuDisconnected(f"disconnected while waiting for {OP_NAMES[opcode]} response")
            if len(data) < 3 or data[0] != OP_RESPONSE:
                raise DfuError(f"malformed response {data.hex()}")
            if data[1] != opcode:
                if data[1] == OP_CRC and self.prn:
                    continue  # late packet receipt notification
                raise DfuError(f"response for opcode 0x{data[1]:02x}, expected 0x{opcode:02x}")
            if data[2] != RES_SUCCESS:
                msg = RES_NAMES.get(data[2], f"result 0x{data[2]:02x}")
                if data[2] == RES_EXT_ERROR and len(data) > 3:
                    msg = "extended error: " + EXT_ERROR_NAMES.get(data[3], f"0x{data[3]:02x}")
                raise DfuError(f"{OP_NAMES[opcode]} failed: {msg}")
            return data[3:]

    async def request(self, payload, timeout=10.0):
        while not self.responses.empty():
            stale = self.responses.get_nowait()
            if stale is None:
                raise DfuDisconnected("device disconnected")
        await self.client.write_gatt_char(CTRL_PT_UUID, payload, response=True)
        return await self._wait_response(payload[0], timeout)

    async def _stream(self, chunk, base_offset, base_crc):
        """Write one object's bytes to the packet characteristic, checking PRN reports."""
        for n, i in enumerate(range(0, len(chunk), PACKET_LEN), start=1):
            end = i + PACKET_LEN
            await self.client.write_gatt_char(PACKET_UUID, chunk[i:end], response=False)
            if self.prn and n % self.prn == 0:
                rsp = await self._wait_response(OP_CRC, 10)
                offset, crc = struct.unpack("<II", rsp[:8])
                written = min(end, len(chunk))
                expected = (base_offset + written, zlib.crc32(chunk[:written], base_crc))
                if (offset, crc) != expected:
                    raise CrcMismatch(f"PRN at offset {offset} (expected {expected[0]}), "
                                      f"crc {crc:08x} (expected {expected[1]:08x})")

    async def send_object(self, obj_type, data, label):
        rsp = await self.request(bytes([OP_SELECT, obj_type]))
        max_size, _offset, _crc = struct.unpack("<III", rsp[:12])
        if obj_type == OBJ_COMMAND and len(data) > max_size:
            raise DfuError(f"init packet is {len(data)} B, bootloader accepts {max_size} B")

        crc_so_far = 0
        for base in range(0, len(data), max_size):
            chunk = data[base:base + max_size]
            expected = (base + len(chunk), zlib.crc32(chunk, crc_so_far))
            for attempt in range(1, OBJECT_RETRIES + 1):
                await self.request(bytes([OP_CREATE, obj_type]) + struct.pack("<I", len(chunk)))
                try:
                    await self._stream(chunk, base, crc_so_far)
                    offset, crc = struct.unpack("<II", (await self.request(bytes([OP_CRC])))[:8])
                    if (offset, crc) == expected:
                        break
                    print(f"\n{label}: CRC mismatch at offset {offset} "
                          f"(crc {crc:08x}, expected {expected[1]:08x}), "
                          f"retry {attempt}/{OBJECT_RETRIES}")
                except CrcMismatch as e:
                    print(f"\n{label}: {e}, retry {attempt}/{OBJECT_RETRIES}")
            else:
                raise DfuError(f"{label}: object at offset {base} failed CRC check "
                               f"{OBJECT_RETRIES} times")
            # The last execute validates the whole image (hash + signature), so allow longer.
            last = base + len(chunk) == len(data)
            await self.request(bytes([OP_EXECUTE]), timeout=60.0 if last else 15.0)
            crc_so_far = expected[1]
            done = base + len(chunk)
            print(f"\r{label}: {done}/{len(data)} B ({100 * done // len(data)}%)",
                  end="", flush=True)
        print()


async def run(pkg, addr, name, prn):
    init_packet, firmware = load_package(pkg)

    disconnected = asyncio.Event()
    transfer = None

    def on_disconnect(_client):
        disconnected.set()
        if transfer is not None:
            transfer.on_disconnect()

    client = await connect(addr, None if addr else name, on_disconnect)
    app_addr = client.address
    try:
        if has_char(client, CTRL_PT_UUID):
            print("device is already in bootloader mode")
            dfu_addr = app_addr
            app_addr = dfu_address(dfu_addr, -1)
        elif has_char(client, BUTTONLESS_UUID):
            await enter_bootloader(client, disconnected)
            dfu_addr = dfu_address(app_addr)
            bluez_forget(dfu_addr)
            disconnected.clear()
            client = await connect(dfu_addr, DFU_ADV_NAME, on_disconnect)
            dfu_addr = client.address
            if not has_char(client, CTRL_PT_UUID):
                raise DfuError("bootloader does not expose the DFU control point")
        else:
            raise DfuError("device has neither buttonless DFU nor a DFU control point")

        transfer = DfuTransfer(client, prn)
        print(f"DFU target {dfu_addr}, PRN {prn}")
        await transfer.start()
        await transfer.send_object(OBJ_COMMAND, init_packet, "init packet")
        await transfer.send_object(OBJ_DATA, firmware, "firmware")
        print("image validated; device is resetting into the new application")
    finally:
        if client.is_connected:
            try:
                await client.disconnect()
            except BleakError:
                pass

    # The new app may expose a different GATT table; make BlueZ rediscover it.
    bluez_forget(app_addr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("package", help="nrfutil DFU package (.zip)")
    ap.add_argument("--addr", help="device BLE address (app or bootloader)")
    ap.add_argument("--name", default="Thingy", help="advertised name when --addr is not given")
    ap.add_argument("--prn", type=int, default=0,
                    help="packet receipt notification interval (0 = off)")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.package, args.addr, args.name, args.prn))
    except (DfuError, BleakError, OSError, zipfile.BadZipFile, KeyError) as e:
        sys.exit(f"DFU failed: {e}")


if __name__ == "__main__":
    main()
