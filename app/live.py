"""Enumerate live cameras from the MediaMTX API.

MediaMTX is the single source of truth for which cameras are streaming
live: each configured path becomes a camera tile. The browser plays
{HLS_PREFIX}/{name}/index.m3u8 (proxied by Caddy to MediaMTX :8888).
"""
from __future__ import annotations

import httpx

from config import MEDIAMTX_API, HLS_PREFIX


async def list_live_cameras() -> list[dict]:
    url = f"{MEDIAMTX_API}/v3/paths/list"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    names = {item.get("name") for item in data.get("items", [])}
    cameras = []
    for item in data.get("items", []):
        name = item.get("name")
        if not name:
            continue
        # Hide the internal "<cam>src" helper paths: those pull raw G711 from the
        # camera and are transcoded into the matching "<cam>" path that HLS
        # serves. Only the served paths should appear as camera tiles.
        if name.endswith("src"):
            continue
        cam = {
            "name": name,
            "ready": bool(item.get("ready")),
            # Audio path (H264 + AAC).
            "hls": f"{HLS_PREFIX}/{name}/index.m3u8",
        }
        # Video-only fallback: some browsers' MSE (Firefox/Linux) can't decode
        # AAC and reject the audio manifest entirely. The "<cam>src" path is the
        # same video without audio; the live page falls back to it so video
        # still plays. Only advertise it when that helper path actually exists.
        if f"{name}src" in names:
            cam["hls_video_only"] = f"{HLS_PREFIX}/{name}src/index.m3u8"
        cameras.append(cam)
    cameras.sort(key=lambda c: c["name"])
    return cameras
