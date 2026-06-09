"use strict";

const { contextBridge, webUtils } = require("electron");

// Expose a minimal surface to the renderer — no Node.js APIs leak through.
// contextIsolation: true (set in main.js) keeps the renderer in a separate
// JS context; this bridge is the only communication channel.
contextBridge.exposeInMainWorld("electronAPI", {
    // Flag the UI can use to enable Electron-specific behaviour (e.g. native
    // file-path resolution from drag-and-drop events).
    isElectron: true,

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
});
