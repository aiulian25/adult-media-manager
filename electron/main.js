"use strict";

const { app, BrowserWindow, nativeImage, shell, dialog, ipcMain } = require("electron");
const { spawn, execFileSync }                     = require("child_process");
const path  = require("path");
const fs    = require("fs");
const os    = require("os");
const http  = require("http");
const net   = require("net");

// ── Linux sandbox & Wayland ───────────────────────────────────────────────────
// Must be called BEFORE app.whenReady().
// chrome-sandbox needs setuid-root for the renderer sandbox; most distros skip
// that, so we disable the OS-level sandbox. contextIsolation + contextBridge
// remain in effect for JS-side isolation. postinst.sh sets the setuid bit for
// the .deb install so the full sandbox is restored after installation.
if (process.platform === "linux") {
    app.commandLine.appendSwitch("no-sandbox");
    app.commandLine.appendSwitch("disable-gpu-sandbox");
    // Stay on XWayland: native-Wayland drag-and-drop is broken on hybrid-GPU
    // (NVIDIA Optimus) systems. XWayland composites through Xorg which handles
    // Optimus transparently and lets the file manager share the same X11 surface.
}
// ─────────────────────────────────────────────────────────────────────────────

let pyProc     = null;
let mainWindow = null;
// Resolved to a free TCP port at launch (see findFreePort) — never hardcoded.
// A leftover process holding a fixed port (a not-fully-closed previous window or
// an orphaned backend child) would make the new backend fail with EADDRINUSE and
// the window's waitForServer would time out, so the app "fails to launch". 47821
// is only the *preferred* value; startPython/waitForServer/loadURL all read PORT.
let PORT       = 47821;

// ── Paths ─────────────────────────────────────────────────────────────────────
const XDG_DATA   = process.env.XDG_DATA_HOME   || path.join(os.homedir(), ".local", "share");
const XDG_CONFIG = process.env.XDG_CONFIG_HOME  || path.join(os.homedir(), ".config");

// Data directory: history.json + settings.json + embed-tmp staging
const DATA_DIR   = path.join(XDG_DATA, "adult-media-manager");

// ── Icon resolution ───────────────────────────────────────────────────────────
function getIconPath() {
    const candidates = [
        path.join(process.resourcesPath, "app", "app", "amm.png"),
        path.join(process.resourcesPath, "app", "build", "icons", "512x512.png"),
        path.join(__dirname, "..", "build", "icons", "512x512.png"),
        path.join(__dirname, "..", "build", "icon.png"),
    ];
    for (const p of candidates) {
        try { fs.accessSync(p); return p; } catch {}
    }
    return null;
}

// ── Bundled Python resolution ────────────────────────────────────────────────
// We ship a python-build-standalone interpreter in resources/bundled-python
// and all pip packages flat in resources/bundled-packages.
// This makes the app completely independent of whatever Python (if any) is
// installed on the host — no version mismatch, no missing modules.
function findPython() {
    const candidates = [
        // Packaged (deb / AppImage): electron-builder copies extraResources here
        path.join(process.resourcesPath, "bundled-python", "bin", "python3"),
        // Dev mode: prepare-build.sh puts them in the project root
        path.join(__dirname, "..", "bundled-python", "bin", "python3"),
    ];
    for (const p of candidates) {
        try { fs.accessSync(p, fs.constants.X_OK); return p; } catch {}
    }
    // Final fallback for contributors running the dev server manually
    console.warn("[main] Bundled Python not found — falling back to system python3");
    return "python3";
}

function findPackagesDir() {
    const candidates = [
        path.join(process.resourcesPath, "bundled-packages"),
        path.join(__dirname, "..", "bundled-packages"),
    ];
    for (const p of candidates) {
        try { fs.accessSync(p); return p; } catch {}
    }
    return null;
}

