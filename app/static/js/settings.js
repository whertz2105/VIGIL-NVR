// Settings page: load/save tier-1 knobs, camera display config, the Frigate
// zone/mask drawing tool, and admin password change. Every call hits an
// admin-gated API; a 401 means the session expired → bounce to /login.
(function () {
  const $ = (id) => document.getElementById(id);
  const toast = $("st-toast");
  let liveCameras = [];
  let cameraState = {}; // name -> {label,color,order,visible}

  const PALETTE = ["#2f81f7", "#f0883e", "#3fb950", "#a371f7", "#db61a2", "#e3b341"];
  const COMMON_TZ = [
    "UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Phoenix", "America/Toronto", "Europe/London", "Europe/Paris", "Europe/Berlin",
    "Europe/Madrid", "Asia/Tokyo", "Asia/Shanghai", "Asia/Kolkata", "Australia/Sydney",
  ];

  function flash(msg, ok) {
    toast.textContent = msg;
    toast.className = "st-toast " + (ok ? "ok" : "err");
    toast.hidden = false;
    clearTimeout(flash._t);
    flash._t = setTimeout(() => { toast.hidden = true; }, 4000);
  }

  async function api(url, opts) {
    const r = await fetch(url, opts);
    if (r.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
    return r;
  }

  // ---------------- load ----------------
  async function load() {
    $("st-tzlist").innerHTML = COMMON_TZ.map((t) => `<option value="${t}">`).join("");
    let data;
    try {
      data = await (await api("/api/settings")).json();
    } catch (e) { return; }
    const s = data.settings || {};
    liveCameras = data.cameras_live || [];
    cameraState = s.cameras || {};

    $("st-brand").value = s.brand || "";
    $("st-tz").value = s.tz_name || "";
    for (const k of ["group_gap_seconds", "mute_seconds", "flag_pre_seconds",
                     "flag_post_seconds", "rec_mobile_window_hours"]) {
      $("st-" + k).value = s[k];
    }
    const tabs = s.tabs || {};
    for (const t of ["live", "recordings", "timelapse", "alerts"]) {
      $("st-tab-" + t).checked = tabs[t] !== false;
    }
    renderCameras();
  }

  function renderCameras() {
    const wrap = $("st-cameras");
    wrap.innerHTML = "";
    if (!liveCameras.length) {
      wrap.innerHTML = '<p class="st-note">No cameras reported by MediaMTX.</p>';
      return;
    }
    liveCameras.forEach((name, i) => {
      const c = cameraState[name] || {};
      const row = document.createElement("div");
      row.className = "st-cam-row";
      row.innerHTML =
        `<span class="st-cam-name">${name}</span>` +
        `<input class="st-cam-label" data-cam="${name}" type="text" placeholder="${name}" value="${(c.label || "").replace(/"/g, "&quot;")}">` +
        `<input class="st-cam-color" data-cam="${name}" type="color" value="${c.color || PALETTE[i % PALETTE.length]}">` +
        `<input class="st-cam-order" data-cam="${name}" type="number" title="sort order" value="${c.order != null ? c.order : i}">` +
        `<label class="st-cam-vis"><input class="st-cam-visible" data-cam="${name}" type="checkbox" ${c.visible === false ? "" : "checked"}> show</label>`;
      wrap.appendChild(row);
    });
  }

  function collectCameras() {
    const out = {};
    document.querySelectorAll(".st-cam-label").forEach((el) => {
      const name = el.dataset.cam;
      out[name] = out[name] || {};
      out[name].label = el.value.trim();
    });
    document.querySelectorAll(".st-cam-color").forEach((el) => {
      out[el.dataset.cam].color = el.value;
    });
    document.querySelectorAll(".st-cam-order").forEach((el) => {
      out[el.dataset.cam].order = parseInt(el.value, 10) || 0;
    });
    document.querySelectorAll(".st-cam-visible").forEach((el) => {
      out[el.dataset.cam].visible = el.checked;
    });
    return out;
  }

  // ---------------- save tier-1 + cameras ----------------
  async function save() {
    const patch = {
      brand: $("st-brand").value,
      tz_name: $("st-tz").value,
      tabs: {
        live: $("st-tab-live").checked, recordings: $("st-tab-recordings").checked,
        timelapse: $("st-tab-timelapse").checked, alerts: $("st-tab-alerts").checked,
      },
      cameras: collectCameras(),
    };
    for (const k of ["group_gap_seconds", "mute_seconds", "flag_pre_seconds",
                     "flag_post_seconds", "rec_mobile_window_hours"]) {
      patch[k] = parseInt($("st-" + k).value, 10);
    }
    let r;
    try { r = await api("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
    }); } catch (e) { return; }
    if (r.ok) { flash("Settings saved.", true); }
    else { const e = await r.json().catch(() => ({})); flash("Error: " + (e.detail || r.status), false); }
  }

  // ---------------- change password ----------------
  async function changePassword() {
    const cur = $("st-pw-cur").value, nw = $("st-pw-new").value;
    let r;
    try { r = await api("/api/settings/password", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current: cur, new: nw }),
    }); } catch (e) { return; }
    if (r.ok) { $("st-pw-cur").value = ""; $("st-pw-new").value = ""; flash("Password changed.", true); }
    else { const e = await r.json().catch(() => ({})); flash("Error: " + (e.detail || r.status), false); }
  }

  $("st-save").addEventListener("click", save);
  $("st-pw-save").addEventListener("click", changePassword);

  // ---------------- frigate zone/mask tool ----------------
  const fgCanvas = $("fg-canvas");
  if (fgCanvas) initFrigateTool(fgCanvas);

  async function initFrigateTool(canvas) {
    const ctx = canvas.getContext("2d");
    const MASK_COLOR = "#ff5a3c";
    const NEW_PALETTE = ["#e6194b","#3cb44b","#4363d8","#f58231","#911eb4","#42d4f4","#f032e6","#bfef45","#469990","#9a6324"];
    const MON_LABEL = { person: "Person", car: "Car", both: "Both", nothing: "Nothing" };
    const S = { cam: null, frameW: 0, frameH: 0, img: null, zones: [], masks: [], cameras: [], required: [], mode: "zones", sel: null, drag: -1 };

    const editor = document.querySelector(".fg-editor");
    const modeTabsWrap = document.querySelector(".fg-modetabs");
    const listEl = $("fg-list"), propsEl = document.querySelector(".fg-props");
    let maskPattern = null;

    async function loadCams() {
      const d = await (await api("/api/settings/frigate/cameras")).json();
      $("fg-cam").innerHTML = (d.cameras || []).map((c) => `<option>${c}</option>`).join("");
    }
    await loadCams();

    // ---- load a camera (shapes + snapshot) ----
    function frameUrl(cam) {
      const latest = $("fg-latest");
      const src = latest && latest.checked ? "latest" : "noon";  // default = noon
      return `/api/settings/frigate/frame/${cam}?source=${src}&t=${Date.now()}`;
    }
    function loadFrame(cam) {
      return new Promise((res) => {
        const img = new Image();
        img.onload = () => { S.img = img; canvas.width = S.frameW; canvas.height = S.frameH; res(); };
        img.onerror = () => { S.img = null; canvas.width = S.frameW; canvas.height = S.frameH; res(); };
        img.src = frameUrl(cam);
      });
    }
    async function loadCamera() {
      const cam = $("fg-cam").value; if (!cam) return;
      let shapes;
      try { shapes = await (await api(`/api/settings/frigate/camera/${cam}`)).json(); }
      catch (e) { return; }
      S.cam = cam; S.frameW = shapes.width; S.frameH = shapes.height;
      S.zones = shapes.zones || []; S.masks = shapes.masks || []; S.sel = null;
      S.cameras = shapes.cameras || []; S.required = shapes.required_zones || [];
      await loadFrame(cam);
      editor.hidden = false; modeTabsWrap.hidden = false;
      showProps(); renderList(); redraw();
    }
    $("fg-load").addEventListener("click", loadCamera);
    // Toggling noon/latest re-fetches just the background, keeping any edit in progress.
    if ($("fg-latest")) $("fg-latest").addEventListener("change", async () => { if (S.cam) { await loadFrame(S.cam); redraw(); } });


    // ---- mode tabs ----
    document.querySelectorAll(".fg-modetab").forEach((b) => b.addEventListener("click", () => {
      document.querySelectorAll(".fg-modetab").forEach((x) => x.classList.toggle("on", x === b));
      S.mode = b.dataset.mode; S.sel = null; showProps(); renderList(); redraw();
    }));
    function coordKind() {
      $("fg-coordkind").textContent = S.mode === "masks"
        ? "Masks = EXCLUDED areas (striped) · absolute pixels."
        : "Zones = WATCHED areas · normalized 0–1. Colour = zone identity, NOT what it monitors.";
    }

    // ---- list ----
    function renderList() {
      coordKind();
      listEl.innerHTML = "";
      if (S.mode === "zones") {
        S.zones.forEach((z) => {
          const li = document.createElement("li"); li.className = "fg-li";
          li.innerHTML = `<span class="fg-sw" style="background:${z.color}"></span>` +
            `<span class="fg-li-name">${z.name}</span><span class="fg-li-mon">${MON_LABEL[z.monitors] || z.monitors}</span>`;
          if (S.sel && S.sel.kind === "zone" && S.sel.origName === z.name) li.classList.add("on");
          li.addEventListener("click", () => selectZone(z));
          listEl.appendChild(li);
        });
      } else {
        S.masks.forEach((m) => {
          const li = document.createElement("li"); li.className = "fg-li";
          li.innerHTML = `<span class="fg-sw fg-sw-mask"></span>` +
            `<span class="fg-li-name">Mask #${m.index}</span><span class="fg-li-mon">excluded</span>`;
          if (S.sel && S.sel.kind === "mask" && S.sel.index === m.index) li.classList.add("on");
          li.addEventListener("click", () => selectMask(m));
          listEl.appendChild(li);
        });
      }
      renderRequiredZones();
    }

    // ---- alert zones (Frigate required_zones) ----
    function renderRequiredZones() {
      const wrap = $("fg-required-wrap");
      if (!wrap) return;
      wrap.style.display = S.mode === "zones" ? "" : "none";   // zones only
      const box = $("fg-required");
      box.innerHTML = "";
      if (!S.zones.length) { box.innerHTML = '<span class="st-note">No zones yet.</span>'; return; }
      S.zones.forEach((z) => {
        const lbl = document.createElement("label"); lbl.className = "fg-req-item";
        const cb = document.createElement("input"); cb.type = "checkbox"; cb.value = z.name;
        cb.checked = S.required.includes(z.name);
        lbl.append(cb, document.createTextNode(" " + z.name));
        box.appendChild(lbl);
      });
    }
    if ($("fg-required-save")) $("fg-required-save").addEventListener("click", async () => {
      const zones = [...document.querySelectorAll("#fg-required input:checked")].map((c) => c.value);
      let r;
      try { r = await api("/api/settings/frigate/required-zones", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ camera: S.cam, zones }) }); } catch (e) { return; }
      const d = await r.json().catch(() => ({}));
      if (r.ok) { result(true, `Alert zones saved. ${d.restart_hint} (backup: ${d.backup})`); flash("Alert zones saved.", true); await reload(null); }
      else { result(false, "Error: " + (d.detail || r.status)); flash("Error: " + (d.detail || r.status), false); }
    });
    $("fg-new").addEventListener("click", () => {
      S.sel = S.mode === "zones"
        ? { kind: "zone", origName: null, name: "", monitors: "both", color: nextColor(), pts: [] }
        : { kind: "mask", index: null, color: MASK_COLOR, pts: [] };
      showProps(); renderList(); redraw();
    });
    function nextColor() {
      const used = new Set(S.zones.map((z) => z.color));
      return NEW_PALETTE.find((c) => !used.has(c)) || NEW_PALETTE[S.zones.length % NEW_PALETTE.length];
    }

    function selectZone(z) {
      S.sel = { kind: "zone", origName: z.name, name: z.name, monitors: z.monitors, color: z.color,
                pts: z.points.map((p) => [p[0] * S.frameW, p[1] * S.frameH]) };
      showProps(); renderList(); redraw();
    }
    function selectMask(m) {
      S.sel = { kind: "mask", index: m.index, color: MASK_COLOR, pts: m.points.map((p) => [p[0], p[1]]) };
      showProps(); renderList(); redraw();
    }

    // ---- props panel ----
    function showProps() {
      propsEl.hidden = !S.sel;
      if (!S.sel) return;
      const isZone = S.sel.kind === "zone";
      $("fg-name-wrap").style.display = isZone ? "" : "none";
      $("fg-monitors-wrap").style.display = isZone ? "" : "none";
      const existing = isZone ? !!S.sel.origName : (S.sel.index != null && S.sel.index >= 0);
      $("fg-delete").style.display = existing ? "" : "none";
      if (isZone) { $("fg-name").value = S.sel.name || ""; $("fg-monitors").value = S.sel.monitors; }
    }
    $("fg-name").addEventListener("input", () => { if (S.sel && S.sel.kind === "zone") { S.sel.name = $("fg-name").value; redraw(); } });
    $("fg-monitors").addEventListener("change", () => { if (S.sel && S.sel.kind === "zone") { S.sel.monitors = $("fg-monitors").value; redraw(); } });
    $("fg-cancel").addEventListener("click", () => { S.sel = null; showProps(); renderList(); redraw(); });

    // ---- canvas drawing ----
    function stripe() {
      if (maskPattern) return maskPattern;
      const p = document.createElement("canvas"); p.width = p.height = 10;
      const c = p.getContext("2d");
      c.strokeStyle = MASK_COLOR; c.lineWidth = 3;
      c.beginPath(); c.moveTo(0, 10); c.lineTo(10, 0); c.stroke();
      c.beginPath(); c.moveTo(-2, 2); c.lineTo(2, -2); c.stroke();
      c.beginPath(); c.moveTo(8, 12); c.lineTo(12, 8); c.stroke();
      maskPattern = ctx.createPattern(p, "repeat"); return maskPattern;
    }
    function hexA(hex, a) { const n = parseInt(hex.slice(1), 16); return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`; }
    function drawPoly(pts, color, striped, editable, label) {
      if (!pts.length) return;
      ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
      const closed = pts.length > 2;
      if (closed) {
        ctx.closePath();
        ctx.fillStyle = striped ? stripe() : hexA(color, 0.18);
        ctx.fill();
      }
      ctx.lineWidth = Math.max(2, S.frameW / 480) * (editable ? 1.7 : 1);
      ctx.strokeStyle = color; ctx.stroke();
      if (editable) {
        const r = Math.max(5, S.frameW / 150);
        pts.forEach((p) => {
          ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, 7); ctx.fillStyle = color; ctx.fill();
          ctx.lineWidth = 2; ctx.strokeStyle = "#fff"; ctx.stroke();
        });
      }
      if (label && closed) {
        const cx = pts.reduce((s, p) => s + p[0], 0) / pts.length, cy = pts.reduce((s, p) => s + p[1], 0) / pts.length;
        ctx.font = `${Math.max(15, S.frameW / 55)}px system-ui`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        const tw = ctx.measureText(label).width, h = Math.max(20, S.frameW / 42);
        ctx.fillStyle = "rgba(0,0,0,.62)"; ctx.fillRect(cx - tw / 2 - 7, cy - h / 2, tw + 14, h);
        ctx.fillStyle = "#fff"; ctx.fillText(label, cx, cy); ctx.textAlign = "start";
      }
    }
    function redraw() {
      if (S.img) ctx.drawImage(S.img, 0, 0, canvas.width, canvas.height);
      else { ctx.fillStyle = "#0c0f14"; ctx.fillRect(0, 0, canvas.width, canvas.height); }
      const selZoneName = S.sel && S.sel.kind === "zone" ? S.sel.origName : null;
      const selMaskIdx = S.sel && S.sel.kind === "mask" ? S.sel.index : null;
      S.masks.forEach((m) => { if (m.index === selMaskIdx) return; drawPoly(m.points.map((p) => [p[0], p[1]]), MASK_COLOR, true, false, "Mask #" + m.index); });
      S.zones.forEach((z) => {
        if (z.name === selZoneName) return;
        drawPoly(z.points.map((p) => [p[0] * S.frameW, p[1] * S.frameH]), z.color, false, false, `${z.name} · ${MON_LABEL[z.monitors]}`);
      });
      if (S.sel) {
        const label = S.sel.kind === "mask" ? "Mask (excluded)" : `${S.sel.name || "(unnamed)"} · ${MON_LABEL[S.sel.monitors]}`;
        drawPoly(S.sel.pts, S.sel.color, S.sel.kind === "mask", true, label);
      }
    }

    // ---- canvas interaction: add / move / remove points ----
    function evToFrame(e) {
      const r = canvas.getBoundingClientRect();
      return [(e.clientX - r.left) * (canvas.width / r.width), (e.clientY - r.top) * (canvas.height / r.height)];
    }
    function vertexAt(pt) {
      if (!S.sel) return -1;
      const thr = Math.max(10, S.frameW / 110);
      for (let i = 0; i < S.sel.pts.length; i++) {
        if (Math.hypot(S.sel.pts[i][0] - pt[0], S.sel.pts[i][1] - pt[1]) <= thr) return i;
      }
      return -1;
    }
    function insertNearestEdge(pt) {
      const p = S.sel.pts;
      let best = p.length, bestD = Infinity;
      for (let i = 0; i < p.length; i++) {
        const a = p[i], b = p[(i + 1) % p.length];
        const vx = b[0] - a[0], vy = b[1] - a[1], wx = pt[0] - a[0], wy = pt[1] - a[1];
        const c2 = vx * vx + vy * vy, t = c2 ? Math.max(0, Math.min(1, (vx * wx + vy * wy) / c2)) : 0;
        const d = Math.hypot(pt[0] - (a[0] + t * vx), pt[1] - (a[1] + t * vy));
        if (d < bestD) { bestD = d; best = i + 1; }
      }
      p.splice(best, 0, pt);
    }
    canvas.addEventListener("mousedown", (e) => {
      if (!S.sel) return;
      const pt = evToFrame(e), vi = vertexAt(pt);
      if (vi >= 0) { S.drag = vi; return; }
      if (S.sel.pts.length < 3) S.sel.pts.push(pt); else insertNearestEdge(pt);
      redraw();
    });
    canvas.addEventListener("mousemove", (e) => { if (S.drag >= 0 && S.sel) { S.sel.pts[S.drag] = evToFrame(e); redraw(); } });
    window.addEventListener("mouseup", () => { S.drag = -1; });
    function removeVertexAt(pt) {
      const vi = vertexAt(pt);
      if (vi < 0) return false;
      if (S.sel.pts.length > 3) { S.sel.pts.splice(vi, 1); redraw(); }
      else flash("A polygon needs at least 3 points.", false);
      return true;
    }
    canvas.addEventListener("dblclick", (e) => { if (S.sel) removeVertexAt(evToFrame(e)); });
    // Right-click a vertex to remove it (suppress the browser menu while editing).
    canvas.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      if (S.sel) removeVertexAt(evToFrame(e));
    });

    // ---- save / delete ----
    async function reload(selectName) {
      const shapes = await (await api(`/api/settings/frigate/camera/${S.cam}`)).json();
      S.zones = shapes.zones || []; S.masks = shapes.masks || []; S.frameW = shapes.width; S.frameH = shapes.height;
      S.cameras = shapes.cameras || []; S.required = shapes.required_zones || [];
      S.sel = null;
      if (selectName) { const z = S.zones.find((z) => z.name === selectName); if (z) selectZone(z); }
      showProps(); renderList(); redraw();
    }
    function result(ok, msg) { const r = $("fg-result"); r.hidden = false; r.className = "st-note " + (ok ? "ok" : "err"); r.textContent = msg; }

    $("fg-save").addEventListener("click", async () => {
      if (!S.sel || S.sel.pts.length < 3) { flash("Add at least 3 points.", false); return; }
      let url = "/api/settings/frigate/zone", payload, keep = null;
      if (S.sel.kind === "zone") {
        const name = $("fg-name").value.trim();
        if (!/^[A-Za-z0-9_]{1,40}$/.test(name)) { flash("Zone name: letters, digits, underscore only.", false); return; }
        // Client-side guards (server enforces these too): no camera-name collision,
        // no collision with another existing zone.
        if (S.cameras.includes(name)) { flash(`"${name}" is a camera name — Frigate forbids a zone with that name.`, false); return; }
        if (name !== S.sel.origName && S.zones.some((z) => z.name === name)) { flash(`A zone named "${name}" already exists.`, false); return; }
        payload = { camera: S.cam, name, monitors: $("fg-monitors").value, points: S.sel.pts.map((p) => [p[0] / S.frameW, p[1] / S.frameH]) };
        if (S.sel.origName) payload.old_name = S.sel.origName;
        keep = name;
      } else {
        url = "/api/settings/frigate/mask";
        payload = { camera: S.cam, points: S.sel.pts.map((p) => [Math.round(p[0]), Math.round(p[1])]) };
        if (S.sel.index != null && S.sel.index >= 0) payload.index = S.sel.index;
      }
      let r;
      try { r = await api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); } catch (e) { return; }
      const d = await r.json().catch(() => ({}));
      if (r.ok) { result(true, `${d.restart_hint}  (backup: ${d.backup})`); flash("Saved.", true); await reload(keep); }
      else { result(false, "Error: " + (d.detail || r.status)); flash("Error: " + (d.detail || r.status), false); }
    });

    $("fg-delete").addEventListener("click", async () => {
      if (!S.sel) return;
      let url;
      if (S.sel.kind === "zone" && S.sel.origName) {
        if (!confirm(`Delete zone "${S.sel.origName}"?`)) return;
        url = `/api/settings/frigate/zone/${S.cam}/${S.sel.origName}`;
      } else if (S.sel.kind === "mask" && S.sel.index != null && S.sel.index >= 0) {
        if (!confirm(`Delete mask #${S.sel.index}?`)) return;
        url = `/api/settings/frigate/mask/${S.cam}/${S.sel.index}`;
      } else return;
      let r;
      try { r = await api(url, { method: "DELETE" }); } catch (e) { return; }
      const d = await r.json().catch(() => ({}));
      if (r.ok) { result(true, `Deleted. ${d.restart_hint}  (backup: ${d.backup})`); flash("Deleted.", true); await reload(null); }
      else { result(false, "Error: " + (d.detail || r.status)); }
    });
  }

  load();
})();
