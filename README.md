# VIGIL NVR

A self-hosted **viewer / playback / alerts UI** for an existing camera setup. It
sits on top of your own recorder + [MediaMTX](https://github.com/bluenviron/mediamtx)
(and, optionally, [Frigate](https://github.com/blakeblackshear/frigate) for
detection). It does **not** record or pull from cameras itself — it reads what
your pipeline already produces.

- **Live** — multi-camera HLS grid (via hls.js; fmp4 for iOS reliability), with a
  video-only fallback for browsers whose MSE can't decode AAC.
- **Recordings** — timeline-first synchronized multi-camera playback (zoom / pan /
  scrub), mobile-friendly window, detection markers you can click to jump to.
- **Timelapse** — plays your nightly per-camera timelapse clips.
- **Saved Alerts** — flagged events saved as multi-camera clips for review.
- **Settings (admin)** — behaviour knobs, per-camera labels/colours, and a Frigate
  **zone/mask editor** with a validation gate (it can't write a config Frigate
  would reject). Optional; appears only when configured.

Detection/alerts are **optional** — with no Frigate, those features simply don't
appear (no errors).

```
 cameras ──RTSP──> recorder ───> recordings/  ─┐
         └─RTSP──> MediaMTX ──HLS─┐            ├─(read-only)─> nvr-viewer ──HTTP──> browser
                          (api)   │            │   (this app)
                 Frigate ─(reads MediaMTX)─> /api/events ─(optional)─┘
```

---

## ⚠️ Security — read this first

- **The image ships with NO authentication.** Anyone who can reach it gets the
  viewer; anyone who reaches the admin login page can attempt it.
- **Put it behind your own auth / VPN / reverse proxy.** Do **not** expose the
  viewer — or your cameras — to the public internet without it.
- The app has a **self-contained admin login** (argon2-hashed password, signed
  httpOnly sessions, login rate-limiting) that gates the Settings page. On first
  run it **generates a random admin password and prints it once to the container
  logs** — there is no static default. A reverse-proxy-passed username
  (`TRUSTED_USER_HEADER`) is **display-only and never grants admin**.
- **No Docker socket, nothing privileged.** The zone editor is **write-and-prompt**:
  it writes the Frigate config + a timestamped backup and tells you to run
  `docker restart frigate` yourself. The viewer never restarts or reloads Frigate.
- **Camera-pull constraint:** many inexpensive cameras tolerate only a couple of
  concurrent RTSP pulls; a 3rd can drop an existing one (e.g. multi-hour recording
  gaps). Keep it to **recorder + MediaMTX** — Frigate and this viewer read from
  MediaMTX, never the camera directly. (See `companion/mediamtx/`.)

---

## Runtime contract

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `TZ` | `UTC` | Interpreting recording folder names / labels. |
| `BRAND` | `NVR` | Header/title text. |
| `MEDIAMTX_API` | `http://mediamtx:9997` | Your MediaMTX HTTP API (camera auto-discovery). |
| `HLS_PREFIX` | `/hls` | URL prefix the browser uses for HLS (proxied to MediaMTX `:8888`). |
| `RECORDINGS_DIR` | `/recordings` | Recorder output (mount **read-only**). |
| `TIMELAPSES_DIR` | `/timelapses` | Timelapse clips (mount **read-only**). |
| `FLAGGED_DIR` | `/flagged` | Saved flagged clips (mount **read-write**). |
| `CACHE_DIR` | `/data` | Cache, `settings.json`, `admin.json` (mount **read-write**, keep private). |
| `SESSION_MAX_AGE` | `604800` | Admin session lifetime (seconds). |
| `TRUSTED_USER_HEADER` | *(unset)* | Header to display a proxy username (never grants admin). |
| `FRIGATE_API` | `http://frigate:5000` | Frigate API (optional; detection features). |
| `FRIGATE_CONFIG` | *(unset)* | Path to a Frigate config file to enable the zone editor (optional). |
| `GROUP_GAP_SECONDS` | `60` | Collapse same-camera+label detections within N s into one alert. |
| `FLAG_PRE_SECONDS` / `FLAG_POST_SECONDS` | `30` / `300` | Flagged-clip window. |
| `MUTE_SECONDS` | `7200` | "Silence alerts" snooze duration. |

Most of these are **also editable at runtime** in the Settings GUI, which writes
`/data/settings.json` (resolution order: built-in default → env → settings file).

### Volume mounts

| Mount | Mode | Contents |
|---|---|---|
| `…:/recordings` | ro | Recorder output. |
| `…:/timelapses` | ro | Nightly timelapse clips. |
| `…:/flagged` | rw | Saved flagged-alert clips. |
| `…:/data` | rw | Cache + `settings.json` + `admin.json`. |
| `…:/frigate-config` | rw | *Only* if using the zone editor (`FRIGATE_CONFIG`). |

### Expected on-disk layouts

```
recordings/  {YY_MM_DD}/{HH}/{cam}/{cam}_{HH-MM}.mp4     # 15-min H.264 segments
timelapses/  {YY_MM_DD}/{cam}.mp4
```

### MediaMTX path convention

Two paths per camera so audio can be added:

- `<cam>src` — pulls RTSP from the real camera (source URL with credentials lives
  in *your* env, not here).
- `<cam>` — what HLS serves / the viewer plays; fed by a `runOnReady` ffmpeg that
  reads `<cam>src` over localhost and transcodes G.711 audio → AAC (fmp4 HLS
  carries AAC, not G.711), copying the video. **It reads from MediaMTX, never the
  camera** (no extra camera pull). The viewer hides any path ending in `src`.

`<cam>` should match the recording-folder name so live + playback line up. Full
annotated example in `companion/mediamtx/mediamtx.example.yml`.

---

## Quickstart

```bash
git clone <this-repo> nvr-viewer && cd nvr-viewer
cp .env.example .env          # adjust as needed
cp docker-compose.example.yml docker-compose.yml   # then edit the volume paths
docker compose up -d --build
docker compose logs nvr-viewer | grep -A5 'admin account'   # one-time admin password
```

Then point a browser at the viewer (behind your reverse proxy / VPN). Log in at
`/login` with `admin` + the printed password and change it under Settings.

The example compose includes a generic MediaMTX for convenience — **bring your
own / adapt it.** The image does not require this specific stack: set the env
vars and mounts and point it at whatever recorder + MediaMTX you run.

---

## Optional companions

These live in `companion/` and are **not required** by the viewer image:

- `companion/mediamtx/mediamtx.example.yml` — annotated MediaMTX config (the
  G.711→AAC transcode pattern, fmp4-for-iOS, the never-a-3rd-pull rationale, the
  `<cam>src` single-token naming constraint).
- `companion/frigate/config.example.yml` — example Frigate config for detection.
  All coordinates are **placeholder rectangles** — draw your own zones/masks in
  the Settings editor or the Frigate UI. After editing zones, **restart Frigate
  yourself** to apply (`docker restart frigate`).
- `companion/scripts/nvr-timelapse.sh` — example nightly timelapse generator.

### Hardware note

The viewer image itself needs **no GPU**. The iGPU / OpenVINO bits in the Frigate
example are *Frigate's* detector choice — swap them for your hardware. Detection
is an entirely optional, separate component.

---

## License & attribution

MIT — see [LICENSE](LICENSE).

Bundled / referenced third-party components and their licenses (all MIT-compatible):

| Component | License | Use |
|---|---|---|
| [hls.js](https://github.com/video-dev/hls.js) | Apache-2.0 | bundled (`app/static/js/hls.min.js`) for HLS playback |
| [FastAPI](https://github.com/fastapi/fastapi) / [Starlette](https://github.com/encode/starlette) | MIT / BSD-3-Clause | web framework |
| [uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server |
| [Jinja2](https://github.com/pallets/jinja) | BSD-3-Clause | templates |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | HTTP client |
| [argon2-cffi](https://github.com/hynek/argon2-cffi) | MIT | password hashing |
| [itsdangerous](https://github.com/pallets/itsdangerous) | BSD-3-Clause | signed session cookies |
| [ruamel.yaml](https://sourceforge.net/projects/ruamel-yaml/) | MIT | comment-preserving YAML editing |
| [MediaMTX](https://github.com/bluenviron/mediamtx) | MIT | streaming server (companion) |
| [Frigate](https://github.com/blakeblackshear/frigate) | MIT | detection (optional companion) |

Apache-2.0 (hls.js) and BSD are all MIT-compatible. hls.js is bundled in minified
form; its Apache-2.0 license applies to that file.
