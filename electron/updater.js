/**
 * Update-download helpers for the desktop shell (deb / rpm / AppImage).
 *
 * Plain Node — no Electron imports — so the logic is unit-testable outside
 * the app. main.js owns the platform wiring (package-type detection, IPC,
 * Downloads path, reveal-in-file-manager); this module owns the two pure
 * jobs: picking the right release asset and downloading it safely.
 *
 * Safety properties of downloadAsset():
 *  - HTTPS only, host allow-listed to GitHub (github.com /
 *    *.githubusercontent.com — release assets redirect there). A poisoned
 *    asset URL can never make the app fetch from elsewhere.
 *  - Writes to `dest + ".part"` and renames into place only after BOTH the
 *    expected byte size and the release's sha256 digest (when provided by
 *    the GitHub API) have been verified — a truncated or tampered download
 *    never lands under the final filename.
 */

"use strict";

const https = require("https");
const fs = require("fs");
const crypto = require("crypto");
const { URL } = require("url");

const ALLOWED_HOSTS = /(^|\.)github\.com$|(^|\.)githubusercontent\.com$/i;

// Suffix per (packageType, arch), matching electron-builder's artifact names:
//   adult-media-manager_X.Y.Z_amd64.deb / adult-media-manager-X.Y.Z.x86_64.rpm
//   Adult.Media.Manager-X.Y.Z.AppImage
// x64 AppImage is the unsuffixed one, so it is matched by exclusion in
// pickAsset() rather than listed here.
const SUFFIX = {
    "deb:x64": "_amd64.deb",
    "deb:arm64": "_arm64.deb",
    "rpm:x64": ".x86_64.rpm",
    "rpm:arm64": ".aarch64.rpm",
    "appimage:arm64": "-arm64.AppImage",
};

/** Pick the release asset matching this install's package type + CPU arch.
 *  `assets` is the list from /api/version → update.assets. Returns the asset
 *  object or null when the release carries no matching package. */
function pickAsset(assets, pkgType, arch) {
    if (!Array.isArray(assets)) return null;
    if (pkgType === "appimage" && arch !== "arm64") {
        return assets.find(a => a && typeof a.name === "string"
            && a.name.endsWith(".AppImage") && !/arm64/i.test(a.name)) || null;
    }
    const suffix = SUFFIX[`${pkgType}:${arch}`];
    if (!suffix) return null;
    return assets.find(a => a && typeof a.name === "string" && a.name.endsWith(suffix)) || null;
}

/** Download `url` to `dest` with redirect following (max 5), size and sha256
 *  verification, and per-percent progress callbacks. Resolves to `dest`. */
function downloadAsset(url, dest, { expectedSize, digest, onProgress } = {}, redirects = 0) {
    return new Promise((resolve, reject) => {
        let u;
        try { u = new URL(url); } catch { return reject(new Error(`Bad download URL`)); }
        if (u.protocol !== "https:" || !ALLOWED_HOSTS.test(u.hostname)) {
            return reject(new Error(`Refusing download from non-GitHub host: ${u.hostname}`));
        }

        const req = https.get(u, {
            headers: { "User-Agent": "AMM-updater", "Accept": "application/octet-stream" },
        }, res => {
            // GitHub asset URLs redirect to objects.githubusercontent.com.
            if ([301, 302, 303, 307, 308].includes(res.statusCode)) {
                res.resume();
                if (redirects >= 5) return reject(new Error("Too many redirects"));
                let next;
                try { next = new URL(res.headers.location, u).toString(); }
                catch { return reject(new Error("Bad redirect location")); }
                return resolve(downloadAsset(next, dest, { expectedSize, digest, onProgress }, redirects + 1));
            }
            if (res.statusCode !== 200) {
                res.resume();
                return reject(new Error(`Download failed: HTTP ${res.statusCode}`));
            }

            const tmp = dest + ".part";
            const out = fs.createWriteStream(tmp, { mode: 0o644 });
            const hash = crypto.createHash("sha256");
            const total = expectedSize || parseInt(res.headers["content-length"] || "0", 10) || 0;
            let got = 0;
            let lastPct = -1;

            res.on("data", chunk => {
                got += chunk.length;
                hash.update(chunk);
                if (onProgress && total) {
                    const pct = Math.min(100, Math.round((100 * got) / total));
                    if (pct !== lastPct) { lastPct = pct; onProgress(pct, got, total); }
                }
            });
            res.pipe(out);

            const fail = err => {
                out.destroy();
                fs.unlink(tmp, () => {});
                reject(err);
            };

            out.on("finish", () => out.close(() => {
                try {
                    if (expectedSize && got !== expectedSize) {
                        throw new Error(`Size mismatch: got ${got} bytes, expected ${expectedSize}`);
                    }
                    const want = String(digest || "").replace(/^sha256:/, "").toLowerCase();
                    if (want) {
                        const have = hash.digest("hex");
                        if (have !== want) {
                            throw new Error("Checksum mismatch — the download is corrupted or tampered with");
                        }
                    }
                    fs.renameSync(tmp, dest);
                    resolve(dest);
                } catch (err) {
                    fs.unlink(tmp, () => {});
                    reject(err);
                }
            }));
            out.on("error", fail);
            res.on("error", fail);
        });

        req.on("error", reject);
        // Inactivity timeout (not total duration — big files on slow links are fine).
        req.setTimeout(30000, () => req.destroy(new Error("Download timed out (no data for 30 s)")));
    });
}

/** Re-verify an already-downloaded file (size + sha256) by streaming it —
 *  never loads it into memory. Called immediately before install to close the
 *  window between download-time verification and install-time use: a file
 *  swapped in ~/Downloads after the download can never reach the package
 *  manager. Resolves to `file`, rejects on any mismatch. */
function verifyFile(file, { expectedSize, digest } = {}) {
    return new Promise((resolve, reject) => {
        let st;
        try { st = fs.statSync(file); }
        catch { return reject(new Error("Downloaded update is no longer there — download it again")); }
        if (expectedSize && st.size !== expectedSize) {
            return reject(new Error("File size changed since download — refusing to install it"));
        }
        const want = String(digest || "").replace(/^sha256:/, "").toLowerCase();
        if (!want) return resolve(file);
        const hash = crypto.createHash("sha256");
        const s = fs.createReadStream(file);
        s.on("data", c => hash.update(c));
        s.on("error", reject);
        s.on("end", () => {
            if (hash.digest("hex") !== want) {
                reject(new Error("File checksum changed since download — refusing to install it"));
            } else {
                resolve(file);
            }
        });
    });
}

module.exports = { pickAsset, downloadAsset, verifyFile, SUFFIX, ALLOWED_HOSTS };
