"""Optional editor for a Frigate config file (zones + motion masks).

Enabled only when config.FRIGATE_CONFIG points at a readable YAML file; otherwise
every function reports "unavailable" and the Settings UI hides the tool.

Coordinate conventions — getting these wrong silently breaks detection, so they
are kept explicit throughout:
  * ZONES        → normalized 0..1 of the frame, written as `coordinates: "x,y,..."`.
  * MOTION MASKS → ABSOLUTE PIXELS of the frame, written under `motion: { mask: [...] }`.

Safety: the previous config is copied to a timestamped backup before any write,
the result is re-parsed to confirm it is valid YAML, and we NEVER reload/restart
Frigate — the caller is told to run `docker restart frigate` to apply changes.
This process has no Docker socket and cannot control other containers.
"""
from __future__ import annotations

import io
import shutil
from datetime import datetime
from pathlib import Path

from ruamel.yaml import YAML

import config
from config import TZ

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # don't wrap our coordinate strings


def available() -> bool:
    return bool(config.FRIGATE_CONFIG) and Path(config.FRIGATE_CONFIG).is_file()


def _load() -> dict:
    with open(config.FRIGATE_CONFIG) as f:
        data = _yaml.load(f)
    return data or {}


def _frame_dims(cam_cfg: dict) -> tuple[int, int]:
    det = (cam_cfg or {}).get("detect") or {}
    w = int(det.get("width") or 1920)
    h = int(det.get("height") or 1080)
    return w, h


def _parse_points(coord_str) -> list[list[float]]:
    """'x,y,x,y,...' -> [[x,y],...]."""
    nums = [float(p) for p in str(coord_str).split(",") if p.strip() != ""]
    return [[nums[i], nums[i + 1]] for i in range(0, len(nums) - 1, 2)]


# --- "monitors" property <-> Frigate zone `objects:` list ---
# Colour is identity only; THIS is what a zone actually watches.
MONITORS = ("person", "car", "both", "nothing")
_MON_TO_OBJ = {"person": ["person"], "car": ["car"], "both": ["person", "car"], "nothing": []}


def _objects_list(monitors: str) -> list:
    return list(_MON_TO_OBJ[monitors])


def _zone_monitors(zdict: dict) -> str:
    # No `objects:` key at all = Frigate matches all objects -> treat as "both".
    if "objects" not in zdict or zdict.get("objects") is None:
        return "both"
    s = {str(o) for o in (zdict.get("objects") or [])}
    if not s:
        return "nothing"
    has_p, has_c = "person" in s, "car" in s
    if has_p and has_c:
        return "both"
    if has_p:
        return "person"
    if has_c:
        return "car"
    return "nothing"


# Identity palette: distinct, saturated hues. Colour conveys WHICH zone, never
# what it monitors. Stable per camera (deterministic from the set of zone names).
_PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
            "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
            "#9a6324", "#808000"]


def _zone_colors(names: list[str]) -> dict:
    """Assign each zone a distinct, stable colour. Derived from the zone name
    (hash), with collision-probing so two zones never share a colour."""
    taken: set[int] = set()
    out: dict[str, str] = {}
    for n in sorted(names):
        base = sum(ord(c) for c in n) % len(_PALETTE)
        idx = base
        for k in range(len(_PALETTE)):
            cand = (base + k) % len(_PALETTE)
            if cand not in taken:
                idx = cand
                break
        taken.add(idx)
        out[n] = _PALETTE[idx]
    return out


def _mask_list(mraw) -> list[str]:
    """motion.mask may be a single string or a list of polygon strings."""
    if not mraw:
        return []
    if isinstance(mraw, str):
        return [mraw]
    if isinstance(mraw, (list, tuple)):
        return [m for m in mraw if isinstance(m, str) and m.strip()]
    return []


