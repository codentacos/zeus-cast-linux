"""Overlay (OSD) themes: transparent PNGs drawn over the background the cooler plays.

Layouts are designed on a 480 x 480 grid, kept inside the round safe area, and
drawn at 2x then downsampled for smooth edges.
"""

from __future__ import annotations

import calendar
import datetime as dt
import functools
import math
import os
import subprocess
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from .sensors import Reading, Snapshot, read_metric

DESIGN_SIZE = 480
SUPERSAMPLE = 2
WHITE = (255, 255, 255, 255)
DIM = (255, 255, 255, 160)
TRACK = (255, 255, 255, 48)
SHADE = (0, 0, 0, 115)
GAUGE_START = 135
GAUGE_SWEEP = 270


@dataclass(frozen=True)
class Option:
    key: str
    label: str
    kind: str  # "metric", "bool", "text" or "int"
    default: object
    allow_none: bool = False
    minimum: int = 0
    maximum: int = 0


@dataclass
class RenderContext:
    size: int
    now: dt.datetime
    snapshot: Snapshot | None
    options: dict
    accent: tuple[int, int, int, int] = (63, 184, 255, 255)
    fahrenheit: bool = False
    backdrop: bool = True

    def option(self, key: str, default=None):
        return self.options.get(key, default)

    def metric(self, key: str) -> Reading:
        return read_metric(self.snapshot or Snapshot(), key, self.fahrenheit)


def parse_color(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    value = (value or "").lstrip("#")
    try:
        r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        r, g, b = 63, 184, 255
    return (r, g, b, alpha)


@functools.lru_cache(maxsize=4)
def _font_file(weight: str) -> str | None:
    for pattern in (f"Poppins:{weight}", f"sans-serif:{weight}"):
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{file}\n%{family}", pattern], capture_output=True, text=True, timeout=3
            )
        except (OSError, subprocess.SubprocessError):
            return None
        path, _, family = result.stdout.partition("\n")
        if pattern.startswith("Poppins") and "Poppins" not in family:
            continue
        if path and os.path.exists(path):
            return path
    return None


@functools.lru_cache(maxsize=128)
def font(pixel_size: int, weight: str = "bold") -> ImageFont.FreeTypeFont:
    path = _font_file(weight)
    if path:
        try:
            return ImageFont.truetype(path, pixel_size)
        except OSError:
            pass
    return ImageFont.load_default(pixel_size)


