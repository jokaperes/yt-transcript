"use strict";

const {
  app,
  BrowserWindow,
  ipcMain,
  dialog,
  shell,
  clipboard,
  Tray,
  Menu,
  nativeImage,
} = require("electron");
const { autoUpdater } = require("electron-updater");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");

const SETTINGS_PATH = path.join(app.getPath("userData"), "settings.json");

let mainWindow = null;
let tray = null;
let engineProc = null;
let isQuitting = false;

// ---------------------------------------------------------------------------
// Settings persistence (mirrors the Tkinter settings.json behaviour)
// ---------------------------------------------------------------------------
function loadSettings() {
  try {
    return JSON.parse(fs.readFileSync(SETTINGS_PATH, "utf-8"));
  } catch {
    return {};
  }
}

function saveSettings(settings) {
  try {
    fs.writeFileSync(SETTINGS_PATH, JSON.stringify(settings, null, 2), "utf-8");
  } catch {
    /* best effort */
  }
}

// ---------------------------------------------------------------------------
// Locate the bundled Python engine. In a packaged app it lives under
// resources/engine/. In dev we fall back to running the source with python.
// ---------------------------------------------------------------------------
function resolveEngine() {
  const isWin = process.platform === "win32";
  if (app.isPackaged) {
    const exeName = isWin ? "yt-transcript.exe" : "yt-transcript";
    const exePath = path.join(process.resourcesPath, "engine", exeName);
    return { command: exePath, baseArgs: [] };
  }
  // Dev: bundled exe if present next to the project, else the python source.
  const repoRoot = path.join(__dirname, "..");
  const localExe = path.join(
    __dirname,
    "engine",
    isWin ? "yt-transcript.exe" : "yt-transcript"
  );
  if (fs.existsSync(localExe)) {
    return { command: localExe, baseArgs: [] };
  }
  const py = isWin ? "python" : "python3";
  return { command: py, baseArgs: [path.join(repoRoot, "transcribe_youtube.py")] };
}

function send(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

// ---------------------------------------------------------------------------
// Engine lifecycle
// ---------------------------------------------------------------------------
function startEngine(options) {
  if (engineProc) {
    return { ok: false, error: "A job is already running." };
  }

  const { command, baseArgs } = resolveEngine();
  const args = [...baseArgs, "--json-events"];

  args.push("--output-dir", options.outputDir);
  args.push("--model", options.model);
  args.push("--device", options.device);
  args.push("--compute-type", options.computeType);
  args.push("--language", options.language);
  args.push("--task", options.task || "transcribe");
  for (const fmt of options.formats) {
    args.push("--format", fmt);
  }
  if (options.cookies) {
    args.push("--cookies", options.cookies);
  }
  for (const url of options.urls) {
    args.push(url);
  }

  try {
    engineProc = spawn(command, args, {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
  } catch (err) {
    engineProc = null;
    return { ok: false, error: `Failed to launch engine: ${err.message}` };
  }

  let stdoutBuffer = "";
  engineProc.stdout.setEncoding("utf-8");
  engineProc.stdout.on("data", (chunk) => {
    stdoutBuffer += chunk;
    let idx;
    while ((idx = stdoutBuffer.indexOf("\n")) >= 0) {
      const line = stdoutBuffer.slice(0, idx).trim();
      stdoutBuffer = stdoutBuffer.slice(idx + 1);
      if (!line) continue;
      try {
        send("engine-event", JSON.parse(line));
      } catch {
        send("engine-event", { type: "log", message: line });
      }
    }
  });

  engineProc.stderr.setEncoding("utf-8");
  engineProc.stderr.on("data", (chunk) => {
    // Engine routes human logs to stderr; surface them as log events.
    for (const raw of chunk.split(/\r?\n/)) {
      const line = raw.trim();
      if (line) send("engine-event", { type: "log", message: line });
    }
  });

  engineProc.on("error", (err) => {
    send("engine-event", { type: "log", message: `Engine error: ${err.message}` });
  });

  engineProc.on("close", (code) => {
    engineProc = null;
    send("engine-exit", { code });
    updateTrayTooltip("YT Transcript");
  });

  return { ok: true };
}

function cancelEngine() {
  if (!engineProc) return;
  try {
    engineProc.stdin.write("cancel\n");
  } catch {
    /* ignore */
  }
  // Give the engine a moment to stop gracefully, then force-kill.
  const proc = engineProc;
  setTimeout(() => {
    if (proc && !proc.killed) {
      try {
        proc.kill();
      } catch {
        /* ignore */
      }
    }
  }, 8000);
}

// ---------------------------------------------------------------------------
// Dependency checks (ffmpeg / node — mirrors check_dependencies in the engine)
// ---------------------------------------------------------------------------
function which(cmd) {
  const isWin = process.platform === "win32";
  const exts = isWin ? (process.env.PATHEXT || ".EXE").split(";") : [""];
  const dirs = (process.env.PATH || "").split(path.delimiter);
  for (const dir of dirs) {
    for (const ext of exts) {
      const full = path.join(dir, cmd + ext);
      try {
        fs.accessSync(full, fs.constants.X_OK);
        return full;
      } catch {
        /* keep looking */
      }
    }
  }
  return null;
}

function checkDependencies() {
  const issues = [];
  if (!which("ffmpeg")) {
    issues.push(
      "FFmpeg was not found on PATH. Install it (winget install Gyan.FFmpeg) and reopen the app."
    );
  }
  const warnings = [];
  if (!which("node")) {
    warnings.push(
      "Node.js was not found. Some YouTube videos may fail player-challenge solving without it."
    );
  }
  return { issues, warnings };
}

// ---------------------------------------------------------------------------
// Tray
// ---------------------------------------------------------------------------
function trayIconImage() {
  const file = path.join(__dirname, "build", "icon.png");
  const img = nativeImage.createFromPath(file);
  return img.isEmpty() ? nativeImage.createEmpty() : img;
}

function setupTray() {
  try {
    tray = new Tray(trayIconImage());
    const menu = Menu.buildFromTemplate([
      { label: "Show window", click: () => showWindow() },
      { label: "Cancel job", click: () => cancelEngine() },
      { type: "separator" },
      {
        label: "Quit",
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]);
    tray.setToolTip("YT Transcript");
    tray.setContextMenu(menu);
    tray.on("click", () => showWindow());
  } catch {
    tray = null;
  }
}

function updateTrayTooltip(text) {
  if (tray) {
    try {
      tray.setToolTip(text);
    } catch {
      /* ignore */
    }
  }
}

function showWindow() {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  }
}

// ---------------------------------------------------------------------------
// Window
// ---------------------------------------------------------------------------
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 900,
    height: 800,
    minWidth: 760,
    minHeight: 560,
    icon: path.join(__dirname, "build", "icon.png"),
    title: `YT Transcript v${app.getVersion()}`,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.removeMenu();
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));

  mainWindow.on("close", (e) => {
    // Minimize to tray instead of quitting, unless really quitting.
    if (!isQuitting && tray) {
      e.preventDefault();
      mainWindow.hide();
      updateTrayTooltip("YT Transcript — minimized");
    }
  });
}

