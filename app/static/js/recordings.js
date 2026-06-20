// Timeline-first recordings browser with SYNCHRONIZED multi-camera playback
// and a zoomable / pannable timeline.
//
// One global playback position (`viewTime`, epoch seconds) drives every
// selected camera at once. Each camera plays its own segments; one camera is
// the "master" clock and the others are drift-corrected to it. The timeline
// can be zoomed (wheel / buttons) and panned (drag) for fine seeking, and the
// white handle scrubs.
(function () {
  const dateSelect = document.getElementById("date-select");
  const camFilter = document.getElementById("cam-filter");
  const statusEl = document.getElementById("rec-status");
  const grid = document.getElementById("player-grid");
  const lanesEl = document.getElementById("timeline-lanes");
  const axisEl = document.getElementById("timeline-axis");
  const timelineEl = document.getElementById("timeline");
  const playhead = document.getElementById("playhead");
  const tooltip = document.getElementById("time-tooltip");
  const playBtn = document.getElementById("play-btn");
  const timeReadout = document.getElementById("time-readout");
  const tileTpl = document.getElementById("rec-tile-tpl");
  const eventsRow = document.getElementById("events-row");
  const eventsLegend = document.getElementById("events-legend");
  const eventPreview = document.getElementById("event-preview");
  const alertReview = document.getElementById("alert-review");
  const arSnap = document.getElementById("ar-snap");
  const arInfo = document.getElementById("ar-info");
  const arPos = document.getElementById("ar-pos");
  const arActions = document.getElementById("ar-actions");
  const arMark = document.getElementById("ar-mark");
  const ALERT_LEAD = 5;       // focus an alert this many seconds before it triggered

  const MIN_SPAN = 20;        // closest zoom: 20 seconds across the timeline
  const DRIFT_TOL = 0.4;      // seconds before a non-master camera is re-synced

  let data = null;            // timeline payload
  let selected = new Set();   // cameras currently active (grid + sync)
  let tiles = {};             // cam -> { wrap, video, badge, nameEl, noFootage, curIndex }
  let view = { start: 0, end: 0 };   // visible epoch window
  let viewTime = 0;           // global playback position (epoch)
  let playing = false;
  let master = null;          // camera driving the clock
  let audioCam = null;        // camera whose audio is unmuted
  let events = [];            // Frigate person/car detections for the day
  let alertIdx = -1;          // index into `events` of the currently-reviewed alert
  // Deep link from the live page's alert filmstrip: ?t=<epoch> → open that day
  // and seek the player to that moment. Consumed once on first matching day.
  let deepLinkT = parseFloat(new URLSearchParams(location.search).get("t"));
  if (!isFinite(deepLinkT)) deepLinkT = null;

  // ---------- time / mapping helpers ----------
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const span = () => view.end - view.start;
  const fracOf = (epoch) => (epoch - view.start) / span();
  const pctOf = (epoch) => (clamp(fracOf(epoch), 0, 1) * 100).toFixed(4) + "%";

  function epochAtClientX(clientX) {
    const rect = timelineEl.getBoundingClientRect();
    const f = clamp((clientX - rect.left) / rect.width, 0, 1);
    return view.start + f * span();
  }
  function fmtClock(epoch, withSec) {
    const o = { hour: "2-digit", minute: "2-digit" };
    if (withSec) o.second = "2-digit";
    return new Date(epoch * 1000).toLocaleTimeString([], o);
  }
  function fmtSize(b) { return (b / 1048576).toFixed(0) + " MB"; }

  function clipIndexAt(cam, t) {
    const cl = (data.clips[cam] || []);
    for (let i = 0; i < cl.length; i++) {
      if (t >= cl[i].start && t < cl[i].start + cl[i].duration) return i;
    }
    return -1;
  }
  // The most recent moment that has footage. We open the day here rather than at
  // the first clip: midnight footage is dark and cameras that only start later
  // would show "no footage", which reads as broken. The latest segment is what
  // an NVR user actually wants to see.
  function latestClipTime() {
    let best = null;
    for (const cam of data.cameras) {
      const cl = data.clips[cam] || [];
      if (!cl.length) continue;
      const last = cl[cl.length - 1];
      if (!best || last.start + last.duration > best.start + best.duration) best = last;
    }
    if (!best) return data.day_start;
    // Start at the BEGINNING of the most recent complete segment so autoplay has
    // a full ~15 min of runway. Landing near a segment's end gave only a few
    // seconds before hitting the (excluded) in-progress segment, and a paused,
    // freshly-seeked video paints no frame — which looked like "no footage".
    return best.start;
  }

  // ---------- loading data ----------
  async function loadDays() {
    const days = (await (await fetch("/api/recordings/days")).json()).days || [];
    dateSelect.innerHTML = "";
    if (!days.length) { statusEl.textContent = "No recordings found."; return; }
    for (const d of days) {
      const opt = document.createElement("option");
      opt.value = d.date;
      opt.textContent = d.has_footage === false ? `${d.date} (empty)` : d.date;
      dateSelect.appendChild(opt);
    }
    // A deep link (?t=) wins: resolve the epoch to its local day and open that.
    let def = null;
    if (deepLinkT != null) {
      try {
        const loc = await (await fetch("/api/recordings/locate?t=" + deepLinkT)).json();
        def = days.find((d) => d.date === loc.date);
      } catch (e) { /* fall through to default */ }
    }
    // Otherwise default to the newest day that actually has footage. The newest
    // folder is often an empty post-midnight stub (full hour skeleton, no
    // segments yet), which is why we skip it rather than land on a blank timeline.
    if (!def) def = days.find((d) => d.has_footage !== false) || days[0];
    dateSelect.value = def.date;
    await loadTimeline(def.date);
  }

  async function loadTimeline(date) {
    pause();
    statusEl.textContent = "Loading…";
    data = await (await fetch("/api/recordings/timeline?date=" + encodeURIComponent(date))).json();
    selected = new Set(data.cameras);   // all cameras on by default
    audioCam = null;
    master = null;
    viewTime = latestClipTime();
    // If we arrived via a ?t= deep link and it falls on this day, seek there
    // instead of the latest footage. Consume it so later day changes are normal.
    if (deepLinkT != null && deepLinkT >= data.day_start && deepLinkT < data.day_end) {
      viewTime = deepLinkT;
      deepLinkT = null;
    }
    view = defaultView(viewTime);   // whole day on desktop; 4h window on mobile
    buildCamFilter();
    buildGrid();
    renderTimeline();
    setViewTime(viewTime, true);
    const total = data.cameras.reduce((n, c) => n + (data.clips[c] || []).length, 0);
    statusEl.textContent = total ? `${data.cameras.length} cameras · ${total} segments` : "No segments this day.";
    if (total) play();   // autoplay the most recent footage (muted → allowed)
    loadEvents();        // person/car markers for this day (async, non-blocking)
  }

  // ---------- Frigate detection events ----------
  async function loadEvents() {
    events = [];
    alertIdx = -1;
    renderEvents();
    updateReview();
    if (!data) return;
    try {
      const r = await fetch(`/api/events?after=${Math.floor(data.day_start)}&before=${Math.ceil(data.day_end)}`);
      events = (await r.json()).events || [];
    } catch (e) { events = []; }
    renderEvents();
  }

  function renderEvents() {
    const has = !!data && events.length > 0;
    eventsRow.hidden = !has;
    eventsLegend.hidden = !has;
    eventsRow.innerHTML = "";
    if (!has) return;
    // Legend built from the cameras actually present (config-driven colour + label).
    const legendCams = [...new Set(events.map((e) => e.camera))].filter(Boolean).sort();
    eventsLegend.innerHTML = "· " + legendCams.map((c) =>
      `<span class="ev-key cam-${c}"></span> ${window.camLabel ? window.camLabel(c) : c}`
    ).join(" · ") + " (click to jump)";
    // Single lane; each marker is colored by camera (see legend) instead of
    // splitting cameras into separate rows.
    const lane = document.createElement("div");
    lane.className = "events-lane-row";
    const label = document.createElement("div");
    label.className = "events-label"; label.textContent = "alerts";
    const track = document.createElement("div");
    track.className = "events-lane";
    events.forEach((ev, idx) => {
      const f = fracOf(ev.start);
      if (f < 0 || f > 1) return;
      const m = document.createElement("button");
      m.type = "button";
      m.className = "ev-marker cam-" + ev.camera + (idx === alertIdx ? " current" : "");
      m.style.left = (f * 100).toFixed(4) + "%";
      m.title = `${ev.camera} · ${ev.label} · ${fmtClock(ev.start, true)}` + (ev.score ? ` · ${Math.round(ev.score * 100)}%` : "");
      // Handle on pointerdown and stop it bubbling to the timeline, otherwise the
      // timeline's own press/drag handler captures the pointer and seeks to the
      // click pixel. Focus the alert: seek to 5s before it triggered + review it.
      m.addEventListener("pointerdown", (e) => {
        e.stopPropagation();
        e.preventDefault();
        focusAlert(idx);
      });
      if (ev.has_snapshot) {
        m.addEventListener("mouseenter", () => showEventPreview(ev, m));
        m.addEventListener("mouseleave", hideEventPreview);
      }
      track.appendChild(m);
    });
    lane.appendChild(label);
    lane.appendChild(track);
    eventsRow.appendChild(lane);
  }

  function showEventPreview(ev, marker) {
    eventPreview.innerHTML =
      `<img src="/api/events/${encodeURIComponent(ev.id)}/snapshot.jpg" alt="">` +
      `<span>${ev.label} · ${fmtClock(ev.start, true)}</span>`;
    eventPreview.hidden = false;
    const mr = marker.getBoundingClientRect();
    const wr = timelineEl.getBoundingClientRect();
    eventPreview.style.left = clamp(mr.left - wr.left - 80, 0, wr.width - 168) + "px";
  }
  function hideEventPreview() { eventPreview.hidden = true; }

  // ---------- alert review: focus / prev-next / mark ----------
  function focusAlert(idx) {
    if (idx < 0 || idx >= events.length) return;
    alertIdx = idx;
    setViewTime(events[idx].start - ALERT_LEAD, true);   // 5s before it triggered
    // Keep the focused alert visible (the mobile window is only 4h wide).
    if (viewTime < view.start || viewTime > view.end) {
      const sp = span();
      view.start = viewTime - sp * 0.5;
      view.end = view.start + sp;
      clampView();
    }
    if (!playing) play();
    renderTimeline();   // re-draw axis/lanes + markers (highlights the current one)
    updateReview();
  }
  // dir>0 next alert, dir<0 previous. If an alert is already focused, step by
  // index (the playhead sits ALERT_LEAD before it, so a viewTime comparison would
  // just re-find the current one); otherwise pick relative to the playhead.
  function gotoAlertRelative(dir) {
    if (!events.length) return;
    let idx;
    if (alertIdx >= 0 && alertIdx < events.length) {
      idx = alertIdx + dir;
    } else if (dir > 0) {
      idx = events.findIndex((e) => e.start > viewTime + 0.5);
    } else {
      idx = -1;
      for (let i = events.length - 1; i >= 0; i--) { if (events[i].start < viewTime - 0.5) { idx = i; break; } }
    }
    if (idx >= 0 && idx < events.length) focusAlert(idx);
  }
  function updateReview() {
    if (!alertReview) return;
    if (alertIdx < 0 || alertIdx >= events.length) { alertReview.hidden = true; return; }
    const ev = events[alertIdx];
    alertReview.hidden = false;
    if (ev.has_snapshot) {
      arSnap.src = `/api/events/${encodeURIComponent(ev.id)}/snapshot.jpg`;
      arSnap.style.display = "";
    } else { arSnap.removeAttribute("src"); arSnap.style.display = "none"; }
    arInfo.textContent = `${ev.camera} · ${ev.label} · ${fmtClock(ev.start, true)}`
      + (ev.count > 1 ? ` · ${ev.count} detections` : "");
    arPos.textContent = `alert ${alertIdx + 1} / ${events.length}`;
    if (ev.mark === "flagged") {
      arActions.hidden = true;
      arMark.hidden = false;
      arMark.innerHTML = "";
      if (ev.clip_status === "done" && ev.clip) {
        const a = document.createElement("a");
        a.className = "btn";
        a.href = "/alerts?a=" + encodeURIComponent(ev.clip);
        a.textContent = "🚩 flagged · view clips";
        arMark.appendChild(a);
      } else {
        arMark.textContent = ev.clip_status === "failed" ? "🚩 flagged · clip failed" : "🚩 flagged · saving clip…";
      }
    } else {
      arActions.hidden = false;
      arMark.hidden = true;
    }
  }
  async function markCurrent(status) {
    if (alertIdx < 0 || alertIdx >= events.length) return;
    const ev = events[alertIdx];
    try {
      await fetch("/api/alert/mark", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status, id: ev.id, camera: ev.camera, start: ev.start, ids: ev.ids || [ev.id] }),
      });
    } catch (e) { return; }
    if (status === "benign") {
      events.splice(alertIdx, 1);          // remove it
      if (!events.length) { alertIdx = -1; renderEvents(); updateReview(); return; }
      alertIdx = Math.min(alertIdx, events.length - 1);
      focusAlert(alertIdx);                // advance to the next alert
    } else {
      ev.mark = "flagged"; ev.clip_status = "pending";
      renderEvents();
      updateReview();
    }
  }

  // ---------- camera filter ----------
  function buildCamFilter() {
    camFilter.innerHTML = "";
    for (const cam of data.cameras) {
      const b = document.createElement("button");
      b.className = "btn on cam-btn"; b.type = "button"; b.textContent = cam;
      b.addEventListener("click", () => {
        if (selected.has(cam)) {
          if (selected.size === 1) return; // keep at least one
          selected.delete(cam); b.classList.remove("on");
        } else { selected.add(cam); b.classList.add("on"); }
        buildGrid(); renderTimeline(); setViewTime(viewTime, true);
      });
      camFilter.appendChild(b);
    }
  }

  // ---------- player grid ----------
  function buildGrid() {
    grid.innerHTML = ""; tiles = {};
    for (const cam of data.cameras) {
      if (!selected.has(cam)) continue;
      const node = tileTpl.content.firstElementChild.cloneNode(true);
      const video = node.querySelector("video");
      const nameEl = node.querySelector(".tile-name");
      const badge = node.querySelector(".tile-badge");
      const noFootage = node.querySelector(".no-footage");
      nameEl.textContent = cam;
      video.muted = true;

      const t = { wrap: node, video, badge, nameEl, noFootage, curIndex: -1 };
      tiles[cam] = t;

      // Click a tile to make it the audio source (others muted).
      node.addEventListener("click", () => {
        if (audioCam === cam) { audioCam = null; video.muted = true; }
        else { audioCam = cam; }
        for (const [c, tt] of Object.entries(tiles)) tt.video.muted = (c !== audioCam);
        updateBadges();
      });

      video.addEventListener("timeupdate", () => { if (cam === master) onMasterTick(); });
      video.addEventListener("ended", () => { if (cam === master) onMasterEnded(); });

      grid.appendChild(node);
    }
    updateBadges();
  }

  function setTileFootage(cam, has) {
    const t = tiles[cam]; if (!t) return;
    t.noFootage.hidden = has;
    t.video.style.visibility = has ? "visible" : "hidden";
  }
  function updateBadges() {
    for (const [cam, t] of Object.entries(tiles)) {
      t.badge.textContent = audioCam === cam ? "🔊 audio" : "muted";
      t.badge.classList.toggle("audio", audioCam === cam);
    }
  }

  // ---------- clip loading / seeking ----------
  function seekTile(cam, offset) {
    const t = tiles[cam]; if (!t) return;
    try { t.video.currentTime = Math.max(0, offset); } catch (e) {}
  }

  function loadClip(cam, index, offset, autoplay) {
    const t = tiles[cam]; if (!t) return;
    const clip = (data.clips[cam] || [])[index];
    if (!clip) return;
    t.curIndex = index;
    setTileFootage(cam, true);
    if (t.video.dataset.url !== clip.url) {
      t.video.dataset.url = clip.url;
      t.video.onloadedmetadata = () => {
        seekTile(cam, offset);
        if (autoplay && playing) t.video.play().catch(() => {});
      };
      t.video.src = clip.url;
    } else {
      seekTile(cam, offset);
      if (autoplay && playing) t.video.play().catch(() => {});
    }
  }

  // Ensure a single camera shows the right content for time t.
  function syncCam(cam, t, forceSeek) {
    const tile = tiles[cam]; if (!tile) return;
    const idx = clipIndexAt(cam, t);
    if (idx < 0) {
      if (tile.curIndex !== -1) { tile.video.pause(); tile.curIndex = -1; }
      setTileFootage(cam, false);
      return;
    }
    const clip = data.clips[cam][idx];
    const expected = t - clip.start;
    if (tile.curIndex !== idx) {
      loadClip(cam, idx, expected, true);
    } else {
      if (forceSeek || Math.abs(tile.video.currentTime - expected) > DRIFT_TOL) seekTile(cam, expected);
      if (playing && tile.video.paused) tile.video.play().catch(() => {});
    }
  }

  function pickMaster(t) {
    if (master && selected.has(master) && clipIndexAt(master, t) >= 0) return master;
    for (const cam of data.cameras) {
      if (selected.has(cam) && clipIndexAt(cam, t) >= 0) return cam;
    }
    return null;
  }

  // Set the global position and align every selected camera to it.
  function setViewTime(t, forceSeek) {
    viewTime = clamp(t, data.day_start, data.day_end);
    for (const cam of selected) syncCam(cam, viewTime, forceSeek);
    master = pickMaster(viewTime);
    updatePlayhead();
    updateClock();
  }

  // ---------- playback ----------
  function onMasterTick() {
    const t = tiles[master]; if (!t || t.curIndex < 0) return;
    const clip = data.clips[master][t.curIndex];
    viewTime = clip.start + t.video.currentTime;
    for (const cam of selected) if (cam !== master) syncCam(cam, viewTime, false);
    autoPan();
    updatePlayhead();
    updateClock();
  }

  function onMasterEnded() {
    const next = tiles[master].curIndex + 1;
    const cl = data.clips[master] || [];
    if (next < cl.length) {
      // jump to the next segment of the same camera (skips dead air)
      viewTime = cl[next].start;
      loadClip(master, next, 0, true);
      for (const cam of selected) if (cam !== master) syncCam(cam, viewTime, true);
    } else {
      // master is out of footage — hand off to any camera with footage ahead
      let bestCam = null, bestStart = Infinity;
      for (const cam of selected) {
        for (const c of (data.clips[cam] || [])) {
          if (c.start >= viewTime && c.start < bestStart) { bestStart = c.start; bestCam = cam; }
        }
      }
      if (bestCam) { master = bestCam; setViewTime(bestStart, true); }
      else pause();
    }
  }

  function play() {
    if (!data) return;
    master = pickMaster(viewTime);
    if (!master) return; // nothing to play at this position
    playing = true;
    playBtn.textContent = "❚❚ Pause";
    for (const cam of selected) syncCam(cam, viewTime, true);
    for (const cam of selected) { const t = tiles[cam]; if (t && t.curIndex >= 0) t.video.play().catch(() => {}); }
  }
  function pause() {
    playing = false;
    if (playBtn) playBtn.textContent = "▶ Play";
    for (const t of Object.values(tiles)) t.video.pause();
  }
  function togglePlay() { playing ? pause() : play(); }

  // ---------- timeline rendering ----------
  const isMobile = () => window.matchMedia("(max-width: 640px)").matches;
  // Default visible window: the whole day on desktop, a 4h window around `vt`
  // (mostly its lead-up) on phones, where a full day is unreadably dense.
  function defaultView(vt) {
    if (!isMobile()) return { start: data.day_start, end: data.day_end };
    const sp = 4 * 3600;
    let start = vt - sp * 0.7, end = start + sp;
    if (end > data.day_end) { end = data.day_end; start = end - sp; }
    if (start < data.day_start) { start = data.day_start; end = Math.min(start + sp, data.day_end); }
    return { start, end };
  }
  function tickInterval(sec) {
    const opts = [15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];
    for (const o of opts) if (sec / o <= 14) return o;   // aim for <=14 ticks
    return 10800;
  }
  // [markInterval, labelInterval] — on phones, sparser hour-aligned labels with
  // intermediate (e.g. 30-min) tick marks so the axis isn't a wall of text.
  function tickPlan(sp) {
    if (!isMobile()) { const i = tickInterval(sp); return [i, i]; }
    if (sp <= 2 * 3600) return [900, 1800];     // ≤2h: 15-min marks, 30-min labels
    if (sp <= 6 * 3600) return [1800, 3600];    // ~4h default: 30-min marks, hourly labels
    if (sp <= 14 * 3600) return [3600, 7200];   // hourly marks, 2-hour labels
    return [7200, 14400];
  }
  function renderTimeline() {
    if (!data) return;
    const sp = span();
    const [markInterval, labelInterval] = tickPlan(sp);
    const withSec = labelInterval < 60;
    axisEl.innerHTML = "";
    const first = Math.ceil(view.start / markInterval) * markInterval;
    for (let t = first; t <= view.end; t += markInterval) {
      const el = document.createElement("span");
      const labeled = Math.round(t) % labelInterval === 0;
      el.className = "tick" + (labeled ? "" : " minor");
      el.style.left = pctOf(t);
      if (labeled) el.textContent = fmtClock(t, withSec);
      axisEl.appendChild(el);
    }

    lanesEl.innerHTML = "";
    for (const cam of data.cameras) {
      const lane = document.createElement("div");
      lane.className = "lane" + (selected.has(cam) ? "" : " dim");
      const label = document.createElement("div");
      label.className = "lane-label"; label.textContent = cam;
      lane.appendChild(label);
      const track = document.createElement("div");
      track.className = "lane-track";
      for (const clip of (data.clips[cam] || [])) {
        const end = clip.start + clip.duration;
        if (end <= view.start || clip.start >= view.end) continue; // off-screen
        const l = clamp(fracOf(clip.start), 0, 1);
        const r = clamp(fracOf(end), 0, 1);
        const el = document.createElement("div");
        el.className = "clip";
        el.style.left = (l * 100).toFixed(4) + "%";
        el.style.width = Math.max(0.2, (r - l) * 100).toFixed(4) + "%";
        el.title = `${cam} ${fmtClock(clip.start, true)} · ${Math.round(clip.duration / 60)}m · ${fmtSize(clip.size)}`;
        track.appendChild(el);
      }
      lane.appendChild(track);
      lanesEl.appendChild(lane);
    }
    renderEvents();
    updatePlayhead();
  }

  function updatePlayhead() {
    if (!data) return;
    const f = fracOf(viewTime);
    if (f < 0 || f > 1) { playhead.hidden = true; return; }
    playhead.hidden = false;
    playhead.style.left = (f * 100).toFixed(4) + "%";
  }
  function updateClock() {
    if (!data) return;
    timeReadout.textContent = fmtClock(viewTime, true);
  }

  // ---------- zoom / pan ----------
  function clampView() {
    const full = data.day_end - data.day_start;
    let sp = clamp(view.end - view.start, MIN_SPAN, full);
    if (view.start < data.day_start) view.start = data.day_start;
    view.end = view.start + sp;
    if (view.end > data.day_end) { view.end = data.day_end; view.start = view.end - sp; }
    if (view.start < data.day_start) view.start = data.day_start;
  }
  function zoomAround(centerEpoch, factor) {
    const sp = span();
    const nsp = clamp(sp * factor, MIN_SPAN, data.day_end - data.day_start);
    const ratio = (centerEpoch - view.start) / sp;
    view.start = centerEpoch - ratio * nsp;
    view.end = view.start + nsp;
    clampView();
    renderTimeline();
  }

  timelineEl.addEventListener("wheel", (e) => {
    if (!data) return;
    e.preventDefault();
    zoomAround(epochAtClientX(e.clientX), e.deltaY < 0 ? 0.8 : 1.25);
  }, { passive: false });

  document.getElementById("zoom-in").addEventListener("click", () => zoomAround((view.start + view.end) / 2, 0.6));
  document.getElementById("zoom-out").addEventListener("click", () => zoomAround((view.start + view.end) / 2, 1.7));
  document.getElementById("zoom-fit").addEventListener("click", () => {
    view = { start: data.day_start, end: data.day_end }; renderTimeline();
  });

  function autoPan() {
    if (viewTime > view.end || viewTime < view.start) {
      const sp = span();
      view.start = viewTime - sp * 0.15;
      view.end = view.start + sp;
      clampView();
      renderTimeline();
    }
  }

  // ---------- pointer interaction (seek / pan / scrub) ----------
  let drag = null;
  const handle = playhead.querySelector(".playhead-handle");

  handle.addEventListener("pointerdown", (e) => {
    e.stopPropagation(); e.preventDefault();
    drag = { mode: "scrub" };
    handle.setPointerCapture(e.pointerId);
  });

  timelineEl.addEventListener("pointerdown", (e) => {
    if (drag) return;
    drag = { mode: "press", startX: e.clientX, startViewStart: view.start, moved: false };
    timelineEl.setPointerCapture(e.pointerId);
  });

  timelineEl.addEventListener("pointermove", (e) => {
    if (!data) {
      return;
    }
    if (!drag) { showTooltip(e.clientX); return; }
    if (drag.mode === "scrub") {
      setViewTime(epochAtClientX(e.clientX), true);
      return;
    }
    const dx = e.clientX - drag.startX;
    if (Math.abs(dx) > 4) drag.moved = true;
    if (drag.moved) {
      const epochPerPx = span() / timelineEl.getBoundingClientRect().width;
      view.start = drag.startViewStart - dx * epochPerPx;
      view.end = view.start + span();
      clampView();
      renderTimeline();
    }
  });

  function endDrag(e) {
    if (!drag) return;
    if (drag.mode === "press" && !drag.moved) setViewTime(epochAtClientX(e.clientX), true);
    drag = null;
  }
  timelineEl.addEventListener("pointerup", endDrag);
  handle.addEventListener("pointerup", () => { drag = null; });
  timelineEl.addEventListener("pointerleave", () => { tooltip.hidden = true; });

  function showTooltip(clientX) {
    const rect = timelineEl.getBoundingClientRect();
    tooltip.hidden = false;
    tooltip.style.left = clamp(clientX - rect.left, 0, rect.width) + "px";
    tooltip.textContent = fmtClock(epochAtClientX(clientX), true);
  }

  // ---------- transport buttons / keys ----------
  playBtn.addEventListener("click", togglePlay);
  document.getElementById("back-btn").addEventListener("click", () => setViewTime(viewTime - 10, true));
  document.getElementById("fwd-btn").addEventListener("click", () => setViewTime(viewTime + 10, true));
  document.getElementById("prev-alert").addEventListener("click", () => gotoAlertRelative(-1));
  document.getElementById("next-alert").addEventListener("click", () => gotoAlertRelative(1));
  document.getElementById("ar-benign").addEventListener("click", () => markCurrent("benign"));
  document.getElementById("ar-flag").addEventListener("click", () => markCurrent("flagged"));
  dateSelect.addEventListener("change", () => loadTimeline(dateSelect.value));

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "SELECT" || e.target.tagName === "INPUT") return;
    if (e.code === "Space") { e.preventDefault(); togglePlay(); }
    else if (e.code === "ArrowLeft") setViewTime(viewTime - (e.shiftKey ? 60 : 5), true);
    else if (e.code === "ArrowRight") setViewTime(viewTime + (e.shiftKey ? 60 : 5), true);
  });

  window.addEventListener("resize", updatePlayhead);

  loadDays();
})();