// ── Bundled mkvpropedit resolution ───────────────────────────────────────────
// Shipped in resources/bundled-tools (assembled by prepare-build.sh) so the
// AppImage and deb behave like Docker, which apt-installs mkvtoolnix. The path
// we return is a launcher script that sets LD_LIBRARY_PATH to the bundled libs
// ONLY for mkvpropedit — ffmpeg and the bundled Python are unaffected. The
// backend honours this via the AMM_MKVPROPEDIT env var (_mkvpropedit_path); if
// the bundle is absent it falls back to PATH, then to the ffmpeg remux.
function findMkvpropedit() {
    const candidates = [
        path.join(process.resourcesPath, "bundled-tools", "bin", "mkvpropedit"),
        path.join(__dirname, "..", "bundled-tools", "bin", "mkvpropedit"),
    ];
    for (const p of candidates) {
        try { fs.accessSync(p, fs.constants.X_OK); return p; } catch {}
    }
    return null;
}

// ── Bundled AtomicParsley resolution (review item R4) ─────────────────────────
// Same as mkvpropedit but for MP4/M4V/MOV in-place tagging; the backend honours
// it via AMM_ATOMICPARSLEY (_atomicparsley_path). Absent → PATH → ffmpeg remux.
function findAtomicParsley() {
    const candidates = [
        path.join(process.resourcesPath, "bundled-tools", "bin", "AtomicParsley"),
        path.join(__dirname, "..", "bundled-tools", "bin", "AtomicParsley"),
    ];
    for (const p of candidates) {
        try { fs.accessSync(p, fs.constants.X_OK); return p; } catch {}
    }
    return null;
}

function findCwd() {
    // Packaged: app source is under resources/app
    const packaged = path.join(process.resourcesPath, "app");
    try {
        fs.accessSync(path.join(packaged, "app", "main.py"));
        return packaged;
    } catch {
        return path.join(__dirname, "..");
    }
}

// ── Extra allowed scan roots ───────────────────────────────────────────────────
// Build a colon-separated list of directories the user is likely to store media
// in on a native Linux install. Injected as AMM_EXTRA_ROOTS into the Python
// process; the backend merges these with its Docker defaults (/mnt, /media …).
function buildExtraRoots() {
    const roots = new Set();

    // Home directory subtrees most people use for media
    const home = os.homedir();
    const homeSubdirs = ["Videos", "Movies", "Media", "Downloads", "Torrents"];
    roots.add(home);                                     // allow scanning $HOME
    for (const d of homeSubdirs) roots.add(path.join(home, d));

    // XDG_DATA_HOME (where we also write our own data – the API already
    // prevents writing to DATA_DIR itself since it is not in ALLOWED_ROOTS)
    // Removable media / NAS common mount points (non-Docker, real host paths)
    for (const mp of ["/run/media", "/run/user", "/srv", "/nas", "/storage"]) {
        roots.add(mp);
    }

    return [...roots].filter(r => r && r !== "/").join(":");
}

