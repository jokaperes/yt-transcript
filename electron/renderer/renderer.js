"use strict";

const $ = (id) => document.getElementById(id);
const FORMATS = ["md", "txt", "srt", "vtt", "json"];

// Phase -> [start%, weight%] for the overall bar; mirrors the Tkinter weights.
const PHASE_WEIGHTS = {
  download: [0, 30],
  extract: [25, 10],
  model: [35, 20],
  transcribe: [55, 40],
  write: [95, 5],
};
const PHASE_LABELS = {
  download: "Downloading audio",
  extract: "Extracting audio",
  model: "Loading Whisper model",
  transcribe: "Transcribing",
  write: "Writing files",
};

const state = {
  running: false,
  startedAt: null,
  total: 0,
  overall: 0,
  elapsedTimer: null,
  pendingUpdateVersion: null,
};

// ---------------------------------------------------------------------------
// Log helpers
// ---------------------------------------------------------------------------
function appendLog(message, level = "info") {
  const log = $("log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  const line = document.createElement("div");
  line.className = level;
  line.textContent = message;
  log.appendChild(line);
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function appendSeparator() {
  appendLog("─".repeat(60), "separator");
}

function levelFor(message) {
  const m = message.toLowerCase();
  if (message.includes("ERROR:") || m.includes("failed") || m.includes("error")) return "error";
  if (message.includes("WARNING:") || m.includes("warning")) return "warning";
  return "info";
}

// ---------------------------------------------------------------------------
// URL parsing (mirrors parse_urls in the engine)
// ---------------------------------------------------------------------------
const URL_RE =
  /https?:\/\/(?:www\.)?(?:youtube\.com\/(?:watch\?\S*v=|shorts\/|embed\/|live\/)|youtu\.be\/)[\w-]{11}/g;

function parseUrls(text) {
  const out = [];
  for (let line of text.split(/\r?\n/)) {
    line = line.trim();
    if (!line || line.startsWith("#")) continue;
    const matches = line.match(URL_RE);
    if (matches) {
      for (const m of matches) if (!out.includes(m)) out.push(m);
    } else if (line.startsWith("http")) {
      if (!out.includes(line)) out.push(line);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Progress + ETA
// ---------------------------------------------------------------------------
function setOverall(value) {
  state.overall = Math.max(state.overall, Math.min(100, value));
  $("total-bar").value = state.overall;
  $("total-text").textContent = `Overall: ${state.overall.toFixed(0)}%`;
}

function updateProgress(phase, percent, detail) {
  $("status").textContent = PHASE_LABELS[phase] || phase;
  const [start, weight] = PHASE_WEIGHTS[phase] || [0, 0];
  const phaseBar = $("phase-bar");

  if (percent === null || percent === undefined) {
    phaseBar.removeAttribute("value"); // indeterminate
    $("phase-text").textContent = "Current step: working";
    setOverall(start);
    $("detail").textContent = detail;
  } else {
    const v = Math.max(0, Math.min(100, percent));
    phaseBar.value = v;
    $("phase-text").textContent = `Current step: ${v.toFixed(1)}%`;
    setOverall(start + weight * (v / 100));
    $("detail").textContent = `${detail} (${v.toFixed(1)}%)`;
  }
  updateEta();
}

function updateEta() {
  if (!state.startedAt) return;
  const elapsed = (Date.now() - state.startedAt) / 1000;
  const frac = state.overall / 100;
  if (elapsed < 2 || frac <= 0.01) return;
  const remaining = elapsed / frac - elapsed;
  if (remaining < 0) return;
  let m = Math.floor(remaining / 60);
  const s = Math.floor(remaining % 60);
  const h = Math.floor(m / 60);
  m = m % 60;
  if (h > 0) $("eta").textContent = `ETA: ${h}h ${m}m`;
  else if (m > 0) $("eta").textContent = `ETA: ${m}m ${s}s`;
  else $("eta").textContent = `ETA: ${s}s`;
}

function tickElapsed() {
  if (!state.startedAt) return;
  const sec = Math.floor((Date.now() - state.startedAt) / 1000);
  let m = Math.floor(sec / 60);
  const s = sec % 60;
  const h = Math.floor(m / 60);
  m = m % 60;
  if (h) $("elapsed").textContent = `Elapsed: ${h}h ${String(m).padStart(2, "0")}m`;
  else if (m) $("elapsed").textContent = `Elapsed: ${m}:${String(s).padStart(2, "0")}`;
  else $("elapsed").textContent = `Elapsed: ${s}s`;
}

// ---------------------------------------------------------------------------
// Busy state
// ---------------------------------------------------------------------------
function setBusy(busy) {
  state.running = busy;
  $("start").disabled = busy;
  $("cancel").disabled = !busy;
  $("urls").disabled = busy;
  if (!busy) {
    if (state.elapsedTimer) clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
    $("eta").textContent = "";
    $("phase-bar").value = 0;
  }
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
function collectSettings() {
  const s = {
    output_dir: $("output-dir").value,
    cookies: $("cookies").value,
    model: $("model").value,
    device: $("device").value,
    compute_type: $("compute").value,
    language: $("language").value,
    task: $("task").value,
  };
  for (const f of FORMATS) s[`fmt_${f}`] = $(`fmt-${f}`).checked;
  return s;
}

async function loadSettings() {
  const s = await window.api.loadSettings();
  const def = await window.api.defaultOutputDir();
  $("output-dir").value = s.output_dir || def;
  $("cookies").value = s.cookies || "";
  if (s.model) $("model").value = s.model;
  if (s.device) $("device").value = s.device;
  if (s.compute_type) $("compute").value = s.compute_type;
  if (s.language) $("language").value = s.language;
  if (s.task) $("task").value = s.task;
  for (const f of FORMATS) {
    if (s[`fmt_${f}`] !== undefined) $(`fmt-${f}`).checked = s[`fmt_${f}`];
  }
}

// ---------------------------------------------------------------------------
// Start / cancel
// ---------------------------------------------------------------------------
async function start() {
  if (state.running) return;
  const urls = parseUrls($("urls").value);
  if (urls.length === 0) {
    alert("Paste one or more YouTube URLs in the text box.");
    return;
  }
  const formats = FORMATS.filter((f) => $(`fmt-${f}`).checked);
  if (formats.length === 0) {
    alert("Select at least one output format.");
    return;
  }

  const deps = await window.api.checkDeps();
  if (deps.issues.length) {
    alert(deps.issues.join("\n"));
    return;
  }
  for (const w of deps.warnings) appendLog(w, "warning");

  const options = {
    urls,
    formats,
    outputDir: $("output-dir").value.trim(),
    cookies: $("cookies").value.trim(),
    model: $("model").value,
    device: $("device").value,
    computeType: $("compute").value,
    language: $("language").value,
    task: $("task").value,
  };

  await window.api.saveSettings(collectSettings());

  // Reset UI
  state.startedAt = Date.now();
  state.overall = 0;
  state.total = urls.length;
  $("total-bar").value = 0;
  $("phase-bar").value = 0;
  $("phase-text").textContent = "Current step: --";
  $("total-text").textContent = "Overall: 0%";
  $("detail").textContent = "Preparing";
  $("queue").textContent = `Queue: 0/${urls.length}`;
  $("status").textContent = "Running";
  setBusy(true);
  state.elapsedTimer = setInterval(tickElapsed, 1000);

  appendLog(`Starting batch: ${urls.length} URL(s)`);
  appendSeparator();

  const res = await window.api.startTranscription(options);
  if (!res.ok) {
    appendLog(res.error, "error");
    setBusy(false);
    $("status").textContent = "Error";
  }
}

function cancel() {
  window.api.cancelTranscription();
  $("status").textContent = "Cancelling";
  appendLog("Cancel requested — will stop after current step.", "warning");
}

// ---------------------------------------------------------------------------
// Engine events
// ---------------------------------------------------------------------------
function handleEngineEvent(ev) {
  switch (ev.type) {
    case "batch_start":
      state.total = ev.total;
      break;
    case "queue":
      $("queue").textContent = `Queue: ${ev.current}/${ev.total}`;
      appendLog(`[${ev.current}/${ev.total}] ${ev.url}`);
      window.api.setTrayTooltip(`YT Transcript — [${ev.current}/${ev.total}]`);
      break;
    case "log":
      appendLog(ev.message, levelFor(ev.message));
      break;
    case "progress":
      updateProgress(ev.phase, ev.percent, ev.detail);
      break;
    case "video_done":
      appendLog(`Done: ${ev.title}`, "success");
      for (const f of ev.files) appendLog(f, "success");
      appendSeparator();
      break;
    case "video_failed":
      appendLog(`Error processing ${ev.url}: ${ev.error}`, "error");
      appendSeparator();
      break;
    case "batch_done": {
      setBusy(false);
      setOverall(100);
      $("phase-bar").value = 100;
      $("eta").textContent = "";
      const { completed, failed, total, cancelled } = ev;
      if (cancelled) {
        $("status").textContent = "Cancelled";
        $("detail").textContent = `Cancelled after ${completed}/${total}`;
        appendLog(`Cancelled. ${completed} of ${total} completed.`, "warning");
      } else if (failed === 0) {
        $("status").textContent = "Done";
        $("detail").textContent = `All ${completed} video(s) completed`;
        appendLog(`Batch complete: ${completed}/${total} succeeded.`, "success");
      } else {
        $("status").textContent = "Done (with errors)";
        $("detail").textContent = `${completed} done, ${failed} failed of ${total}`;
        appendLog(`Batch complete: ${completed} ok, ${failed} failed of ${total}.`, "warning");
      }
      $("queue").textContent = `Done: ${completed}/${total}`;
      window.api.setTrayTooltip(`YT Transcript — done ${completed}/${total}`);
      break;
    }
  }
}

// ---------------------------------------------------------------------------
// Update flow
// ---------------------------------------------------------------------------
function handleUpdateStatus(s) {
  const el = $("update-state");
  switch (s.state) {
    case "checking":
      el.textContent = "Checking for updates…";
      break;
    case "up-to-date":
      el.textContent = `Up to date (v${s.version})`;
      break;
    case "available":
      state.pendingUpdateVersion = s.version;
      el.textContent = `v${s.version} available`;
      if (confirm(`Version ${s.version} is available. Download it now?`)) {
        el.textContent = "Downloading update…";
        window.api.downloadUpdate();
      }
      break;
    case "downloading":
      el.textContent = `Downloading update… ${Math.round(s.percent || 0)}%`;
      break;
    case "downloaded":
      el.textContent = `v${s.version} ready`;
      if (confirm(`Version ${s.version} downloaded. Restart now to install?`)) {
        window.api.installUpdate();
      }
      break;
    case "error":
      el.textContent = "Update check failed";
      appendLog(`Update error: ${s.message}`, "error");
      break;
  }
}

// ---------------------------------------------------------------------------
// Context menu for log
// ---------------------------------------------------------------------------
function setupLogMenu() {
  const menu = $("log-menu");
  $("log").addEventListener("contextmenu", (e) => {
    e.preventDefault();
    menu.style.left = `${e.clientX}px`;
    menu.style.top = `${e.clientY}px`;
    menu.style.display = "block";
  });
  document.addEventListener("click", () => (menu.style.display = "none"));
  menu.addEventListener("click", (e) => {
    const action = e.target.dataset.action;
    if (action === "copy") {
      document.execCommand("copy");
    } else if (action === "selectall") {
      const range = document.createRange();
      range.selectNodeContents($("log"));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } else if (action === "clear") {
      $("log").innerHTML = "";
    }
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
window.addEventListener("DOMContentLoaded", async () => {
  const version = await window.api.getVersion();
  document.title = `YT Transcript v${version}`;
  $("check-update").textContent = `Check for updates (v${version})`;

  await loadSettings();
  setupLogMenu();

  $("start").addEventListener("click", start);
  $("cancel").addEventListener("click", cancel);
  $("paste").addEventListener("click", async () => {
    const text = await window.api.readClipboard();
    const box = $("urls");
    box.value += (box.value && !box.value.endsWith("\n") ? "\n" : "") + text;
  });
  $("clear-urls").addEventListener("click", () => ($("urls").value = ""));
  $("clear-cookies").addEventListener("click", () => ($("cookies").value = ""));
  $("browse-output").addEventListener("click", async () => {
    const dir = await window.api.pickFolder($("output-dir").value);
    if (dir) $("output-dir").value = dir;
  });
  $("browse-cookies").addEventListener("click", async () => {
    const file = await window.api.pickCookies();
    if (file) $("cookies").value = file;
  });
  $("open-output").addEventListener("click", () =>
    window.api.openOutput($("output-dir").value.trim())
  );
  $("check-update").addEventListener("click", async () => {
    const r = await window.api.checkUpdate();
    if (r.state === "dev") {
      $("update-state").textContent = "Dev build — no auto-update";
      appendLog(r.message, "info");
    }
  });

  window.api.onEngineEvent(handleEngineEvent);
  window.api.onEngineExit((p) => {
    if (state.running) {
      // Engine died without a batch_done event.
      setBusy(false);
      $("status").textContent = p.code === 0 ? "Done" : "Stopped";
    }
  });
  window.api.onUpdateStatus(handleUpdateStatus);
});