class Canvas:
    """Drawing surface addressed in 480-unit design coordinates."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.k = size / DESIGN_SIZE * SUPERSAMPLE
        side = size * SUPERSAMPLE
        self.image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)

    def p(self, value: float) -> float:
        return value * self.k

    def _width(self, value: float) -> int:
        return max(1, round(self.p(value)))

    def _box(self, cx: float, cy: float, r: float) -> list[float]:
        return [self.p(cx - r), self.p(cy - r), self.p(cx + r), self.p(cy + r)]

    def text(self, x, y, text, size, fill=WHITE, weight="bold", anchor="mm") -> None:
        self.draw.text((self.p(x), self.p(y)), text, font=font(self._width(size), weight), fill=fill, anchor=anchor)

    def text_width(self, text: str, size: float, weight: str = "bold") -> float:
        return self.draw.textlength(text, font=font(self._width(size), weight)) / self.k

    def fit(self, text: str, max_width: float, size: float, weight: str = "bold") -> float:
        while size > 10 and self.text_width(text, size, weight) > max_width:
            size -= 2
        return size

    def circle(self, cx, cy, r, fill=None, outline=None, width=0) -> None:
        self.draw.ellipse(self._box(cx, cy, r), fill=fill, outline=outline, width=self._width(width) if width else 0)

    def arc(self, cx, cy, r, start, end, fill, width) -> None:
        self.draw.arc(self._box(cx, cy, r), start, end, fill=fill, width=self._width(width))

    def line(self, points, fill, width) -> None:
        self.draw.line([(self.p(x), self.p(y)) for x, y in points], fill=fill, width=self._width(width), joint="curve")

    def rounded(self, x0, y0, x1, y1, radius, fill) -> None:
        self.draw.rounded_rectangle([self.p(x0), self.p(y0), self.p(x1), self.p(y1)], radius=self.p(radius), fill=fill)

    def finish(self) -> Image.Image:
        return self.image.resize((self.size, self.size), Image.LANCZOS)


def draw_gauge(c: Canvas, cx: float, cy: float, r: float, reading: Reading, accent, width: float) -> None:
    c.arc(cx, cy, r, GAUGE_START, GAUGE_START + GAUGE_SWEEP, TRACK, width)
    if reading.fraction:
        c.arc(cx, cy, r, GAUGE_START, GAUGE_START + GAUGE_SWEEP * reading.fraction, accent, width)
    c.text(cx, cy - r * 0.08, reading.text, c.fit(reading.text, r * 1.35, r * 0.62))
    c.text(cx, cy + r * 0.36, reading.unit, r * 0.17, DIM, "regular")
    c.text(cx, cy + r * 0.84, reading.label, c.fit(reading.label, r * 1.1, r * 0.15), DIM)


class Theme:
    key = ""
    name = ""
    options: tuple[Option, ...] = ()
    has_overlay = True

    def defaults(self) -> dict:
        return {option.key: option.default for option in self.options}

    def render(self, ctx: RenderContext) -> Image.Image | None:
        c = Canvas(ctx.size)
        if ctx.backdrop:
            c.circle(240, 240, 236, fill=SHADE)
        self.draw(c, ctx)
        return c.finish()

    def draw(self, c: Canvas, ctx: RenderContext) -> None:
        raise NotImplementedError


class OneGaugeTheme(Theme):
    key = "gauge1"
    name = "System monitor: 1 gauge"
    options = (
        Option("metric", "Metric", "metric", "cpu_temp"),
        Option("secondary", "Secondary line", "metric", "cpu_usage", allow_none=True),
    )

    def draw(self, c, ctx):
        draw_gauge(c, 240, 246, 190, ctx.metric(ctx.option("metric", "cpu_temp")), ctx.accent, 22)
        secondary = ctx.option("secondary")
        if secondary:
            reading = ctx.metric(secondary)
            c.text(240, 112, f"{reading.label}  {reading.text}{reading.unit}", 24, DIM)


class TwoGaugeTheme(Theme):
    key = "gauge2"
    name = "System monitor: 2 gauges"
    options = (
        Option("metric1", "Left", "metric", "cpu_temp"),
        Option("metric2", "Right", "metric", "gpu_temp"),
    )

    def draw(self, c, ctx):
        draw_gauge(c, 130, 240, 94, ctx.metric(ctx.option("metric1", "cpu_temp")), ctx.accent, 13)
        draw_gauge(c, 350, 240, 94, ctx.metric(ctx.option("metric2", "gpu_temp")), ctx.accent, 13)


class ThreeGaugeTheme(Theme):
    key = "gauge3"
    name = "System monitor: 3 gauges"
    options = (
        Option("metric1", "Top", "metric", "cpu_usage"),
        Option("metric2", "Bottom left", "metric", "cpu_temp"),
        Option("metric3", "Bottom right", "metric", "ram_usage"),
    )

    def draw(self, c, ctx):
        draw_gauge(c, 240, 140, 88, ctx.metric(ctx.option("metric1", "cpu_usage")), ctx.accent, 12)
        draw_gauge(c, 140, 326, 80, ctx.metric(ctx.option("metric2", "cpu_temp")), ctx.accent, 11)
        draw_gauge(c, 340, 326, 80, ctx.metric(ctx.option("metric3", "ram_usage")), ctx.accent, 11)


class DashboardTheme(Theme):
    key = "dashboard"
    name = "System monitor: bars"
    options = (
        Option("metric1", "Row 1", "metric", "cpu_usage", allow_none=True),
        Option("metric2", "Row 2", "metric", "cpu_temp", allow_none=True),
        Option("metric3", "Row 3", "metric", "gpu_usage", allow_none=True),
        Option("metric4", "Row 4", "metric", "gpu_temp", allow_none=True),
        Option("metric5", "Row 5", "metric", "ram_usage", allow_none=True),
    )

    def draw(self, c, ctx):
        keys = [ctx.option(f"metric{i}") for i in range(1, 6)]
        readings = [ctx.metric(key) for key in keys if key]
        row = 66
        top = 240 - len(readings) * row / 2
        for index, reading in enumerate(readings):
            y = top + index * row
            c.text(82, y + 20, reading.label, 21, DIM, anchor="lm")
            c.text(398, y + 20, f"{reading.text}{reading.unit}", 27, anchor="rm")
            c.rounded(82, y + 42, 398, y + 52, 5, TRACK)
            if reading.fraction:
                c.rounded(82, y + 42, 82 + max(10, 316 * reading.fraction), y + 52, 5, ctx.accent)


class DigitalClockTheme(Theme):
    key = "digital_clock"
    name = "Digital clock"
    options = (
        Option("24h", "24-hour time", "bool", True),
        Option("show_date", "Show date", "bool", True),
    )

    def draw(self, c, ctx):
        now = ctx.now
        if ctx.option("24h", True):
            time_text, suffix = now.strftime("%H:%M"), ""
        else:
            time_text, suffix = f"{now.hour % 12 or 12}:{now.minute:02d}", now.strftime("%p")
        show_date = ctx.option("show_date", True)
        y = 212 if show_date else 240
        c.text(240, y, time_text, c.fit(time_text, 380, 132))
        if suffix:
            c.text(240, y - 92, suffix, 28, ctx.accent)
        if show_date:
            c.rounded(200, 292, 280, 297, 2, ctx.accent)
            c.text(240, 330, now.strftime("%A").upper(), 26, DIM)
            c.text(240, 368, f"{now.day} {now.strftime('%B')}", 30)


class AnalogClockTheme(Theme):
    key = "analog_clock"
    name = "Analog clock"
    options = (
        Option("numbers", "Show numbers", "bool", True),
        Option("second_hand", "Second hand (updates every refresh)", "bool", False),
    )

    @staticmethod
    def _point(fraction: float, length: float) -> tuple[float, float]:
        angle = fraction * 2 * math.pi
        return 240 + length * math.sin(angle), 240 - length * math.cos(angle)

    def draw(self, c, ctx):
        for tick in range(60):
            hour = tick % 5 == 0
            inner = 186 if hour else 202
            c.line([self._point(tick / 60, inner), self._point(tick / 60, 214)], WHITE if hour else DIM, 6 if hour else 2)
        if ctx.option("numbers", True):
            for hour in range(1, 13):
                x, y = self._point(hour / 12, 156)
                c.text(x, y, str(hour), 32)
        now = ctx.now
        minutes = now.minute + now.second / 60
        hours = now.hour % 12 + minutes / 60
        c.line([self._point(hours / 12 + 0.5, 22), self._point(hours / 12, 112)], WHITE, 12)
        c.line([self._point(minutes / 60 + 0.5, 22), self._point(minutes / 60, 172)], WHITE, 8)
        if ctx.option("second_hand", False):
            c.line([self._point(now.second / 60 + 0.5, 30), self._point(now.second / 60, 190)], ctx.accent, 3)
        c.circle(240, 240, 12, fill=ctx.accent)


class CalendarTheme(Theme):
    key = "calendar"
    name = "Calendar"
    options = (Option("monday_first", "Week starts on Monday", "bool", False),)

    def draw(self, c, ctx):
        today = ctx.now.date()
        first_weekday = 0 if ctx.option("monday_first", False) else 6
        month = calendar.Calendar(first_weekday).monthdayscalendar(today.year, today.month)
        c.text(240, 88, today.strftime("%B %Y").upper(), 30)
        names = [calendar.day_abbr[(first_weekday + i) % 7][:2].upper() for i in range(7)]
        step_x, step_y = 50, 44
        left = 240 - 3 * step_x
        top = 140 if len(month) < 6 else 132
        for col, name in enumerate(names):
            c.text(left + col * step_x, top, name, 19, DIM)
        for row, week in enumerate(month):
            y = top + 46 + row * step_y
            for col, day in enumerate(week):
                if not day:
                    continue
                x = left + col * step_x
                if day == today.day:
                    c.circle(x, y, 20, fill=ctx.accent)
                c.text(x, y, str(day), 23, WHITE if day == today.day else (255, 255, 255, 225), "bold" if day == today.day else "regular")


class NoteTheme(Theme):
    key = "note"
    name = "Note"
    options = (
        Option("text", "Text", "text", "Hello from Linux!"),
        Option("font_size", "Font size", "int", 44, minimum=16, maximum=120),
    )

    def draw(self, c, ctx):
        size = int(ctx.option("font_size", 44))
        lines: list[str] = []
        for paragraph in str(ctx.option("text", "")).splitlines() or [""]:
            words = paragraph.split()
            line = ""
            for word in words:
                candidate = f"{line} {word}".strip()
                if line and c.text_width(candidate, size) > 330:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
        height = size * 1.25
        top = 240 - (len(lines) - 1) * height / 2
        for index, line in enumerate(lines):
            c.text(240, top + index * height, line, c.fit(line, 360, size))


class BackgroundOnlyTheme(Theme):
    key = "background"
    name = "Background only"
    has_overlay = False

    def render(self, ctx):
        return None


THEMES: dict[str, Theme] = {
    theme.key: theme
    for theme in (
        OneGaugeTheme(),
        TwoGaugeTheme(),
        ThreeGaugeTheme(),
        DashboardTheme(),
        DigitalClockTheme(),
        AnalogClockTheme(),
        CalendarTheme(),
        NoteTheme(),
        BackgroundOnlyTheme(),
    )
}


def render_overlay(theme: Theme, ctx: RenderContext, width: int, height: int) -> Image.Image | None:
    """Render ``theme`` for a width x height panel; layouts are square, so they're centered."""
    ctx.size = min(width, height)
    square = theme.render(ctx)
    if square is None or (width, height) == square.size:
        return square
    panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    panel.alpha_composite(square, ((width - square.width) // 2, (height - square.height) // 2))
    return panel


def composite(background: Image.Image | None, overlay: Image.Image | None, width: int, height: int) -> Image.Image:
    """What the panel shows: the background with the overlay on top."""
    size = (width, height)
    base = background.convert("RGBA").resize(size) if background else Image.new("RGBA", size, (0, 0, 0, 255))
    if overlay is not None:
        base.alpha_composite(overlay.resize(size))
    return base.convert("RGB")
