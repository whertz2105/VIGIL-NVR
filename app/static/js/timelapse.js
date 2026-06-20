// Timelapse browser: pick a date + camera, play the nightly clip.
(function () {
  const dateSel = document.getElementById("tl-date");
  const camsEl = document.getElementById("tl-cams");
  const statusEl = document.getElementById("tl-status");
  const video = document.getElementById("tl-video");
  const emptyEl = document.getElementById("tl-empty");
  const dlBtn = document.getElementById("tl-download");

  let days = [];
  let current = { dir: null, camera: null };

  const isoToDir = (iso) => iso.slice(2).replace(/-/g, "_"); // 2026-06-17 -> 26_06_17

  function fileUrl(dir, camera, download) {
    return `/api/timelapse/file/${dir}/${encodeURIComponent(camera)}` + (download ? "?download=1" : "");
  }

  function load(dir, camera) {
    current = { dir, camera };
    video.src = fileUrl(dir, camera);
    video.load();
    dlBtn.hidden = false;
    dlBtn.href = fileUrl(dir, camera, true);
    for (const b of camsEl.children) b.classList.toggle("on", b.dataset.cam === camera);
  }

  function buildCams(day) {
    camsEl.innerHTML = "";
    for (const cam of day.cameras) {
      const b = document.createElement("button");
      b.className = "btn cam-btn"; b.type = "button"; b.textContent = cam; b.dataset.cam = cam;
      b.addEventListener("click", () => load(day.dir, cam));
      camsEl.appendChild(b);
    }
  }

  function selectDay(iso) {
    const day = days.find((d) => d.date === iso);
    if (!day) return;
    buildCams(day);
    // Default to the first camera for the day.
    const cam = day.cameras[0];
    load(day.dir, cam);
    statusEl.textContent = `${day.cameras.length} camera${day.cameras.length > 1 ? "s" : ""}`;
  }

  async function init() {
    try {
      days = (await (await fetch("/api/timelapse/list")).json()).days || [];
    } catch (e) {
      statusEl.textContent = "Failed to load timelapses.";
      return;
    }
    if (!days.length) {
      emptyEl.hidden = false;
      video.hidden = true;
      statusEl.textContent = "";
      return;
    }
    dateSel.innerHTML = "";
    for (const d of days) {
      const opt = document.createElement("option");
      opt.value = d.date; opt.textContent = d.date;
      dateSel.appendChild(opt);
    }
    dateSel.value = days[0].date;
    dateSel.addEventListener("change", () => selectDay(dateSel.value));
    selectDay(days[0].date);
  }

  init();
})();
