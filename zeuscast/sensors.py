"""Hardware sensor readings on Linux (psutil, hwmon, nvidia-smi, amdgpu sysfs)."""

from __future__ import annotations

import glob
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

import psutil

# (hwmon chip, preferred labels) in priority order.
_CPU_SENSORS = (
    ("k10temp", ("Tctl", "Tdie")),
    ("zenpower", ("Tctl", "Tdie")),
    ("coretemp", ("Package id 0",)),
    ("cpu_thermal", ()),
    ("acpitz", ()),
)


@dataclass
class Snapshot:
    cpu_usage: float | None = None
    cpu_temp: float | None = None
    cpu_freq_mhz: float | None = None
    ram_used_gb: float | None = None
    ram_total_gb: float | None = None
    ram_usage: float | None = None
    gpu_name: str | None = None
    gpu_usage: float | None = None
    gpu_temp: float | None = None
    vram_usage: float | None = None
    disk_usage: float | None = None
    net_down_bps: float | None = None
    net_up_bps: float | None = None
    fans: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Metric:
    key: str
    label: str


METRICS = {
    m.key: m
    for m in (
        Metric("cpu_usage", "CPU"),
        Metric("cpu_temp", "CPU TEMP"),
        Metric("cpu_freq", "CPU FREQ"),
        Metric("gpu_usage", "GPU"),
        Metric("gpu_temp", "GPU TEMP"),
        Metric("vram_usage", "VRAM"),
        Metric("ram_usage", "RAM"),
        Metric("disk_usage", "DISK"),
        Metric("net_down", "DOWNLOAD"),
        Metric("net_up", "UPLOAD"),
        Metric("fan", "FAN"),
    )
}


@dataclass
class Reading:
    label: str
    text: str
    unit: str
    fraction: float | None  # 0..1 for gauges, None when there's no natural scale


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def format_rate(bytes_per_second: float) -> tuple[str, str]:
    value = bytes_per_second
    for unit in ("B/s", "KB/s", "MB/s"):
        if value < 1000:
            return (f"{value:.0f}" if value >= 10 or unit == "B/s" else f"{value:.1f}"), unit
        value /= 1024
    return f"{value:.1f}", "GB/s"


def read_metric(snapshot: Snapshot, key: str, fahrenheit: bool = False) -> Reading:
    label = METRICS[key].label if key in METRICS else key.upper()
    if key in ("cpu_temp", "gpu_temp"):
        unit = "°F" if fahrenheit else "°C"
        celsius = getattr(snapshot, key)
        if celsius is None:
            return Reading(label, "--", unit, None)
        shown = celsius * 9 / 5 + 32 if fahrenheit else celsius
        return Reading(label, f"{shown:.0f}", unit, _clamp(celsius / 100))
    if key == "cpu_freq":
        mhz = snapshot.cpu_freq_mhz
        return Reading(label, "--" if mhz is None else f"{mhz / 1000:.1f}", "GHz", None)
    if key in ("net_down", "net_up"):
        rate = snapshot.net_down_bps if key == "net_down" else snapshot.net_up_bps
        if rate is None:
            return Reading(label, "--", "B/s", None)
        text, unit = format_rate(rate)
        # Log scale up to ~1 Gbit/s so everyday traffic still moves the gauge.
        return Reading(label, text, unit, _clamp(math.log10(1 + rate) / math.log10(1 + 125e6)))
    if key == "fan":
        rpm = max(snapshot.fans.values()) if snapshot.fans else None
        return Reading(label, "--" if rpm is None else str(rpm), "RPM", None if rpm is None else _clamp(rpm / 3000))
    percent = getattr(snapshot, key, None)
    if percent is None:
        return Reading(label, "--", "%", None)
    return Reading(label, f"{percent:.0f}", "%", _clamp(percent / 100))


def _read_float(path: str, scale: float = 1.0) -> float | None:
    try:
        with open(path) as f:
            return float(f.read().strip()) / scale
    except (OSError, ValueError):
        return None


def _to_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


