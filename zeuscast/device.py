"""Driver for GAMDIAS LCD coolers, which show up as a USB CDC-ACM serial port."""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol
from .protocol import FrameReader, Response

log = logging.getLogger(__name__)

VENDOR_ID = 0x1B80
CHUNK_SIZE = 1024  # ZEUS CAST writes in 1 KiB pieces
RESPONSE_TIMEOUT = 3.0
ROTATIONS = (0, 90, 180, 270)
POWER_EVENTS = ("resume", "suspend", "shutdown", "lock-screen", "unlock-screen")
# The firmware keeps time in UTC+8; ZEUS CAST compensates by sending local wall-clock time minus 8 h.
DEVICE_CLOCK_OFFSET_MS = 8 * 3600 * 1000

_ACK_NUMBER = re.compile(r"AckNumber=(\d+)")


@dataclass(frozen=True)
class Model:
    product_id: int
    name: str
    width: int
    height: int


MODELS = {
    model.product_id: model
    for model in (
        Model(0xB547, "GAMDIAS AURA LCD", 480, 480),
        Model(0xB53D, "GAMDIAS CHIONE LCD", 480, 480),
        Model(0xB53B, "GAMDIAS BOREAS LCD", 272, 480),
    )
}
DEFAULT_MODEL = MODELS[0xB547]


class DeviceError(Exception):
    """A command failed or the device answered unexpectedly."""


class DeviceNotFound(DeviceError):
    pass


class DeviceUnavailable(DeviceError):
    """The port exists but can't be opened (permissions, or another program has it)."""


class DeviceDisconnected(DeviceError):
    """Reading or writing the port failed; the device is probably gone."""


@dataclass
class DevicePort:
    path: str
    model: Model
    serial_number: str | None = None


def find_devices() -> list[DevicePort]:
    from serial.tools import list_ports

    return [
        DevicePort(port.device, MODELS[port.pid], port.serial_number)
        for port in sorted(list_ports.comports(), key=lambda p: p.device)
        if port.vid == VENDOR_ID and port.pid in MODELS
    ]


def _field(data: dict, name: str, default=None):
    """Case-insensitive lookup; ZEUS CAST parses replies with Json.NET, which ignores case."""
    for key, value in data.items():
        if key.lower() == name.lower():
            return value
    return default


