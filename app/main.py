"""NVR Viewer — live camera grid + recordings timeline.

Ships with NO authentication: anonymous visitors get Live / Recordings /
Timelapse / Saved. An optional, self-contained admin login (see auth.py) unlocks
the Settings page. That login is the ONLY thing that grants admin/Settings
access — it does not depend on, and cannot be bypassed by, any upstream proxy.
A reverse proxy may optionally pass an authenticated username in a header
(TRUSTED_USER_HEADER) which is displayed read-only and never grants admin.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path

import httpx

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import recordings as rec
import timelapse as tl
import frigate as fg
import frigate_config as fgc
import marks
import auth
import settings
import config
from config import TZ, FRIGATE_API
from live import list_live_cameras

BASE_DIR = Path(__file__).parent

app = FastAPI(title="NVR Viewer", docs_url=None, redoc_url=None, openapi_url=None)

# Self-contained admin auth: ensure a session secret + first-run password exist,
# then sign sessions with that secret (httpOnly, SameSite=Lax, time-limited).
auth.init()
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.get_session_secret(),
    session_cookie="nvr_session",
    max_age=config.SESSION_MAX_AGE,
    same_site="lax",
    https_only=False,  # TLS is terminated upstream; cookie still httpOnly + signed
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


async def _json_body(request: Request) -> dict:
    """Parse a JSON request body, returning 400 (not 500) on malformed input."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body


def require_admin(request: Request) -> bool:
    """Dependency: 401 unless a valid admin session is present. This is the
    server-side gate for EVERY settings route — the UI hiding the link is not
    relied upon."""
    if not auth.is_admin(request):
        raise HTTPException(status_code=401, detail="admin login required")
    return True


def _ctx(request: Request, active: str, **extra) -> dict:
    """Common template context, including the admin flag + display config."""
    cams = settings.cameras()
    nvr_js = {
        "cameras": cams,
        "recMobileWindowHours": settings.rec_mobile_window_hours(),
    }
    return {
        "request": request,
        "active": active,
        "static_v": STATIC_V,
        "user": _current_user(request),
        "is_admin": auth.is_admin(request),
        "brand": settings.brand(),
        "tabs": settings.tabs(),
        "cameras": cams,
        "nvr_js": nvr_js,
        **extra,
    }


def _static_version() -> str:
    """Newest mtime under static/, hex — appended as ?v= to bust browser caches
    on every rebuild so clients don't run stale JS/CSS after a deploy."""
    latest = 0
    for p in (BASE_DIR / "static").rglob("*"):
        try:
            latest = max(latest, int(p.stat().st_mtime))
        except OSError:
            pass
    return format(latest, "x")


STATIC_V = _static_version()

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 1024 * 1024  # 1 MiB


@app.on_event("startup")
async def _start_clip_poller():
    """Periodically cut clips for flagged alerts once their footage is recorded
    (the '+5 min' window often isn't on disk yet at flag time)."""
    async def _loop():
        while True:
            try:
                await asyncio.get_event_loop().run_in_executor(None, marks.process_pending)
            except Exception:
                pass
            await asyncio.sleep(30)
    asyncio.create_task(_loop())


# Optional: a reverse proxy MAY authenticate users and pass the username in a
# header; set TRUSTED_USER_HEADER to that header's name to display it (read-only).
# It is NEVER used for authorization — the app's own admin login is the only thing
# that grants Settings access. Unset (the default) = anonymous.
_TRUSTED_USER_HEADER = os.environ.get("TRUSTED_USER_HEADER", "")


def _current_user(request: Request) -> str | None:
    if _TRUSTED_USER_HEADER:
        return request.headers.get(_TRUSTED_USER_HEADER)
    return None


# ----------------------------- pages -----------------------------

@app.get("/", response_class=HTMLResponse)
async def live_page(request: Request):
    return templates.TemplateResponse("live.html", _ctx(request, "live"))


@app.get("/recordings", response_class=HTMLResponse)
async def recordings_page(request: Request):
    return templates.TemplateResponse("recordings.html", _ctx(request, "recordings"))


@app.get("/timelapse", response_class=HTMLResponse)
async def timelapse_page(request: Request):
    return templates.TemplateResponse("timelapse.html", _ctx(request, "timelapse"))


@app.get("/alerts", response_class=HTMLResponse)
async def saved_alerts_page(request: Request):
    return templates.TemplateResponse("saved.html", _ctx(request, "alerts"))


# ----------------------------- auth + settings -----------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.is_admin(request):
        return RedirectResponse("/settings", status_code=303)
    return templates.TemplateResponse("login.html", _ctx(request, None))


