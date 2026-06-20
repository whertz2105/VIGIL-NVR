"""Runtime configuration, all overridable via environment.

A subset of these are ALSO overridable at runtime by the Settings GUI, which
writes /data/settings.json (see settings.py). Most of those are read live via
settings.py accessors; TZ is the exception (it is pervasive and built into a
ZoneInfo at startup), so its file override is applied here at import and a change
takes effect on the next restart.
"""
import json
import os
from zoneinfo import ZoneInfo


def _settings_file_override(key: str):
    """Read one editable knob from /data/settings.json, or None. Standalone (no
    settings.py import) to avoid a circular dependency. Fails safe to None."""
    cache_dir = os.environ.get("CACHE_DIR", "/data")
    try:
        with open(os.path.join(cache_dir, "settings.json")) as f:
            val = json.load(f).get("settings", {}).get(key)
            return val if val not in ("", None) else None
    except (OSError, ValueError, AttributeError):
        return None

# Root of the recordings tree, mounted read-only.
# Layout: {RECORDINGS_DIR}/{YY_MM_DD}/{HH}/{camera}/{camera}_{HH-MM}.mp4
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")

# MediaMTX HTTP API, used to enumerate live cameras.
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://mediamtx:9997").rstrip("/")

# URL prefix (proxied by Caddy to MediaMTX :8888) where the browser fetches HLS.
# e.g. {HLS_PREFIX}/<cam>/index.m3u8
HLS_PREFIX = os.environ.get("HLS_PREFIX", "/hls").rstrip("/")

# Persisted ffprobe duration cache.
CACHE_DIR = os.environ.get("CACHE_DIR", "/data")

# Nightly timelapse clips, mounted read-only.
# Layout: {TIMELAPSES_DIR}/{YY_MM_DD}/{camera}.mp4
TIMELAPSES_DIR = os.environ.get("TIMELAPSES_DIR", "/timelapses")

# Where flagged-alert clips are saved (read-write). A flagged alert extracts a
# clip from 30s before to 5min after the event into {FLAGGED_DIR}/{YY_MM_DD}/.
FLAGGED_DIR = os.environ.get("FLAGGED_DIR", "/flagged")
FLAG_PRE_SECONDS = int(os.environ.get("FLAG_PRE_SECONDS", "30"))
FLAG_POST_SECONDS = int(os.environ.get("FLAG_POST_SECONDS", "300"))

# Frigate HTTP API (headless detection engine), reachable over the docker
# network. Used to surface person/car detection events on the recordings page.
FRIGATE_API = os.environ.get("FRIGATE_API", "http://frigate:5000").rstrip("/")

# Alert filtering (which detections to surface) is derived from the Frigate zone
# definitions and what each zone monitors — see frigate_config.zone_object_index()
# and frigate.list_events(). There is no separate car-zone env var.

# Collapse same-camera+label detections within this many seconds into ONE alert
# (Frigate fragments a single moving person into many short tracks at 5fps).
GROUP_GAP_SECONDS = int(os.environ.get("GROUP_GAP_SECONDS", "60"))

# Timezone: settings-file override (applied at startup) → env → default.
TZ_NAME = _settings_file_override("tz_name") or os.environ.get("TZ", "UTC")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    TZ_NAME = "UTC"
    TZ = ZoneInfo("UTC")

# Path to a Frigate config file the zone/mask tool may edit (optional). When
# unset or the file is absent, the Frigate drawing tool is hidden entirely.
FRIGATE_CONFIG = os.environ.get("FRIGATE_CONFIG", "")

# How long an admin session stays valid.
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(7 * 24 * 3600)))
