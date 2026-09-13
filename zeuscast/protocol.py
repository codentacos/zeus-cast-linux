"""Serial wire protocol spoken by GAMDIAS LCD coolers.

Recovered from ZEUS CAST 1.4.3.39 (``GASerialControl.SerialMessenger``).

Every request and response is one frame::

    5A | LEN_HI LEN_LO | ASCII payload | CHECKSUM | 5A

* ``LEN`` is the unescaped frame length (payload + 5).
* ``CHECKSUM`` is the low byte of the sum of ``LEN_HI``, ``LEN_LO`` and the payload.
* Between the delimiters, 0x5A is sent as ``5B 01`` and 0x5B as ``5B 02``.

Payloads look like tiny HTTP requests::

    POST brightness 1\\r\\n
    SeqNumber=N\\r\\n            (only for device app firmware >= V1.0.10)
    ContentType=json\\r\\n
    ContentLength=12\\r\\n
    \\r\\n
    {"value":80}

Responses carry ``200\\r\\n`` followed by an optional JSON body.

File uploads (background JPEG/MP4, overlay PNG, firmware) take three steps:
a framed ``POST transport 1`` announcing name and size, the raw *unframed*
file bytes, then a framed ``POST transported 1`` carrying the MD5.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

FRAME_DELIMITER = 0x5A
ESCAPE = 0x5B
STATUS_OK = "200\r\n"
MAX_FRAME_LENGTH = 0xFFFF

# Device app firmware from this version on expects SeqNumber headers.
SEQUENCE_NUMBER_MIN_VERSION = (1, 0, 10)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f]+")


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == FRAME_DELIMITER:
            out += b"\x5b\x01"
        elif b == ESCAPE:
            out += b"\x5b\x02"
        else:
            out.append(b)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == ESCAPE and i + 1 < len(data) and data[i + 1] in (1, 2):
            out.append(FRAME_DELIMITER if data[i + 1] == 1 else ESCAPE)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def encode_frame(payload: bytes | str) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("ascii")
    length = len(payload) + 5
    if length > MAX_FRAME_LENGTH:
        raise ValueError(f"payload too large for one frame ({len(payload)} bytes)")
    body = bytes([(length >> 8) & 0xFF, length & 0xFF]) + payload
    body += bytes([checksum(body)])
    return bytes([FRAME_DELIMITER]) + escape(body) + bytes([FRAME_DELIMITER])


def parse_json_body(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(_CONTROL_CHARS.sub("", text[start : end + 1]))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


@dataclass
class Response:
    text: str
    body: dict | None = None
    valid_checksum: bool = True

    @classmethod
    def from_payload(cls, payload: bytes, valid_checksum: bool = True) -> Response:
        text = payload.decode("ascii", errors="replace")
        return cls(text=text, body=parse_json_body(text), valid_checksum=valid_checksum)

    @property
    def ok(self) -> bool:
        return STATUS_OK in self.text

    @property
    def state(self) -> str | None:
        value = self.body.get("state") if self.body else None
        return value if isinstance(value, str) else None

    @property
    def succeeded(self) -> bool:
        """A 200 whose JSON body, if any, doesn't report failure."""
        return self.ok and self.state != "failure"


def decode_frame(frame: bytes) -> Response:
    """Decode one complete frame, delimiters included."""
    if len(frame) < 2 or frame[0] != FRAME_DELIMITER or frame[-1] != FRAME_DELIMITER:
        raise ValueError("not a delimited frame")
    body = unescape(frame[1:-1])
    if len(body) < 3:
        raise ValueError("frame too short")
    declared = (body[0] << 8) | body[1]
    valid = declared == len(body) + 2 and checksum(body[:-1]) == body[-1]
    return Response.from_payload(body[2:-1], valid_checksum=valid)


class FrameReader:
    """Reassembles frames from a byte stream that may arrive in pieces."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Response]:
        self._buffer += data
        responses = []
        while True:
            start = self._buffer.find(FRAME_DELIMITER)
            if start < 0:
                self._buffer.clear()
                break
            end = self._buffer.find(FRAME_DELIMITER, start + 1)
            if end < 0:
                del self._buffer[:start]
                break
            try:
                responses.append(decode_frame(bytes(self._buffer[start : end + 1])))
            except ValueError:
                # Misaligned: "start" was the closing delimiter of an earlier frame.
                del self._buffer[:end]
                continue
            del self._buffer[: end + 1]
        return responses

    def pending(self) -> bytes:
        return bytes(self._buffer)


def build_request(command: str, body: dict | None = None, seq: int | None = None) -> str:
    """Build a request payload, e.g. ``build_request("POST brightness 1", {"value": 80})``."""
    if body is None:
        return f"{command}\r\n\r\n"
    content = json.dumps(body, separators=(",", ":"))
    header = f"SeqNumber={seq}\r\n" if seq is not None else ""
    return f"{command}\r\n{header}ContentType=json\r\nContentLength={len(content)}\r\n\r\n{content}"


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text or "")[:3])


def uses_sequence_numbers(app_version: str) -> bool:
    version = parse_version(app_version)
    return bool(version) and version >= SEQUENCE_NUMBER_MIN_VERSION
