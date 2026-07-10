"use strict";

// Pure planner for the in-app update install (update-notifier extension).
// No Electron imports — unit-testable with plain node, mirroring
// app/core/embedder.py's "pure decision + thin adapter" split: this module
// DECIDES what a downloaded release file may do; main.js EXECUTES the plan.
//
// Security contract: the renderer hands us a path it got from the backend's
// download endpoint, but we trust nothing — the file must live directly in
// ~/Downloads, its basename must match the app's real release-artifact names,
// and each extension maps to a FIXED argv (no shell, nothing user-shaped is
// interpreted). deb/rpm go through pkexec, whose native authentication dialog
// is the user's consent gate; AppImage needs no privilege at all.

const path = require("path");

// Anchored to the artifacts electron-builder actually produces:
//   adult-media-manager_1.6.0_amd64.deb
//   adult-media-manager-1.6.0.x86_64.rpm
//   Adult.Media.Manager-1.6.0.AppImage
const RELEASE_FILE_RE = /^adult[._-]media[._-]manager.*\.(deb|rpm|appimage)$/i;

/**
 * Validate a downloaded release file and return how to install it.
 *
 * @param {string} filePath path returned by POST /api/version/download
 * @param {string} homedir  os.homedir() of the running user
 * @returns {{error: string} |
 *           {kind: "pkexec", argv: string[], path: string} |
 *           {kind: "appimage", path: string, dest: string}}
 */
function planInstall(filePath, homedir) {
    if (typeof filePath !== "string" || !filePath.trim()) {
        return { error: "No downloaded file to install" };
    }
    const resolved  = path.resolve(filePath);
    const downloads = path.join(homedir, "Downloads");
    if (path.dirname(resolved) !== downloads) {
        return { error: "File is not in the Downloads folder" };
    }
    const base = path.basename(resolved);
    if (!RELEASE_FILE_RE.test(base)) {
        return { error: "Not an Adult Media Manager release file" };
    }

    const ext = path.extname(base).toLowerCase();
    if (ext === ".deb") {
        return { kind: "pkexec", path: resolved,
                 argv: ["pkexec", "apt-get", "install", "-y", resolved] };
    }
    if (ext === ".rpm") {
        return { kind: "pkexec", path: resolved,
                 argv: ["pkexec", "dnf", "install", "-y", resolved] };
    }
    // .AppImage — userland self-upgrade: overwrite the self-installed copy
    // (installDesktopEntry's permanent location) with the new build.
    return { kind: "appimage", path: resolved,
             dest: path.join(homedir, ".local", "bin", "adult-media-manager.AppImage") };
}

module.exports = { planInstall, RELEASE_FILE_RE };