// ── Start Python backend ──────────────────────────────────────────────────────
function startPython() {
    const python = findPython();
    const cwd    = findCwd();
    console.log(`[main] Python: ${python}  cwd: ${cwd}  port: ${PORT}`);

    // Build child environment — never blindly inherit the parent env so we
    // don't leak unrelated secrets. Pass only what the backend needs.
    const childEnv = {
        PATH:             process.env.PATH || "/usr/local/bin:/usr/bin:/bin",
        HOME:             os.homedir(),
        USER:             process.env.USER || "",
        LANG:             process.env.LANG || "en_US.UTF-8",
        XDG_DATA_HOME:    XDG_DATA,
        XDG_CONFIG_HOME:  XDG_CONFIG,
        // Tell the backend where to persist its data (history, settings, staging)
        DATA_DIR:         DATA_DIR,
        // Tell the backend which extra roots the user can scan
        AMM_EXTRA_ROOTS:  buildExtraRoots(),
        // Signal that we're running as a native desktop app (not Docker).
        // The backend uses this to lift the Docker-centric path allowlist so
        // users can browse and scan any directory on their machine.
        AMM_NATIVE:       "1",
        AMM_HOME:         os.homedir(),
        // Single version source for the native build: app.getVersion() reads
        // package.json's version, so the backend's /docs + /api/health match the
        // app (no stale literal in main.py). See app.main._resolve_app_version.
        AMM_VERSION:      app.getVersion(),
        // Uvicorn / Python diagnostics
        PYTHONUNBUFFERED: "1",
        // Python must not try to write .pyc files to read-only package dirs
        PYTHONDONTWRITEBYTECODE: "1",
    };

    // ── Point Python at the bundled packages ─────────────────────────────────
    // PYTHONPATH makes the bundled-packages directory importable without a venv.
    // We prepend it so it wins over anything in the system site-packages.
    const pkgDir = findPackagesDir();
    if (pkgDir) {
        childEnv.PYTHONPATH = pkgDir + (process.env.PYTHONPATH ? ":" + process.env.PYTHONPATH : "");
        console.log("[main] PYTHONPATH:", childEnv.PYTHONPATH);
    } else {
        console.warn("[main] bundled-packages not found — imports may fail");
    }

    // ── Bundled mkvpropedit (smart embed mode) ───────────────────────────────
    // Point the backend at the bundled launcher when present so behaviour
    // matches Docker/deb. When absent, AMM_MKVPROPEDIT stays unset and the
    // backend resolves mkvpropedit on PATH, then falls back to the ffmpeg remux.
    const mkvpropedit = findMkvpropedit();
    if (mkvpropedit) {
        childEnv.AMM_MKVPROPEDIT = mkvpropedit;
        console.log("[main] Bundled mkvpropedit:", mkvpropedit);
    } else {
        console.warn("[main] bundled mkvpropedit not found — smart mode uses PATH/ffmpeg fallback");
    }

    // ── Bundled AtomicParsley (smart embed mode, MP4/M4V/MOV) ─────────────────
    const atomicParsley = findAtomicParsley();
    if (atomicParsley) {
        childEnv.AMM_ATOMICPARSLEY = atomicParsley;
        console.log("[main] Bundled AtomicParsley:", atomicParsley);
    } else {
        console.warn("[main] bundled AtomicParsley not found — smart mode uses PATH/ffmpeg fallback");
    }

    // ── Optional API key passthrough ─────────────────────────────────────────
    // Power users may set TPDB_API_KEY / STASHDB_API_KEY in their shell (e.g.
    // .bashrc or a wrapper script). If present they are forwarded here and the
    // backend treats them as "env" source — they take priority over anything
    // saved via the Settings modal, exactly as docker-compose vars do.
    // If NOT set in the shell the backend falls back to settings.json, which
    // is what the Settings modal writes to.
    for (const key of ["TPDB_API_KEY", "STASHDB_API_KEY"]) {
        const val = process.env[key];
        if (val && val.trim()) childEnv[key] = val.trim();
    }

    // Ensure data directory exists before Python tries to open files inside it
    try { fs.mkdirSync(DATA_DIR, { recursive: true }); } catch {}

    pyProc = spawn(
        python,
        ["-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1",
         "--port", String(PORT),
         "--no-access-log"],
        { cwd, env: childEnv, stdio: ["ignore", "pipe", "pipe"] }
    );

    pyProc.stdout.on("data", d => console.log("[py]", d.toString().trimEnd()));
    pyProc.stderr.on("data", d => console.log("[py]", d.toString().trimEnd()));
    pyProc.on("exit", code => {
        console.log(`[py] exited with code ${code}`);
        pyProc = null;
    });
}

// ── Resolve a free local port ─────────────────────────────────────────────────
// Try the preferred port first; if it's taken (stale window, orphaned backend,
// or anything else on the box), fall back to an OS-assigned free one (port 0).
// We bind, read the granted port, then close immediately so the Python backend
// can bind it a moment later. There's a tiny TOCTOU window between our close and
// uvicorn's bind, but it's vanishingly small on loopback and beats a hard-coded
// port that collides deterministically.
function findFreePort(preferred) {
    const tryListen = (p) => new Promise((resolve, reject) => {
        const srv = net.createServer();
        srv.once("error", reject);
        srv.listen(p, "127.0.0.1", () => {
            const got = srv.address().port;
            srv.close(() => resolve(got));   // close, then let the backend bind it
        });
    });
    return tryListen(preferred).catch(() => tryListen(0)); // 0 = OS-assigned free port
}

