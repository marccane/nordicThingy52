#!/usr/bin/env python3
"""Terminal dashboard with the latest value of every sensor, for one or more Thingy:52s at once.

Connects to every Thingy advertising nearby, or only to the ones given with --addr, and shows one
column per Thingy that updates as notifications arrive. A Thingy that disconnects or falls asleep
is reconnected when it advertises again (its button wakes it). Decoding comes from sensors.py.

Keys: q quit, m motion on/off. Turning motion off also unsubscribes from it, which lets the
Thingys power their motion sensor down.

Usage: dashboard.py [--addr AA:BB:..[=label] ...] [--name Thingy] [--no-motion]
"""
import argparse
import asyncio
import collections
import contextlib
import curses
import datetime
import locale
import logging
import struct
import time

from bleak import BleakClient, BleakScanner

from sensors import SENSORS, thingy_uuid

FW_VERSION_UUID = thingy_uuid(0x0107)
MOTION = {"orientation", "tap", "pedometer", "heading", "euler", "quaternion", "gravity", "raw", "rotation"}
EVENTS = {"button", "tap", "orientation", "pedometer"}  # notified only when something happens
TREND_WINDOW = 600  # seconds of temperature history in the sparkline
SPARK = "▁▂▃▄▅▆▇█"
LABEL_W = 17


def vec(values, keys, fmt):
    return " ".join(fmt.format(values[k]) for k in keys)


def axes(prefix, unit):
    return tuple(f"{prefix}_{a}_{unit}" for a in "xyz")


# (row label, sensor, format(decoded values) -> str); decoded values come from sensors.SENSORS.
ENV_ROWS = [
    ("Temperature", "temperature", lambda v: f"{v['temperature_c']:.2f} °C"),
    ("Pressure", "pressure", lambda v: f"{v['pressure_hpa']:.2f} hPa"),
    ("Humidity", "humidity", lambda v: f"{v['humidity_pct']} %"),
    ("eCO2 / TVOC", "gas", lambda v: f"{v['eco2_ppm']} ppm / {v['tvoc_ppb']} ppb"),
    ("Light R G B C", "color", lambda v: f"{v['red']} {v['green']} {v['blue']} {v['clear']}"),
    ("Battery", "battery", lambda v: f"{v['battery_pct']} %"),
    ("Button", "button", lambda v: "pressed" if v["pressed"] else "released"),
]
MOTION_ROWS = [
    ("Orientation", "orientation", lambda v: str(v["orientation"])),
    ("Tap", "tap", lambda v: f"{v['direction']} x{v['count']}"),
    ("Steps", "pedometer", lambda v: f"{v['steps']} ({v['time_ms'] / 1000:.0f} s)"),
    ("Heading", "heading", lambda v: f"{v['heading_deg']:.1f}°"),
    ("Roll Pitch Yaw °", "euler", lambda v: vec(v, ("roll_deg", "pitch_deg", "yaw_deg"), "{:+7.1f}")),
    ("Quaternion wxyz", "quaternion", lambda v: vec(v, "wxyz", "{:+.3f}")),
    ("Gravity m/s²", "gravity", lambda v: vec(v, ("x_ms2", "y_ms2", "z_ms2"), "{:+6.2f}")),
    ("Accel g", "raw", lambda v: vec(v, axes("accel", "g"), "{:+6.2f}")),
    ("Gyro °/s", "raw", lambda v: vec(v, axes("gyro", "dps"), "{:+7.1f}")),
    ("Compass µT", "raw", lambda v: vec(v, axes("compass", "ut"), "{:+7.1f}")),
    ("Rotation matrix", "rotation", lambda v: vec(v, ("m00", "m01", "m02"), "{:+.3f}")),
    ("", "rotation", lambda v: vec(v, ("m10", "m11", "m12"), "{:+.3f}")),
    ("", "rotation", lambda v: vec(v, ("m20", "m21", "m22"), "{:+.3f}")),
]


