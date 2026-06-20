// Saved Alerts: pick a flagged alert; play ALL its cameras in sync. ?a=<id>
// deep-links to a specific alert (used by the "view clip" badges).
(function () {
  const listEl = document.getElementById("sa-list");
  const gridEl = document.getElementById("sa-grid");
  const controls = document.getElementById("sa-controls");
  const playBtn = document.getElementById("sa-play");
  const seek = document.getElementById("sa-seek");
  const timeEl = document.getElementById("sa-time");
  const emptyEl = document.getElementById("sa-empty");
  const delBtn = document.getElementById("sa-delete");
  const statusEl = document.getElementById("sa-status");

  let alerts = [];
  let current = null;
  let videos = [];
  let master = null;
  let scrubbing = false;

  const fmtWhen = (epoch) =>
    new Date(epoch * 1000).toLocaleString([], {
      weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  const fmtT = (s) => {
    s = Math.max(0, Math.floor(s || 0));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  };
  const fileUrl = (name) => `/api/flagged/file/${encodeURIComponent(name)}`;

  function clearGrid() {
    for (const v of videos) { v.pause(); v.removeAttribute("src"); v.load(); }
    videos = []; master = null; gridEl.innerHTML = ""; controls.hidden = true;
  }

  function select(alert) {
    current = alert;
    emptyEl.hidden = true;
    delBtn.hidden = false;
    clearGrid();
    gridEl.style.setProperty("--n", Math.min(alert.cameras.length, 2));
    for (const cam of alert.cameras) {
      const cell = document.createElement("div");
      cell.className = "sa-cell cam-" + cam.camera;
      const v = document.createElement("video");
      v.muted = true; v.playsInline = true; v.preload = "auto";
      v.src = fileUrl(cam.name);
      const lbl = document.createElement("div");
      lbl.className = "sa-cell-label"; lbl.textContent = cam.camera;
      cell.append(v, lbl);
      gridEl.appendChild(cell);
      videos.push(v);
    }
    master = videos[0] || null;
    controls.hidden = !master;
    if (master) {
      master.addEventListener("loadedmetadata", () => { seek.max = master.duration || 0; });
      master.addEventListener("timeupdate", () => {
        if (!scrubbing) {
          seek.value = master.currentTime;
          timeEl.textContent = fmtT(master.currentTime) + " / " + fmtT(master.duration);
        }
        for (const v of videos) {
          if (v !== master && Math.abs(v.currentTime - master.currentTime) > 0.3) {
            try { v.currentTime = master.currentTime; } catch (e) {}
          }
        }
      });
      master.addEventListener("ended", () => { playBtn.textContent = "▶ Play"; });
    }
    playAll();
    for (const el of listEl.children) el.classList.toggle("on", el.dataset.id === alert.id);
  }

  function playAll() { for (const v of videos) v.play().catch(() => {}); playBtn.textContent = "❚❚ Pause"; }
  function pauseAll() { for (const v of videos) v.pause(); playBtn.textContent = "▶ Play"; }
  function togglePlay() { if (master) (master.paused ? playAll() : pauseAll()); }

  playBtn.addEventListener("click", togglePlay);
  seek.addEventListener("input", () => {
    scrubbing = true;
    const t = parseFloat(seek.value);
    for (const v of videos) { try { v.currentTime = t; } catch (e) {} }
    timeEl.textContent = fmtT(t) + " / " + fmtT(master ? master.duration : 0);
  });
  seek.addEventListener("change", () => { scrubbing = false; });

  function renderList() {
    listEl.innerHTML = "";
    for (const a of alerts) {
      const item = document.createElement("button");
      item.type = "button"; item.className = "sa-item"; item.dataset.id = a.id;
      const when = document.createElement("span");
      when.className = "sa-when"; when.textContent = fmtWhen(a.start);
      const cams = document.createElement("span");
      cams.className = "sa-size"; cams.textContent = a.cameras.map((c) => c.camera).join(" · ");
      item.append(when, cams);
      item.addEventListener("click", () => select(a));
      listEl.appendChild(item);
    }
  }

  async function del() {
    if (!current) return;
    if (!confirm(`Delete this saved alert (${fmtWhen(current.start)}) — all cameras?`)) return;
    try {
      await fetch(`/api/flagged/alert/${encodeURIComponent(current.id)}`, { method: "DELETE" });
    } catch (e) { return; }
    alerts = alerts.filter((a) => a.id !== current.id);
    current = null; clearGrid(); delBtn.hidden = true;
    if (alerts.length) {
      renderList(); select(alerts[0]);
      statusEl.textContent = `${alerts.length} alert${alerts.length > 1 ? "s" : ""}`;
    } else {
      renderList(); emptyEl.hidden = false; statusEl.textContent = "";
    }
  }
  delBtn.addEventListener("click", del);

  async function init() {
    try {
      alerts = (await (await fetch("/api/flagged/list")).json()).alerts || [];
    } catch (e) {
      statusEl.textContent = "Failed to load saved alerts.";
      return;
    }
    if (!alerts.length) {
      emptyEl.hidden = false; statusEl.textContent = "";
      return;
    }
    statusEl.textContent = `${alerts.length} alert${alerts.length > 1 ? "s" : ""}`;
    renderList();
    const want = new URLSearchParams(location.search).get("a");
    select(alerts.find((a) => a.id === want) || alerts[0]);
  }
  init();
})();
