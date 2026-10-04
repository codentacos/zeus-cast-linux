"""Background worker that owns the serial port.

It keeps the cooler connected, re-renders and uploads the overlay on a timer,
runs queued commands from the GUI/CLI, and follows system sleep.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import logging
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from PIL import Image

from . import media
from .config import Config, cache_dir
from .device import DEFAULT_MODEL, DeviceDisconnected, DeviceError, DeviceInfo, LCDDevice, Model
from .render import THEMES, RenderContext, parse_color, render_overlay
from .sensors import SensorReader, Snapshot

log = logging.getLogger(__name__)

RECONNECT_DELAY = 3.0
MAX_CONSECUTIVE_FAILURES = 3
SLEEP_GAP = 15.0  # wall clock running this far ahead of the monotonic clock means we slept


class Listener:
    """Engine callbacks. They run on the engine thread; override the ones you need."""

    def status_changed(self, connected: bool, message: str) -> None:
        pass

    def info_received(self, info: DeviceInfo) -> None:
        pass

    def overlay_rendered(self, image: Image.Image | None) -> None:
        pass

    def task_finished(self, description: str, error: str | None) -> None:
        pass

    def upload_progress(self, sent: int, total: int) -> None:
        pass


def _cached_background(is_video: bool) -> Path:
    return cache_dir() / ("background.mp4" if is_video else "background.jpg")


def prepare_background(path: str | Path, model: Model, mode: str) -> tuple[bytes, bool]:
    """Returns (file bytes, is_video) ready to upload, and keeps a copy in the cache for restores."""
    if media.is_video(path):
        output = _cached_background(True)
        media.prepare_video(path, output, model.width, model.height, mode)
        return output.read_bytes(), True
    data = media.prepare_image(path, model.width, model.height, mode)
    try:
        _cached_background(False).write_bytes(data)
    except OSError as e:  # the cache only saves work later; the upload can still go ahead
        log.warning("Can't cache the prepared background: %s", e)
    return data, False


def cached_background(path: str | Path, model: Model, mode: str, expected_md5: str | None) -> tuple[bytes, bool]:
    """Like prepare_background, but reuses the cached copy of the last upload when its MD5 still matches.

    This keeps reconnects (e.g. after a reboot) from re-running ffmpeg, and still works if the
    source file has been moved since it was uploaded.
    """
    if expected_md5:
        for is_video in (media.is_video(path), not media.is_video(path)):
            try:
                data = _cached_background(is_video).read_bytes()
            except OSError:
                continue
            if hashlib.md5(data).hexdigest() == expected_md5:
                return data, is_video
    if not Path(path).exists():
        raise media.MediaError(f"Background {path} no longer exists; choose it again")
    return prepare_background(path, model, mode)


def render_theme_overlay(config: Config, snapshot: Snapshot | None, model: Model, now: dt.datetime | None = None):
    theme = THEMES.get(config["theme"]) or THEMES["gauge1"]
    ctx = RenderContext(
        size=min(model.width, model.height),
        now=now or dt.datetime.now(),
        snapshot=snapshot,
        options=config.theme_options(theme),
        accent=parse_color(config["accent"]),
        fahrenheit=bool(config["fahrenheit"]),
        backdrop=bool(config["backdrop"]),
    )
    return theme, render_overlay(theme, ctx, model.width, model.height)


class PowerMonitor(threading.Thread):
    """Follows logind's PrepareForSleep/PrepareForShutdown signals through `gdbus monitor`."""

    def __init__(self, on_event: Callable[[str], None]) -> None:
        super().__init__(name="zeuscast-power", daemon=True)
        self.on_event = on_event
        self._process: subprocess.Popen | None = None

    def run(self) -> None:
        gdbus = shutil.which("gdbus")
        if not gdbus:
            log.info("gdbus not found; sleep is detected from clock jumps only")
            return
        command = [gdbus, "monitor", "--system", "--dest", "org.freedesktop.login1", "--object-path", "/org/freedesktop/login1"]
        try:
            self._process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            log.info("Can't watch logind: %s", e)
            return
        for line in self._process.stdout:
            if "PrepareForSleep (true" in line or "PrepareForShutdown (true" in line:
                self.on_event("suspend")  # ZEUS CAST also sends "suspend" on shutdown/logoff
            elif "PrepareForSleep (false" in line:
                self.on_event("resume")

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()