// ── Wait for uvicorn to be ready ──────────────────────────────────────────────
function waitForServer(timeout = 25000) {
    return new Promise((resolve, reject) => {
        const deadline = Date.now() + timeout;
        const check = () => {
            http.get(`http://127.0.0.1:${PORT}/api/health`, res => resolve())
                .on("error", () => {
                    if (Date.now() >= deadline) {
                        reject(new Error(`AMM server did not start within ${timeout / 1000}s`));
                    } else {
                        setTimeout(check, 300);
                    }
                });
        };
        check();
    });
}

// ── AppImage: self-install on first launch ────────────────────────────────────
// On the very first run the AppImage copies itself to ~/.local/bin/ and writes
// a .desktop entry + icon set so it appears in the app launcher immediately.
// Subsequent launches from the permanent location skip the copy.
// This means: double-click anywhere → app launches AND installs itself.
// No separate install script needed.
function installDesktopEntry() {
    const isAppImage =
        !!process.env.APPIMAGE ||
        process.resourcesPath.includes("/.mount_") ||
        process.resourcesPath.includes("/tmp/appimage_extracted");
    if (!isAppImage) return;

    const srcAppImage = process.env.APPIMAGE || process.execPath;

    const binDir    = path.join(os.homedir(), ".local", "bin");
    const appsDir   = path.join(os.homedir(), ".local", "share", "applications");
    const hicolor   = path.join(os.homedir(), ".local", "share", "icons", "hicolor");
    const destName  = "adult-media-manager.AppImage";
    const destPath  = path.join(binDir, destName);
    const desktopFile = path.join(appsDir, "adult-media-manager.desktop");

    try {
        fs.mkdirSync(binDir,   { recursive: true });
        fs.mkdirSync(appsDir,  { recursive: true });

        // ── Step 1: Copy AppImage to permanent location (skip if already there) ──
        const alreadyInstalled = path.resolve(srcAppImage) === path.resolve(destPath);
        if (!alreadyInstalled) {
            console.log(`[main] Installing AppImage → ${destPath}`);
            fs.copyFileSync(srcAppImage, destPath);
            fs.chmodSync(destPath, 0o755);
        }

        // ── Step 2: Install icon sizes ────────────────────────────────────────────
        const sizes = ["16x16","24x24","32x32","48x48","64x64","96x96","128x128","256x256","512x512"];
        for (const sz of sizes) {
            const src = path.join(__dirname, "..", "build", "icons", `${sz}.png`);
            if (!fs.existsSync(src)) continue;
            const sizeDir = path.join(hicolor, sz, "apps");
            fs.mkdirSync(sizeDir, { recursive: true });
            fs.copyFileSync(src, path.join(sizeDir, "adult-media-manager.png"));
        }
        try { execFileSync("gtk-update-icon-cache", ["-f", "-t", hicolor]); } catch {}

        // ── Step 3: Write .desktop pointing at the permanent location ─────────────
        // APPIMAGE_EXTRACT_AND_RUN=1 works on systems without FUSE 2 (Ubuntu 22.04+)
        const desktop = [
            "[Desktop Entry]",
            "Version=1.0",
            "Type=Application",
            "Name=Adult Media Manager",
            "GenericName=Adult Media Organizer",
            "Comment=Smart metadata organizer for adult content",
            `Exec=env APPIMAGE_EXTRACT_AND_RUN=1 ${destPath} --no-sandbox %U`,
            "Icon=adult-media-manager",
            "Categories=AudioVideo;Video;Utility;",
            "Terminal=false",
            "StartupNotify=true",
            "StartupWMClass=AdultMediaManager",
            "MimeType=",
        ].join("\n") + "\n";

        fs.writeFileSync(desktopFile, desktop, { mode: 0o644 });
        try { execFileSync("update-desktop-database", [appsDir]); } catch {}

        if (!alreadyInstalled) {
            console.log("[main] AppImage self-installed to", destPath);
        }
    } catch (err) {
        // Non-fatal — app still works, just won't appear in the launcher
        console.error("[main] Self-install failed:", err.message);
    }
}

