"""Editable runtime settings, layered over config.py.

Resolution order for every editable knob:  built-in default → env var → settings
file (/data/settings.json, written by the Settings GUI).

Accessors return the LIVE value (in-memory state, updated immediately on save),
so most knobs take effect without a restart. Timezone is the exception — it is
applied at process start (config.TZ), so changing it needs a restart; the file
override is still picked up cleanly on the next boot.

Everything fails safe: a missing/corrupt settings file logs a warning and falls
back to env/defaults; the viewer never crashes because of it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from zoneinfo import available_timezones

import config

log = logging.getLogger("nvr.settings")

_LOCK = threading.RLock()
_PATH = Path(config.CACHE_DIR) / "settings.json"

# In-memory copy of the editable settings dict (the file's "settings" object).
_state: dict = {}

_HEX_COLOR = None  # compiled lazily

# Tabs the viewer can show. Keys match the `active` values used in main.py.
_TAB_KEYS = ("live", "recordings", "timelapse", "alerts")


def builtin_defaults() -> dict:
    """Default knob values from env/config — the baseline the GUI edits."""
    return {
        "brand": os.environ.get("BRAND", "NVR"),
        "group_gap_seconds": config.GROUP_GAP_SECONDS,
        "mute_seconds": int(os.environ.get("MUTE_SECONDS", "7200")),
        "flag_pre_seconds": config.FLAG_PRE_SECONDS,
        "flag_post_seconds": config.FLAG_POST_SECONDS,
        "tz_name": config.TZ_NAME,
        "rec_mobile_window_hours": int(os.environ.get("REC_MOBILE_WINDOW_HOURS", "4")),
        "tabs": {k: True for k in _TAB_KEYS},
        # Per-camera display overrides. name -> {label, color, order, visible}.
        # Empty by default so the UI looks exactly as before until customised.
        "cameras": {},
    }


# --------------------------- load / save ---------------------------

def load() -> None:
    """(Re)load the settings file into memory. Safe to call anytime."""
    global _state
    with _LOCK:
        data = {}
        try:
            if _PATH.is_file():
                with open(_PATH, "r") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    data = raw.get("settings", {})
                    if not isinstance(data, dict):
                        data = {}
        except (OSError, ValueError) as e:
            log.warning("settings file unreadable (%s); using env/defaults", e)
            data = {}
        _state = data


def _write_locked() -> None:
    """Persist _state to disk atomically. Caller holds _LOCK."""
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump({"version": 1, "settings": _state}, f, indent=2, sort_keys=True)
    os.replace(tmp, _PATH)


# --------------------------- typed getters ---------------------------

def _merged() -> dict:
    d = builtin_defaults()
    with _LOCK:
        for k, v in _state.items():
            d[k] = v
    # tabs: fill any missing key as visible
    tabs = d.get("tabs") or {}
    d["tabs"] = {k: bool(tabs.get(k, True)) for k in _TAB_KEYS}
    return d


def all_settings() -> dict:
    """The full merged, GUI-facing settings object (NEVER includes auth data)."""
    return _merged()


def brand() -> str:
    return str(_merged()["brand"]) or "NVR"


def group_gap_seconds() -> int:
    return int(_merged()["group_gap_seconds"])


def mute_seconds() -> int:
    return int(_merged()["mute_seconds"])


def flag_pre_seconds() -> int:
    return int(_merged()["flag_pre_seconds"])


def flag_post_seconds() -> int:
    return int(_merged()["flag_post_seconds"])


def tz_name() -> str:
    return str(_merged()["tz_name"])


def tabs() -> dict:
    return _merged()["tabs"]


def rec_mobile_window_hours() -> int:
    return int(_merged()["rec_mobile_window_hours"])


def cameras() -> dict:
    """Admin camera overrides: name -> {label, color, order, visible}."""
    c = _merged().get("cameras") or {}
    return c if isinstance(c, dict) else {}


# --------------------------- validation + update ---------------------------

class SettingsError(ValueError):
    """Raised for invalid settings input (caller returns 400, no write)."""


def _check_int(d: dict, key: str, lo: int, hi: int) -> int:
    try:
        v = int(d[key])
    except (KeyError, TypeError, ValueError):
        raise SettingsError(f"{key} must be an integer")
    if not (lo <= v <= hi):
        raise SettingsError(f"{key} must be between {lo} and {hi}")
    return v


def _check_color(val: str) -> str:
    import re
    global _HEX_COLOR
    if _HEX_COLOR is None:
        _HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
    if not isinstance(val, str) or not _HEX_COLOR.match(val):
        raise SettingsError(f"color must be a #rrggbb hex value, got {val!r}")
    return val.lower()


def update(patch: dict, known_cameras: set[str] | None = None) -> dict:
    """Validate `patch` (a subset of editable keys) and persist. Returns the new
    merged settings. Raises SettingsError on bad input (nothing is written)."""
    if not isinstance(patch, dict):
        raise SettingsError("body must be an object")

    clean: dict = {}

    if "brand" in patch:
        b = str(patch["brand"]).strip()
        if not (1 <= len(b) <= 60):
            raise SettingsError("brand must be 1–60 characters")
        clean["brand"] = b

    for key, lo, hi in (
        ("group_gap_seconds", 0, 3600),
        ("mute_seconds", 60, 86400),
        ("flag_pre_seconds", 0, 600),
        ("flag_post_seconds", 5, 3600),
        ("rec_mobile_window_hours", 1, 24),
    ):
        if key in patch:
            clean[key] = _check_int(patch, key, lo, hi)

    if "tz_name" in patch:
        tz = str(patch["tz_name"]).strip()
        if tz not in available_timezones():
            raise SettingsError(f"unknown timezone: {tz}")
        clean["tz_name"] = tz

    if "tabs" in patch:
        t = patch["tabs"]
        if not isinstance(t, dict):
            raise SettingsError("tabs must be an object")
        clean["tabs"] = {k: bool(t.get(k, True)) for k in _TAB_KEYS}

    if "cameras" in patch:
        cams = patch["cameras"]
        if not isinstance(cams, dict):
            raise SettingsError("cameras must be an object")
        out = {}
        for name, c in cams.items():
            import re
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(name)):
                raise SettingsError(f"bad camera name: {name!r}")
            # Drop entries for cameras MediaMTX no longer reports (stale).
            if known_cameras is not None and name not in known_cameras:
                continue
            if not isinstance(c, dict):
                raise SettingsError(f"camera {name} must be an object")
            entry = {}
            label = str(c.get("label", "")).strip()
            if label:
                if len(label) > 40:
                    raise SettingsError("camera label too long (max 40)")
                entry["label"] = label
            if c.get("color"):
                entry["color"] = _check_color(c["color"])
            if "order" in c:
                try:
                    entry["order"] = int(c["order"])
                except (TypeError, ValueError):
                    raise SettingsError("camera order must be an integer")
            entry["visible"] = bool(c.get("visible", True))
            out[name] = entry
        clean["cameras"] = out

    if not clean:
        raise SettingsError("no recognised settings in request")

    with _LOCK:
        _state.update(clean)
        _write_locked()
    return all_settings()


# Load once at import so accessors work immediately.
load()
