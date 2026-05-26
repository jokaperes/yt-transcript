"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("api", {
  // request/response
  getVersion: () => ipcRenderer.invoke("get-version"),
  loadSettings: () => ipcRenderer.invoke("load-settings"),
  saveSettings: (s) => ipcRenderer.invoke("save-settings", s),
  defaultOutputDir: () => ipcRenderer.invoke("default-output-dir"),
  checkDeps: () => ipcRenderer.invoke("check-deps"),
  readClipboard: () => ipcRenderer.invoke("read-clipboard"),
  pickFolder: (current) => ipcRenderer.invoke("pick-folder", current),
  pickCookies: () => ipcRenderer.invoke("pick-cookies"),
  openOutput: (dir) => ipcRenderer.invoke("open-output", dir),
  startTranscription: (opts) => ipcRenderer.invoke("start-transcription", opts),
  cancelTranscription: () => ipcRenderer.invoke("cancel-transcription"),
  setTrayTooltip: (text) => ipcRenderer.invoke("set-tray-tooltip", text),
  checkUpdate: () => ipcRenderer.invoke("check-update"),
  downloadUpdate: () => ipcRenderer.invoke("download-update"),
  installUpdate: () => ipcRenderer.invoke("install-update"),

  // events main -> renderer
  onEngineEvent: (cb) =>
    ipcRenderer.on("engine-event", (_e, payload) => cb(payload)),
  onEngineExit: (cb) =>
    ipcRenderer.on("engine-exit", (_e, payload) => cb(payload)),
  onUpdateStatus: (cb) =>
    ipcRenderer.on("update-status", (_e, payload) => cb(payload)),
});