// ── App lifecycle ─────────────────────────────────────────────────────────────
// ── Native file/folder picker ─────────────────────────────────────────────────
// Renderer calls electronAPI.pickPaths(mode) → this IPC handler opens the native
// GTK/KDE/portal chooser, bypassing the web-based /api/browse modal entirely.
//
// GTK on Linux can NOT combine file + directory selection in one dialog — passing
// ["openFile","openDirectory"] shows a directory-only selector — so the renderer
// picks a mode first and we map it to a single dialog. We never spread renderer-
// supplied options into showOpenDialog: the property list is whitelisted here so a
// compromised renderer can't, say, flip on system-modal or arbitrary dialog flags.
// Both modes allow multi-select and reveal hidden files.
ipcMain.handle("dialog:open", async (_e, mode) => {
    const files = mode === "files";
    const { canceled, filePaths } = await dialog.showOpenDialog(mainWindow, {
        title:      files ? "Select Media Files" : "Select Media Folders",
        properties: [
            files ? "openFile" : "openDirectory",
            "multiSelections",
            "showHiddenFiles",
        ],
    });
    return canceled ? [] : filePaths;
});

// ── Single-instance lock ──────────────────────────────────────────────────────
// Re-clicking the launcher (or `second-instance`) focuses the existing window
// instead of spawning a second backend that would race for the same resources.
// The lock is keyed by app name, so during a version transition an *old* running
// build holds it and would block the new one — but the dynamic port above means a
// fresh build can still launch independently if the user force-quits the old one.
// If we don't get the lock, quit quietly and let the running instance take over.
const gotInstanceLock = app.requestSingleInstanceLock();
if (!gotInstanceLock) {
    app.quit();
} else {
    app.on("second-instance", () => {
        if (mainWindow) {
            if (mainWindow.isMinimized()) mainWindow.restore();
            mainWindow.focus();
        }
    });
    startApp();
}

async function startApp() {
    app.setName("Adult Media Manager");

    await app.whenReady();

    // Resolve a free port BEFORE spawning the backend so a stale process holding
    // the preferred port can't make uvicorn fail with EADDRINUSE (see PORT note).
    PORT = await findFreePort(PORT);
    console.log(`[main] Resolved backend port: ${PORT}`);

    // Self-register in the desktop for AppImage users
    installDesktopEntry();

    startPython();

    try {
        await waitForServer();
    } catch (err) {
        console.error(err.message);
        app.quit();
        return;
    }

    const iconPath = getIconPath();
    const icon     = iconPath ? nativeImage.createFromPath(iconPath) : null;

    mainWindow = new BrowserWindow({
        width:           1440,
        height:          920,
        minWidth:        960,
        minHeight:       560,
        title:           "Adult Media Manager",
        icon:            icon || undefined,
        backgroundColor: "#08080d",
        autoHideMenuBar: true,
        webPreferences: {
            nodeIntegration:   false,
            contextIsolation:  true,
            sandbox:           false,   // required when --no-sandbox is set process-wide
            preload: path.join(__dirname, "preload.js"),
        },
    });

    // Redundant setIcon call needed on some Linux compositors
    if (icon && !icon.isEmpty()) mainWindow.setIcon(icon);

    mainWindow.loadURL(`http://127.0.0.1:${PORT}`);

    // Open external links (e.g. theporndb.net) in the system browser
    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        if (url.startsWith("http://127.0.0.1") || url.startsWith("http://localhost")) {
            return { action: "allow" };
        }
        shell.openExternal(url);
        return { action: "deny" };
    });

    mainWindow.on("closed", () => { mainWindow = null; });
}

function killPython() {
    if (pyProc) {
        pyProc.kill("SIGTERM");
        pyProc = null;
    }
}

app.on("window-all-closed", () => { killPython(); app.quit(); });
app.on("before-quit",       () => { killPython(); });