// ---------------------------------------------------------------------------
// Auto-update (electron-updater against GitHub Releases)
// ---------------------------------------------------------------------------
function setupAutoUpdate() {
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on("checking-for-update", () =>
    send("update-status", { state: "checking" })
  );
  autoUpdater.on("update-available", (info) =>
    send("update-status", { state: "available", version: info.version })
  );
  autoUpdater.on("update-not-available", (info) =>
    send("update-status", { state: "up-to-date", version: info.version })
  );
  autoUpdater.on("download-progress", (p) =>
    send("update-status", { state: "downloading", percent: p.percent })
  );
  autoUpdater.on("update-downloaded", (info) =>
    send("update-status", { state: "downloaded", version: info.version })
  );
  autoUpdater.on("error", (err) =>
    send("update-status", { state: "error", message: String(err) })
  );

  // Silent check shortly after launch (only meaningful in a packaged build).
  if (app.isPackaged) {
    setTimeout(() => autoUpdater.checkForUpdates().catch(() => {}), 4000);
  }
}

// ---------------------------------------------------------------------------
// IPC wiring
// ---------------------------------------------------------------------------
function registerIpc() {
  ipcMain.handle("get-version", () => app.getVersion());
  ipcMain.handle("load-settings", () => loadSettings());
  ipcMain.handle("save-settings", (_e, settings) => {
    saveSettings(settings);
    return true;
  });
  ipcMain.handle("default-output-dir", () =>
    path.join(app.getPath("documents"), "yt-transcripts")
  );
  ipcMain.handle("check-deps", () => checkDependencies());
  ipcMain.handle("read-clipboard", () => clipboard.readText());

  ipcMain.handle("pick-folder", async (_e, current) => {
    const res = await dialog.showOpenDialog(mainWindow, {
      properties: ["openDirectory", "createDirectory"],
      defaultPath: current || os.homedir(),
    });
    return res.canceled ? null : res.filePaths[0];
  });

  ipcMain.handle("pick-cookies", async () => {
    const res = await dialog.showOpenDialog(mainWindow, {
      properties: ["openFile"],
      filters: [
        { name: "Cookies text file", extensions: ["txt"] },
        { name: "All files", extensions: ["*"] },
      ],
    });
    return res.canceled ? null : res.filePaths[0];
  });

  ipcMain.handle("open-output", async (_e, dir) => {
    try {
      fs.mkdirSync(dir, { recursive: true });
    } catch {
      /* ignore */
    }
    shell.openPath(dir);
    return true;
  });

  ipcMain.handle("start-transcription", (_e, options) => {
    const res = startEngine(options);
    if (res.ok) updateTrayTooltip("YT Transcript — processing");
    return res;
  });

  ipcMain.handle("cancel-transcription", () => {
    cancelEngine();
    return true;
  });

  ipcMain.handle("set-tray-tooltip", (_e, text) => {
    updateTrayTooltip(text);
    return true;
  });

  ipcMain.handle("check-update", async () => {
    if (!app.isPackaged) {
      return { state: "dev", message: "Auto-update only works in an installed build." };
    }
    try {
      const r = await autoUpdater.checkForUpdates();
      return { state: "checked", version: r?.updateInfo?.version };
    } catch (err) {
      return { state: "error", message: String(err) };
    }
  });

  ipcMain.handle("download-update", async () => {
    try {
      await autoUpdater.downloadUpdate();
      return { ok: true };
    } catch (err) {
      return { ok: false, error: String(err) };
    }
  });

  ipcMain.handle("install-update", () => {
    isQuitting = true;
    autoUpdater.quitAndInstall();
    return true;
  });
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());

  app.whenReady().then(() => {
    registerIpc();
    createWindow();
    setupTray();
    setupAutoUpdate();

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on("before-quit", () => {
    isQuitting = true;
    if (engineProc) {
      try {
        engineProc.kill();
      } catch {
        /* ignore */
      }
    }
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
}
