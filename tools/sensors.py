#!/usr/bin/env python3
"""Stream every sensor of a Thingy:52 over BLE and print (and optionally log) the values.

Subscribes to all notifications of the Thingy environment, motion, UI and battery services. Data
formats follow thingy-fw/include/ble_services/ble_tes.h and ble_tms.h; the fixed-point scaling
follows m_environment.c and m_motion.c.

Usage: sensors.py [--addr AA:BB:..|--name Thingy] [--only temperature,humidity] [--interval 1]
                  [--csv log.csv] [--duration SECONDS] [--temp-interval MS]

Printing is throttled to one line per sensor per --interval seconds (0 prints everything);
the CSV gets every notification, one row per field.
"""
import argparse
import asyncio
import csv
import datetime
import struct
import sys

from bleak import BleakClient, BleakScanner


def thingy_uuid(short):
    return f"ef68{short:04x}-9b35-4933-9b10-52ffa9740042"


BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
ENV_CONFIG_UUID = thingy_uuid(0x0206)

TAP_DIRECTIONS = {1: "x+", 2: "x-", 3: "y+", 4: "y-", 5: "z+", 6: "z-"}
ORIENTATIONS = {0: "portrait", 1: "landscape", 2: "reverse portrait", 3: "reverse landscape"}


def temperature(d):
    integer, decimal = struct.unpack("<bB", d)
    # The firmware stores the fraction as an unsigned byte, so it is lost for negative values.
    return {"temperature_c": integer + decimal / 100 if integer >= 0 else integer - decimal / 100}


def pressure(d):
    integer, decimal = struct.unpack("<iB", d)
    return {"pressure_hpa": integer + decimal / 100}


def gas(d):
    eco2, tvoc = struct.unpack("<HH", d)
    return {"eco2_ppm": eco2, "tvoc_ppb": tvoc}


def color(d):
    return dict(zip(("red", "green", "blue", "clear"), struct.unpack("<4H", d)))


def tap(d):
    direction, count = struct.unpack("<BB", d)
    return {"direction": TAP_DIRECTIONS.get(direction, direction), "count": count}


def quaternion(d):
    return dict(zip("wxyz", (v / (1 << 30) for v in struct.unpack("<4i", d))))


def pedometer(d):
    steps, time_ms = struct.unpack("<II", d)
    return {"steps": steps, "time_ms": time_ms}


def raw(d):
    v = struct.unpack("<9h", d)
    axes = ("x", "y", "z")
    out = {f"accel_{a}_g": x / (1 << 10) for a, x in zip(axes, v[0:3])}         # Q6.10
    out.update({f"gyro_{a}_dps": x / (1 << 5) for a, x in zip(axes, v[3:6])})  # Q11.5
    out.update({f"compass_{a}_ut": x / (1 << 4) for a, x in zip(axes, v[6:9])})  # Q12.4
    return out


def euler(d):
    return dict(zip(("roll_deg", "pitch_deg", "yaw_deg"), (v / (1 << 16) for v in struct.unpack("<3i", d))))


def rotation(d):
    return {f"m{i // 3}{i % 3}": v / (1 << 14) for i, v in enumerate(struct.unpack("<9h", d))}


def heading(d):
    return {"heading_deg": struct.unpack("<i", d)[0] / (1 << 16)}


def gravity(d):
    return dict(zip(("x_ms2", "y_ms2", "z_ms2"), struct.unpack("<3f", d)))


# name -> (characteristic UUID, decoder)
SENSORS = {
    "temperature": (thingy_uuid(0x0201), temperature),
    "pressure":    (thingy_uuid(0x0202), pressure),
    "humidity":    (thingy_uuid(0x0203), lambda d: {"humidity_pct": d[0]}),
    "gas":         (thingy_uuid(0x0204), gas),
    "color":       (thingy_uuid(0x0205), color),
    "battery":     (BATTERY_LEVEL_UUID, lambda d: {"battery_pct": d[0]}),
    "button":      (thingy_uuid(0x0302), lambda d: {"pressed": bool(d[0])}),
    "tap":         (thingy_uuid(0x0402), tap),
    "orientation": (thingy_uuid(0x0403), lambda d: {"orientation": ORIENTATIONS.get(d[0], d[0])}),
    "quaternion":  (thingy_uuid(0x0404), quaternion),
    "pedometer":   (thingy_uuid(0x0405), pedometer),
    "raw":         (thingy_uuid(0x0406), raw),
    "euler":       (thingy_uuid(0x0407), euler),
    "rotation":    (thingy_uuid(0x0408), rotation),
    "heading":     (thingy_uuid(0x0409), heading),
    "gravity":     (thingy_uuid(0x040A), gravity),
}


def fmt(value):
    return f"{value:.3f}" if isinstance(value, float) else str(value)


async def set_temperature_interval(client, interval_ms):
    """Rewrite the temperature interval in the environment config (stored in the Thingy's flash)."""
    config = bytearray(await client.read_gatt_char(ENV_CONFIG_UUID))
    struct.pack_into("<H", config, 0, interval_ms)
    await client.write_gatt_char(ENV_CONFIG_UUID, config, response=True)
    print(f"temperature interval set to {interval_ms} ms")


async def main(args):
    names = args.only.split(",") if args.only else list(SENSORS)
    unknown = [n for n in names if n not in SENSORS]
    if unknown:
        sys.exit(f"unknown sensor(s): {', '.join(unknown)}; choose from {', '.join(SENSORS)}")

    def match(dev, adv):
        if args.addr:
            return dev.address.upper() == args.addr.upper()
        return args.name in (adv.local_name, dev.name)

    dev = await BleakScanner.find_device_by_filter(match, timeout=20)
    if dev is None:
        sys.exit(f"{args.addr or args.name} not found (press the Thingy's button to wake it)")

    csv_file = open(args.csv, "a", newline="") if args.csv else None
    writer = csv.writer(csv_file) if csv_file else None
    if writer and csv_file.tell() == 0:
        writer.writerow(("time", "sensor", "field", "value"))
    last_printed = {}
    disconnected = asyncio.Event()

    def handler_for(name, decode):
        def on_notify(_char, data):
            now = datetime.datetime.now()
            try:
                values = decode(bytes(data))
            except struct.error:
                print(f"{name}: unexpected {len(data)}-byte payload {bytes(data).hex()}")
                return
            if writer:
                for field, value in values.items():
                    writer.writerow((now.isoformat(timespec="milliseconds"), name, field, value))
            last = last_printed.get(name)
            if last is None or (now - last).total_seconds() >= args.interval:
                last_printed[name] = now
                fields = "  ".join(f"{k}={fmt(v)}" for k, v in values.items())
                print(f"{now:%H:%M:%S.%f}"[:-3] + f"  {name:<11} {fields}", flush=True)
        return on_notify

    async with BleakClient(dev, disconnected_callback=lambda _c: disconnected.set()) as client:
        print(f"connected to {dev.address} ({dev.name})")
        if args.temp_interval:
            await set_temperature_interval(client, args.temp_interval)
        subscribed = []
        for name in names:
            uuid, decode = SENSORS[name]
            if client.services.get_characteristic(uuid) is None:
                print(f"{name}: not offered by this firmware")
                continue
            handler = handler_for(name, decode)
            await client.start_notify(uuid, handler)
            if name == "battery":
                # Battery level is only notified when it changes; show the current one right away.
                handler(None, await client.read_gatt_char(uuid))
            subscribed.append(name)
        print(f"streaming {', '.join(subscribed)} (Ctrl-C to stop)", flush=True)
        try:
            await asyncio.wait_for(disconnected.wait(), timeout=args.duration)
            print("device disconnected")
        except asyncio.TimeoutError:
            pass
    if csv_file:
        csv_file.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--addr", help="device BLE address")
    ap.add_argument("--name", default="Thingy", help="advertised name when --addr is not given")
    ap.add_argument("--only", help=f"comma-separated subset of: {', '.join(SENSORS)}")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="print at most one line per sensor every N seconds (0 = all)")
    ap.add_argument("--csv", help="append every notification to this CSV file")
    ap.add_argument("--duration", type=float, help="stop after N seconds (default: run until Ctrl-C)")
    ap.add_argument("--temp-interval", type=int, metavar="MS",
                    help="set the temperature notification interval (stored on the Thingy)")
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        pass
