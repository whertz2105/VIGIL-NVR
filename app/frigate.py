"""Thin client for Frigate's detection-event API.

Frigate runs headless (no UI exposed); the viewer surfaces its person/car
events as a strip on the recordings page. We poll a small, normalized slice of
`/api/events` and proxy snapshots, so the browser never talks to Frigate
directly (it isn't routed through Caddy).
"""
from __future__ import annotations

import httpx

from config import FRIGATE_API
import frigate_config as fgc

_LABELS = "person,car"


async def list_events(after: float, before: float, limit: int = 200) -> list[dict]:
    """Normalized detection events in [after, before] (epoch seconds).

    Returns [] (never raises) if Frigate is unreachable, so the recordings page
    degrades gracefully to "no events" instead of erroring.
    """
    params = {"labels": _LABELS, "after": after, "before": before, "limit": limit}
    # Limit to the cameras Frigate is configured for (if we can read the config);
    # otherwise let Frigate return all of them.
    cams = fgc.camera_names()
    if cams:
        params["cameras"] = ",".join(cams)
    # No server-side `zones` filter here: it would also drop persons. We apply the
    # zone-derived per-label rule below.
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{FRIGATE_API}/api/events", params=params)
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    # Alert filtering is DERIVED from the zone definitions: an event is surfaced
    # only if it entered a zone (on its camera) whose monitors include that object
    # type. Multiple zones per object are supported. A camera with no zones at all
    # is absent from the index → its events all pass through (graceful fallback).
    zone_idx = fgc.zone_object_index()
    events = []
    for e in raw if isinstance(raw, list) else []:
        start = e.get("start_time")
        if start is None:
            continue
        zones = e.get("zones") or []
        cam_idx = zone_idx.get(e.get("camera"))
        if cam_idx is not None:
            watch_zones = cam_idx.get(e.get("label"), set())
            if not watch_zones or set(zones).isdisjoint(watch_zones):
                continue
        events.append({
            "id": e.get("id"),
            "camera": e.get("camera"),
            "label": e.get("label"),
            "start": start,                       # epoch seconds (float)
            "end": e.get("end_time"),             # epoch or null (ongoing)
            "score": e.get("top_score") or e.get("data", {}).get("top_score"),
            "zones": zones,
            "has_snapshot": bool(e.get("has_snapshot")),
            "has_clip": bool(e.get("has_clip")),
        })
    events.sort(key=lambda x: x["start"])
    return events


async def get_snapshot(event_id: str) -> tuple[bytes, str] | None:
    """(bytes, content_type) for an event snapshot, or None."""
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"{FRIGATE_API}/api/events/{event_id}/snapshot.jpg")
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type", "image/jpeg")
    except httpx.HTTPError:
        return None
