// Live camera grid: one HLS <video> per camera, discovered from the API.
(function () {
  const grid = document.getElementById("grid");
  const statusEl = document.getElementById("live-status");
  const muteAllBtn = document.getElementById("mute-all");
  const tpl = document.getElementById("tile-tpl");
  const players = []; // {video, hls, camera}

  // onLive(videoOnly) is called when playback can start; videoOnly=true means we
  // fell back to the audio-less stream.
  function attachHls(video, cam, onLive) {
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      // Native HLS (Safari/iOS) — handles AAC fine, so always use the audio path.
      video.src = cam.hls;
      video.addEventListener("loadedmetadata", () => onLive(false), { once: true });
      return;
    }
    if (!(window.Hls && Hls.isSupported())) return;
    let hls;
    let usedFallback = false;
    function build(url) {
      hls = new Hls({ lowLatencyMode: true, backBufferLength: 10, liveSyncDurationCount: 3 });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => onLive(usedFallback));
      hls.on(Hls.Events.ERROR, (_e, data) => {
        if (!data.fatal) return;
        // Some browsers' MSE (notably Firefox on Linux) can't decode AAC, so the
        // audio+video manifest reports incompatible codecs. Fall back ONCE to the
        // video-only stream so live video still plays (without audio).
        if (cam.hls_video_only && !usedFallback && /codec/i.test(data.details || "")) {
          usedFallback = true;
          hls.destroy();
          build(cam.hls_video_only);
          return;
        }
        // Recover from transient network/media errors; cameras may blip.
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
        else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else hls.destroy();
      });
    }
    build(cam.hls);
  }

  function makeTile(cam) {
    const node = tpl.content.firstElementChild.cloneNode(true);
    const video = node.querySelector("video");
    const nameEl = node.querySelector(".tile-name");
    const badge = node.querySelector(".tile-badge");
    nameEl.textContent = cam.name;
    badge.textContent = "connecting…";

    attachHls(video, cam, (videoOnly) => {
      badge.textContent = videoOnly ? "live · no audio" : "live";
      badge.classList.add("live");
      if (videoOnly) badge.title = "This browser can't decode the stream's AAC audio (try Chrome); showing video only.";
      video.play().catch(() => {});
    });

    // Click toggles expand + audio for this tile (and mutes the others).
    node.addEventListener("click", () => {
      const expanding = !node.classList.contains("expanded");
      document.querySelectorAll(".tile.expanded").forEach((t) => t.classList.remove("expanded"));
      if (expanding) {
        node.classList.add("expanded");
        players.forEach((p) => { if (p.video !== video) p.video.muted = true; });
        video.muted = false;
        video.play().catch(() => {});
        refreshAudioBadges();
        node.scrollIntoView({ behavior: "smooth", block: "start" });
      } else {
        video.muted = true;
        refreshAudioBadges();
      }
    });

    grid.appendChild(node);
    players.push({ video, camera: cam.name, badge });
  }

  function refreshAudioBadges() {
    players.forEach((p) => {
      p.badge.classList.toggle("audio", !p.video.muted);
      if (!p.video.muted) p.badge.textContent = "audio on";
      else if (p.badge.classList.contains("live")) p.badge.textContent = "live";
    });
  }

  muteAllBtn.addEventListener("click", () => {
    players.forEach((p) => (p.video.muted = true));
    refreshAudioBadges();
  });

  async function init() {
    let cameras = [];
    try {
      const r = await fetch("/api/cameras");
      cameras = (await r.json()).cameras || [];
    } catch (e) {
      statusEl.textContent = "Failed to load cameras.";
      return;
    }
    if (!cameras.length) {
      statusEl.textContent = "No live cameras available (is MediaMTX running?).";
      return;
    }
    statusEl.textContent = `${cameras.length} camera${cameras.length > 1 ? "s" : ""}`;
    cameras.forEach(makeTile);
    liveCameras = cameras;
    renderMuteControls();
    refreshMute();
  }

  // ---------- recent-alerts filmstrip ----------
  const alertsStrip = document.getElementById("alerts-strip");
  const alertsTrack = document.getElementById("alerts-track");
  const alertsCount = document.getElementById("alerts-count");
  const muteControls = document.getElementById("mute-controls");
  const batchBar = document.getElementById("batch-bar");
  const batchCount = document.getElementById("batch-count");
  const ALERT_WINDOW = 12 * 3600; // last 12 hours
  const ALERT_LEAD = 5;           // open recordings this many seconds before the event

  const fmtAlertTime = (epoch) =>
    new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const selected = new Set();   // event ids selected for batch action
  const eventById = {};         // id -> event (for batch actions)
  let orderedEvents = [];       // events in render order (for shift-range select)
  let lastSelIdx = -1;          // anchor index for shift-click range selection
  let liveCameras = [];
  let muted = {};               // camera -> until epoch

  async function loadAlerts() {
    if (!alertsTrack) return;
    const now = Math.floor(Date.now() / 1000);
    let events = [];
    try {
      events = (await (await fetch(`/api/events?after=${now - ALERT_WINDOW}&before=${now}`)).json()).events || [];
    } catch (e) { return; }
    events.sort((a, b) => a.start - b.start); // chronological: newest on the right
    orderedEvents = events;
    for (const ev of events) eventById[ev.id] = ev;
    // Drop selections/cache for alerts that are gone now.
    const present = new Set(events.map((e) => e.id));
    for (const id of [...selected]) if (!present.has(id)) selected.delete(id);
    for (const k of Object.keys(eventById)) if (!present.has(k)) delete eventById[k];
    paintAlerts();
  }

  // Re-render the strip from `orderedEvents` (no refetch) — used after selection
  // changes so shift-range select reflects immediately.
  function paintAlerts() {
    // Keep the newest (rightmost) in view, else preserve the scroll position.
    const prevScroll = alertsTrack.scrollLeft;
    const keepRight = alertsTrack.scrollWidth <= alertsTrack.clientWidth + 1 ||
      prevScroll + alertsTrack.clientWidth >= alertsTrack.scrollWidth - 64;
    alertsTrack.innerHTML = "";
    alertsStrip.hidden = false;   // always show the dock (placeholder when empty)
    if (!orderedEvents.length) {
      const empty = document.createElement("div");
      empty.className = "alerts-empty";
      empty.textContent = "No alerts in the last 12 hours.";
      alertsTrack.appendChild(empty);
      alertsCount.textContent = "";
      updateBatchBar();
      return;
    }
    // One combined row; each thumb is tagged by camera (colored left border +
    // a camera label below it) rather than split into per-camera rows.
    orderedEvents.forEach((ev, idx) => alertsTrack.appendChild(buildThumb(ev, idx)));
    alertsTrack.scrollLeft = keepRight ? alertsTrack.scrollWidth : prevScroll;
    alertsCount.textContent = `${orderedEvents.length}`;
    updateBatchBar();
  }

  function buildThumb(ev, idx) {
    // Click → recordings page, jumped to 5s BEFORE the event that triggered it.
    const a = document.createElement("a");
    a.className = "alert-thumb cam-" + ev.camera + (ev.mark === "flagged" ? " flagged" : "")
      + (selected.has(ev.id) ? " selected" : "");
    a.href = `/recordings?t=${Math.round(ev.start - ALERT_LEAD)}`;
    a.title = `${ev.camera} · ${ev.label} · ${fmtAlertTime(ev.start)} — open in recordings`;
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = ev.label;
    img.src = `/api/events/${encodeURIComponent(ev.id)}/snapshot.jpg`;
    img.addEventListener("error", () => a.remove()); // drop ones without a frame
    a.appendChild(img);

    // Hover → play (up to 15s of) the alert's clip over the snapshot.
    if (ev.has_clip) {
      let vid = null;
      a.addEventListener("mouseenter", () => {
        if (vid) return;
        vid = document.createElement("video");
        vid.className = "alert-clip";
        vid.muted = true; vid.playsInline = true; vid.loop = true; vid.preload = "auto";
        vid.src = `/api/events/${encodeURIComponent(ev.id)}/clip.mp4`;
        vid.addEventListener("timeupdate", () => { if (vid && vid.currentTime >= 15) vid.currentTime = 0; });
        vid.addEventListener("error", () => { if (vid) { vid.remove(); vid = null; } });
        a.appendChild(vid);
        vid.play().catch(() => {});
      });
      a.addEventListener("mouseleave", () => {
        if (!vid) return;
        vid.pause(); vid.removeAttribute("src"); vid.load(); vid.remove(); vid = null;
      });
    }

    if (!ev.mark) {
      // Select checkbox (top-left) for batch actions.
      const sel = document.createElement("button");
      sel.type = "button";
      sel.className = "alert-select" + (selected.has(ev.id) ? " on" : "");
      sel.title = "Select for batch benign/flag (shift-click to select a range)";
      sel.textContent = selected.has(ev.id) ? "✓" : "";
      sel.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        if (e.shiftKey && lastSelIdx >= 0 && lastSelIdx < orderedEvents.length) {
          // Shift-click: select every (unmarked) alert between the anchor and here.
          const lo = Math.min(lastSelIdx, idx), hi = Math.max(lastSelIdx, idx);
          for (let i = lo; i <= hi; i++) {
            const e2 = orderedEvents[i];
            if (e2 && !e2.mark) selected.add(e2.id);
          }
          paintAlerts();   // re-render to show the whole range selected
        } else {
          if (selected.has(ev.id)) selected.delete(ev.id);
          else selected.add(ev.id);
          lastSelIdx = idx;   // anchor for a future shift-click
          const on = selected.has(ev.id);
          sel.classList.toggle("on", on);
          sel.textContent = on ? "✓" : "";
          a.classList.toggle("selected", on);
          updateBatchBar();
        }
      });
      a.appendChild(sel);
      // Unreviewed: offer benign / flag actions.
      const actions = document.createElement("div");
      actions.className = "alert-actions";
      const b = document.createElement("button");
      b.type = "button"; b.className = "alert-act benign"; b.textContent = "✓";
      b.title = "Benign — dismiss this alert";
      b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); markAlert(ev, "benign"); });
      const f = document.createElement("button");
      f.type = "button"; f.className = "alert-act flag"; f.textContent = "🚩";
      f.title = "Flag — save a clip (30s before → 5 min after)";
      f.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); markAlert(ev, "flagged"); });
      actions.appendChild(b); actions.appendChild(f);
      a.appendChild(actions);
    } else if (ev.mark === "flagged") {
      a.appendChild(flagBadge(ev.clip_status, ev.clip));
    }

    const cap = document.createElement("div");
    cap.className = "alert-cap";
    cap.textContent = `${ev.label === "person" ? "🚶" : "🚗"} ${fmtAlertTime(ev.start)}`
      + (ev.count > 1 ? ` ·×${ev.count}` : "");
    const camtag = document.createElement("div");
    camtag.className = "alert-camtag cam-" + ev.camera;
    camtag.textContent = ev.camera;
    a.appendChild(cap);
    a.appendChild(camtag);
    return a;
  }

  function flagBadge(clipStatus, clip) {
    const done = clipStatus === "done" && clip;
    const el = document.createElement(done ? "button" : "div");
    el.className = "alert-flagbadge";
    if (done) {
      el.type = "button";
      el.textContent = "🚩 view clip";
      el.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        window.location = `/alerts?a=${encodeURIComponent(clip)}`;   // open in Saved Alerts
      });
    } else {
      el.textContent = clipStatus === "failed" ? "🚩 clip failed" : "🚩 saving clip…";
    }
    return el;
  }

  function alertMarkBody(ev, status) {
    return JSON.stringify({ status, id: ev.id, camera: ev.camera, start: ev.start, ids: ev.ids || [ev.id] });
  }
  async function markAlert(ev, status) {
    try {
      await fetch("/api/alert/mark", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: alertMarkBody(ev, status),
      });
    } catch (err) { return; }
    if (status === "benign") {
      orderedEvents = orderedEvents.filter((x) => x.id !== ev.id);
      selected.delete(ev.id);
      delete eventById[ev.id];
      lastSelIdx = -1;
    } else {
      ev.mark = "flagged"; ev.clip_status = "pending";   // mutates the object in orderedEvents
    }
    paintAlerts();
  }

  // ---------- batch select ----------
  function updateBatchBar() {
    if (!batchBar) return;
    batchBar.hidden = selected.size === 0;
    if (batchCount) batchCount.textContent = `${selected.size} selected`;
  }
  async function batchMark(status) {
    const groups = [...selected].map((id) => eventById[id]).filter(Boolean);
    if (!groups.length) return;
    try {
      await Promise.all(groups.map((g) => fetch("/api/alert/mark", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: alertMarkBody(g, status),
      })));
    } catch (e) { /* ignore; reload reflects server state */ }
    selected.clear();
    updateBatchBar();
    loadAlerts();
  }
  if (batchBar) {
    document.getElementById("batch-benign").addEventListener("click", () => batchMark("benign"));
    document.getElementById("batch-flag").addEventListener("click", () => batchMark("flagged"));
    document.getElementById("batch-clear").addEventListener("click", () => {
      selected.clear(); updateBatchBar(); loadAlerts();
    });
  }

  // ---------- mute / snooze per camera ----------
  function renderMuteControls() {
    if (!muteControls) return;
    muteControls.innerHTML = "";
    for (const cam of liveCameras) {
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "btn mute-btn"; btn.dataset.cam = cam.name;
      btn.addEventListener("click", () => toggleMute(cam.name));
      muteControls.appendChild(btn);
    }
    paintMute();
  }
  function paintMute() {
    if (!muteControls) return;
    const now = Date.now() / 1000;
    for (const btn of muteControls.children) {
      const until = muted[btn.dataset.cam];
      if (until && until > now) {
        btn.textContent = `🔔 ${btn.dataset.cam} alerts silenced ${Math.max(1, Math.round((until - now) / 60))}m · resume`;
        btn.classList.add("on");
      } else {
        btn.textContent = `🔕 Silence ${btn.dataset.cam} alerts`;
        btn.classList.remove("on");
      }
    }
  }
  async function toggleMute(cam) {
    const now = Date.now() / 1000;
    const on = !(muted[cam] && muted[cam] > now);
    try {
      const r = await fetch("/api/mute", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ camera: cam, on }),
      });
      muted = (await r.json()).muted || {};
    } catch (e) { return; }
    paintMute();
    loadAlerts(); // reflect suppression right away
  }
  async function refreshMute() {
    try { muted = (await (await fetch("/api/mute")).json()).muted || {}; } catch (e) {}
    paintMute();
  }

  init();
  loadAlerts();
  setInterval(loadAlerts, 60000); // refresh as new alerts come in
  setInterval(() => { refreshMute(); }, 60000); // keep mute state + countdown fresh
})();
