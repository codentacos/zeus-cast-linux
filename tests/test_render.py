import datetime as dt
import unittest

try:
    from zeuscast.render import THEMES, RenderContext, composite, render_overlay
    from zeuscast.sensors import Snapshot, read_metric
except ImportError as e:  # Pillow/psutil not installed
    raise unittest.SkipTest(str(e))


SNAPSHOT = Snapshot(
    cpu_usage=37.0,
    cpu_temp=64.5,
    cpu_freq_mhz=4200,
    ram_usage=52.0,
    gpu_usage=88.0,
    gpu_temp=71.0,
    vram_usage=40.0,
    disk_usage=63.0,
    net_down_bps=2_500_000,
    net_up_bps=12_000,
    fans={"fan1": 1450},
)


def context(options=None, snapshot=SNAPSHOT):
    return RenderContext(size=480, now=dt.datetime(2026, 9, 12, 14, 5, 30), snapshot=snapshot, options=options or {})


class RenderTests(unittest.TestCase):
    def test_every_theme_renders_square_and_portrait(self):
        for theme in THEMES.values():
            for width, height in ((480, 480), (272, 480)):
                with self.subTest(theme=theme.key, size=(width, height)):
                    image = render_overlay(theme, context(theme.defaults()), width, height)
                    if not theme.has_overlay:
                        self.assertIsNone(image)
                        continue
                    self.assertEqual(image.size, (width, height))
                    self.assertEqual(image.mode, "RGBA")
                    self.assertEqual(image.getpixel((0, 0))[3], 0)  # corners stay transparent
                    self.assertEqual(composite(None, image, width, height).size, (width, height))

    def test_missing_sensors_render_placeholders(self):
        for key in ("gauge1", "gauge3", "dashboard"):
            theme = THEMES[key]
            self.assertIsNotNone(render_overlay(theme, context(theme.defaults(), snapshot=None), 480, 480))

    def test_dashboard_skips_empty_rows(self):
        theme = THEMES["dashboard"]
        options = {"metric1": "cpu_temp", "metric2": None, "metric3": None, "metric4": None, "metric5": None}
        self.assertIsNotNone(render_overlay(theme, context(options), 480, 480))

    def test_metric_formatting(self):
        self.assertEqual(read_metric(SNAPSHOT, "cpu_temp").text, "64")
        self.assertEqual(read_metric(SNAPSHOT, "cpu_temp", fahrenheit=True).unit, "°F")
        self.assertEqual(read_metric(SNAPSHOT, "net_down").unit, "MB/s")
        self.assertEqual(read_metric(Snapshot(), "gpu_usage").text, "--")
        self.assertEqual(read_metric(SNAPSHOT, "fan").text, "1450")


if __name__ == "__main__":
    unittest.main()
