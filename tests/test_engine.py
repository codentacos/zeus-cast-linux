import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from zeuscast.config import Config
from zeuscast.device import LCDDevice
from zeuscast.engine import Engine, Listener

from .test_device import FakeCooler


class RecordingListener(Listener):
    def __init__(self):
        self.finished: list[tuple[str, str | None]] = []

    def task_finished(self, description, error):
        self.finished.append((description, error))


class RestoreBackgroundTests(unittest.TestCase):
    """The cooler forgets its background when it loses power, so connecting must re-send it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Keep config.json and the prepared-background cache inside the temp dir
        env = {"XDG_CONFIG_HOME": str(self.tmp / "config"), "XDG_CACHE_HOME": str(self.tmp / "cache")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

        self.fake = FakeCooler()
        # Every connect gets a fresh LCDDevice talking to the same simulated cooler
        opener = mock.patch.object(LCDDevice, "open_first", side_effect=lambda port=None: LCDDevice("/dev/fake", transport=self.fake))
        opener.start()
        self.addCleanup(opener.stop)

        self.image = self.tmp / "wall.png"
        Image.new("RGB", (64, 64), (200, 30, 30)).save(self.image)
        self.config = Config()
        self.listener = RecordingListener()

    def connect(self) -> Engine:
        # Run the engine-thread steps by hand instead of starting the thread
        engine = Engine(self.config, self.listener)
        self.assertTrue(engine._connect())
        engine._drain_tasks()
        engine._update_overlay()
        return engine

    def uploaded(self, prefix: str) -> list[str]:
        return [name for name in self.fake.files if name.startswith(prefix)]

    def test_connect_restores_saved_background_before_overlay(self):
        self.config["background"] = {"path": str(self.image), "mode": "fill", "uploaded_md5": None}
        self.connect()
        names = list(self.fake.files)
        self.assertEqual(len(self.uploaded("BG_")), 1)
        self.assertTrue(names[0].startswith("BG_") and names[0].endswith(".jpg"))
        self.assertTrue(any(name.startswith("OSD_") for name in names[1:]))
        self.assertEqual(self.listener.finished, [("Restore background wall.png", None)])
        # The MD5 of what was sent is remembered for the next restore
        data = self.fake.files[names[0]]
        self.assertEqual(self.config["background"]["uploaded_md5"], hashlib.md5(data).hexdigest())

    def test_nothing_restored_without_background_or_when_disabled(self):
        self.connect()
        self.assertEqual(self.uploaded("BG_"), [])

        self.config["background"] = {"path": str(self.image), "mode": "fill", "uploaded_md5": None}
        self.config["restore_background"] = False
        self.connect()
        self.assertEqual(self.uploaded("BG_"), [])

    def test_restore_uses_cache_when_source_is_gone(self):
        self.config["background"] = {"path": str(self.image), "mode": "fill", "uploaded_md5": None}
        self.connect()
        first = self.fake.files[self.uploaded("BG_")[0]]

        # Simulate a reboot after the original file was moved away
        self.image.unlink()
        self.fake.files.clear()
        self.connect()
        self.assertEqual(self.fake.files[self.uploaded("BG_")[0]], first)

    def test_missing_source_and_cache_reports_error_and_stays_connected(self):
        self.config["background"] = {"path": str(self.tmp / "gone.png"), "mode": "fill", "uploaded_md5": None}
        engine = self.connect()
        self.assertEqual(self.uploaded("BG_"), [])
        description, error = self.listener.finished[0]
        self.assertEqual(description, "Restore background gone.png")
        self.assertIn("no longer exists", error)
        self.assertIsNotNone(engine.device)


if __name__ == "__main__":
    unittest.main()