class Device:
    def __init__(self, address, label=None):
        self.address = address
        self.label = label
        self.status = "searching"  # searching -> connecting -> connected -> searching ...
        self.note = ""
        self.fw = ""
        self.client = None
        self.values = {}  # sensor -> (time.monotonic(), decoded values)
        self.missing = set()  # sensors this firmware does not offer
        self.temps = collections.deque()  # (time.monotonic(), °C) for the sparkline

    def handler(self, name):
        decode = SENSORS[name][1]

        def on_notify(_char, data):
            try:
                values = decode(bytes(data))
            except (struct.error, IndexError):
                logging.warning("%s %s: unexpected payload %s", self.address, name, bytes(data).hex())
                return
            now = time.monotonic()
            self.values[name] = (now, values)
            if name == "temperature":
                self.temps.append((now, values["temperature_c"]))
                while self.temps[0][0] < now - TREND_WINDOW:
                    self.temps.popleft()
        return on_notify

    def lost(self, client):
        # Ignore callbacks from an earlier, replaced connection, and any that arrive while connecting:
        # when BlueZ aborts an attempt (le-connection-abort-by-local), bleak reports a disconnect and
        # then retries within the same connect() call, which may still succeed. connect() decides.
        if client is not self.client or self.status != "connected":
            return
        self.client = None
        self.status = "searching"
        self.note = f"disconnected {datetime.datetime.now():%H:%M:%S}"


class State:
    def __init__(self, args):
        self.name = args.name
        self.autodiscover = not args.addr
        self.devices = {}
        for spec in args.addr or []:
            addr, _, label = spec.partition("=")
            self.devices[addr.upper()] = Device(addr.upper(), label or None)
        self.motion = not args.no_motion
        self.scanning = False
        self.message = (0.0, "")  # (time.monotonic(), text) of the last warning
        self.ble_lock = asyncio.Lock()  # connecting and (un)subscribing don't interleave
        self.tasks = set()


class FooterLog(logging.Handler):
    """Shows log warnings (from bleak too) in the footer instead of writing over the screen."""

    def __init__(self, state):
        super().__init__(logging.WARNING)
        self.state = state

    def emit(self, record):
        self.state.message = (time.monotonic(), record.getMessage().replace("\n", " "))


async def scan(state, seconds):
    """Scan for the Thingys we are waiting for. Returns {address: BLEDevice}."""
    found = {}

    def wanted(addr):
        d = state.devices.get(addr)
        return d.status == "searching" if d else state.autodiscover

    def seen(dev, adv):
        addr = dev.address.upper()
        if wanted(addr) and (addr in state.devices or state.name in (adv.local_name, dev.name)):
            found[addr] = dev

    state.scanning = True
    try:
        async with BleakScanner(seen):
            start = time.monotonic()
            while (elapsed := time.monotonic() - start) < seconds:
                await asyncio.sleep(0.25)
                waiting = [a for a, d in state.devices.items() if d.status == "searching"]
                if found and (elapsed > 1.5 or (not state.autodiscover and set(waiting) <= set(found))):
                    break
    finally:
        state.scanning = False
    return found


async def connect(state, d, ble_device):
    d.status, d.note = "connecting", ""
    client = BleakClient(ble_device, disconnected_callback=d.lost)
    d.client = client
    async with state.ble_lock:
        try:
            await client.connect(timeout=20)
            if client.services.get_characteristic(FW_VERSION_UUID):
                major, minor, patch = (await client.read_gatt_char(FW_VERSION_UUID))[:3]
                d.fw = f"v{patch}" if (major, minor) == (0xFF, 0xFF) else f"{major}.{minor}.{patch}"
            for name, (uuid, _) in SENSORS.items():
                if client.services.get_characteristic(uuid) is None:
                    d.missing.add(name)
                elif name not in MOTION or state.motion:
                    await client.start_notify(uuid, d.handler(name))
            if "battery" not in d.missing:
                # The battery level is notified only when it changes; show the current one now.
                d.handler("battery")(None, await client.read_gatt_char(SENSORS["battery"][0]))
            if not client.is_connected:
                raise ConnectionError("disconnected while subscribing")
            d.status = "connected"
        except Exception as e:
            d.client = None
            d.status = "searching"
            d.note = f"connect failed: {str(e) or type(e).__name__}"
            await disconnect(client)