class Engine:
    def __init__(self, config: Config, listener: Listener | None = None, port: str | None = None) -> None:
        self.config = config
        self.listener = listener or Listener()
        self.port = port or config["port"]
        self.device: LCDDevice | None = None
        self.model: Model = DEFAULT_MODEL
        self.sensors = SensorReader()
        self._tasks: queue.Queue[tuple[str, Callable[[LCDDevice], None]]] = queue.Queue()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._render_now = False
        self._pending_power: str | None = None
        self._suspended = False
        self._failures = 0
        self._overlay_enabled: bool | None = None
        self._status: tuple[bool, str] | None = None
        self._thread: threading.Thread | None = None
        self._power_monitor = PowerMonitor(self.power_event)

    # Public API (any thread)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="zeuscast-engine")
        self._thread.start()
        self._power_monitor.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._power_monitor.stop()
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def refresh(self) -> None:
        """Re-render the overlay now (settings changed)."""
        self._render_now = True
        self._wake.set()

    def power_event(self, event: str) -> None:
        self._pending_power = event
        self._wake.set()

    def submit(self, description: str, action: Callable[[LCDDevice], None]) -> None:
        self._tasks.put((description, action))
        self._wake.set()

    def set_brightness(self, percent: int) -> None:
        self.submit(f"Brightness {percent}%", lambda d: d.set_brightness(percent))

    def set_rotation(self, degrees: int) -> None:
        self.submit(f"Rotation {degrees}°", lambda d: d.set_rotation(degrees))

    def set_startup_logo(self, option: int) -> None:
        self.submit("Startup screen", lambda d: d.set_startup_logo(option))

    def set_clock_position(self, position: int) -> None:
        self.submit("Hardware clock", lambda d: d.set_clock_position(position))

    def reboot(self) -> None:
        self.submit("Reboot", lambda d: d.reboot())

    def factory_reset(self) -> None:
        self.submit("Factory reset", lambda d: d.factory_reset())

    def upload_background(self, path: str | Path, mode: str) -> None:
        self.submit(f"Upload background {Path(path).name}", self._background_action(path, mode, use_cache=False))

    def _background_action(self, path: str | Path, mode: str, use_cache: bool) -> Callable[[LCDDevice], None]:
        def action(device: LCDDevice) -> None:
            if use_cache:
                expected = self.config["background"].get("uploaded_md5")
                data, is_video = cached_background(path, device.model, mode, expected)
            else:
                data, is_video = prepare_background(path, device.model, mode)
            if is_video:
                device.set_background_video(data, progress=self.listener.upload_progress)
            else:
                device.set_background_image(data, progress=self.listener.upload_progress)
            md5 = hashlib.md5(data).hexdigest()
            if self.config["background"] != {"path": str(path), "mode": mode, "uploaded_md5": md5}:
                self.config["background"] = {"path": str(path), "mode": mode, "uploaded_md5": md5}
                self.config.save()

        return action

    # Engine thread

    def _run(self) -> None:
        next_connect = next_render = 0.0
        last_wall, last_mono = time.time(), time.monotonic()
        while not self._stop.is_set():
            wall, mono = time.time(), time.monotonic()
            if (wall - last_wall) - (mono - last_mono) > SLEEP_GAP and self._pending_power is None:
                self._pending_power = "resume"
            last_wall, last_mono = wall, mono

            if self.device is None and mono >= next_connect:
                if not self._connect():
                    next_connect = time.monotonic() + RECONNECT_DELAY
            self._handle_power()
            self._drain_tasks()

            if self._render_now or time.monotonic() >= next_render:
                self._render_now = False
                self._update_overlay()
                next_render = time.monotonic() + max(0.5, float(self.config["update_interval"]))

            wake_at = next_render if self.device else min(next_render, next_connect)
            if self._wake.wait(max(0.05, wake_at - time.monotonic())):
                self._wake.clear()
        self._shutdown()

    def _set_status(self, connected: bool, message: str) -> None:
        if self._status != (connected, message):
            self._status = (connected, message)
            log.info("%s", message)
            self.listener.status_changed(connected, message)

    def _connect(self) -> bool:
        try:
            device = LCDDevice.open_first(self.port)
        except DeviceError as e:
            self._set_status(False, str(e))
            return False
        try:
            info = device.handshake()
        except DeviceError as e:
            device.close()
            self._set_status(False, f"Found {device.model.name} on {device.port}, but the handshake failed: {e}")
            return False
        self.device, self.model = device, device.model
        self._failures = 0
        self._overlay_enabled = None
        self._render_now = True
        self._set_status(True, f"Connected to {device.model.name} on {device.port} (app {info.app_version or '?'})")
        self.listener.info_received(info)
        self._restore_background()
        return True

    def _restore_background(self) -> None:
        """Queue the saved background again: the cooler forgets it when it loses power (e.g. a reboot)."""
        background = self.config["background"]
        if not self.config["restore_background"] or not background.get("path"):
            return
        path, mode = background["path"], background.get("mode") or "fill"
        # Tasks run before the next overlay render, so the background goes up first.
        self.submit(f"Restore background {Path(path).name}", self._background_action(path, mode, use_cache=True))

    def _drop_device(self, reason: str) -> None:
        if self.device:
            self.device.close()
        self.device = None
        self._set_status(False, reason)

    def _call(self, action: Callable[[LCDDevice], None]) -> str | None:
        """Run an action against the device; returns an error message or None."""
        if self.device is None:
            return "Cooler not connected"
        try:
            action(self.device)
        except DeviceDisconnected as e:
            self._drop_device(f"Disconnected: {e}")
            return str(e)
        except DeviceError as e:
            self._failures += 1
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                self._drop_device(f"Cooler stopped responding: {e}")
            return str(e)
        self._failures = 0
        return None

    def _handle_power(self) -> None:
        event, self._pending_power = self._pending_power, None
        if event == "suspend":
            self._suspended = True
            self._call(lambda d: (d.set_power_state("suspend"), d.set_overlay_enabled(False)))
            self._overlay_enabled = False
        elif event == "resume":
            self._suspended = False
            if self.device and self._call(lambda d: d.handshake()) is None:
                self.listener.info_received(self.device.info)
            self._render_now = True

    def _drain_tasks(self) -> None:
        while self.device is not None:
            try:
                description, action = self._tasks.get_nowait()
            except queue.Empty:
                return
            try:
                error = self._call(action)
            except (media.MediaError, ValueError, OSError) as e:
                error = str(e)
            except Exception as e:  # keep the engine alive whatever a task does
                log.exception("%s failed", description)
                error = f"{type(e).__name__}: {e}"
            self.listener.task_finished(description, error)
            if error is None and self.device and self.device.info:
                self.listener.info_received(self.device.info)

    def _update_overlay(self) -> None:
        theme, image = render_theme_overlay(self.config, self.sensors.read(), self.model)
        self.listener.overlay_rendered(image)
        if self.device is None or self._suspended:
            return
        if image is None:
            if self._overlay_enabled is not False and self._call(lambda d: d.set_overlay_enabled(False)) is None:
                self._overlay_enabled = False
            return
        png = io.BytesIO()
        image.save(png, "PNG")
        if self._call(lambda d: d.show_overlay(png.getvalue())) is None:
            self._overlay_enabled = True

    def _shutdown(self) -> None:
        if self.device:
            try:
                self.device.set_overlay_enabled(False)
            except DeviceError:
                pass
            self.device.close()
            self.device = None