def _number(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value == -1:
        return None
    return int(value)


@dataclass
class DeviceInfo:
    raw: dict
    product_id: str | None = None
    serial_number: str | None = None
    os: str | None = None
    app_version: str = ""
    firmware_version: str | None = None
    hardware_version: str | None = None
    brightness: int | None = None
    rotation: int | None = None
    mode: int | None = None
    osd_state: int | None = None
    startup_logo: int | None = None
    timeout: int | None = None
    space: int | None = None
    attributes: list = field(default_factory=list)

    @classmethod
    def from_json(cls, body: dict) -> DeviceInfo:
        version = _field(body, "version")
        version = version if isinstance(version, dict) else {}
        attributes = _field(body, "attribute")
        return cls(
            raw=body,
            product_id=_field(body, "productId"),
            serial_number=_field(body, "sn"),
            os=_field(body, "os"),
            app_version=str(_field(version, "app") or ""),
            firmware_version=_field(version, "firmware"),
            hardware_version=_field(version, "hardware"),
            brightness=_number(_field(body, "brightness")),
            rotation=_number(_field(body, "degree")),
            mode=_number(_field(body, "mode")),
            osd_state=_number(_field(body, "osdState")),
            startup_logo=_number(_field(body, "logo")),
            timeout=_number(_field(body, "timeout")),
            space=_number(_field(body, "space")),
            attributes=attributes if isinstance(attributes, list) else [],
        )

    @property
    def clock_position(self) -> int:
        """Built-in hardware clock layout: 0 = off, 1-3 = layouts A-C."""
        return (self.mode or 0) & 0xF


def device_timestamp(now: dt.datetime | None = None) -> int:
    local = (now or dt.datetime.now()).replace(tzinfo=None)
    return calendar.timegm(local.timetuple()) * 1000 + local.microsecond // 1000 - DEVICE_CLOCK_OFFSET_MS


class LCDDevice:
    def __init__(self, port: str, model: Model = DEFAULT_MODEL, transport=None) -> None:
        self.port = port
        self.model = model
        self.info: DeviceInfo | None = None
        self.use_sequence_numbers = False
        self._serial = transport
        self._lock = threading.RLock()
        self._seq = 100

    @classmethod
    def open_first(cls, port: str | None = None) -> LCDDevice:
        devices = find_devices()
        if port:
            model = next((d.model for d in devices if d.path == port), DEFAULT_MODEL)
        elif devices:
            port, model = devices[0].path, devices[0].model
        else:
            raise DeviceNotFound("No GAMDIAS LCD cooler found (USB 1b80:b547, b53d or b53b)")
        device = cls(port, model)
        device.open()
        return device

    def open(self) -> None:
        if self._serial is not None:
            return
        import serial

        try:
            self._serial = serial.Serial(self.port, 115200, timeout=0, write_timeout=5, exclusive=True)
        except serial.SerialException as e:
            message = str(e)
            if "Permission denied" in message:
                raise DeviceUnavailable(
                    f"Permission denied opening {self.port}. Install packaging/60-zeuscast.rules "
                    "(see README) and replug the cooler."
                ) from e
            if "lock" in message.lower() or "busy" in message.lower():
                raise DeviceUnavailable(
                    f"{self.port} is in use by another program (is the zeuscast service already running?)"
                ) from e
            raise DeviceError(f"Can't open {self.port}: {message}") from e

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except OSError:
                    pass
                self._serial = None

    def __enter__(self) -> LCDDevice:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    # Transport

    def _port(self):
        if self._serial is None:
            raise DeviceDisconnected("Device is not open")
        return self._serial

    def _write(self, data: bytes, progress: Callable[[int, int], None] | None = None) -> None:
        port = self._port()
        try:
            for offset in range(0, len(data), CHUNK_SIZE):
                port.write(data[offset : offset + CHUNK_SIZE])
                if progress and (offset // CHUNK_SIZE) % 64 == 0:
                    progress(offset, len(data))
            port.flush()
        except OSError as e:
            raise DeviceDisconnected(f"Write to {self.port} failed: {e}") from e
        if progress:
            progress(len(data), len(data))

    def _discard_input(self) -> None:
        try:
            self._port().reset_input_buffer()
        except OSError as e:
            raise DeviceDisconnected(f"{self.port} failed: {e}") from e

    def _read_response(self, timeout: float) -> Response | None:
        port = self._port()
        reader = FrameReader()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = port.read(port.in_waiting or 1)
            except OSError as e:
                raise DeviceDisconnected(f"Read from {self.port} failed: {e}") from e
            if not chunk:
                time.sleep(0.02)
                continue
            responses = reader.feed(chunk)
            if responses:
                if not responses[0].valid_checksum:
                    log.debug("Reply with bad length/checksum: %r", responses[0].text)
                return responses[0]
        leftover = reader.pending()
        if leftover:  # half a frame: keep whatever text arrived
            return Response.from_payload(leftover.strip(bytes([protocol.FRAME_DELIMITER])), valid_checksum=False)
        return None

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _header_seq(self, seq: int) -> int | None:
        return seq if self.use_sequence_numbers else None

    # Requests

    def request(self, command: str, body: dict | None = None, timeout: float = RESPONSE_TIMEOUT) -> Response:
        with self._lock:
            seq = self._next_seq()
            payload = protocol.build_request(command, body, self._header_seq(seq) if body is not None else None)
            log.debug("-> %r", payload)
            self._discard_input()
            self._write(protocol.encode_frame(payload))
            response = self._read_response(timeout)
        if response is None:
            raise DeviceError(f"No reply to {command!r}")
        log.debug("<- %r", response.text)
        if not response.succeeded:
            log.warning("%s was not accepted: %r", command, response.text)
        return response

    def connect(self) -> DeviceInfo:
        response = self.request("POST conn")
        if not response.body:
            raise DeviceError(f"Unexpected handshake reply: {response.text!r}")
        self.info = DeviceInfo.from_json(response.body)
        self.use_sequence_numbers = protocol.uses_sequence_numbers(self.info.app_version)
        return self.info

    def handshake(self) -> DeviceInfo:
        """What ZEUS CAST sends whenever it (re)connects: power resume, clock sync, then conn."""
        self.set_power_state("resume")
        self.sync_clock()
        return self.connect()

    def set_brightness(self, percent: int) -> Response:
        return self.request("POST brightness 1", {"value": max(0, min(100, int(percent)))})

    def set_rotation(self, degrees: int) -> Response:
        if degrees not in ROTATIONS:
            raise ValueError(f"rotation must be one of {ROTATIONS}")
        return self.request("POST rotate 1", {"degree": degrees})

    def set_power_state(self, event: str) -> Response:
        if event not in POWER_EVENTS:
            raise ValueError(f"power event must be one of {POWER_EVENTS}")
        return self.request("POST power 1", {"event": event})

    def sync_clock(self, now: dt.datetime | None = None) -> Response:
        return self.request("STATE timestamp 1", {"timestamp": device_timestamp(now)})

    def set_overlay_enabled(self, enabled: bool) -> Response:
        return self.request("POST osdState 1", {"enable": bool(enabled)})

    def set_startup_logo(self, option: int) -> Response:
        """1 = GAMDIAS boot logo, 2 = custom."""
        return self.request("POST logo 1", {"option": int(option)})

    def set_idle_timeout(self, value: int) -> Response:
        return self.request("POST timeout 1", {"value": int(value)})

    def set_mode(self, value: int) -> Response:
        return self.request("POST mode 1", {"value": int(value)})

    def set_clock_position(self, position: int) -> None:
        """Show the firmware's built-in clock: 0 = off, 1-3 = layouts A-C."""
        if position not in (0, 1, 2, 3):
            raise ValueError("clock position must be 0-3")
        mode = ((self.info.mode or 0) & 0xFFF0 if self.info else 0) | position
        if position:
            self.request("POST presetThemeId 1", {"index": 0})
        self.request("POST presetThemeId 1", {"index": position})
        self.set_mode(mode)
        if self.info:
            self.info.mode = mode

    def set_realtime_display(self, enabled: bool) -> Response:
        return self.request("POST realtimeDisplay 1", {"enable": bool(enabled)})

    def reboot(self) -> Response:
        return self.request("POST reboot 1", {"enable": True})

    def factory_reset(self) -> Response:
        return self.request("POST recovery 1", {"enable": True})

    # File transfer

    def upload(self, data: bytes, filename: str, kind: str = "media", progress: Callable[[int, int], None] | None = None) -> None:
        with self._lock:
            seq = self._next_seq()
            header_seq = self._header_seq(seq)

            announce = {"type": kind, "fileName": filename, "fileSize": len(data)}
            self._discard_input()
            self._write(protocol.encode_frame(protocol.build_request("POST transport 1", announce, header_seq)))
            reply = self._read_response(RESPONSE_TIMEOUT)
            if reply is None or reply.state == "failure":
                raise DeviceError(f"Device refused {filename}: {reply.text.strip() if reply else 'no reply'}")

            self._write(data, progress)
            reply = self._read_response(RESPONSE_TIMEOUT + len(data) / 1_000_000 * 2)
            if reply is None or not reply.ok:
                raise DeviceError(f"{filename} data not acknowledged: {reply.text.strip() if reply else 'no reply'}")
            ack = _ACK_NUMBER.search(reply.text)
            if self.use_sequence_numbers and ack and int(ack.group(1)) not in (seq + 1, 0, 1):
                log.warning("Unexpected AckNumber %s for SeqNumber %s", ack.group(1), seq)

            done = {"fileName": filename, "md5": hashlib.md5(data).hexdigest()}
            self._discard_input()
            self._write(protocol.encode_frame(protocol.build_request("POST transported 1", done, header_seq)))
            reply = self._read_response(RESPONSE_TIMEOUT)
            if reply is None or not reply.succeeded:
                raise DeviceError(f"{filename} failed verification: {reply.text.strip() if reply else 'no reply'}")

    def show_overlay(self, png: bytes) -> None:
        """Replace the overlay drawn over the background (a transparent PNG at panel size)."""
        self.upload(png, f"OSD_{self._seq + 1}.osd")

    def set_background_image(self, jpeg: bytes, progress: Callable[[int, int], None] | None = None) -> None:
        self.upload(jpeg, f"BG_{self._seq + 1}.jpg", progress=progress)

    def set_background_video(self, mp4: bytes, progress: Callable[[int, int], None] | None = None) -> None:
        self.upload(mp4, f"BG_{self._seq + 1}.mp4", progress=progress)
