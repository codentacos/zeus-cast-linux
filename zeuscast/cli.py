"""Command-line entry point: GUI, headless daemon and one-shot device commands."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__, media
from .config import Config
from .device import MODELS, ROTATIONS, DeviceError, LCDDevice, find_devices
from .engine import Engine, Listener, prepare_background, render_theme_overlay
from .render import THEMES, composite
from .sensors import SensorReader

log = logging.getLogger("zeuscast")


def _open(args, config: Config) -> LCDDevice:
    device = LCDDevice.open_first(args.port or config["port"])
    try:
        device.handshake()
    except DeviceError:
        device.close()
        raise
    return device


def _parse_value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def cmd_gui(args, config: Config) -> int:
    try:
        from . import gui
    except ImportError as e:
        print(f"zeuscast: the GUI needs PySide6 ({e}). On Fedora: sudo dnf install python3-pyside6", file=sys.stderr)
        return 1
    return gui.run(config, port=args.port, minimized=getattr(args, "minimized", False))


class _LogListener(Listener):
    def task_finished(self, description, error):
        if error:
            log.error("%s failed: %s", description, error)
        else:
            log.info("%s: done", description)


def cmd_daemon(args, config: Config) -> int:
    engine = Engine(config, _LogListener(), port=args.port)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    engine.start()
    mtime = config.mtime()
    while not stop.wait(2):
        if config.mtime() != mtime:
            mtime = config.mtime()
            config.load()
            engine.refresh()
            log.info("Settings reloaded")
    engine.stop()
    return 0


def cmd_list(args, config: Config) -> int:
    devices = find_devices()
    if not devices:
        print("No GAMDIAS LCD coolers found.")
        return 1
    for d in devices:
        print(f"{d.path}\t{d.model.name} ({d.model.width}x{d.model.height})\tUSB 1b80:{d.model.product_id:04x}")
    return 0


def cmd_info(args, config: Config) -> int:
    with _open(args, config) as device:
        info = device.info
        if args.json:
            print(json.dumps(info.raw, indent=2))
            return 0
        rows = [
            ("Port", device.port),
            ("Model", f"{device.model.name} ({device.model.width}x{device.model.height})"),
            ("Product ID", info.product_id),
            ("Serial number", info.serial_number),
            ("App version", info.app_version),
            ("Firmware", info.firmware_version),
            ("Hardware", info.hardware_version),
            ("Protocol", "with SeqNumber" if device.use_sequence_numbers else "legacy"),
            ("Brightness", info.brightness),
            ("Rotation", info.rotation),
            ("Startup screen", {1: "default", 2: "custom"}.get(info.startup_logo, info.startup_logo)),
            ("Hardware clock", "off" if not info.clock_position else "ABC"[info.clock_position - 1]),
            ("Free space (raw)", info.space),
        ]
        for label, value in rows:
            print(f"{label + ':':18}{'-' if value is None else value}")
    return 0


def cmd_brightness(args, config: Config) -> int:
    with _open(args, config) as device:
        device.set_brightness(args.percent)
    return 0


def cmd_rotate(args, config: Config) -> int:
    with _open(args, config) as device:
        device.set_rotation(args.degrees)
    return 0


def cmd_background(args, config: Config) -> int:
    mode = "fit" if args.fit else "fill"
    with _open(args, config) as device:
        print(f"Preparing {args.file} for {device.model.width}x{device.model.height}…", file=sys.stderr)
        data, is_video = prepare_background(args.file, device.model, mode)

        def progress(sent: int, total: int) -> None:
            print(f"\rUploading {sent * 100 // max(total, 1):3d}%", end="", file=sys.stderr, flush=True)

        upload = device.set_background_video if is_video else device.set_background_image
        upload(data, progress=progress)
        print(file=sys.stderr)
    config["background"] = {"path": str(Path(args.file).resolve()), "mode": mode, "uploaded_md5": None}
    config.save()
    return 0


def cmd_theme(args, config: Config) -> int:
    theme = THEMES[args.name]
    config["theme"] = theme.key
    known = {option.key for option in theme.options}
    for assignment in args.set or []:
        key, _, value = assignment.partition("=")
        if key not in known:
            print(f"zeuscast: {theme.key} has no option {key!r} (options: {', '.join(sorted(known)) or 'none'})", file=sys.stderr)
            return 2
        config.set_theme_option(theme.key, key, _parse_value(value))
    config.save()
    print(f"Theme set to {theme.name}; a running zeuscast picks it up automatically.")
    return 0


def cmd_clock(args, config: Config) -> int:
    with _open(args, config) as device:
        device.set_clock_position(args.position)
    return 0


def cmd_startup_logo(args, config: Config) -> int:
    with _open(args, config) as device:
        device.set_startup_logo(1 if args.choice == "default" else 2)
    return 0


def cmd_overlay_off(args, config: Config) -> int:
    with _open(args, config) as device:
        device.set_overlay_enabled(False)
    return 0


def cmd_reboot(args, config: Config) -> int:
    with _open(args, config) as device:
        device.reboot()
    return 0


def cmd_raw(args, config: Config) -> int:
    with _open(args, config) as device:
        body = json.loads(args.json) if args.json else None
        print(device.request(args.request, body).text)
    return 0


def cmd_preview(args, config: Config) -> int:
    if args.theme:
        config["theme"] = args.theme
    model = MODELS[int(args.model, 16)]
    reader = SensorReader()
    time.sleep(0.5)  # let CPU usage and network rates accumulate
    _, overlay = render_theme_overlay(config, reader.read(), model, dt.datetime.now())
    background = media.load_preview(args.background, model.width, model.height) if args.background else None
    composite(background, overlay, model.width, model.height).save(args.output)
    print(f"Wrote {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zeuscast", description="Control the LCD on GAMDIAS coolers.")
    parser.add_argument("--version", action="version", version=f"zeuscast {__version__}")
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log protocol traffic")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("gui", help="open the settings window (default)")
    p.add_argument("--minimized", action="store_true", help="start in the system tray")
    p.set_defaults(func=cmd_gui)
    sub.add_parser("daemon", help="run headless with the saved settings").set_defaults(func=cmd_daemon)
    sub.add_parser("list", help="list connected coolers").set_defaults(func=cmd_list)
    p = sub.add_parser("info", help="show device information")
    p.add_argument("--json", action="store_true", help="print the raw handshake reply")
    p.set_defaults(func=cmd_info)
    p = sub.add_parser("brightness", help="set panel brightness")
    p.add_argument("percent", type=int)
    p.set_defaults(func=cmd_brightness)
    p = sub.add_parser("rotate", help="rotate the picture")
    p.add_argument("degrees", type=int, choices=ROTATIONS)
    p.set_defaults(func=cmd_rotate)
    p = sub.add_parser("background", help="upload a background image or video")
    p.add_argument("file")
    p.add_argument("--fit", action="store_true", help="letterbox instead of cropping")
    p.set_defaults(func=cmd_background)
    p = sub.add_parser("theme", help="choose the overlay theme used by gui/daemon")
    p.add_argument("name", choices=list(THEMES))
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help='theme option, e.g. --set text="Hi"')
    p.set_defaults(func=cmd_theme)
    p = sub.add_parser("clock", help="firmware clock: 0 = off, 1-3 = layouts A-C")
    p.add_argument("position", type=int, choices=range(4))
    p.set_defaults(func=cmd_clock)
    p = sub.add_parser("startup-logo", help="boot screen")
    p.add_argument("choice", choices=("default", "custom"))
    p.set_defaults(func=cmd_startup_logo)
    sub.add_parser("overlay-off", help="hide the overlay").set_defaults(func=cmd_overlay_off)
    sub.add_parser("reboot", help="restart the display controller").set_defaults(func=cmd_reboot)
    p = sub.add_parser("raw", help="send a raw request, e.g. raw 'POST brightness 1' '{\"value\":50}'")
    p.add_argument("request")
    p.add_argument("json", nargs="?")
    p.set_defaults(func=cmd_raw)
    p = sub.add_parser("preview", help="render what the panel would show to a PNG (no device needed)")
    p.add_argument("output")
    p.add_argument("--theme", choices=list(THEMES))
    p.add_argument("--background", help="image or video to draw behind the overlay")
    p.add_argument("--model", default="b547", choices=[f"{pid:04x}" for pid in MODELS])
    p.set_defaults(func=cmd_preview)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config()
    handler = getattr(args, "func", cmd_gui)
    try:
        return handler(args, config)
    except (DeviceError, media.MediaError) as e:
        print(f"zeuscast: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
