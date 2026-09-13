import calendar
import datetime as dt
import hashlib
import json
import unittest

from zeuscast import protocol
from zeuscast.device import DeviceError, LCDDevice, device_timestamp


class FakeCooler:
    """Imitates the cooler's side of the serial link closely enough to exercise LCDDevice."""

    def __init__(self, app_version="V1.0.12", corrupt_md5=False, silent=False):
        self.app_version = app_version
        self.corrupt_md5 = corrupt_md5
        self.silent = silent
        self.requests: list[str] = []
        self.files: dict[str, bytes] = {}
        self.state = {"brightness": 60, "degree": 0}
        self._outgoing = bytearray()
        self._reader = protocol.FrameReader()
        self._receiving: tuple[str, int, bytearray] | None = None

    # The subset of pyserial that LCDDevice uses
    @property
    def in_waiting(self):
        return len(self._outgoing)

    def read(self, size=1):
        data = bytes(self._outgoing[:size])
        del self._outgoing[:size]
        return data

    def write(self, data):
        data = bytes(data)
        if self._receiving is not None:
            name, size, buffer = self._receiving
            buffer += data
            if len(buffer) >= size:
                self.files[name] = bytes(buffer[:size])
                self._receiving = None
                self._reply("200\r\nAckNumber=0\r\n")
            return len(data)
        for response in self._reader.feed(data):
            self._handle(response.text)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._outgoing.clear()

    def close(self):
        pass

    def _reply(self, text):
        if not self.silent:
            self._outgoing += protocol.encode_frame(text)

    def _handle(self, text):
        self.requests.append(text)
        command = text.split("\r\n", 1)[0]
        body = protocol.parse_json_body(text) or {}
        if command == "POST conn":
            info = {
                "Version": {"App": self.app_version, "Firmware": "FW1", "Hardware": "HW1"},
                "ProductId": "B547",
                "SN": "SN0001",
                "Brightness": self.state["brightness"],
                "Degree": self.state["degree"],
                "Mode": 0x10,
                "Logo": 1,
                "Space": -1,
            }
            self._reply("200\r\n" + json.dumps(info))
        elif command == "POST transport 1":
            self._receiving = (body["fileName"], body["fileSize"], bytearray())
            self._reply('200\r\n{"state":"success","blockMaxSize":4096}')
        elif command == "POST transported 1":
            data = self.files.get(body["fileName"], b"")
            matches = hashlib.md5(data).hexdigest() == body["md5"] and not self.corrupt_md5
            self._reply('200\r\n{"state":"%s"}' % ("success" if matches else "failure"))
        elif command == "POST brightness 1":
            self.state["brightness"] = body["value"]
            self._reply('200\r\n{"state":"success"}')
        else:
            self._reply("200\r\n")


def make_device(**kwargs):
    fake = FakeCooler(**kwargs)
    return LCDDevice("/dev/fake", transport=fake), fake


class DeviceTests(unittest.TestCase):
    def test_handshake_parses_info_case_insensitively(self):
        device, fake = make_device()
        info = device.handshake()
        self.assertEqual([r.split("\r\n", 1)[0] for r in fake.requests], ["POST power 1", "STATE timestamp 1", "POST conn"])
        self.assertEqual(info.serial_number, "SN0001")
        self.assertEqual(info.app_version, "V1.0.12")
        self.assertEqual(info.brightness, 60)
        self.assertIsNone(info.space)  # -1 means unknown
        self.assertTrue(device.use_sequence_numbers)

    def test_sequence_numbers_follow_firmware_version(self):
        device, fake = make_device()
        device.connect()
        device.set_brightness(30)
        self.assertIn("SeqNumber=", fake.requests[-1])
        self.assertEqual(fake.state["brightness"], 30)

        device, fake = make_device(app_version="V1.0.9")
        device.connect()
        device.set_brightness(30)
        self.assertNotIn("SeqNumber=", fake.requests[-1])

    def test_upload_binary_with_delimiter_bytes(self):
        device, fake = make_device()
        device.connect()
        data = bytes(range(256)) * 40  # plenty of 0x5A / 0x5B, more than one chunk
        progress = []
        device.upload(data, "BG_1.mp4", progress=lambda sent, total: progress.append((sent, total)))
        self.assertEqual(fake.files["BG_1.mp4"], data)
        self.assertEqual(progress[-1], (len(data), len(data)))
        self.assertTrue(fake.requests[-1].startswith("POST transported 1"))

    def test_overlay_file_name(self):
        device, fake = make_device()
        device.show_overlay(b"\x89PNG fake")
        self.assertTrue(any(name.startswith("OSD_") and name.endswith(".osd") for name in fake.files))

    def test_md5_rejection_raises(self):
        device, _ = make_device(corrupt_md5=True)
        with self.assertRaises(DeviceError):
            device.upload(b"data", "OSD_1.osd")

    def test_no_reply_raises(self):
        device, _ = make_device(silent=True)
        with self.assertRaises(DeviceError):
            device.request("POST conn", timeout=0.2)

    def test_clock_position_preserves_upper_mode_bits(self):
        device, fake = make_device()
        device.connect()
        device.set_clock_position(2)
        self.assertEqual(protocol.parse_json_body(fake.requests[-1]), {"value": 0x12})
        self.assertEqual(device.info.clock_position, 2)

    def test_timestamp_is_local_time_shifted_to_utc8(self):
        now = dt.datetime(2026, 1, 1, 8, 0, 0)
        self.assertEqual(device_timestamp(now), calendar.timegm((2026, 1, 1, 0, 0, 0)) * 1000)

    def test_rejects_invalid_arguments(self):
        device, _ = make_device()
        with self.assertRaises(ValueError):
            device.set_rotation(45)
        with self.assertRaises(ValueError):
            device.set_power_state("hibernate")


if __name__ == "__main__":
    unittest.main()