async def disconnect(client):
    with contextlib.suppress(Exception):
        await asyncio.wait_for(client.disconnect(), 5)


async def manage(state):
    """Scan while any Thingy is missing and connect to whatever turns up, one at a time."""
    last_scan = -1e9
    while True:
        waiting = any(d.status == "searching" for d in state.devices.values())
        if waiting or not state.devices or (state.autodiscover and time.monotonic() - last_scan > 30):
            found = await scan(state, 6)
            last_scan = time.monotonic()
            for addr in sorted(found):
                d = state.devices.setdefault(addr, Device(addr))
                if d.status == "searching":
                    await connect(state, d, found[addr])
        await asyncio.sleep(1)


async def set_motion(state, on):
    state.motion = on
    async with state.ble_lock:
        for d in list(state.devices.values()):
            for name in MOTION - d.missing:
                d.values.pop(name, None)
                if d.status != "connected":
                    continue
                uuid = SENSORS[name][0]
                with contextlib.suppress(Exception):
                    if on:
                        await d.client.start_notify(uuid, d.handler(name))
                    else:
                        await d.client.stop_notify(uuid)


def sparkline(samples, width, now):
    start = now - TREND_WINDOW
    values = [v for t, v in samples if t >= start]
    if width < 4 or not values:
        return ""
    buckets = [[] for _ in range(width)]
    for t, v in samples:
        i = int((t - start) / TREND_WINDOW * width)
        if 0 <= i < width:
            buckets[i].append(v)
    lo, hi = min(values), max(values)
    span = max(hi - lo, 0.5)  # don't blow 0.01 °C of noise up to full height
    base = (lo + hi - span) / 2
    line = "".join(SPARK[min(7, int((sum(b) / len(b) - base) / span * 8))] if b else " " for b in buckets)
    return f"{line} {lo:.1f}–{hi:.1f}"


def age_text(age):
    if age < 60:
        return f"{age:.0f}s"
    return f"{age / 60:.0f}m" if age < 3600 else f"{age / 3600:.0f}h"


def init_colors():
    if not curses.has_colors():
        return {}
    curses.start_color()
    bg = -1
    try:
        curses.use_default_colors()
    except curses.error:
        bg = curses.COLOR_BLACK
    for pair, fg in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED), 1):
        curses.init_pair(pair, fg, bg)
    curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLUE)
    return {name: curses.color_pair(i) for i, name in enumerate(("ok", "wait", "bad", "bar"), 1)}


