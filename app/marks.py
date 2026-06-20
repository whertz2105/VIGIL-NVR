"""User triage of detection alerts: mark each event 'benign' or 'flagged'.

- benign  → hidden from the alert bar (and recordings strip).
- flagged → a clip from FLAG_PRE_SECONDS before to FLAG_POST_SECONDS after the
            event is cut from the main recordings into FLAGGED_DIR.

Marks are persisted as JSON in CACHE_DIR. Clip extraction is deferred: a flagged
event's "+5 min" footage often isn't recorded/closed yet, and our 15-min
recording segments aren't decodable until they roll over — so a background poller
(driven from main.py) cuts the clip once the footage is available.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import CACHE_DIR, RECORDINGS_DIR, FLAGGED_DIR, TZ
import settings
from recordings import _SEG_RE, _probe_duration

_MARKS_PATH = Path(CACHE_DIR) / "event_marks.json"
_lock = threading.Lock()
_CAM_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _load() -> dict:
    try:
        return json.loads(_MARKS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save(marks: dict) -> None:
    try:
        _MARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MARKS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(marks))
        tmp.replace(_MARKS_PATH)
    except OSError:
        pass


def get_all() -> dict:
    with _lock:
        return _load()


def set_mark(event_id: str, status: str, camera: str | None, start: float | None) -> dict:
    """Record a benign/flagged decision. For 'flagged', leaves clip extraction
    pending (the poller does it). Returns the stored record."""
    with _lock:
        marks = _load()
        rec = {"status": status}
        if status == "flagged":
            rec.update({
                "camera": camera,
                "start": start,
                "clip": None,
                "clip_status": "pending",
            })
        marks[event_id] = rec
        _save(marks)
        return rec


def annotate_and_filter(events: list[dict]) -> list[dict]:
    """Drop benign events and events muted at their time; tag the rest with their
    mark (status/clip) so the UI can badge flagged ones and link the saved clip."""
    marks = get_all()
    mutes = get_mutes()
    out = []
    for e in events:
        if _muted(e, mutes):
            continue
        m = marks.get(e.get("id"))
        if m and m.get("status") == "benign":
            continue
        if m and m.get("status") == "flagged":
            aid = None
            if m.get("start"):
                try:
                    aid = datetime.fromtimestamp(m["start"], TZ).strftime("%y_%m_%d_%H%M%S")
                except (ValueError, OverflowError, OSError):
                    aid = None
            # `clip` now carries the alert id (links to the Saved Alerts tab).
            e = {**e, "mark": "flagged", "clip": aid, "clip_status": m.get("clip_status")}
        else:
            e = {**e, "mark": None}
        out.append(e)
    return out


def group_alerts(events: list[dict], gap: float) -> list[dict]:
    """Collapse same-camera+label detections within `gap` seconds into one alert.
    A single moving person is fragmented by Frigate into many short tracks; this
    reunites them. Each group keeps ALL member ids (so marking applies to the
    whole burst) and a representative id (a flagged member if any, else the
    best-scoring member with a snapshot) for the thumbnail/clip."""
    evs = sorted(events, key=lambda e: e.get("start") or 0)
    last = {}   # (camera, label) -> open group
    out = []
    for e in evs:
        key = (e.get("camera"), e.get("label"))
        end = e.get("end") or e.get("start") or 0
        g = last.get(key)
        if g is not None and (e.get("start") or 0) - g["_end"] <= gap:
            g["ids"].append(e.get("id"))
            g["count"] += 1
            g["_end"] = max(g["_end"], end)
            g["end"] = g["_end"]
            g["has_snapshot"] = g.get("has_snapshot") or e.get("has_snapshot")
            g["has_clip"] = g.get("has_clip") or e.get("has_clip")
            g["score"] = max(g.get("score") or 0, e.get("score") or 0)
            if e.get("mark") == "flagged":
                g.update({"mark": "flagged", "clip": e.get("clip"),
                          "clip_status": e.get("clip_status"), "id": e.get("id"), "_rep_locked": True})
            elif not g.get("_rep_locked") and e.get("has_snapshot") \
                    and (e.get("score") or 0) > (g.get("_rep_score") or -1):
                g["id"] = e.get("id")
                g["_rep_score"] = e.get("score") or 0
        else:
            g = {
                **e,
                "ids": [e.get("id")],
                "count": 1,
                "_end": end,
                "_rep_locked": e.get("mark") == "flagged",
                "_rep_score": (e.get("score") or 0) if e.get("has_snapshot") else -1,
            }
            last[key] = g
            out.append(g)
    for g in out:
        g.pop("_end", None); g.pop("_rep_locked", None); g.pop("_rep_score", None)
    out.sort(key=lambda g: g.get("start") or 0)
    return out


# ------------------------------- mute / snooze -------------------------------
# Per-camera "snooze" so e.g. parents doing yardwork don't flood the alert bar.
# A mute is a [since, until] window; alerts from that camera whose start falls in
# the window are hidden (even after it expires, so the yardwork burst stays gone).

_MUTES_PATH = Path(CACHE_DIR) / "mutes.json"
_mute_lock = threading.Lock()


def _load_mutes() -> dict:
    try:
        return json.loads(_MUTES_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_mutes(m: dict) -> None:
    try:
        _MUTES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MUTES_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(m))
        tmp.replace(_MUTES_PATH)
    except OSError:
        pass


def get_mutes() -> dict:
    with _mute_lock:
        return _load_mutes()


def _muted(event: dict, mutes: dict) -> bool:
    w = mutes.get(event.get("camera"))
    if not w:
        return False
    return w.get("since", 0) <= (event.get("start") or 0) <= w.get("until", 0)


def set_mute(camera: str, on: bool) -> dict:
    now = time.time()
    with _mute_lock:
        mutes = _load_mutes()
        if on:
            mutes[camera] = {"since": now, "until": now + settings.mute_seconds()}
        elif camera in mutes:
            # End the window now; alerts during [since, now] stay suppressed.
            mutes[camera]["until"] = min(mutes[camera].get("until", now), now)
        # prune windows that ended long ago (older than the 12h alert view)
        mutes = {c: w for c, w in mutes.items() if w.get("until", 0) > now - 43200}
        _save_mutes(mutes)
        return mutes


def active_mutes() -> dict:
    """{camera: until_epoch} for cameras currently muted."""
    now = time.time()
    return {c: w["until"] for c, w in get_mutes().items() if w.get("until", 0) > now}


# --------------------------- clip extraction ---------------------------

def _overlapping_segments(camera: str, t0: float, t1: float) -> list[dict]:
    """Complete (probeable) recording segments for `camera` overlapping [t0, t1],
    sorted by start. Incomplete/still-recording segments are skipped."""
    segs = []
    cur = datetime.fromtimestamp(t0, TZ).replace(minute=0, second=0, microsecond=0)
    last = datetime.fromtimestamp(t1, TZ)
    while cur <= last:
        midnight = cur.replace(hour=0)
        folder = Path(RECORDINGS_DIR) / cur.strftime("%y_%m_%d") / cur.strftime("%H") / camera
        if folder.is_dir():
            for f in sorted(folder.glob(f"{camera}_*.mp4")):
                m = _SEG_RE.match(f.name)
                if not m:
                    continue
                seg_start = (midnight + timedelta(hours=int(m.group("hh")),
                                                  minutes=int(m.group("mm")))).timestamp()
                try:
                    st = f.stat()
                except OSError:
                    continue
                dur = _probe_duration(f, st)
                if dur is None:  # still recording / undecodable
                    continue
                seg_end = seg_start + dur
                if seg_start < t1 and seg_end > t0:
                    segs.append({"path": str(f), "start": seg_start, "end": seg_end})
        cur += timedelta(hours=1)
    segs.sort(key=lambda s: s["start"])
    return segs


def _cameras_at(epoch: float) -> list[str]:
    """Cameras that have a recording folder at this time."""
    dt = datetime.fromtimestamp(epoch, TZ)
    folder = Path(RECORDINGS_DIR) / dt.strftime("%y_%m_%d") / dt.strftime("%H")
    try:
        return sorted(p.name for p in folder.iterdir() if p.is_dir() and _CAM_RE.match(p.name))
    except OSError:
        return []


def _make_clip(camera: str, t0: float, t1: float, out_path: str) -> bool:
    """Trim [t0,t1] out of `camera`'s segments and write out_path. Trims each
    segment individually (single-file seeks are accurate; seeking across a concat
    of these recordings is not) and joins the pieces. Returns True on success."""
    segs = _overlapping_segments(camera, t0, t1)
    if not segs:
        return False
    pieces: list[str] = []
    list_name = None
    try:
        for i, s in enumerate(segs):
            a = max(t0, s["start"])
            b = min(t1, s["end"])
            if b - a <= 0.05:
                continue
            fd, piece = tempfile.mkstemp(prefix=f"nvr-flag-{i}-", suffix=".mp4", dir="/tmp")
            os.close(fd)
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{a - s['start']:.3f}",
                   "-i", s["path"], "-t", f"{b - a:.3f}", "-c", "copy",
                   "-avoid_negative_ts", "make_zero", piece]
            if not _run_ff(cmd, 120) or not _nonempty(piece):
                return False
            pieces.append(piece)
        if not pieces:
            return False
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        if len(pieces) == 1:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", pieces[0],
                   "-c", "copy", "-movflags", "+faststart", out_path]
        else:
            list_fd, list_name = tempfile.mkstemp(suffix=".txt", dir="/tmp")
            with os.fdopen(list_fd, "w") as lf:
                for p in pieces:
                    lf.write("file '%s'\n" % p.replace("'", "'\\''"))
            cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                   "-i", list_name, "-c", "copy", "-movflags", "+faststart", out_path]
        return _run_ff(cmd, 180) and _nonempty(out_path)
    finally:
        for p in pieces:
            try:
                os.unlink(p)
            except OSError:
                pass
        if list_name:
            try:
                os.unlink(list_name)
            except OSError:
                pass


def extract_alert_clips(primary: str, start: float):
    """Cut a clip from EVERY camera that fully covers the alert window, so the
    Saved Alerts tab can show what was happening everywhere at once. All clips
    share the same [start-30, start+300] window, so they play in sync.

    Returns: None = footage not ready yet (retry); str alert-id (YY_MM_DD_HHMMSS)
    = success; False = permanent failure."""
    t0 = start - settings.flag_pre_seconds()
    t1 = start + settings.flag_post_seconds()
    if t1 <= t0:
        return False
    cams = _cameras_at(t0)
    if primary and _CAM_RE.match(primary) and primary not in cams:
        cams = [primary] + cams
    stale = time.time() > t1 + 1200  # >20min past the window → stop waiting on laggards

    coverage, waiting = {}, False
    for cam in cams:
        segs = _overlapping_segments(cam, t0, t1)
        if not segs:
            continue
        if segs[0]["start"] <= t0 + 1 and segs[-1]["end"] >= t1 - 1:
            coverage[cam] = True            # full, complete coverage
        elif segs[-1]["end"] < t1 - 1:
            waiting = True                  # footage exists but window not closed yet
    if not coverage:
        return None if not stale else False
    if waiting and not stale:
        return None                         # wait so we capture all cameras together

    aid = datetime.fromtimestamp(start, TZ).strftime("%y_%m_%d_%H%M%S")
    made = []
    for cam in coverage:
        out = str(Path(FLAGGED_DIR) / f"{cam}_{aid}.mp4")
        if _make_clip(cam, t0, t1, out):
            made.append(cam)
    return aid if made else False


def _run_ff(cmd: list, timeout: int) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).returncode == 0
    except subprocess.SubprocessError:
        return False


def _nonempty(path) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def process_pending() -> int:
    """Try to produce clips for any pending flagged events. Runs in a worker
    thread (called via run_in_executor). Returns how many were resolved."""
    with _lock:
        marks = _load()
        pending = [(eid, m) for eid, m in marks.items()
                   if m.get("status") == "flagged" and m.get("clip_status") == "pending"]
    if not pending:
        return 0
    resolved = {}
    for eid, m in pending:
        res = extract_alert_clips(m.get("camera"), m.get("start"))
        if res is None:
            continue  # not ready; leave pending
        resolved[eid] = ("done", res) if res else ("failed", None)
    if not resolved:
        return 0
    with _lock:
        marks = _load()
        for eid, (status, rel) in resolved.items():
            if eid in marks:
                marks[eid]["clip_status"] = status
                marks[eid]["clip"] = rel
        _save(marks)
    return len(resolved)


_CLIP_NAME_RE = re.compile(r"^(?P<cam>.+)_(\d{2})_(\d{2})_(\d{2})_(\d{6})\.mp4$")


def list_flagged_alerts() -> list[dict]:
    """Saved flagged alerts, newest first. All cameras' clips for one alert share
    the same <YY_MM_DD_HHMMSS> suffix, so we group by it. Each alert lists its
    per-camera clips."""
    try:
        entries = list(Path(FLAGGED_DIR).glob("*.mp4"))
    except OSError:
        return []
    groups: dict[str, dict] = {}
    for f in entries:
        m = _CLIP_NAME_RE.match(f.name)
        if not m:
            continue
        yy, mm, dd, hms = m.group(2), m.group(3), m.group(4), m.group(5)
        aid = f"{yy}_{mm}_{dd}_{hms}"
        try:
            dt = datetime.strptime(f"20{yy}-{mm}-{dd} {hms}", "%Y-%m-%d %H%M%S").replace(tzinfo=TZ)
        except ValueError:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        g = groups.setdefault(aid, {"id": aid, "start": dt.timestamp(), "cameras": []})
        g["cameras"].append({"camera": m.group("cam"), "name": f.name, "size": size})
    out = sorted(groups.values(), key=lambda g: g["start"], reverse=True)
    for g in out:
        g["cameras"].sort(key=lambda c: c["camera"])
    return out


def _aid_re_ok(aid: str) -> bool:
    return bool(re.fullmatch(r"\d{2}_\d{2}_\d{2}_\d{6}", aid or ""))


def delete_alert(aid: str) -> bool:
    """Delete ALL cameras' clips for an alert AND demote the event to benign, so
    it also drops off the timeline / alert bar. Returns True if anything removed."""
    if not _aid_re_ok(aid):
        return False
    removed = 0
    for f in Path(FLAGGED_DIR).glob(f"*_{aid}.mp4"):
        m = _CLIP_NAME_RE.match(f.name)
        if not m or f"{m.group(2)}_{m.group(3)}_{m.group(4)}_{m.group(5)}" != aid:
            continue
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    with _lock:
        marks = _load()
        changed = False
        for eid, rec in marks.items():
            if rec.get("status") == "flagged" and rec.get("start"):
                try:
                    raid = datetime.fromtimestamp(rec["start"], TZ).strftime("%y_%m_%d_%H%M%S")
                except (ValueError, OverflowError, OSError):
                    raid = None
                if raid == aid:
                    marks[eid] = {"status": "benign"}
                    changed = True
        if changed:
            _save(marks)
    return removed > 0


def resolve_clip_path(rel: str) -> Path | None:
    """Validate a saved-clip filename and return its real path."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.mp4", rel or ""):
        return None
    root = Path(FLAGGED_DIR).resolve()
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p if p.is_file() else None
