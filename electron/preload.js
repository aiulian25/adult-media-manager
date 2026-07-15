"use strict";

const { contextBridge, webUtils, ipcRenderer } = require("electron");

// Expose a minimal surface to the renderer — no Node.js APIs leak through.
// contextIsolation: true (set in main.js) keeps the renderer in a separate
// JS context; this bridge is the only communication channel.
const { os: nodeOs } = (() => {
    try { return { os: require("os") }; } catch { return { os: null }; }
})();

contextBridge.exposeInMainWorld("electronAPI", {
    // Flag the UI can use to enable Electron-specific behaviour (e.g. native
    // file-path resolution from drag-and-drop events).
    isElectron: true,

    // Real home directory of the running user — used as the default browse path.
    homedir: nodeOs ? nodeOs.homedir() : "/home",

    // webUtils.getPathForFile() is the Electron 32+ supported way to resolve
    // the real on-disk path from a File object supplied by a drag-drop event.
    // The preload guard prevents a broken sandbox from surfacing as an
    // uncaught renderer exception.
    getPathForFile: (file) => {
        try {
            return webUtils.getPathForFile(file) || "";
        } catch (err) {
            console.error("[preload] getPathForFile failed:", err.message);
            return "";
        }
    },

    // Software update bridge — the renderer passes NO arguments to any of
    // these; the main process owns asset selection, verification (sha256
    // against the GitHub release digest) and what a restart actually does.
    // One-click update download (Settings). The main process picks the right
    // package for this install (deb/rpm/AppImage × arch) and verifies it.
    downloadUpdate: () => ipcRenderer.invoke("update:download"),
    // Install the update downloadUpdate() verified. deb/rpm go through the
    // system package manager under polkit authorization; AppImage is replaced
    // in place with no privileges.
    installUpdate: () => ipcRenderer.invoke("update:install"),
    // Download progress events ({pct, transferred, total}).
    onUpdateProgress: (cb) => {
        ipcRenderer.removeAllListeners("update:download-progress");
        ipcRenderer.on("update:download-progress", (_evt, p) => cb(p));
    },
    // Fired when an installed update awaits a restart (deb/rpm upgrade seen
    // on disk, or a replaced AppImage). The renderer shows the themed prompt.
    onUpdateRestartPending: (cb) => {
        ipcRenderer.removeAllListeners("update:restart-pending");
        ipcRenderer.on("update:restart-pending", (_evt, info) => cb(info));
    },
    // Perform the restart a pending update requires. Takes no arguments —
    // the main process decided relaunch-vs-spawn when the install landed.
    restartApp: () => ipcRenderer.invoke("update:restart"),

    // Opens the native OS picker (GTK/KDE/XDG portal). `mode` is "folder"
    // (directories) or "files" — Linux GTK can't combine the two in one dialog,
    // so the caller chooses. Both modes allow multi-select and show hidden files.
    // Returns an array of selected absolute paths, or [] if cancelled.
    pickPaths: (mode) => ipcRenderer.invoke("dialog:open", mode),
});