@app.post("/login")
async def login(request: Request):
    ip = (request.client.host if request.client else "?") or "?"
    if not auth.rate_allowed(ip):
        raise HTTPException(status_code=429, detail="too many attempts; wait a minute")
    body = await _json_body(request)
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    if username == auth.ADMIN_USER and auth.verify_password(password):
        auth.rate_reset(ip)
        request.session["admin"] = True
        return {"ok": True}
    auth.rate_record_failure(ip)
    await asyncio.sleep(0.5)  # slow brute force without blocking the loop
    raise HTTPException(status_code=401, detail="invalid username or password")


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "settings.html",
        _ctx(request, "settings", frigate_tool=fgc.available()),
    )


@app.get("/api/settings")
async def api_settings_get(request: Request, _: bool = Depends(require_admin)):
    return {"settings": settings.all_settings(), "cameras_live": await _live_camera_names()}


@app.post("/api/settings")
async def api_settings_post(request: Request, _: bool = Depends(require_admin)):
    body = await _json_body(request)
    try:
        known = await _live_camera_names()
        updated = settings.update(body, known_cameras=set(known))
    except settings.SettingsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "settings": updated}


@app.post("/api/settings/password")
async def api_settings_password(request: Request, _: bool = Depends(require_admin)):
    body = await _json_body(request)
    try:
        auth.set_password(str(body.get("current", "")), str(body.get("new", "")))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


async def _live_camera_names() -> list[str]:
    try:
        return [c["name"] for c in await list_live_cameras()]
    except Exception:
        return []


# --- Frigate zone/mask tool (all admin-gated; no-op if no Frigate config) ---

@app.get("/api/settings/frigate/cameras")
async def api_fg_cameras(request: Request, _: bool = Depends(require_admin)):
    return {"available": fgc.available(), "cameras": fgc.camera_names()}


@app.get("/api/settings/frigate/camera/{cam}")
async def api_fg_camera(cam: str, request: Request, _: bool = Depends(require_admin)):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cam):
        raise HTTPException(status_code=400, detail="bad camera")
    data = fgc.read_camera(cam)
    if data is None:
        raise HTTPException(status_code=404, detail="camera not in Frigate config")
    return data


@app.get("/api/settings/frigate/snapshot/{cam}")
async def api_fg_snapshot(cam: str, request: Request, _: bool = Depends(require_admin)):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cam):
        raise HTTPException(status_code=400, detail="bad camera")
    url = f"{FRIGATE_API}/api/{cam}/latest.jpg"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(url)
            r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="snapshot unavailable")
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "no-store"})