class SensorReader:
    def __init__(self, disk_path: str = "/") -> None:
        self.disk_path = disk_path
        self._last_net: tuple[float, int, int] | None = None
        self._nvidia_smi = shutil.which("nvidia-smi")
        self._amd_device = self._find_amd_device()
        psutil.cpu_percent(interval=None)  # the first call only primes the counters

    def read(self) -> Snapshot:
        s = Snapshot()
        s.cpu_usage = psutil.cpu_percent(interval=None)
        freq = psutil.cpu_freq()
        s.cpu_freq_mhz = freq.current if freq else None
        s.cpu_temp = self._cpu_temp()
        mem = psutil.virtual_memory()
        s.ram_used_gb = (mem.total - mem.available) / 1024**3
        s.ram_total_gb = mem.total / 1024**3
        s.ram_usage = mem.percent
        try:
            s.disk_usage = psutil.disk_usage(self.disk_path).percent
        except OSError:
            pass
        self._read_network(s)
        if not (self._nvidia_smi and self._read_nvidia(s)) and self._amd_device:
            self._read_amd(s)
        self._read_fans(s)
        return s

    @staticmethod
    def _cpu_temp() -> float | None:
        try:
            temps = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        for chip, labels in _CPU_SENSORS:
            entries = temps.get(chip)
            if not entries:
                continue
            for label in labels:
                for entry in entries:
                    if entry.label == label:
                        return entry.current
            return entries[0].current
        return None

    def _read_network(self, s: Snapshot) -> None:
        counters = psutil.net_io_counters(pernic=True)
        sent = sum(c.bytes_sent for name, c in counters.items() if name != "lo")
        received = sum(c.bytes_recv for name, c in counters.items() if name != "lo")
        now = time.monotonic()
        if self._last_net:
            then, prev_sent, prev_received = self._last_net
            elapsed = now - then
            if elapsed > 0:
                s.net_up_bps = max(0.0, (sent - prev_sent) / elapsed)
                s.net_down_bps = max(0.0, (received - prev_received) / elapsed)
        self._last_net = (now, sent, received)

    def _read_nvidia(self, s: Snapshot) -> bool:
        query = "name,utilization.gpu,temperature.gpu,memory.used,memory.total"
        try:
            result = subprocess.run(
                [self._nvidia_smi, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
        except subprocess.TimeoutExpired:
            return False
        except (OSError, subprocess.CalledProcessError):
            self._nvidia_smi = None  # driver not loaded; stop trying
            return False
        lines = result.stdout.strip().splitlines()
        parts = [p.strip() for p in lines[0].split(",")] if lines else []
        if len(parts) < 5:
            return False
        s.gpu_name = parts[0]
        s.gpu_usage = _to_float(parts[1])
        s.gpu_temp = _to_float(parts[2])
        used, total = _to_float(parts[3]), _to_float(parts[4])
        if used is not None and total:
            s.vram_usage = used / total * 100
        return True

    @staticmethod
    def _find_amd_device() -> str | None:
        for device in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
            if os.path.exists(os.path.join(device, "gpu_busy_percent")):
                return device
        return None

    def _read_amd(self, s: Snapshot) -> None:
        device = self._amd_device
        s.gpu_name = "AMD GPU"
        s.gpu_usage = _read_float(f"{device}/gpu_busy_percent")
        used = _read_float(f"{device}/mem_info_vram_used")
        total = _read_float(f"{device}/mem_info_vram_total")
        if used is not None and total:
            s.vram_usage = used / total * 100
        for hwmon in sorted(glob.glob(f"{device}/hwmon/hwmon*")):
            temp = _read_float(f"{hwmon}/temp1_input", 1000)
            if temp is not None:
                s.gpu_temp = temp
                break

    @staticmethod
    def _read_fans(s: Snapshot) -> None:
        try:
            fans = psutil.sensors_fans()
        except (AttributeError, OSError):
            return
        for chip, entries in fans.items():
            for index, entry in enumerate(entries, 1):
                if entry.current:
                    s.fans[entry.label or f"{chip} {index}"] = int(entry.current)
