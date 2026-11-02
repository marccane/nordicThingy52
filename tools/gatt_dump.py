#!/usr/bin/env python3
"""Connect to a BLE device and dump its GATT table (services, characteristics, readable values)."""
import asyncio
import sys

from bleak import BleakClient, BleakScanner


async def main(target: str) -> None:
    dev = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.address.upper() == target.upper() or (ad.local_name or "") == target,
        timeout=15,
    )
    if dev is None:
        sys.exit(f"device {target} not found")
    print(f"found {dev.address} {dev.name}")
    async with BleakClient(dev) as client:
        for svc in client.services:
            print(f"[S] {svc.uuid}  {svc.description}")
            for ch in svc.characteristics:
                line = f"    [C] {ch.uuid}  props={','.join(ch.properties)}  {ch.description}"
                if "read" in ch.properties:
                    try:
                        val = await client.read_gatt_char(ch)
                        line += f"  = {val.hex()} {val!r}"
                    except Exception as e:  # noqa: BLE001
                        line += f"  (read err: {e})"
                print(line)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "Thingy"))
