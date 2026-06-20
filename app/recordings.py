"""Scan the recordings tree and build a timeline the frontend can render.

Files are named by their *start* time: {camera}_{HH-MM}.mp4, living under
{date}/{HH}/{camera}/. Most segments are 15 min, but recorder restarts can
leave shorter partials, so durations are read with ffprobe (cached) rather
than assumed. Each clip is placed on an absolute (epoch) timeline so the UI
can render lanes with real gaps.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import RECORDINGS_DIR, CACHE_DIR, TZ

# Recording day dir, e.g. 26_06_18
_DATE_DIR_RE = re.compile(r"^(\d{2})_(\d{2})_(\d{2})$")
# Hour dir, e.g. 09
_HOUR_DIR_RE = re.compile(r"^\d{2}$")
# Segment file, e.g. cam1_09-15.mp4  -> capture HH, MM of start time
_SEG_RE = re.compile(r"^(?P<cam>.+)_(?P<hh>\d{2})-(?P<mm>\d{2})\.mp4$")

_CACHE_PATH = Path(CACHE_DIR) / "probe_cache.json"
_cache_lock = threading.Lock()
_duration_cache: dict[str, dict] = {}


def _load_cache() -> None:
    global _duration_cache
    try:
        _duration_cache = json.loads(_CACHE_PATH.read_text())
    except (OSError, ValueError):
        _duration_cache = {}


def _save_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_duration_cache))
        tmp.replace(_CACHE_PATH)
    except OSError:
        pass


_load_cache()


def date_dir_to_iso(name: str) -> str | None:
    """26_06_18 -> 2026-06-18"""
    m = _DATE_DIR_RE.match(name)
    if not m:
        return None
    yy, mm, dd = m.groups()
    return f"20{yy}-{mm}-{dd}"


def iso_to_date_dir(iso: str) -> str | None:
    """2026-06-18 -> 26_06_18"""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return None
    return d.strftime("%y_%m_%d")


def _probe_duration(path: Path, st: os.stat_result) -> float | None:
    """Duration in seconds, cached by (path, mtime, size). Returns None if the
    file can't be probed — which is the case for the segment being recorded RIGHT
    NOW: it's a fragmented mp4 still being written, so its moov isn't finalized
    and neither ffprobe nor the browser can decode it. We use that as the signal
    to skip the in-progress segment until it closes (a failure is NOT cached, so
    it's re-probed and appears as soon as it's complete)."""
    key = str(path)
    sig = {"mtime": int(st.st_mtime), "size": st.st_size}
    with _cache_lock:
        hit = _duration_cache.get(key)
        if hit and hit.get("mtime") == sig["mtime"] and hit.get("size") == sig["size"]:
            return hit["duration"]
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None
    with _cache_lock:
        _duration_cache[key] = {**sig, "duration": duration}
    return duration


def _day_has_footage(day_dir: Path) -> bool:
    """True if the day contains at least one segment. Cheap: stops at the first
    match rather than walking the whole tree. The recorder pre-creates the empty
    {date}/{HH}/ hour skeleton at midnight rollover, so a day dir existing does
    not mean it has any recordings yet."""
    try:
        return next(day_dir.glob("*/*/*.mp4"), None) is not None
    except OSError:
        return False


def list_days() -> list[dict]:
    """All recording days, newest first. `has_footage` flags days that actually
    contain segments (vs. an empty post-midnight stub)."""
    root = Path(RECORDINGS_DIR)
    days = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir():
            continue
        iso = date_dir_to_iso(entry.name)
        if iso:
            days.append({
                "date": iso,
                "dir": entry.name,
                "has_footage": _day_has_footage(entry),
            })
    days.sort(key=lambda d: d["date"], reverse=True)
    return days


def list_cameras_for_day(date_iso: str) -> list[str]:
    date_dir = iso_to_date_dir(date_iso)
    if not date_dir:
        return []
    root = Path(RECORDINGS_DIR) / date_dir
    cams: set[str] = set()
    try:
        hours = list(root.iterdir())
    except OSError:
        return []
    for hour in hours:
        if not (hour.is_dir() and _HOUR_DIR_RE.match(hour.name)):
            continue
        for cam in hour.iterdir():
            # Only count cameras that actually have segments that day.
            if cam.is_dir() and any(cam.glob("*.mp4")):
                cams.add(cam.name)
    return sorted(cams)


def build_timeline(date_iso: str, camera: str | None = None) -> dict:
    """Return clips for a day, grouped by camera, on an absolute epoch axis."""
    date_dir = iso_to_date_dir(date_iso)
    if not date_dir:
        return {"date": date_iso, "cameras": [], "clips": {}}

    root = Path(RECORDINGS_DIR) / date_dir
    day_start = datetime.strptime(date_iso, "%Y-%m-%d").replace(tzinfo=TZ)
    day_start_epoch = day_start.timestamp()
    now = time.time()

    clips: dict[str, list[dict]] = {}
    probed_new = False

    try:
        hours = sorted(p for p in root.iterdir() if p.is_dir() and _HOUR_DIR_RE.match(p.name))
    except OSError:
        hours = []

    for hour in hours:
        for cam_dir in sorted(hour.iterdir()):
            if not cam_dir.is_dir():
                continue
            cam = cam_dir.name
            if camera and cam != camera:
                continue
            for f in sorted(cam_dir.glob("*.mp4")):
                m = _SEG_RE.match(f.name)
                if not m:
                    continue
                hh, mm = int(m.group("hh")), int(m.group("mm"))
                try:
                    st = f.stat()
                except OSError:
                    continue
                start_dt = day_start + timedelta(hours=hh, minutes=mm)
                start_epoch = start_dt.timestamp()
                before = len(_duration_cache)
                duration = _probe_duration(f, st)
                if len(_duration_cache) != before:
                    probed_new = True
                # None => the segment is still being recorded (fragmented mp4, no
                # finalized moov). ffprobe can't read it and neither can the
                # browser ("Cannot parse metadata"), so skip it until it closes.
                # This is what kept the playhead landing on an undecodable clip.
                if duration is None:
                    continue
                # Recorded content is always in the past; cap the end at 'now' as
                # a safety net (past segments, end << now, are unaffected).
                duration = max(0.0, min(start_epoch + duration, now) - start_epoch)
                if duration <= 0:
                    continue
                clips.setdefault(cam, []).append({
                    "camera": cam,
                    "filename": f.name,
                    "hour": hour.name,
                    "start": start_epoch,
                    "duration": round(duration, 3),
                    "size": st.st_size,
                    "url": f"/api/recordings/file/{date_dir}/{hour.name}/{cam}/{f.name}",
                })

    if probed_new:
        with _cache_lock:
            _save_cache()

    for cam in clips:
        clips[cam].sort(key=lambda c: c["start"])

    return {
        "date": date_iso,
        "cameras": sorted(clips.keys()),
        "day_start": day_start_epoch,
        "day_end": day_start_epoch + 86400,
        "clips": clips,
    }


def resolve_recording_path(date_dir: str, hour: str, camera: str, filename: str) -> Path | None:
    """Validate path components and return the real path, or None if invalid /
    escaping the recordings root."""
    if not _DATE_DIR_RE.match(date_dir):
        return None
    if not _HOUR_DIR_RE.match(hour):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", camera):
        return None
    if not _SEG_RE.match(filename):
        return None
    root = Path(RECORDINGS_DIR).resolve()
    candidate = (root / date_dir / hour / camera / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _find_noon_segment(camera: str):
    """Most recent {camera}_12-MM.mp4 recording (prefer the 12-00 segment, i.e.
    the one starting at 12:00 PM). Returns a Path or None if no noon footage."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", camera or ""):
        return None
    root = Path(RECORDINGS_DIR)
    try:
        days = sorted((d.name for d in root.iterdir()
                       if d.is_dir() and _DATE_DIR_RE.match(d.name)), reverse=True)
    except OSError:
        return None
    for day in days:
        cam_dir = root / day / "12" / camera
        if not cam_dir.is_dir():
            continue
        segs = sorted(cam_dir.glob(f"{camera}_12-*.mp4"))
        if not segs:
            continue
        preferred = cam_dir / f"{camera}_12-00.mp4"
        return preferred if preferred.is_file() else segs[0]
    return None


def noon_frame_jpeg(camera: str) -> bytes | None:
    """A JPEG frame from the start (~12:00 PM) of the most recent noon recording,
    or None if there's no noon footage. Blocking (ffmpeg) — run in an executor."""
    seg = _find_noon_segment(camera)
    if seg is None:
        return None
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", "0", "-i", str(seg),
             "-frames:v", "1", "-q:v", "2", "-f", "mjpeg", "pipe:1"],
            capture_output=True, timeout=20,
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except subprocess.SubprocessError:
        pass
    return None