def draw(scr, state, colors):
    scr.erase()
    h, w = scr.getmaxyx()
    now = time.monotonic()
    dim, bar = curses.A_DIM, colors.get("bar", curses.A_REVERSE) | curses.A_BOLD

    def put(y, x, text, attr=0, width=None):
        if 0 <= y < h and 0 <= x < w:
            with contextlib.suppress(curses.error):  # writing the bottom-right cell "fails" after drawing it
                scr.addnstr(y, x, text, min(w - x, width or w), attr)

    devices = [state.devices[a] for a in (state.devices if not state.autodiscover else sorted(state.devices))]
    connected = sum(d.status == "connected" for d in devices)
    put(0, 0, f" Thingy:52 sensors   {connected} of {len(devices)} connected".ljust(w), bar)
    put(0, w - 10, f"{datetime.datetime.now():%H:%M:%S} ", bar)

    if not devices:
        put(2, 2, f"Looking for Thingys advertising as '{state.name}'...")
        put(3, 2, "Press a Thingy's button to wake it. A Thingy that is connected elsewhere", dim)
        put(4, 2, "(nRF app, sensors.py, another dashboard) does not advertise.", dim)
        y = 6
    else:
        colw = max(22, (w - LABEL_W) // len(devices))
        width = colw - 2  # of a value; the rest separates the columns
        xs = [LABEL_W + i * colw for i in range(len(devices))]
        for d, x in zip(devices, xs):
            put(1, x, f"{d.label}  {d.address}" if d.label else d.address, curses.A_BOLD, width)
            if d.status == "connected":
                put(2, x, f"● connected  fw {d.fw}", colors.get("ok", 0), width)
            else:
                bad = d.note.startswith("connect failed")
                put(2, x, f"○ {d.status}  {d.note}", colors.get("bad" if bad else "wait", 0), width)

        def section(y, title):
            put(y, 0, f"── {title} ".ljust(w, "─"), dim)

        def row(y, label, sensor, fmt):
            put(y, 0, label)
            for d, x in zip(devices, xs):
                if sensor in d.missing:
                    put(y, x, "not in this firmware", dim, width)
                elif sensor not in d.values:
                    put(y, x, "no event yet" if sensor in EVENTS else "-", dim, width)
                else:
                    t, values = d.values[sensor]
                    age = age_text(now - t) if now - t >= 3 else ""
                    attr = (curses.A_BOLD if sensor == "temperature" else 0) | (dim if d.status != "connected" else 0)
                    put(y, x, fmt(values)[:width - len(age) - 1], attr)
                    put(y, x + width - len(age), age, dim)

        y = 3
        section(y, "Environment")
        for label, sensor, fmt in ENV_ROWS:
            y += 1
            row(y, label, sensor, fmt)
            if sensor == "temperature":
                y += 1
                put(y, 0, f"  last {TREND_WINDOW // 60} min", dim)
                for d, x in zip(devices, xs):
                    put(y, x, sparkline(d.temps, width - 12, now), dim if d.status != "connected" else 0, width)
        if state.motion:
            y += 1
            section(y, "Motion")
            for label, sensor, fmt in MOTION_ROWS:
                y += 1
                row(y, label, sensor, fmt)
        y += 1

    footer = f" q quit   m motion {'on' if state.motion else 'off'}"
    if y > h - 1:
        footer += "   (rows cut off: enlarge the window or press m)"
    if state.scanning:
        footer += "   scanning..."
    when, message = state.message
    if message and now - when < 15:
        footer += f"   {message}"
    put(h - 1, 0, footer.ljust(w), bar)
    scr.refresh()


async def ui(scr, state, manager):
    colors = init_colors()
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    curses.raw()  # Ctrl-C arrives as a key, so quitting always goes through the clean disconnect
    scr.nodelay(True)
    while not manager.done():
        while (key := scr.getch()) != -1:
            if key in (ord("q"), ord("Q"), 3):
                return
            if key in (ord("m"), ord("M")):
                task = asyncio.create_task(set_motion(state, not state.motion))
                state.tasks.add(task)
                task.add_done_callback(state.tasks.discard)
        draw(scr, state, colors)
        await asyncio.sleep(0.2)


async def run(scr, args):
    state = State(args)
    logging.basicConfig(handlers=[FooterLog(state)], level=logging.WARNING, force=True)
    manager = asyncio.create_task(manage(state))
    try:
        await ui(scr, state, manager)
    finally:
        manager.cancel()
        await asyncio.wait([manager])
        h, w = scr.getmaxyx()
        with contextlib.suppress(curses.error):
            scr.addnstr(h - 1, 0, " disconnecting...".ljust(w), w - 1, curses.A_REVERSE)
            scr.refresh()
        # Leave no Thingy connected: BlueZ would keep the link up and the Thingy would not advertise.
        await asyncio.gather(*(disconnect(d.client) for d in state.devices.values() if d.client))
    if not manager.cancelled() and manager.exception():
        raise manager.exception()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--addr", action="append", metavar="ADDR[=LABEL]",
                    help="only this Thingy, optionally with a column label; repeat for more")
    ap.add_argument("--name", default="Thingy", help="advertised name to look for without --addr")
    ap.add_argument("--no-motion", action="store_true", help="start with motion off (press m to turn it on)")
    args = ap.parse_args()
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(lambda scr: asyncio.run(run(scr, args)))
    except KeyboardInterrupt:
        pass
