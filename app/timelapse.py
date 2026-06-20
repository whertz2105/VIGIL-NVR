"""Enumerate and serve the nightly timelapse clips.

Layout on disk (written by /srv/nvr/bin/nvr-timelapse.sh):
    {TIMELAPSES_DIR}/{YY_MM_DD}/{camera}.mp4
Plain progressive MP4 (h264 + faststart), direct-played in a <video> element.
"""
from __future__ import annotations

import re
from pathlib import Path

from config import TIMELAPSES_DIR

_DATE_DIR_RE = re.compile(r"^(\d{2})_(\d{2})_(\d{2})$")
_CAM_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _date_dir_to_iso(name: str) -> str | None:
    m = _DATE_DIR_RE.match(name)
    if not m:
        return None
    yy, mm, dd = m.groups()
    return f"20{yy}-{mm}-{dd}"


def list_timelapses() -> list[dict]:
    """All timelapse days, newest first, each with its available cameras."""
    root = Path(TIMELAPSES_DIR)
    days = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir():
            continue
        iso = _date_dir_to_iso(entry.name)
        if not iso:
            continue
        cams = sorted(
            p.stem for p in entry.glob("*.mp4") if _CAM_RE.match(p.stem)
        )
        if cams:
            days.append({"date": iso, "dir": entry.name, "cameras": cams})
    days.sort(key=lambda d: d["date"], reverse=True)
    return days


def resolve_timelapse_path(date_dir: str, camera: str) -> Path | None:
    """Validate components and return the real file path, or None if invalid /
    escaping the timelapses root."""
    if not _DATE_DIR_RE.match(date_dir):
        return None
    if not _CAM_RE.match(camera):
        return None
    root = Path(TIMELAPSES_DIR).resolve()
    candidate = (root / date_dir / f"{camera}.mp4").resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate
