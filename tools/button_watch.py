#!/usr/bin/env python3
"""Connect to a Thingy:52 and log button press/release notifications for a while.

Usage: button_watch.py <address> [seconds]
"""
import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner

BUTTON_UUID = "ef680302-9b35-4933-9b10-52ffa9740042"


async def main(addr: str, seconds: float) -> None:
    dev = await BleakScanner.find_device_by_address(addr, timeout=20)
    if dev is None:
        sys.exit(f"{addr} not found")
    start = time.monotonic()
    pressed_at = None

    def on_button(_char, data: bytearray) -> None:
        nonlocal pressed_at
        now = time.monotonic()
        if data[0]:
            pressed_at = now
            print(f"{now - start:7.2f}s  PRESSED", flush=True)
        else:
            held = f" (held {now - pressed_at:.2f}s)" if pressed_at is not None else ""
            print(f"{now - start:7.2f}s  released{held}", flush=True)

    async with BleakClient(dev) as client:
        await client.start_notify(BUTTON_UUID, on_button)
        print(f"connected to {addr}; press and hold the button (watching {seconds:.0f}s)", flush=True)
        await asyncio.sleep(seconds)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 180))