@app.get("/api/settings/frigate/frame/{cam}")
async def api_fg_frame(cam: str, request: Request, source: str = "noon", _: bool = Depends(require_admin)):
    """Background frame for the zone editor. Default: a frame from the most recent
    12:00 PM recording (a stable, representative scene). Falls back to — or, with
    source=latest, uses — Frigate's most recent live frame."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cam):
        raise HTTPException(status_code=400, detail="bad camera")
    if source != "latest":
        jpg = await asyncio.get_event_loop().run_in_executor(None, rec.noon_frame_jpeg, cam)
        if jpg:
            return Response(content=jpg, media_type="image/jpeg",
                            headers={"Cache-Control": "no-store", "X-Frame-Source": "noon"})
    url = f"{FRIGATE_API}/api/{cam}/latest.jpg"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(url)
            r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="no frame available")
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "no-store", "X-Frame-Source": "latest"})


# The viewer never restarts Frigate itself (no Docker-socket component, by
# design). Saves write the config + a backup; the admin restarts Frigate manually.
_RESTART_HINT = "Saved. Restart Frigate to apply:  docker restart frigate"


@app.post("/api/settings/frigate/zone")
async def api_fg_zone(request: Request, _: bool = Depends(require_admin)):
    body = await _json_body(request)
    old = body.get("old_name")
    try:
        backup = fgc.save_zone(
            str(body.get("camera", "")), str(body.get("name", "")),
            body.get("points") or [], str(body.get("monitors", "")),
            old_name=str(old) if old else None,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "backup": backup, "restart_hint": _RESTART_HINT}


@app.delete("/api/settings/frigate/zone/{cam}/{name}")
async def api_fg_zone_delete(cam: str, name: str, _: bool = Depends(require_admin)):
    try:
        backup = fgc.delete_zone(cam, name)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "backup": backup, "restart_hint": _RESTART_HINT}


@app.post("/api/settings/frigate/mask")
async def api_fg_mask(request: Request, _: bool = Depends(require_admin)):
    body = await _json_body(request)
    idx = body.get("index")
    try:
        backup = fgc.save_mask(str(body.get("camera", "")), body.get("points") or [],
                               index=int(idx) if idx is not None else None)
    except (ValueError, RuntimeError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "backup": backup, "restart_hint": _RESTART_HINT}


@app.delete("/api/settings/frigate/mask/{cam}/{index}")
async def api_fg_mask_delete(cam: str, index: int, _: bool = Depends(require_admin)):
    try:
        backup = fgc.delete_mask(cam, index)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "backup": backup, "restart_hint": _RESTART_HINT}


@app.post("/api/settings/frigate/required-zones")
async def api_fg_required_zones(request: Request, _: bool = Depends(require_admin)):
    body = await _json_body(request)
    try:
        backup = fgc.set_required_zones(str(body.get("camera", "")), body.get("zones") or [])
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "backup": backup, "restart_hint": _RESTART_HINT}


# ----------------------------- api -----------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/cameras")
async def api_cameras():
    return {"cameras": await list_live_cameras()}


@app.get("/api/recordings/days")
async def api_days():
    return {"days": rec.list_days()}


@app.get("/api/recordings/timeline")
async def api_timeline(date: str, camera: str | None = None):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(status_code=400, detail="bad date")
    return rec.build_timeline(date, camera)


@app.get("/api/recordings/locate")
async def api_locate(t: float):
    """Map an epoch (e.g. an alert's start time) to the local recording day, so
    the recordings page can deep-link to the right day folder regardless of the
    browser's timezone."""
    return {"date": datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d")}


@app.get("/api/recordings/file/{date_dir}/{hour}/{camera}/{filename}")
async def api_file(
    date_dir: str, hour: str, camera: str, filename: str, request: Request, download: int = 0
):
    path = rec.resolve_recording_path(date_dir, hour, camera, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="not found")
    return _ranged_file_response(path, request, as_download=bool(download))


@app.get("/api/timelapse/list")
async def api_timelapse_list():
    return {"days": tl.list_timelapses()}


@app.get("/api/timelapse/file/{date_dir}/{camera}")
async def api_timelapse_file(date_dir: str, camera: str, request: Request, download: int = 0):
    # `camera` arrives without the .mp4 suffix; strip it if a client adds one.
    camera = camera[:-4] if camera.endswith(".mp4") else camera
    path = tl.resolve_timelapse_path(date_dir, camera)
    if path is None:
        raise HTTPException(status_code=404, detail="not found")
    return _ranged_file_response(path, request, as_download=bool(download))


@app.get("/api/events")
async def api_events(after: float, before: float):
    evs = await fg.list_events(after, before)
    evs = marks.annotate_and_filter(evs)
    return {"events": marks.group_alerts(evs, settings.group_gap_seconds())}


@app.get("/api/events/{event_id}/snapshot.jpg")
async def api_event_snapshot(event_id: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", event_id):
        raise HTTPException(status_code=400, detail="bad id")
    shot = await fg.get_snapshot(event_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="not found")
    data, ctype = shot
    return Response(content=data, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})


_CLIP_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges")


@app.get("/api/events/{event_id}/clip.mp4")
async def api_event_clip(event_id: str, request: Request):
    """Proxy Frigate's recorded clip for an event (range-aware) so the live
    filmstrip can play it on hover. The browser never talks to Frigate directly."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", event_id):
        raise HTTPException(status_code=400, detail="bad id")
    url = f"{FRIGATE_API}/api/events/{event_id}/clip.mp4"
    fwd = {"Range": request.headers["range"]} if request.headers.get("range") else {}
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    try:
        resp = await client.send(client.build_request("GET", url, headers=fwd), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(status_code=502, detail="frigate unreachable")
    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=404, detail="no clip")
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() in _CLIP_HEADERS}

    async def body():
        try:
            async for chunk in resp.aiter_bytes(_CHUNK):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=resp.status_code, headers=out_headers, media_type="video/mp4")


@app.post("/api/events/{event_id}/mark")
async def api_event_mark(event_id: str, request: Request):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", event_id):
        raise HTTPException(status_code=400, detail="bad id")
    body = await _json_body(request)
    status = body.get("status")
    if status not in ("benign", "flagged"):
        raise HTTPException(status_code=400, detail="bad status")
    start = body.get("start")
    rec = marks.set_mark(event_id, status, body.get("camera"),
                         float(start) if start is not None else None)
    # Kick the poller so a flagged clip starts cutting ASAP (if footage is ready).
    if status == "flagged":
        asyncio.get_event_loop().run_in_executor(None, marks.process_pending)
    return {"ok": True, "mark": rec}


@app.post("/api/alert/mark")
async def api_alert_mark(request: Request):
    """Mark a grouped alert. benign → all member ids benign. flagged → the
    representative gets flagged (one clip), the other members benign (so the
    whole burst clears with a single saved clip)."""
    body = await _json_body(request)
    status = body.get("status")
    if status not in ("benign", "flagged"):
        raise HTTPException(status_code=400, detail="bad status")
    ids = [i for i in (body.get("ids") or []) if i and re.fullmatch(r"[A-Za-z0-9._-]+", str(i))]
    if status == "benign":
        for i in ids:
            marks.set_mark(i, "benign", None, None)
        return {"ok": True}
    rep = body.get("id")
    if not rep or not re.fullmatch(r"[A-Za-z0-9._-]+", str(rep)):
        raise HTTPException(status_code=400, detail="bad id")
    start = body.get("start")
    marks.set_mark(rep, "flagged", body.get("camera"), float(start) if start is not None else None)
    for i in ids:
        if i != rep:
            marks.set_mark(i, "benign", None, None)
    asyncio.get_event_loop().run_in_executor(None, marks.process_pending)
    return {"ok": True}


@app.post("/api/events/mark-batch")
async def api_event_mark_batch(request: Request):
    body = await _json_body(request)
    status = body.get("status")
    if status not in ("benign", "flagged"):
        raise HTTPException(status_code=400, detail="bad status")
    items = body.get("items") or []
    flagged = False
    for it in items:
        eid = it.get("id")
        if not eid or not re.fullmatch(r"[A-Za-z0-9._-]+", str(eid)):
            continue
        start = it.get("start")
        marks.set_mark(eid, status, it.get("camera"), float(start) if start is not None else None)
        flagged = flagged or status == "flagged"
    if flagged:
        asyncio.get_event_loop().run_in_executor(None, marks.process_pending)
    return {"ok": True, "count": len(items)}


@app.get("/api/mute")
async def api_get_mute():
    return {"muted": marks.active_mutes()}


@app.post("/api/mute")
async def api_set_mute(request: Request):
    body = await _json_body(request)
    camera = body.get("camera")
    if not camera or not re.fullmatch(r"[A-Za-z0-9_-]+", camera):
        raise HTTPException(status_code=400, detail="bad camera")
    marks.set_mute(camera, bool(body.get("on")))
    return {"ok": True, "muted": marks.active_mutes()}


@app.get("/api/flagged/list")
async def api_flagged_list():
    return {"alerts": marks.list_flagged_alerts()}


@app.get("/api/flagged/file/{name}")
async def api_flagged_file(name: str, request: Request, download: int = 0):
    path = marks.resolve_clip_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="not found")
    return _ranged_file_response(path, request, as_download=bool(download))


@app.delete("/api/flagged/alert/{aid}")
async def api_flagged_delete(aid: str):
    # Removes every camera's clip for this alert AND demotes the event to benign
    # so it also leaves the timeline / alert bar.
    if not marks.delete_alert(aid):
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


# ------------------------- ranged file serving -------------------------

def _ranged_file_response(path: Path, request: Request, as_download: bool = False) -> Response:
    """Serve an mp4 with HTTP Range support so the browser can seek/scrub."""
    file_size = path.stat().st_size
    disposition = (
        f'attachment; filename="{path.name}"' if as_download else f'inline; filename="{path.name}"'
    )
    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": disposition,
        "Cache-Control": "private, max-age=3600",
    }

    range_header = request.headers.get("range")
    if not range_header:
        def full_iter():
            with open(path, "rb") as f:
                while chunk := f.read(_CHUNK):
                    yield chunk

        return StreamingResponse(
            full_iter(),
            media_type="video/mp4",
            headers={**base_headers, "Content-Length": str(file_size)},
        )

    m = _RANGE_RE.fullmatch(range_header.strip())
    if not m:
        raise HTTPException(status_code=416, detail="invalid range")
    start_s, end_s = m.groups()
    if start_s == "":
        # suffix range: last N bytes
        length = int(end_s)
        start = max(0, file_size - length)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        return Response(
            status_code=416, headers={"Content-Range": f"bytes */{file_size}", **base_headers}
        )

    length = end - start + 1

    def range_iter():
        remaining = length
        with open(path, "rb") as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        **base_headers,
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(length),
    }
    return StreamingResponse(range_iter(), status_code=206, media_type="video/mp4", headers=headers)
