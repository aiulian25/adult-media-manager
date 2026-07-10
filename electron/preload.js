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

    // Update notifier: running vs on-disk installed version ({running, onDisk};
    // they diverge after a deb/rpm upgrade replaces the install underneath us),
    // and a clean relaunch to finish such an update. No arguments cross the
    // bridge in either direction beyond the version strings.
    getVersions: () => ipcRenderer.invoke("app:versions"),
    relaunchApp: () => ipcRenderer.invoke("app:relaunch"),

    // Opens the native OS picker (GTK/KDE/XDG portal). `mode` is "folder"
    // (directories) or "files" — Linux GTK can't combine the two in one dialog,
    // so the caller chooses. Both modes allow multi-select and show hidden files.
    // Returns an array of selected absolute paths, or [] if cancelled.
    pickPaths: (mode) => ipcRenderer.invoke("dialog:open", mode),
});
