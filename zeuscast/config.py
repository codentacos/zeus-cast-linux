"""Settings stored as JSON in $XDG_CONFIG_HOME/zeuscast/config.json."""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

_save_lock = threading.Lock()

DEFAULTS: dict = {
    "port": None,  # None = auto-detect by USB id
    "theme": "gauge1",
    "theme_options": {},
    "accent": "#3fb8ff",
    "backdrop": True,
    "fahrenheit": False,
    "update_interval": 2.0,
    "background": {"path": None, "mode": "fill", "uploaded_md5": None},
    "start_minimized": False,
    "restore_background": True,  # re-send the saved background whenever the cooler connects
}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "zeuscast"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    path = Path(base) / "zeuscast"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _merge(defaults: dict, loaded: dict) -> dict:
    merged = copy.deepcopy(defaults)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class Config:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "config.json"
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(loaded, dict):
            self.data = _merge(DEFAULTS, loaded)

    def save(self) -> None:
        # The GUI thread and the engine thread both save.
        with _save_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def __getitem__(self, key: str):
        return self.data[key]

    def __setitem__(self, key: str, value) -> None:
        self.data[key] = value

    def theme_options(self, theme) -> dict:
        options = theme.defaults()
        options.update(self.data["theme_options"].get(theme.key, {}))
        return options

    def set_theme_option(self, theme_key: str, key: str, value) -> None:
        self.data["theme_options"].setdefault(theme_key, {})[key] = value