# --- required_zones plumbing (keep references consistent + valid) ---
def _transform_required_zones(node, fn) -> None:
    """Apply fn(list)->list to every 'required_zones' list under node, in place."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if k == "required_zones" and isinstance(v, list):
                node[k] = fn(list(v))
            else:
                _transform_required_zones(v, fn)
    elif isinstance(node, list):
        for it in node:
            _transform_required_zones(it, fn)


def _prune_empty_required_zones(node) -> None:
    """Drop any now-empty required_zones key (empty = no zone requirement)."""
    if isinstance(node, dict):
        for k in list(node.keys()):
            if k == "required_zones" and isinstance(node[k], list) and not node[k]:
                del node[k]
            else:
                _prune_empty_required_zones(node[k])
    elif isinstance(node, list):
        for it in node:
            _prune_empty_required_zones(it)


def _collect_required_zones(node, out, path="") -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "required_zones" and isinstance(v, list):
                out.append((path or "camera", list(v)))
            else:
                _collect_required_zones(v, out, f"{path}/{k}" if path else k)
    elif isinstance(node, list):
        for i, it in enumerate(node):
            _collect_required_zones(it, out, f"{path}[{i}]")


# Frigate's default (COCO) detector labels — used to catch typo'd object types.
_FRIGATE_LABELS = {
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "bird", "cat", "dog", "horse", "sheep", "cow", "bear", "package",
    "license_plate", "dog", "umbrella", "handbag", "suitcase", "bottle",
}


def _validate_config(data: dict) -> None:
    """Replicate the Frigate constraints most likely to get a config REJECTED, so a
    candidate is blocked BEFORE it can replace the live file:
      - a zone may not share a name with any camera (this took detection down),
      - required_zones must reference zones that exist on that camera,
      - zone coords are normalized 0..1 (>=3 pts); mask coords are in-frame pixels,
      - zone objects must be valid labels the camera tracks.
    Raises ValueError on the first violation."""
    cams = data.get("cameras") or {}
    cam_names = set(cams.keys())
    for cam, cfg in cams.items():
        cfg = cfg or {}
        zones = cfg.get("zones") or {}
        zone_names = set(zones.keys())
        track = {str(o) for o in ((cfg.get("objects") or {}).get("track") or [])}
        w, h = _frame_dims(cfg)
        for zn, z in zones.items():
            if zn in cam_names:
                raise ValueError(
                    f"zone '{zn}' (camera '{cam}') shares a name with a camera — "
                    "Frigate rejects this and stops detection")
            if not isinstance(z, dict) or z.get("coordinates") is None:
                raise ValueError(f"zone '{zn}' on '{cam}' is missing coordinates")
            pts = _parse_points(z["coordinates"])
            if len(pts) < 3:
                raise ValueError(f"zone '{zn}' on '{cam}' needs at least 3 points")
            for x, y in pts:
                if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                    raise ValueError(f"zone '{zn}' on '{cam}' has a coordinate outside 0–1")
            for o in (z.get("objects") or []):
                o = str(o)
                if o not in _FRIGATE_LABELS:
                    raise ValueError(f"zone '{zn}' on '{cam}' lists unknown object type '{o}'")
                if track and o not in track:
                    raise ValueError(
                        f"zone '{zn}' on '{cam}' monitors '{o}', which the camera does not "
                        f"track ({sorted(track) or 'nothing'})")
        for i, poly in enumerate(_mask_list((cfg.get("motion") or {}).get("mask"))):
            pts = _parse_points(poly)
            if len(pts) < 3:
                raise ValueError(f"motion mask #{i} on '{cam}' needs at least 3 points")
            for x, y in pts:
                if not (0.0 <= x <= w and 0.0 <= y <= h):
                    raise ValueError(
                        f"motion mask #{i} on '{cam}' has a pixel coordinate outside the "
                        f"{w}x{h} frame")
        rzs: list = []
        _collect_required_zones(cfg, rzs)
        for where, rz in rzs:
            for z in rz:
                if z not in zone_names:
                    raise ValueError(
                        f"required_zones in {cam}/{where} references undefined zone '{z}'")


def camera_names() -> list[str]:
    if not available():
        return []
    try:
        return sorted((_load().get("cameras") or {}).keys())
    except Exception:
        return []


def zone_object_index() -> dict:
    """{camera: {object_type: set(zone_names)}} derived from each zone's monitors.
    Used to filter detection alerts by what the zones actually watch (no separate
    config). A camera with NO zones is omitted, so callers fall back to "show all"
    for it. Returns {} if the Frigate config is unavailable."""
    if not available():
        return {}
    try:
        data = _load()
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for cam, cfg in (data.get("cameras") or {}).items():
        zcfg = (cfg or {}).get("zones") or {}
        idx: dict[str, set] = {}
        has_zone = False
        for zname, z in zcfg.items():
            if not (isinstance(z, dict) and z.get("coordinates") is not None):
                continue
            has_zone = True
            # No objects key = matches all objects; map to person+car (tracked labels).
            objs = {str(o) for o in z["objects"]} if "objects" in z and z.get("objects") is not None \
                else {"person", "car"}
            for o in objs:
                idx.setdefault(o, set()).add(zname)
        if has_zone:
            out[cam] = idx
    return out


def read_camera(cam: str) -> dict | None:
    """Current zones + motion masks for a camera, for the editor.
    Zone points are normalized 0..1; mask points are absolute pixels."""
    if not available():
        return None
    try:
        data = _load()
    except Exception:
        return None
    cams = data.get("cameras") or {}
    if cam not in cams:
        return None
    cam_cfg = cams[cam] or {}
    w, h = _frame_dims(cam_cfg)
    zcfg = cam_cfg.get("zones") or {}
    colors = _zone_colors([n for n, z in zcfg.items()
                           if isinstance(z, dict) and z.get("coordinates") is not None])
    zones = []
    for zname, z in zcfg.items():
        if not (isinstance(z, dict) and z.get("coordinates") is not None):
            continue
        zones.append({
            "name": zname,
            "points": _parse_points(z.get("coordinates")),   # normalized
            "monitors": _zone_monitors(z),
            "color": colors[zname],
        })
    masks = [{"index": i, "points": _parse_points(poly)}      # pixels
             for i, poly in enumerate(_mask_list((cam_cfg.get("motion") or {}).get("mask")))]
    rzs: list = []
    _collect_required_zones(cam_cfg, rzs)
    required = sorted({z for _, lst in rzs for z in lst})
    return {"width": w, "height": h, "zones": zones, "masks": masks,
            "cameras": sorted(cams.keys()), "required_zones": required}


def _validate(points: list, lo_x: float, hi_x: float, lo_y: float, hi_y: float) -> None:
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError("a polygon needs at least 3 points")
    for p in points:
        if (not isinstance(p, (list, tuple)) or len(p) != 2):
            raise ValueError("each point must be [x, y]")
        x, y = p
        if not (lo_x <= float(x) <= hi_x and lo_y <= float(y) <= hi_y):
            raise ValueError(f"point ({x},{y}) is out of range")


def _points_to_str(points: list, as_int: bool) -> str:
    out = []
    for x, y in points:
        if as_int:
            out.append(str(int(round(x))))
            out.append(str(int(round(y))))
        else:
            out.append(f"{round(float(x), 4):g}")
            out.append(f"{round(float(y), 4):g}")
    return ",".join(out)


def _backup_and_write(data: dict) -> str:
    """Validate against Frigate constraints, re-serialize, confirm it parses, back
    up the old file, then write. The single choke point for EVERY edit — a
    candidate Frigate would reject never reaches the live file."""
    _validate_config(data)
    buf = io.StringIO()
    _yaml.dump(data, buf)
    text = buf.getvalue()
    # Confirm the result is still valid YAML before touching disk.
    YAML().load(text)

    src = Path(config.FRIGATE_CONFIG)
    # Sub-second stamp so two saves within the same second don't share (and
    # overwrite) a backup — otherwise the pristine original could be lost.
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f")
    backup = src.with_name(f"{src.name}.bak.{stamp}")
    shutil.copy2(src, backup)

    tmp = src.with_name(f"{src.name}.tmp")
    tmp.write_text(text)
    tmp.replace(src)
    return backup.name


def _cam_cfg(data: dict, cam: str) -> dict:
    cams = data.get("cameras") or {}
    if cam not in cams:
        raise ValueError(f"unknown camera: {cam}")
    return cams[cam]


def save_zone(cam: str, name: str, points: list, monitors: str, old_name: str | None = None) -> str:
    """Create/update a normalized (0..1) zone with a `monitors` setting. If
    `old_name` differs, the zone is renamed (extra keys like inertia preserved).
    Rejects colliding names so an edit can't accidentally fork a new zone.
    Returns the backup filename."""
    import re
    if not available():
        raise RuntimeError("frigate config not available")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", name or ""):
        raise ValueError("zone name must be letters/digits/underscore (1–40 chars)")
    if monitors not in MONITORS:
        raise ValueError("monitors must be person, car, both, or nothing")
    _validate(points, 0.0, 1.0, 0.0, 1.0)
    old_name = old_name or None
    data = _load()
    cam_cfg = _cam_cfg(data, cam)
    zones = cam_cfg.setdefault("zones", {})
    # Collision guard: target name already used by a DIFFERENT zone.
    if name in zones and name != old_name:
        raise ValueError(f"a zone named '{name}' already exists")
    # Reject a name that collides with a camera (Frigate forbids it) — even though
    # _validate_config also catches it, fail early with a clear message.
    if name in (data.get("cameras") or {}):
        raise ValueError(f"'{name}' is a camera name; a zone can't share it")
    if old_name and old_name != name:
        if old_name not in zones:
            raise ValueError(f"zone '{old_name}' not found to rename")
        zones[name] = zones.pop(old_name)        # keep inertia/loitering_time/etc.
        # Repoint any required_zones references from the old name to the new one.
        _transform_required_zones(cam_cfg, lambda rz: [name if z == old_name else z for z in rz])
    zentry = zones.setdefault(name, {})
    zentry["coordinates"] = _points_to_str(points, as_int=False)
    zentry["objects"] = _objects_list(monitors)
    return _backup_and_write(data)


def delete_zone(cam: str, name: str) -> str:
    if not available():
        raise RuntimeError("frigate config not available")
    data = _load()
    cam_cfg = _cam_cfg(data, cam)
    zones = cam_cfg.get("zones") or {}
    if name not in zones:
        raise ValueError(f"zone '{name}' not found")
    del zones[name]
    # Remove the deleted zone from every required_zones list (no dangling refs).
    _transform_required_zones(cam_cfg, lambda rz: [z for z in rz if z != name])
    _prune_empty_required_zones(cam_cfg)
    return _backup_and_write(data)


def _write_masks(cam_cfg: dict, masks: list[str]) -> None:
    motion = cam_cfg.setdefault("motion", {})
    if not masks:
        motion.pop("mask", None)
    elif len(masks) == 1:
        motion["mask"] = masks[0]
    else:
        motion["mask"] = masks


def save_mask(cam: str, points: list, index: int | None = None) -> str:
    """Create (index None) or replace (existing index) a motion mask in ABSOLUTE
    PIXELS. Other masks/zones are untouched. Returns the backup filename."""
    if not available():
        raise RuntimeError("frigate config not available")
    data = _load()
    cam_cfg = _cam_cfg(data, cam)
    w, h = _frame_dims(cam_cfg)
    _validate(points, 0.0, float(w), 0.0, float(h))
    masks = _mask_list((cam_cfg.get("motion") or {}).get("mask"))
    s = _points_to_str(points, as_int=True)
    if index is None or index < 0 or index >= len(masks):
        masks.append(s)
    else:
        masks[index] = s
    _write_masks(cam_cfg, masks)
    return _backup_and_write(data)


def delete_mask(cam: str, index: int) -> str:
    if not available():
        raise RuntimeError("frigate config not available")
    data = _load()
    cam_cfg = _cam_cfg(data, cam)
    masks = _mask_list((cam_cfg.get("motion") or {}).get("mask"))
    if index < 0 or index >= len(masks):
        raise ValueError("mask not found")
    masks.pop(index)
    _write_masks(cam_cfg, masks)
    return _backup_and_write(data)


def set_required_zones(cam: str, zone_list: list) -> str:
    """Set which zones gate alerts/detections/snapshots for a camera (applied
    uniformly to review.alerts, review.detections, snapshots). Each must be an
    existing zone. Empty = no requirement (alerts fire frame-wide)."""
    if not available():
        raise RuntimeError("frigate config not available")
    data = _load()
    cam_cfg = _cam_cfg(data, cam)
    existing = set((cam_cfg.get("zones") or {}).keys())
    clean: list = []
    for z in zone_list or []:
        z = str(z)
        if z not in existing:
            raise ValueError(f"'{z}' is not a zone on '{cam}'")
        if z not in clean:
            clean.append(z)
    review = cam_cfg.setdefault("review", {})
    for sect in ("alerts", "detections"):
        review.setdefault(sect, {})["required_zones"] = list(clean)
    cam_cfg.setdefault("snapshots", {})["required_zones"] = list(clean)
    _prune_empty_required_zones(cam_cfg)
    return _backup_and_write(data)
