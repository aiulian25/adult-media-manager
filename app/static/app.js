/* Adult Media Manager - Frontend Logic */

// ─── Custom confirm modal (replaces blocking window.confirm) ─────────────────
function showConfirmModal(message, onConfirm) {
    const modal   = document.getElementById('confirm-modal');
    const msgEl   = document.getElementById('confirm-modal-message');
    const btnOk   = document.getElementById('confirm-modal-ok');
    const btnCancel = document.getElementById('confirm-modal-cancel');
    msgEl.textContent = message;
    modal.classList.remove('hidden');
    function cleanup() {
        modal.classList.add('hidden');
        btnOk.removeEventListener('click', handleOk);
        btnCancel.removeEventListener('click', handleCancel);
    }
    function handleOk()     { cleanup(); onConfirm(); }
    function handleCancel() { cleanup(); }
    btnOk.addEventListener('click', handleOk);
    btnCancel.addEventListener('click', handleCancel);
}
// ─────────────────────────────────────────────────────────────────────────────

// ─── i18n ─────────────────────────────────────────────────────────────────────
let _i18n = {};

/**
 * Translate a key, optionally interpolating {placeholder} variables.
 * Falls back to the raw key if no translation is found.
 * @param {string} key
 * @param {Object} [vars]
 */
function t(key, vars) {
    let str = _i18n[key] || key;
    if (vars) {
        Object.entries(vars).forEach(([k, v]) => {
            str = str.replaceAll(`{${k}}`, v);
        });
    }
    return str;
}

async function loadI18n() {
    const saved = localStorage.getItem('amm_locale') || 'en';
    try {
        const res = await fetch(`/static/locales/${saved}.json`);
        if (res.ok) {
            _i18n = await res.json();
        }
    } catch (_) {
        // silently fall through to English key names
    }
}

function setLocale(locale) {
    localStorage.setItem('amm_locale', locale);
    loadI18n().then(() => applyI18nToDOM());
}

function applyI18nToDOM() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        const attr = el.getAttribute('data-i18n-attr');
        if (attr) {
            el.setAttribute(attr, t(key));
        } else {
            el.textContent = t(key);
        }
    });
}
// ─────────────────────────────────────────────────────────────────────────────

// State
let scannedFiles = [];
let matchedResults = [];
let privacyMode = false;  // Manual toggle only - disabled by default
let currentBrowsePath = '/media';
let browseHistory = [];              // stack of visited paths for back navigation
let browseSelectionMode = 'folder';  // 'folder' or 'files'
let selectedFiles = [];
let selectedScannedIndices = new Set();   // indices into scannedFiles
let selectedMatchIndices   = new Set();   // indices into matchedResults

// Elements
const scanPath = document.getElementById('scan-path');
const btnScan = document.getElementById('btn-scan');
const btnMatch = document.getElementById('btn-match');
const btnRename = document.getElementById('btn-rename');
const btnBrowse = document.getElementById('btn-browse');
const btnPrivacy = document.getElementById('btn-privacy');
const btnHistory = document.getElementById('btn-history');
const recursive = document.getElementById('recursive');
const action = document.getElementById('action');
const template = document.getElementById('template');
const flatRename = document.getElementById('flat-rename');
const resultsContainer = document.getElementById('results-container');
const statusBar = document.getElementById('status-bar');
const statusText = document.getElementById('status-text');
const progressFill = document.getElementById('progress-fill');

// Home / reset button
document.getElementById('btn-home').addEventListener('click', resetToHome);

function resetToHome() {
    // Clear in-memory state
    scannedFiles = [];
    matchedResults = [];
    selectedFiles = [];
    selectedScannedIndices = new Set();
    selectedMatchIndices   = new Set();

    // Clear scan path input
    scanPath.value = '';

    // Reset button states
    btnMatch.disabled  = true;
    btnRename.disabled = true;

    // Hide status bar
    statusBar.classList.add('hidden');

    // Clear results panel back to initial placeholder
    resultsContainer.innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">🎬</div>
            <div class="empty-title">${t('home.empty_title')}</div>
            <div class="empty-subtitle">${t('home.empty_subtitle')}</div>
        </div>
    `;
}

// Modals
const browseModal = document.getElementById('browse-modal');
const historyModal = document.getElementById('history-modal');
const manualEditModal = document.getElementById('manual-edit-modal');

// Manual editing state
let currentManualFile = null;
let manualPerformers = [];
let manualTags = [];
let generatedThumbnails = [];
let selectedThumbnailIndex = null;

// Initialize
document.addEventListener('DOMContentLoaded', async () => {
    // Initialise i18n before anything else so translated strings are available
    await loadI18n();
    applyI18nToDOM();

    // Wire language selector if present
    const langSelect = document.getElementById('lang-select');
    if (langSelect) {
        langSelect.value = localStorage.getItem('amm_locale') || 'en';
        langSelect.addEventListener('change', () => setLocale(langSelect.value));
    }

    // Load privacy preference from localStorage (user choice only)
    const storedPrivacy = localStorage.getItem('privacyMode');
    if (storedPrivacy !== null) {
        privacyMode = storedPrivacy === 'true';
    }
    
    // Template presets
    document.querySelectorAll('.preset-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            template.value = btn.dataset.template;
        });
    });
    
    // Privacy mode button
    updatePrivacyButton();
    btnPrivacy.addEventListener('click', togglePrivacyMode);
    
    // Scan button
    btnScan.addEventListener('click', scanFolder);
    
    // Match button
    btnMatch.addEventListener('click', matchFiles);
    
    // Rename button
    btnRename.addEventListener('click', renameFiles);
    
    // Browse button
    btnBrowse.addEventListener('click', openBrowseModal);
    
    // History button
    btnHistory.addEventListener('click', openHistoryModal);

    // Settings button
    document.getElementById('btn-settings').addEventListener('click', openSettingsModal);
    
    // Modal close buttons
    document.getElementById('modal-close').addEventListener('click', () => {
        browseModal.classList.add('hidden');
    });
    
    document.getElementById('browse-cancel').addEventListener('click', () => {
        browseModal.classList.add('hidden');
    });
    
    document.getElementById('browse-select').addEventListener('click', selectBrowseFolder);
    
    document.getElementById('history-close').addEventListener('click', () => {
        historyModal.classList.add('hidden');
    });
    
    document.getElementById('history-ok').addEventListener('click', () => {
        historyModal.classList.add('hidden');
    });
    
    document.getElementById('history-undo').addEventListener('click', undoLastRename);
    
    // Manual edit modal
    document.getElementById('manual-edit-close').addEventListener('click', () => {
        manualEditModal.classList.add('hidden');
    });
    
    document.getElementById('manual-edit-cancel').addEventListener('click', () => {
        manualEditModal.classList.add('hidden');
    });
    
    document.getElementById('manual-edit-save').addEventListener('click', saveManualMetadata);
    document.getElementById('manual-add-performer').addEventListener('click', addManualPerformer);
    document.getElementById('manual-add-tag').addEventListener('click', addManualTag);
    document.getElementById('manual-generate-thumbnails').addEventListener('click', generateThumbnails);
    
    // Autocomplete: tags
    const tagInput = document.getElementById('manual-tag-input');
    tagInput.addEventListener('input', () => filterTagSuggestions(tagInput.value));
    tagInput.addEventListener('focus', () => filterTagSuggestions(tagInput.value));
    document.addEventListener('click', (e) => {
        if (!e.target.closest('#manual-tag-input') && !e.target.closest('#tag-suggestions')) {
            document.getElementById('tag-suggestions').style.display = 'none';
        }
    });

    // Autocomplete: sites
    const siteInput = document.getElementById('manual-site');
    let siteSearchTimer = null;
    siteInput.addEventListener('input', () => {
        clearTimeout(siteSearchTimer);
        siteSearchTimer = setTimeout(() => searchSiteSuggestions(siteInput.value), 300);
    });
    // Show known sites immediately on focus even with empty input
    siteInput.addEventListener('focus', () => searchSiteSuggestions(siteInput.value));
    document.addEventListener('click', (e) => {
        if (!e.target.closest('#manual-site') && !e.target.closest('#site-suggestions')) {
            document.getElementById('site-suggestions').style.display = 'none';
        }
    });

    // Enter key support
    document.getElementById('manual-performer-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') addManualPerformer();
    });
    
    document.getElementById('manual-tag-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') addManualTag();
    });
    
    // Enter key in scan path
    scanPath.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            scanFolder();
        }
    });

    // §4.4 — Resume banner: if a rename was interrupted (page refresh / crash)
    // show a dismissible notification so the user knows work was in progress.
    const savedQueue = _loadRenameQueue();
    if (savedQueue && savedQueue.operations && savedQueue.operations.length > 0) {
        const banner = document.createElement('div');
        banner.style.cssText =
            'position:fixed;bottom:1rem;right:1rem;z-index:9999;' +
            'background:rgba(30,30,40,.95);border:1px solid var(--accent,#7f5af0);' +
            'border-radius:12px;padding:1rem 1.2rem;max-width:340px;font-size:.85rem;' +
            'box-shadow:0 4px 20px rgba(0,0,0,.5);color:var(--text,#fff)';

        const count = savedQueue.operations.length;
        const msg = document.createElement('p');
        msg.style.cssText = 'margin:0 0 .7rem';
        msg.textContent =
            t('resume.banner_msg', {
                count: count,
                plural: count !== 1 ? 's' : '',
                action: savedQueue.actionType,
            });

        const btnRow = document.createElement('div');
        btnRow.style.cssText = 'display:flex;gap:.5rem';

        const btnResume = document.createElement('button');
        btnResume.className = 'btn btn-primary';
        btnResume.style.cssText = 'font-size:.8rem;padding:.35rem .8rem';
        btnResume.textContent = t('resume.btn_resume');
        btnResume.addEventListener('click', () => {
            banner.remove();
            _doRename(savedQueue.operations, savedQueue.actionType);
        });

        const btnDiscard = document.createElement('button');
        btnDiscard.className = 'btn btn-secondary';
        btnDiscard.style.cssText = 'font-size:.8rem;padding:.35rem .8rem';
        btnDiscard.textContent = t('resume.btn_discard');
        btnDiscard.addEventListener('click', () => {
            _saveRenameQueue([], '');
            banner.remove();
        });

        btnRow.appendChild(btnResume);
        btnRow.appendChild(btnDiscard);
        banner.appendChild(msg);
        banner.appendChild(btnRow);
        document.body.appendChild(banner);
    }
});

// ═══ Server Settings ═══
// Server settings loading removed - privacy is now user-controlled only via the Privacy button

// ═══ API Key Settings Modal ═══

function _applySettingsStatus(data, isNative = false) {
    // data: { tpdb: {active, source}, stashdb: {active, source} }
    for (const [key, info] of Object.entries(data)) {
        if (!info) continue;
        const badge  = document.getElementById(`settings-${key}-badge`);
        const source = document.getElementById(`settings-${key}-source`);
        const inp    = document.getElementById(`settings-${key}-key`);
        const row    = badge?.closest('.settings-key-row');
        if (!badge) continue;

        if (info.active) {
            badge.textContent = 'Active';
            badge.className   = 'settings-badge settings-badge-active';
        } else {
            badge.textContent = 'Inactive';
            badge.className   = 'settings-badge settings-badge-inactive';
        }

        if (info.source === 'env') {
            row?.classList.add('is-env-locked');
            if (inp) { inp.disabled = true; inp.placeholder = 'Managed by environment variable'; }
            if (source) {
                source.className   = 'settings-source-note note-env';
                source.textContent = isNative
                    ? '🔒 Set via environment variable — remove it from your shell to use the Settings page instead.'
                    : '🔒 Set via environment variable — edit docker-compose.yml to change.';
            }
        } else if (info.source === 'settings') {
            row?.classList.remove('is-env-locked');
            if (inp) { inp.disabled = false; inp.placeholder = 'Paste new key here to replace…'; }
            if (source) {
                source.className   = 'settings-source-note note-saved';
                source.textContent = '✓ Saved in settings — leave blank to keep current.';
            }
        } else {
            row?.classList.remove('is-env-locked');
            if (inp) { inp.disabled = false; inp.placeholder = 'Paste key here…'; }
            if (source) {
                source.className   = 'settings-source-note note-none';
                source.textContent = 'Not configured.';
            }
        }
    }
}

async function openSettingsModal() {
    const modal = document.getElementById('settings-modal');
    modal.classList.remove('hidden');

    // ── Context-aware hint text ──────────────────────────────────────────────
    // window.electronAPI is injected by the preload script only in the native
    // Electron build (deb / AppImage). In Docker / browser it is undefined.
    const isNative = !!(window.electronAPI && window.electronAPI.isElectron);
    const hint = document.getElementById('settings-hint-text');
    if (hint) {
        if (isNative) {
            hint.innerHTML =
                'Keys are saved to <code>~/.local/share/adult-media-manager/settings.json</code> ' +
                'and take effect immediately — no restart needed. ' +
                'You can also set <code>TPDB_API_KEY</code> / <code>STASHDB_API_KEY</code> ' +
                'as shell environment variables; those always take priority over the keys saved here.';
        } else {
            hint.innerHTML =
                'Keys are saved to <code>/data/settings.json</code> inside the container and take ' +
                'effect immediately — no restart needed. ' +
                'Docker users: set them as environment variables in <code>docker-compose.yml</code> ' +
                'instead (env vars take priority and cannot be overridden here).';
        }
    }

    // Clear inputs on every open for security (never pre-fill key values)
    ['settings-tpdb-key', 'settings-stashdb-key'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.value = ''; el.disabled = false; }
    });

    // Fetch current status from the server
    try {
        const resp = await fetch('/api/settings');
        if (resp.ok) {
            const data = await resp.json();
            _applySettingsStatus(data, isNative);
        }
    } catch (_) {
        // Non-fatal — badges stay in default state
    }
}

async function saveSettings() {
    const saveBtn = document.getElementById('settings-save');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';

    const tpdbVal    = document.getElementById('settings-tpdb-key')?.value.trim()    || '';
    const stashdbVal = document.getElementById('settings-stashdb-key')?.value.trim() || '';

    try {
        const resp = await fetch('/api/settings', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                tpdb_api_key:    tpdbVal    || null,
                stashdb_api_key: stashdbVal || null,
            }),
        });

        const data = await resp.json();

        if (!resp.ok) {
            const msg = data.detail || resp.statusText;
            showToast('Settings Error', msg, 'error');
            return;
        }

        // Clear the inputs after save (never keep key in DOM)
        ['settings-tpdb-key', 'settings-stashdb-key'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.value = '';
        });

        // Update badges to reflect new server state
        _applySettingsStatus({ tpdb: data.tpdb, stashdb: data.stashdb }, !!(window.electronAPI && window.electronAPI.isElectron));

        const label = data.changed && data.changed.length
            ? `${data.changed.join(' & ')} key${data.changed.length > 1 ? 's' : ''} saved and active.`
            : 'Settings saved — no new keys provided.';
        showToast('Settings Saved', label, 'success');

        document.getElementById('settings-modal').classList.add('hidden');

    } catch (err) {
        showToast('Settings Error', err.message, 'error');
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save & Apply';
    }
}

// Wire settings modal buttons (called once; modal exists in DOM at parse time)
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('settings-cancel').addEventListener('click', () => {
        document.getElementById('settings-modal').classList.add('hidden');
    });
    document.getElementById('settings-modal-close').addEventListener('click', () => {
        document.getElementById('settings-modal').classList.add('hidden');
    });
    document.getElementById('settings-save').addEventListener('click', saveSettings);

    // Show/hide toggle for each key input
    document.querySelectorAll('.settings-eye-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const inp = document.getElementById(btn.dataset.target);
            if (!inp) return;
            inp.type = inp.type === 'password' ? 'text' : 'password';
        });
    });

    // Close on backdrop click
    document.getElementById('settings-modal').addEventListener('click', (e) => {
        if (e.target === document.getElementById('settings-modal')) {
            document.getElementById('settings-modal').classList.add('hidden');
        }
    });
});

// ═══ Drag-and-Drop ═══
//
// Strategy:
//   1. Attach dragenter/dragleave/dragover/drop to the document (not just the
//      scanbar) so the full-viewport overlay intercepts drops anywhere on the page.
//   2. Use the FileSystem Entry API (webkitGetAsEntry) to distinguish files from
//      folders and to read folder paths.
//   3. Collect all unique parent directories / file paths from the drop, then
//      populate scan-path and trigger a scan — exactly as if the user had typed
//      the path and clicked Scan.
//
// Security: paths are validated server-side by _is_allowed_path() in main.py.
//   The browser never sends raw file bytes for dropped items — only the path
//   string, which the server resolves the same way as any manual path input.

(function initDragDrop() {
    const overlay  = document.getElementById('drop-overlay');
    const scanbar  = document.querySelector('.scanbar');

    // dragenter counter: increments on every child's dragenter, decrements on
    // dragleave. When it hits 0 the drag has truly left the document.
    let _dragDepth = 0;

    // Returns true only when the DataTransfer contains at least one item that
    // could be a file or directory (blocks text/url-only drags like link hovering).
    function _hasFiles(dt) {
        if (!dt) return false;
        // Modern browsers expose types list
        if (dt.types && dt.types.length) {
            return Array.from(dt.types).some(t => t === 'Files');
        }
        return dt.files && dt.files.length > 0;
    }

    document.addEventListener('dragenter', (e) => {
        if (!_hasFiles(e.dataTransfer)) return;
        e.preventDefault();
        _dragDepth++;
        if (_dragDepth === 1) {
            overlay.classList.add('drop-active');
            overlay.removeAttribute('aria-hidden');
            scanbar?.classList.add('drag-over');
        }
    });

    document.addEventListener('dragleave', (e) => {
        if (!_hasFiles(e.dataTransfer)) return;
        _dragDepth--;
        if (_dragDepth <= 0) {
            _dragDepth = 0;
            _hideDrop();
        }
    });

    document.addEventListener('dragover', (e) => {
        if (!_hasFiles(e.dataTransfer)) return;
        e.preventDefault();
        // Show copy cursor to signal the drop is actionable
        e.dataTransfer.dropEffect = 'copy';
    });

    document.addEventListener('drop', (e) => {
        e.preventDefault();
        _dragDepth = 0;
        _hideDrop();
        _handleDrop(e.dataTransfer);
    });

    // Keyboard escape cancels an active drag overlay
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && _dragDepth > 0) {
            _dragDepth = 0;
            _hideDrop();
        }
    });

    function _hideDrop() {
        overlay.classList.remove('drop-active');
        overlay.setAttribute('aria-hidden', 'true');
        scanbar?.classList.remove('drag-over');
    }

    // ── Core drop handler ────────────────────────────────────────────────────

    async function _handleDrop(dataTransfer) {
        const files = Array.from(dataTransfer.files || []);

        // In Electron: webUtils.getPathForFile() returns the real absolute path.
        // This is the only reliable way to get actual filesystem paths from drops.
        if (window.electronAPI && typeof window.electronAPI.getPathForFile === 'function' && files.length > 0) {
            const paths = files
                .map(f => window.electronAPI.getPathForFile(f))
                .filter(p => p && p.length > 0);
            if (paths.length > 0) {
                const resolved = _resolveDropPaths(paths);
                _loadPath(resolved.path, resolved.isMulti);
                return;
            }
        }

        // Browser/Docker: use the FileSystem Entry API (Chromium + Firefox 50+).
        // NOTE: entry.fullPath is a virtual path (e.g. "/video.mp4") — not a real
        // server path. We populate the field so the user can correct it.
        const items = Array.from(dataTransfer.items || []);
        const entries = items
            .map(item => (typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null))
            .filter(Boolean);

        if (entries.length > 0) {
            await _processEntries(entries);
            return;
        }

        // Fallback: plain File objects (Safari, edge cases).
        if (files.length === 0) return;
        const paths = files.map(f => f.name);
        _loadPath(paths[0], paths.length > 1);
    }

    // Walk FileSystemEntry objects (files + recursive directory trees) and
    // collect server-side paths, then load.
    async function _processEntries(entries) {
        const paths = [];   // unique absolute-ish paths to scan

        for (const entry of entries) {
            if (entry.isDirectory) {
                // For a directory we scan its full path on the server.
                // entry.fullPath is a virtual path like "/FolderName" — prepend
                // nothing; the user's scan-path value is the authoritative root.
                // We use entry.name as the leaf and rely on the existing scan-path
                // to provide the parent, OR we try to read file.path if available.
                paths.push(entry.fullPath);
            } else if (entry.isFile) {
                paths.push(entry.fullPath);
            }
        }

        if (paths.length === 0) return;

        // If everything under one common prefix → scan the common parent.
        // Otherwise → comma-separated multi-path scan (existing API behaviour).
        const resolved = _resolveDropPaths(paths);
        _loadPath(resolved.path, resolved.isMulti);
    }

    // Given a list of virtual full-paths (starting with /), decide whether to
    // scan a single common parent directory or pass multiple paths.
    function _resolveDropPaths(paths) {
        if (paths.length === 1) {
            return { path: paths[0], isMulti: false };
        }

        // Find longest common directory prefix
        const parts = paths.map(p => p.split('/').slice(0, -1));
        const common = parts[0].filter((seg, i) =>
            parts.every(arr => arr[i] === seg)
        );
        const commonDir = common.join('/') || '/';

        // If all items share a real common directory, scan that directory.
        // Prefer this over comma-list because the user probably dropped a folder.
        if (commonDir !== '/') {
            return { path: commonDir, isMulti: false };
        }

        // No useful common root → pass as comma-separated (handles /mnt/a, /mnt/b etc.)
        return { path: paths.join(','), isMulti: paths.length > 1 };
    }

    // Populate the scan-path input and optionally trigger a scan.
    function _loadPath(path, isMulti) {
        const cleanPath = path.replace(/^\/+/, '/').trim();

        // Real server/native paths start with a known directory prefix.
        // Virtual paths from the browser FileSystem API (e.g. "/VideoFile.mp4")
        // do NOT — populate the input and let the user correct it instead.
        const looksLikeServerPath =
            /^\/(mnt|media|data|downloads|organized|home|srv|nas|storage|run)\b/.test(cleanPath);

        scanPath.value = cleanPath;

        if (!looksLikeServerPath) {
            showToast(
                'Check scan path',
                'Browser reported: \u201c' + cleanPath.slice(0, 80) +
                    (cleanPath.length > 80 ? '\u2026' : '') +
                    '\u201d \u2014 edit the path above to match the actual server location, then click Scan.',
                'info',
                8000
            );
            // Don't auto-scan a path we know is wrong — let the user fix it first.
            return;
        }

        // Trigger scan — same code path as clicking the Scan button
        scanFolder();
    }
})();

// ═══ Privacy Mode ═══
function togglePrivacyMode() {
    privacyMode = !privacyMode;
    localStorage.setItem('privacyMode', privacyMode);
    updatePrivacyButton();
    applyPrivacyToThumbnails();
}

function updatePrivacyButton() {
    btnPrivacy.style.background = privacyMode 
        ? 'rgba(178, 75, 243, 0.3)' 
        : 'rgba(178, 75, 243, 0.1)';
}

function applyPrivacyToThumbnails() {
    document.querySelectorAll('.match-thumbnail img').forEach(img => {
        if (privacyMode) {
            img.classList.add('blurred');
        } else {
            img.classList.remove('blurred');
        }
    });
}

// ═══ Scan Folder ═══
async function scanFolder() {
    const path = scanPath.value.trim();
    if (!path) {
        showStatus(t('error.no_path'), 'error');
        return;
    }
    
    showStatus(t('status.scanning'));
    progressFill.style.width = '30%';
    btnScan.disabled = true;
    btnMatch.disabled = true;
    
    try {
        const response = await fetch('/api/scan', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                path: path,
                recursive: recursive.checked
            })
        });
        
        if (!response.ok) {
            let errMsg = 'Scan failed';
            try {
                const err = await response.json();
                // FastAPI 422 errors have detail as an array of validation objects
                if (Array.isArray(err.detail)) {
                    errMsg = err.detail.map(e => e.msg || JSON.stringify(e)).join('; ');
                } else if (typeof err.detail === 'string') {
                    errMsg = err.detail;
                } else if (err.detail) {
                    errMsg = JSON.stringify(err.detail);
                }
            } catch (_) {
                errMsg = await response.text().catch(() => errMsg);
            }
            throw new Error(errMsg);
        }
        
        const data = await response.json();
        scannedFiles = data.files;
        
        progressFill.style.width = '100%';
        showStatus(t('status.found', { count: data.count }), 'success');
        
        // Display files
        displayScannedFiles();
        
        // Enable match button
        btnMatch.disabled = scannedFiles.length === 0;
        
        setTimeout(() => {
            statusBar.classList.add('hidden');
            progressFill.style.width = '0%';
        }, 2000);
        
    } catch (error) {
        showStatus(t('error.rename_failed', { message: error.message }), 'error');
        progressFill.style.width = '0%';
    } finally {
        btnScan.disabled = false;
    }
}

function displayScannedFiles() {
    const newFiles      = scannedFiles.filter(f => !f.already_organized);
    const organizedFiles = scannedFiles.filter(f =>  f.already_organized);

    // Only new files are selected by default — never touch already-organised ones
    selectedScannedIndices = new Set(
        scannedFiles.map((f, i) => (!f.already_organized ? i : null)).filter(i => i !== null)
    );

    // ── Already-organised collapsed section ───────────────────────────
    const organizedSection = organizedFiles.length === 0 ? '' : `
        <details class="glass-panel" style="padding:16px;margin-bottom:12px;border:1px solid rgba(0,255,136,.25);">
            <summary style="cursor:pointer;display:flex;align-items:center;gap:10px;list-style:none;user-select:none;">
                <span style="font-size:1.1rem;">✅</span>
                <span style="font-weight:600;">Already Organised &nbsp;
                    <span class="selection-count" style="background:rgba(0,255,136,.2);padding:2px 8px;border-radius:12px;">
                        ${organizedFiles.length}
                    </span>
                </span>
                <span style="font-size:.78rem;color:var(--text-muted);margin-left:4px;">
                    — NFO sidecar found, excluded from matching by default
                </span>
                <span style="margin-left:auto;font-size:.8rem;color:var(--text-muted);">▼ expand</span>
            </summary>
            <div class="file-list" style="margin-top:12px;">
                ${organizedFiles.map((file, _) => {
                    const i = scannedFiles.indexOf(file);
                    const nfo = file.nfo_metadata || {};
                    return `
                    <div class="file-item glass-panel" id="scanned-item-${i}"
                         style="opacity:.7;border:1px solid rgba(0,255,136,.15);">
                        <label class="file-checkbox-wrap">
                            <input type="checkbox" class="file-cb" data-index="${i}"
                                   onchange="toggleScannedFile(${i}, this.checked)">
                        </label>
                        <div class="file-info">
                            <div class="file-name">${escapeHtml(file.filename)}</div>
                            <div class="file-meta">
                                ${nfo.site       ? `<span class="badge site-badge">${escapeHtml(nfo.site)}</span>` : ''}
                                ${nfo.performers && nfo.performers.length ? `<span>${nfo.performers.map(escapeHtml).join(', ')}</span>` : ''}
                                ${nfo.release_date ? `<span>${escapeHtml(nfo.release_date)}</span>` : ''}
                                ${nfo.title      ? `<span style="color:var(--text-muted);font-size:.8rem;">${escapeHtml(nfo.title)}</span>` : ''}
                            </div>
                        </div>
                        <div style="font-size:.72rem;color:rgba(0,255,136,.7);white-space:nowrap;align-self:center;">✅ has NFO</div>
                    </div>`;
                }).join('')}
            </div>
        </details>
    `;

    // ── New files section ──────────────────────────────────────────────
    const newSection = `
        <div class="glass-panel" style="padding: 20px; margin-bottom: 15px;">
            <div class="selection-header">
                <label class="select-all-label">
                    <input type="checkbox" id="select-all-scanned" ${newFiles.length > 0 ? 'checked' : ''}
                           onchange="toggleSelectAllScanned(this.checked)">
                    <span>Select All</span>
                </label>
                <h3>Scanned Files &nbsp;<span class="selection-count" id="scanned-sel-count">${newFiles.length}</span> / ${scannedFiles.length} selected</h3>
            </div>
            <div class="file-list">
                ${newFiles.length === 0
                    ? `<div style="text-align:center;padding:20px;color:var(--text-muted);">
                           All scanned files already have NFO sidecars.
                       </div>`
                    : newFiles.map((file) => {
                        const i = scannedFiles.indexOf(file);
                        return `
                        <div class="file-item glass-panel selectable" id="scanned-item-${i}">
                            <label class="file-checkbox-wrap">
                                <input type="checkbox" class="file-cb" data-index="${i}" checked
                                       onchange="toggleScannedFile(${i}, this.checked)">
                            </label>
                            <div class="file-info">
                                <div class="file-name">${escapeHtml(file.filename)}</div>
                                <div class="file-meta">
                                    ${file.site ? `<span class="badge site-badge">${escapeHtml(file.site)}</span>` : ''}
                                    ${file.performers && file.performers.length > 0 ? `<span>${file.performers.map(escapeHtml).join(', ')}</span>` : ''}
                                    ${file.quality ? `<span class="badge quality-badge">${escapeHtml(file.quality)}</span>` : ''}
                                    ${file.release_date ? `<span>${escapeHtml(file.release_date)}</span>` : ''}
                                </div>
                            </div>
                        </div>`;
                    }).join('')
                }
            </div>
        </div>
    `;

    resultsContainer.innerHTML = organizedSection + newSection;
}

// ═══ Scanned-file selection ═══
function toggleSelectAllScanned(checked) {
    if (checked) {
        // Only select files that don't have an NFO sidecar (not already organised)
        selectedScannedIndices = new Set(
            scannedFiles.map((f, i) => (!f.already_organized ? i : null)).filter(i => i !== null)
        );
    } else {
        selectedScannedIndices = new Set();
    }
    document.querySelectorAll('.file-cb').forEach(cb => {
        const idx = parseInt(cb.dataset.index, 10);
        const file = scannedFiles[idx];
        // Leave already-organised checkboxes as-is when clicking Select All
        if (!file || file.already_organized) return;
        cb.checked = checked;
    });
    _updateScannedUI();
}

function toggleScannedFile(index, checked) {
    if (checked) selectedScannedIndices.add(index);
    else         selectedScannedIndices.delete(index);
    _updateScannedUI();
}

function _updateScannedUI() {
    const sel      = selectedScannedIndices.size;
    const newTotal = scannedFiles.filter(f => !f.already_organized).length;
    const countEl   = document.getElementById('scanned-sel-count');
    const selectAll = document.getElementById('select-all-scanned');
    if (countEl)   countEl.textContent    = sel;
    if (selectAll) {
        selectAll.checked       = sel === newTotal && newTotal > 0;
        selectAll.indeterminate = sel > 0 && sel < newTotal;
    }
    // Dim un-selected rows
    scannedFiles.forEach((_, i) => {
        const row = document.getElementById(`scanned-item-${i}`);
        if (row) row.classList.toggle('unselected', !selectedScannedIndices.has(i));
    });
    btnMatch.disabled = sel === 0;
}

// ═══ Match Files ═══
async function matchFiles() {
    // Only match files the user has checked (default: all)
    const filesToMatch = selectedScannedIndices.size > 0
        ? [...selectedScannedIndices].sort((a,b) => a-b).map(i => scannedFiles[i])
        : scannedFiles;

    if (filesToMatch.length === 0) return;

    const total = filesToMatch.length;
    showStatus(t('status.matching_start', { total }));
    progressFill.style.width = '0%';
    btnMatch.disabled = true;
    btnRename.disabled = true;

    // Build ordered results array; slots filled as SSE result events arrive.
    const ordered = new Array(total).fill(null);
    let doneCount  = 0;

    const datasource = document.getElementById('datasource').value;

    // Stage 1: POST the file list to register a server-side session.
    // This avoids URL query-string size limits that would break large batches.
    let sessionId;
    try {
        const sessResp = await fetch('/api/match-session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ files: filesToMatch, datasource, auto_match: true }),
        });
        if (!sessResp.ok) {
            const err = await sessResp.json().catch(() => ({}));
            showStatus(t('error.match_failed', { message: err.detail || sessResp.statusText }), 'error');
            btnMatch.disabled = false;
            return;
        }
        sessionId = (await sessResp.json()).session_id;
    } catch (err) {
        showStatus(t('error.match_failed', { message: err.message }), 'error');
        btnMatch.disabled = false;
        return;
    }

    // Stage 2: open the SSE stream using only the opaque session token.
    await new Promise((resolve) => {
        const es = new EventSource(`/api/match-stream?session_id=${encodeURIComponent(sessionId)}`);

        es.addEventListener('progress', (e) => {
            const d = JSON.parse(e.data);
            const pct = Math.round((d.done / d.total) * 100);
            progressFill.style.width = `${pct}%`;
            // Show truncated filename so long paths don't overflow the bar
            const short = d.filename.length > 60
                ? '…' + d.filename.slice(-57)
                : d.filename;
            showStatus(t('status.matching_file', { done: d.done, total: d.total, filename: short }));
        });

        es.addEventListener('result', (e) => {
            const d = JSON.parse(e.data);
            ordered[d.index] = d.match;
            doneCount++;
        });

        es.addEventListener('done', (e) => {
            es.close();
            const d = JSON.parse(e.data);
            matchedResults = ordered.filter(Boolean);
            progressFill.style.width = '100%';
            showStatus(
                t('status.matched', { matched: d.matched, total: d.total }),
                'success'
            );
            displayMatches();
            btnRename.disabled = matchedResults.filter(m => m.match).length === 0;
            setTimeout(() => {
                statusBar.classList.add('hidden');
                progressFill.style.width = '0%';
            }, 2000);
            btnMatch.disabled = false;
            resolve();
        });

        es.addEventListener('error', (e) => {
            es.close();
            // EventSource fires a generic error event on connection failure;
            // our server may also emit a structured error event.
            let msg = 'Match failed — connection error';
            if (e.data) {
                try { msg = JSON.parse(e.data).detail || msg; } catch { /* ignore */ }
            }
            showStatus(t('error.match_failed', { message: msg }), 'error');
            progressFill.style.width = '0%';
            btnMatch.disabled = false;
            btnRename.disabled = matchedResults.filter(m => m.match).length === 0;
            resolve();
        });
    });
}

// ═══ Write NFO ═══
async function writeNfo(index) {
    const result = matchedResults[index];
    if (!result || !result.match) return;

    const btn = document.getElementById(`nfo-btn-${index}`);
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Writing…'; }

    try {
        const resp = await fetch('/api/write-nfo', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_path: result.original.path,
                scene_data: result.match,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        if (btn) { btn.textContent = '✅ NFO Written'; btn.style.color = 'var(--success)'; }
    } catch (err) {
        if (btn) { btn.disabled = false; btn.textContent = '📄 Write NFO'; }
        showStatus(`NFO write failed: ${escapeHtml(err.message)}`, 'error');
    }
}

async function writeAllNfos() {
    const toWrite = matchedResults
        .map((r, i) => ({ r, i }))
        .filter(({ r }) => r && r.match);

    if (toWrite.length === 0) return;

    const allBtn = document.getElementById('write-all-nfo-btn');
    if (allBtn) { allBtn.disabled = true; allBtn.textContent = '⏳ Writing…'; }

    showStatus(`Writing NFOs for ${toWrite.length} files…`);
    progressFill.style.width = '0%';

    let done = 0;
    for (const { r, i } of toWrite) {
        try {
            const resp = await fetch('/api/write-nfo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ file_path: r.original.path, scene_data: r.match }),
            });
            const data = await resp.json();
            if (resp.ok) {
                const btn = document.getElementById(`nfo-btn-${i}`);
                if (btn) { btn.textContent = '✅ NFO Written'; btn.style.color = 'var(--success)'; btn.disabled = true; }
            }
        } catch (_) { /* skip individual errors; per-button state shows result */ }
        done++;
        progressFill.style.width = `${Math.round((done / toWrite.length) * 100)}%`;
    }

    if (allBtn) { allBtn.disabled = false; allBtn.textContent = '✅ All NFOs Written'; allBtn.style.color = 'var(--success)'; }
    showStatus(`NFOs written for ${done} / ${toWrite.length} files.`, 'success');
    setTimeout(() => { statusBar.classList.add('hidden'); progressFill.style.width = '0%'; }, 3000);
}

function displayMatches() {
    // Default: all matched items selected
    selectedMatchIndices = new Set(
        matchedResults.map((r, i) => r.match ? i : null).filter(i => i !== null)
    );
    const matchedCount = matchedResults.filter(m => m.match).length;

    resultsContainer.innerHTML = `
        <div class="glass-panel" style="padding: 20px; margin-bottom: 15px;">
            <div class="selection-header">
                <label class="select-all-label">
                    <input type="checkbox" id="select-all-matches" ${matchedCount > 0 ? 'checked' : ''}
                           onchange="toggleSelectAllMatches(this.checked)">
                    <span>Select All</span>
                </label>
                <h3>Matched Scenes &nbsp;<span class="selection-count" id="match-sel-count">${matchedCount}</span> / ${matchedResults.length} selected</h3>
                <button class="glass-btn" id="write-all-nfo-btn" style="font-size:12px;margin-left:auto;" onclick="writeAllNfos()">📄 Write All NFOs</button>
            </div>
        </div>
        ${matchedResults.map((result, index) => {
            if (!result.match) {
                return `
                    <div class="match-item glass-panel unselected" id="match-item-${index}" style="border: 1px solid rgba(255, 71, 87, 0.3);">
                        <div class="match-checkbox-wrap">
                            <input type="checkbox" class="match-cb" data-index="${index}" disabled>
                        </div>
                        <div class="match-info">
                            <div class="scene-title">❌ ${t('match.no_match')}</div>
                            <div class="file-name" style="color: var(--text-muted); margin-bottom: 10px;">${escapeHtml(result.original.filename)}</div>
                            <button class="glass-btn" style="font-size: 12px;" onclick='openManualEditModal(${JSON.stringify(result.original).replace(/'/g, "&#39;")})'>✏️ Edit Manually</button>
                        </div>
                    </div>
                `;
            }

            return `
                <div class="match-item glass-panel selectable" id="match-item-${index}" data-index="${index}">
                    <div class="match-checkbox-wrap">
                        <input type="checkbox" class="match-cb" data-index="${index}" checked
                               onchange="toggleMatchFile(${index}, this.checked)">
                    </div>
                    ${result.match.thumbnail_url ? `
                        <div class="match-thumbnail">
                            <img src="${escapeHtml(result.match.thumbnail_url)}"
                                 alt="${escapeHtml(result.match.title)}"
                                 class="${privacyMode ? 'blurred' : ''}"
                                 onclick="this.classList.toggle('blurred')">
                        </div>
                    ` : ''}
                    <div class="match-info">
                        <div class="scene-title">${escapeHtml(result.match.title)}${result.match.manual_entry ? ' <span style="font-size: 11px; color: var(--accent);">(Manual)</span>' : ''}</div>
                        <div class="scene-meta">
                            <span class="badge site-badge">${escapeHtml(result.match.site)}</span>
                            ${result.match.performers ? `<span class="performers">${result.match.performers.map(escapeHtml).join(', ')}</span>` : ''}
                            ${result.match.release_date ? `<span class="date">${escapeHtml(result.match.release_date)}</span>` : ''}
                        </div>
                        ${result.match.tags && result.match.tags.length > 0 ? `
                            <div class="scene-tags">
                                ${result.match.tags.slice(0, 5).map(tag => `<span class="tag">${escapeHtml(tag)}</span>`).join('')}
                            </div>
                        ` : ''}
                        <div class="file-name" style="margin-top: 10px; color: var(--text-muted); font-size: 12px;">
                            Original: ${escapeHtml(result.original.filename)}
                        </div>
                    </div>
                    <div class="match-confidence">
                        <span style="font-size: 12px; color: var(--text-secondary); display: block; margin-bottom: 8px;">${result.confidence}% match</span>
                        <div class="confidence-bar" style="--confidence: ${result.confidence}%"></div>
                        <button class="glass-btn" style="font-size: 11px; margin-top: 10px; width: 100%;" onclick='openManualEditModal(${JSON.stringify(result.original).replace(/'/g, "&#39;")})'>✏️ Edit</button>
                        <button class="glass-btn" id="nfo-btn-${index}" style="font-size: 11px; margin-top: 6px; width: 100%;" onclick="writeNfo(${index})">📄 Write NFO</button>
                    </div>
                </div>
            `;
        }).join('')}
    `;

    applyPrivacyToThumbnails();
    _updateMatchUI();
}

// ═══ Matched-file selection ═══
function toggleSelectAllMatches(checked) {
    if (checked) {
        selectedMatchIndices = new Set(
            matchedResults.map((r, i) => r.match ? i : null).filter(i => i !== null)
        );
    } else {
        selectedMatchIndices = new Set();
    }
    document.querySelectorAll('.match-cb:not([disabled])').forEach(cb => cb.checked = checked);
    _updateMatchUI();
}

function toggleMatchFile(index, checked) {
    if (checked) selectedMatchIndices.add(index);
    else         selectedMatchIndices.delete(index);
    _updateMatchUI();
}

function _updateMatchUI() {
    const sel         = selectedMatchIndices.size;
    const matchedTotal= matchedResults.filter(r => r.match).length;
    const countEl     = document.getElementById('match-sel-count');
    const selectAll   = document.getElementById('select-all-matches');
    if (countEl)   countEl.textContent    = sel;
    if (selectAll) {
        selectAll.checked       = sel === matchedTotal;
        selectAll.indeterminate = sel > 0 && sel < matchedTotal;
    }
    // Dim un-selected rows
    matchedResults.forEach((r, i) => {
        if (!r.match) return;
        const row = document.getElementById(`match-item-${i}`);
        if (row) row.classList.toggle('unselected', !selectedMatchIndices.has(i));
    });
    btnRename.disabled = sel === 0;
}

// ═══ Rename Files ═══
async function renameFiles() {
    if (matchedResults.length === 0) return;

    // Only process user-checked matches (default: all matched)
    const indicesToRename = selectedMatchIndices.size > 0
        ? [...selectedMatchIndices].sort((a, b) => a - b)
        : matchedResults.map((r, i) => r.match ? i : null).filter(i => i !== null);

    const operations = indicesToRename
        .map(i => matchedResults[i])
        .filter(r => r && r.match)
        .map(r => ({
            old_path: r.original.path,
            scene_data: r.match,
            file_data: r.original,
            template: template.value,
            flat: flatRename.checked
        }));
    
    if (operations.length === 0) {
        showStatus(t('status.no_rename'), 'error');
        return;
    }
    
    const actionType = action.value;

    if (actionType === 'test') {
        // Test mode: just run and display — no pre-flight needed
        _doRename(operations, actionType);
        return;
    }

    // Non-test: run a silent test pass first so the user sees every
    // From → To path before committing an irreversible file-system op.
    showStatus(t('status.building_preview'));
    progressFill.style.width = '25%';
    btnRename.disabled = true;

    // §4.3 — fast template validation before the heavier full-file test run.
    // Call /api/preview-paths with the first 5 ops (pure computation, no I/O).
    // Bail immediately with an actionable message if the template is broken.
    try {
        const pvRes = await fetch('/api/preview-paths', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ operations: operations.slice(0, 5) })
        });
        if (pvRes.ok) {
            const pvData = await pvRes.json();
            // Hard-block only when the template produces an empty filename component
            // (truly broken template).  same_as_source just means the file is already
            // at the correct destination — those are silently skipped during the rename.
            const degenerate = pvData.previews.find(p => p.degenerate);
            if (degenerate) {
                showStatus('Template error: destination filename is empty — edit the template before renaming.', 'error');
                progressFill.style.width = '0%';
                btnRename.disabled = false;
                return;
            }
            // Warn (but don't block) if every sampled file is already at its destination.
            const allSame = pvData.previews.length > 0 && pvData.previews.every(p => p.same_as_source);
            if (allSame) {
                showStatus('Note: sampled files appear to already be at the correct destination.', 'info');
            }
        }
        // If the endpoint is unreachable, fall through to the full test pass.
    } catch (_pvErr) { /* non-fatal */ }

    progressFill.style.width = '40%';

    try {
        const res = await fetch('/api/rename', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ operations, action: 'test' })
        });
        if (!res.ok) throw new Error((await res.json()).detail || 'Preview failed');
        const preview = await res.json();
        _showRenamePreviewModal(preview.results, operations, actionType);
    } catch (err) {
        showStatus(t('error.preview', { message: err.message }), 'error');
    } finally {
        progressFill.style.width = '0%';
        btnRename.disabled = false;
        statusBar.classList.add('hidden');
    }
}

/**
 * Show the rename-preview modal.
 * Lists every From→To path (and any errors from the test pass).
 * If the user clicks Proceed, fires _doRename with the real action.
 */
function _showRenamePreviewModal(testResults, operations, actionType) {
    const modal     = document.getElementById('preview-modal');
    const list      = document.getElementById('preview-modal-list');
    const summary   = document.getElementById('preview-modal-summary');
    const btnOk     = document.getElementById('preview-modal-confirm');
    const btnCancel = document.getElementById('preview-modal-cancel');
    const btnClose  = document.getElementById('preview-modal-close');

    // Partition results into three buckets:
    //  • skips  — old_path === new_path (already at destination, no-op)
    //  • moves  — success and actually changing path
    //  • errors — failed
    const skips  = testResults.filter(r =>  r.success && r.old_path === r.new_path);
    const moves  = testResults.filter(r =>  r.success && r.old_path !== r.new_path);
    const errors = testResults.filter(r => !r.success);

    const parts = [];
    if (moves.length)  parts.push(`${moves.length} file${moves.length  !== 1 ? 's' : ''} will be ${actionType}d`);
    if (errors.length) parts.push(`${errors.length} will fail (see below)`);
    if (skips.length)  parts.push(`${skips.length} already at destination — will skip`);
    summary.textContent = parts.join(' · ') || 'Nothing to do.';

    // Only render errors and actual moves inline; collapse skips into a
    // single summary line so they don't drown out the important rows.
    const renderRow = (r) => {
        const samePlace = r.old_path === r.new_path;
        if (samePlace) return ''; // rendered separately below
        const colour = r.success ? 'rgba(0,255,136,.25)' : 'rgba(255,71,87,.35)';
        return `
            <div style="border:1px solid ${colour};border-radius:8px;padding:.6rem .9rem;font-size:.8rem;line-height:1.5">
                <div style="color:var(--text-muted);margin-bottom:.2rem">
                    ${r.success ? '✅' : '❌'} From: <span style="color:var(--text)">${escapeHtml(r.old_path)}</span>
                </div>
                ${r.new_path ? `<div>→ To: <strong>${escapeHtml(r.new_path)}</strong></div>` : ''}
                ${r.error    ? `<div style="color:var(--error);margin-top:.2rem">⚠ ${escapeHtml(r.error)}</div>` : ''}
            </div>`;
    };

    let html = testResults.map(renderRow).join('');

    if (skips.length) {
        html += `
            <div style="border:1px solid rgba(255,165,0,.3);border-radius:8px;padding:.6rem .9rem;
                        font-size:.8rem;color:var(--text-muted);line-height:1.5">
                ⏭ ${skips.length} file${skips.length !== 1 ? 's are' : ' is'} already named exactly
                as the selected template would produce — nothing to rename.<br>
                <strong style="color:var(--text)">Choose a different template</strong> to give them a new name.
                For example: <code>{site}.{scene}.{quality}</code> or <code>{site} - {performer} - {scene}</code>.
            </div>`;
    }

    list.innerHTML = html;
    modal.classList.remove('hidden');
    btnOk.focus();

    // Disable Proceed if there's nothing actionable (only skips / only errors).
    btnOk.disabled = moves.length === 0;

    // Build the operations list that _doRename will actually execute —
    // exclude same-path entries so we don't make pointless API calls.
    const actionableOps = operations.filter(op => {
        const match = testResults.find(r => r.old_path === op.old_path);
        return !match || match.old_path !== match.new_path;
    });

    function close() {
        modal.classList.add('hidden');
        btnOk.disabled = false;
        btnOk.removeEventListener('click', handleOk);
        btnCancel.removeEventListener('click', close);
        btnClose.removeEventListener('click', close);
    }
    function handleOk() {
        close();
        _doRename(actionableOps, actionType);
    }

    btnOk.addEventListener('click', handleOk);
    btnCancel.addEventListener('click', close);
    btnClose.addEventListener('click', close);
}

// ── §4.4 Chunked-rename helpers ──────────────────────────────────────────────
// Queue key used for localStorage persistence so a page refresh can resume.
const RENAME_QUEUE_KEY = 'amm_rename_queue';
const CHUNK_SIZE       = 10;
const LARGE_BATCH      = 20;

/**
 * Persist the remaining operations to localStorage so the user can resume
 * after a page refresh.  Cleared automatically when the queue drains.
 * @param {Array}  remaining  - operations not yet attempted
 * @param {string} actionType - 'move'|'copy'|'hardlink'
 */
function _saveRenameQueue(remaining, actionType) {
    if (remaining.length === 0) {
        localStorage.removeItem(RENAME_QUEUE_KEY);
    } else {
        localStorage.setItem(RENAME_QUEUE_KEY, JSON.stringify({ operations: remaining, actionType }));
    }
}

/** Return a saved resume queue, or null. */
function _loadRenameQueue() {
    try {
        const raw = localStorage.getItem(RENAME_QUEUE_KEY);
        return raw ? JSON.parse(raw) : null;
    } catch { return null; }
}

/**
 * Send a single chunk to /api/rename and return the parsed response data.
 * Throws on HTTP errors so the caller can decide whether to continue.
 */
async function _sendChunk(chunk, actionType) {
    const res = await fetch('/api/rename', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ operations: chunk, action: actionType })
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
}

/**
 * Core rename dispatcher.  For batches > LARGE_BATCH, operations are split
 * into CHUNK_SIZE chunks processed sequentially with per-chunk progress.
 * Failures in one chunk do not abort subsequent chunks.  The remaining
 * queue is persisted to localStorage so a page refresh can resume.
 *
 * For test-mode and small batches the original single-request path is used.
 */
async function _doRename(operations, actionType) {
    btnRename.disabled = true;

    // ── Test mode or small batch: single request, original behaviour ──────────
    if (actionType === 'test' || operations.length <= LARGE_BATCH) {
        showStatus(t(actionType === 'test' ? 'status.previewing' : 'status.renaming'));
        progressFill.style.width = '70%';
        try {
            const data = await _sendChunk(operations, actionType);
            const successful = data.results.filter(r => r.success).length;
            progressFill.style.width = '100%';
            showStatus(t(actionType === 'test' ? 'status.preview_result' : 'status.renamed_result', { success: successful, total: operations.length }), 'success');
            displayRenameResults(data.results);
            if (actionType !== 'test') {
                _applyRenameResults(data.results, data.embed_job_id);
            } else {
                setTimeout(() => { statusBar.classList.add('hidden'); progressFill.style.width = '0%'; }, 3000);
            }
        } catch (error) {
            showStatus(t('error.rename_failed', { message: error.message }), 'error');
            progressFill.style.width = '0%';
        } finally {
            btnRename.disabled = false;
        }
        return;
    }

    // ── Large batch: chunked processing with per-chunk progress ───────────────
    // Persist full queue so a refresh can resume from where we left off.
    _saveRenameQueue(operations, actionType);

    const total         = operations.length;
    const chunks        = [];
    for (let i = 0; i < total; i += CHUNK_SIZE) chunks.push(operations.slice(i, i + CHUNK_SIZE));

    let allResults     = [];
    let processed      = 0;
    let lastEmbedJobId = null;

    for (let ci = 0; ci < chunks.length; ci++) {
        const chunk = chunks[ci];
        showStatus(t('status.renaming_chunk', { chunk: ci + 1, total_chunks: chunks.length, done: processed + chunk.length, total }));
        progressFill.style.width = `${Math.round(((ci) / chunks.length) * 100)}%`;

        try {
            const data = await _sendChunk(chunk, actionType);
            allResults = allResults.concat(data.results);
            if (data.embed_job_id) lastEmbedJobId = data.embed_job_id;

            // Prune the persisted queue after each successful chunk
            const remaining = operations.slice(processed + chunk.length);
            _saveRenameQueue(remaining, actionType);

        } catch (err) {
            // Chunk network/server error: mark every file in this chunk as failed
            // and continue with the rest — do not abort the whole batch.
            chunk.forEach(op => allResults.push({
                success:   false,
                old_path:  op.old_path,
                new_path:  null,
                action:    actionType,
                error:     err.message,
                embed_warning: null,
            }));
        }
        processed += chunk.length;
    }

    // All chunks done — queue is exhausted
    _saveRenameQueue([], actionType);

    const successful = allResults.filter(r => r.success).length;
    progressFill.style.width = '100%';
    showStatus(t('status.renamed_total', { success: successful, total }), 'success');
    displayRenameResults(allResults);
    _applyRenameResults(allResults, lastEmbedJobId);
    btnRename.disabled = false;
}

/**
 * Post-rename bookkeeping shared by both the single-request and chunked paths:
 * - prune successful files from matchedResults
 * - append a "Show remaining" button if files are still outstanding
 * - start embed-status polling if a Phase-2 job was returned
 */
function _applyRenameResults(results, embedJobId) {
    const successfulPaths = new Set(
        results.filter(r => r.success).map(r => r.old_path)
    );
    matchedResults = matchedResults.filter(r => !successfulPaths.has(r.original.path));
    selectedMatchIndices = new Set();

    if (embedJobId) {
        _pollEmbedStatus(embedJobId, results.length);
        return; // status bar + unmatched panel managed by the poller
    }

    // No embed job — surface unmatched files immediately
    _showUnmatchedPanel();

    if (matchedResults.length === 0) {
        setTimeout(() => { statusBar.classList.add('hidden'); progressFill.style.width = '0%'; }, 3000);
    }
}

/**
 * Render unmatched files in a prominent panel prepended to resultsContainer
 * and scroll to it so the user sees them without any manual action.
 * Called after rename (no embed) and after embed polling completes.
 */
function _showUnmatchedPanel() {
    if (matchedResults.length === 0) return;

    // Remove any previously injected unmatched panel to avoid duplicates.
    const old = document.getElementById('unmatched-panel');
    if (old) old.remove();

    const remaining = matchedResults.length;
    const panel = document.createElement('div');
    panel.id = 'unmatched-panel';
    panel.className = 'glass-panel';
    panel.style.cssText = 'padding:16px;margin-bottom:16px;border:1px solid rgba(255,71,87,.4);';

    // Header row with count + "Continue editing" button
    const header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;';
    header.innerHTML = `
        <h3 style="margin:0;color:var(--error);">⚠ ${remaining} unmatched file${remaining !== 1 ? 's' : ''} — action required</h3>
        <button class="glass-btn btn-primary" onclick="displayMatches();statusBar.classList.add('hidden');document.getElementById('unmatched-panel')?.remove();"
                style="font-size:12px;">✏️ Edit &amp; Rename Remaining</button>
    `;
    panel.appendChild(header);

    // List each unmatched file with inline Edit Manually button
    const list = document.createElement('div');
    list.style.cssText = 'display:flex;flex-direction:column;gap:6px;';
    matchedResults.forEach((r) => {
        const item = document.createElement('div');
        item.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:rgba(255,71,87,.06);border-radius:8px;gap:12px;';
        item.innerHTML = `
            <span style="font-size:.82rem;color:var(--text-muted);word-break:break-all;flex:1;">${escapeHtml(r.original.filename)}</span>
            <button class="glass-btn" style="font-size:11px;white-space:nowrap;flex-shrink:0;"
                    onclick='openManualEditModal(${JSON.stringify(r.original).replace(/'/g, "&#39;")})'>✏️ Edit Manually</button>
        `;
        list.appendChild(item);
    });
    panel.appendChild(list);

    // Prepend above the rename results so it's immediately visible
    resultsContainer.prepend(panel);

    // Scroll the panel into view smoothly
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/**
 * Poll /api/embed-status/{jobId} every 2 s until complete, then append
 * any embed warnings to the already-rendered results panel.
 *
 * @param {string} jobId   - The hex job id returned by /api/rename
 * @param {number} total   - Total files in the batch (for the status label)
 */
async function _pollEmbedStatus(jobId, total) {
    const INTERVAL_MS = 2000;
    const MAX_POLLS   = 300;  // 10 minutes safety cap (300 × 2 s)
    let polls = 0;

    async function tick() {
        polls++;
        try {
            const res = await fetch(`/api/embed-status/${encodeURIComponent(jobId)}`);
            if (!res.ok) {
                // 404 = job expired or invalid; stop silently
                _finishEmbedPolling(null);
                return;
            }
            const job = await res.json();
            showStatus(
                t('status.embedding', { done: job.done, total: job.total }),
                'info'
            );
            progressFill.style.width =
                job.total > 0 ? `${Math.round((job.done / job.total) * 100)}%` : '100%';

            if (job.complete || polls >= MAX_POLLS) {
                _finishEmbedPolling(job.warnings);
            } else {
                setTimeout(tick, INTERVAL_MS);
            }
        } catch {
            // Network error — stop polling, don't break the UI
            _finishEmbedPolling(null);
        }
    }

    setTimeout(tick, INTERVAL_MS);
}

/**
 * Called when embed polling ends. Appends warnings to the results panel
 * and resets the status bar.
 *
 * @param {Array|null} warnings  - Array of {path, warning} or null
 */
function _finishEmbedPolling(warnings) {
    statusBar.classList.add('hidden');
    progressFill.style.width = '0%';

    // Surface unmatched files at the top so user sees them without scrolling
    _showUnmatchedPanel();

    if (!warnings || warnings.length === 0) return;

    // Append a warning section below the existing results panel
    const extra = document.createElement('div');
    extra.className = 'glass-panel';
    extra.style.cssText = 'padding:16px;margin-top:12px;border:1px solid rgba(240,165,0,.35)';
    extra.innerHTML = `
        <h4 style="margin:0 0 10px;color:var(--warning,#f0a500)">
            ⚠ Metadata embedding warnings (${warnings.length})
        </h4>
        <div style="display:flex;flex-direction:column;gap:6px">
            ${warnings.map(w => `
                <div style="font-size:.8rem;line-height:1.5">
                    <span style="color:var(--text-muted)">${escapeHtml(w.path)}</span><br>
                    <span style="color:var(--warning,#f0a500)">${escapeHtml(w.warning)}</span>
                </div>
            `).join('')}
        </div>
    `;
    resultsContainer.appendChild(extra);
}

function displayRenameResults(results) {
    resultsContainer.innerHTML = `
        <div class="glass-panel" style="padding: 20px;">
            <h3 style="margin-bottom: 15px;">Rename Results</h3>
            <div class="file-list">
                ${results.map(result => `
                    <div class="file-item glass-panel" style="border: 1px solid ${result.success ? 'rgba(0, 255, 136, 0.3)' : 'rgba(255, 71, 87, 0.3)'}; padding: 12px;">
                        <div class="file-info">
                            <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 4px;">
                                ${result.success ? '✅' : '❌'} ${escapeHtml(result.action.toUpperCase())}
                            </div>
                            <div class="file-name" style="color: var(--text-muted);">From: ${escapeHtml(result.old_path)}</div>
                            ${result.new_path ? `<div class="file-name">To: ${escapeHtml(result.new_path)}</div>` : ''}
                            ${result.error ? `<div style="color: var(--error); font-size: 12px; margin-top: 4px;">${escapeHtml(result.error)}</div>` : ''}
                            ${result.embed_warning ? `<div style="color: var(--warning, #f0a500); font-size: 11px; margin-top: 4px;">⚠️ ${escapeHtml(result.embed_warning)}</div>` : ''}
                        </div>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

// ═══ Browse Modal ═══
async function openBrowseModal() {
    browseModal.classList.remove('hidden');
    // Derive a directory to start from. If scanPath contains files or a
    // comma-separated list, walk up to the parent directory so the browse
    // API never receives a file path.
    let startPath = (scanPath.value || '').split(',')[0].trim() || '/mnt';
    const lastSlash = startPath.lastIndexOf('/');
    const lastName  = startPath.substring(lastSlash + 1);
    if (lastName.includes('.')) {
        // Looks like a filename — use its parent directory
        startPath = startPath.substring(0, lastSlash) || '/mnt';
    }
    currentBrowsePath = startPath || '/mnt';
    browseHistory = [];
    selectedFiles = [];
    updateBrowseSelectionMode(browseSelectionMode);
    loadBrowseDirectory(currentBrowsePath);
}

async function loadBrowseDirectory(path) {
    document.getElementById('browse-path').textContent = path;
    document.getElementById('browse-list').innerHTML = '<div style="text-align:center;padding:20px;">Loading...</div>';
    
    try {
        const response = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
        if (!response.ok) throw new Error('Failed to browse');
        
        const data = await response.json();
        currentBrowsePath = data.path;
        
        document.getElementById('browse-path').textContent = data.path;
        
        const directories = data.items.filter(item => item.type === 'directory');
        const files = data.items.filter(item => item.type === 'file');

        // Store paths in global arrays — inline onclick references index only,
        // so filenames with ( ) ! ' and other special chars never break the JS parser.
        window._browseDirs  = directories.map(d => d.path);
        window._browsePaths = files.map(f => f.path);

        let html = '';

        // Show directories
        html += directories.map((item, i) => `
            <div class="browse-item directory" onclick="navigateDir(_browseDirs[${i}])">📁 ${escapeHtml(item.name)}</div>
        `).join('');

        // Show files if in file selection mode
        if (browseSelectionMode === 'files') {
            html += files.map((item, i) => {
                const isSelected = selectedFiles.some(f => f.path === item.path);
                return `
                    <div class="browse-item file ${isSelected ? 'selected' : ''}" data-fidx="${i}"
                         onclick="toggleFileSelection(_browsePaths[${i}])">
                        <input type="checkbox" ${isSelected ? 'checked' : ''}
                               onclick="event.stopPropagation(); toggleFileSelection(_browsePaths[${i}])">
                        📄 ${escapeHtml(item.name)}
                    </div>
                `;
            }).join('');
        }
        
        document.getElementById('browse-list').innerHTML = html || '<div style="text-align:center;padding:20px;color:var(--text-muted);">Empty folder</div>';
        updateSelectionCounter();
        updateBrowseBackBtn();
        
    } catch (error) {
        document.getElementById('browse-list').innerHTML = `<div style="color:var(--error);text-align:center;padding:20px;">Error: ${escapeHtml(error.message)}</div>`;
    }
}

function navigateDir(path) {
    browseHistory.push(currentBrowsePath);
    loadBrowseDirectory(path);
}

function browseBack() {
    if (browseHistory.length === 0) return;
    const prev = browseHistory.pop();
    loadBrowseDirectory(prev);
}

function updateBrowseBackBtn() {
    const btn = document.getElementById('browse-back');
    if (btn) btn.disabled = browseHistory.length === 0;
}

function updateBrowseSelectionMode(mode) {
    browseSelectionMode = mode;
    document.getElementById('browse-mode-folder').classList.toggle('active', mode === 'folder');
    document.getElementById('browse-mode-files').classList.toggle('active', mode === 'files');
    
    const selectBtn = document.getElementById('browse-select');
    if (mode === 'folder') {
        selectBtn.textContent = t('modal.browse_select_folder_btn') || 'Select Folder';
        selectedFiles = [];
    } else {
        selectBtn.textContent = t('modal.browse_select_files_btn') || 'Select Files';
    }
    
    // Reload current directory to show/hide files
    if (currentBrowsePath) {
        loadBrowseDirectory(currentBrowsePath);
    }
}

function toggleFileSelection(filePath) {
    const index = selectedFiles.findIndex(f => f.path === filePath);
    if (index > -1) {
        selectedFiles.splice(index, 1);
    } else {
        selectedFiles.push({ path: filePath });
    }

    // Update UI via data-fidx attribute (safe with any filename)
    const fidx = (window._browsePaths || []).indexOf(filePath);
    if (fidx !== -1) {
        const fileItem = document.querySelector(`.browse-item.file[data-fidx="${fidx}"]`);
        if (fileItem) {
            const selected = selectedFiles.some(f => f.path === filePath);
            fileItem.classList.toggle('selected', selected);
            const checkbox = fileItem.querySelector('input[type="checkbox"]');
            if (checkbox) checkbox.checked = selected;
        }
    }

    updateSelectionCounter();
}

function updateSelectionCounter() {
    const counter = document.getElementById('selection-counter');
    if (browseSelectionMode === 'files' && selectedFiles.length > 0) {
        counter.textContent = `${selectedFiles.length} file${selectedFiles.length > 1 ? 's' : ''} selected`;
        counter.style.display = 'block';
    } else {
        counter.style.display = 'none';
    }
}

function selectBrowseFolder() {
    if (browseSelectionMode === 'folder') {
        scanPath.value = currentBrowsePath;
        browseModal.classList.add('hidden');
    } else if (browseSelectionMode === 'files' && selectedFiles.length > 0) {
        // Set scan path and auto-scan immediately — no extra click needed
        scanPath.value = selectedFiles.map(f => f.path).join(',');
        browseModal.classList.add('hidden');
        scanFolder();   // ← auto-trigger
    } else {
        showStatus(t('status.browse_select_required'), 'error');
    }
}

// ═══ History Modal ═══
async function openHistoryModal() {
    historyModal.classList.remove('hidden');
    loadHistory();
}

async function loadHistory() {
    document.getElementById('history-list').innerHTML = '<div style="text-align:center;padding:20px;">Loading...</div>';
    
    try {
        const response = await fetch('/api/history?limit=50');
        if (!response.ok) throw new Error('Failed to load history');
        
        const data = await response.json();
        
        if (data.entries.length === 0) {
            document.getElementById('history-list').innerHTML = `<div style="text-align:center;padding:20px;color:var(--text-muted);">${t('history.no_entries')}</div>`;
            return;
        }
        
        document.getElementById('history-list').innerHTML = data.entries.map(entry => `
            <div class="history-item">
                <div class="history-time">${escapeHtml(entry.timestamp)} - ${escapeHtml(entry.action.toUpperCase())}</div>
                <div class="history-path">From: ${escapeHtml(entry.old_path)}</div>
                <div class="history-path">To: ${escapeHtml(entry.new_path)}</div>
                ${entry.error ? `<div style="color:var(--error);font-size:11px;margin-top:4px;">${escapeHtml(entry.error)}</div>` : ''}
            </div>
        `).join('');
        
    } catch (error) {
        document.getElementById('history-list').innerHTML = `<div style="color:var(--error);text-align:center;padding:20px;">Error: ${escapeHtml(error.message)}</div>`;
    }
}

async function undoLastRename() {
    showConfirmModal('Undo the last rename operation?', async () => {
        try {
            const response = await fetch('/api/history/undo', {method: 'POST'});
            if (!response.ok) throw new Error('Failed to undo');
            
            const data = await response.json();
            if (data.success) {
                alert('Rename undone successfully!');
                loadHistory();
            } else {
                alert(data.error || 'No operations to undo');
            }
            
        } catch (error) {
            alert(`Error: ${error.message}`);
        }
    });
}

// ═══ Manual Edit Modal ═══
function openManualEditModal(fileData) {
    currentManualFile = fileData;
    manualPerformers = [];
    manualTags = [];
    generatedThumbnails = [];
    selectedThumbnailIndex = null;
    
    // Populate form
    document.getElementById('manual-edit-filename').textContent = fileData.filename;
    document.getElementById('manual-title').value = fileData.scene_title || fileData.clean_name || '';
    document.getElementById('manual-site').value = fileData.site || '';
    document.getElementById('manual-date').value = fileData.release_date || '';
    document.getElementById('manual-quality').value = fileData.quality || '';
    
    // Clear lists
    document.getElementById('manual-performers-list').innerHTML = '';
    document.getElementById('manual-tags-list').innerHTML = '';
    document.getElementById('manual-performer-input').value = '';
    document.getElementById('manual-tag-input').value = '';
    
    // Pre-populate performers if available
    if (fileData.performers && fileData.performers.length > 0) {
        fileData.performers.forEach(p => {
            manualPerformers.push(p);
            addChipToList('performers', p);
        });
    }
    
    // Show thumbnails section
    document.getElementById('manual-thumbnails-section').style.display = 'block';
    document.getElementById('manual-thumbnails-grid').innerHTML = '';
    
    manualEditModal.classList.remove('hidden');
}

function addManualPerformer() {
    const input = document.getElementById('manual-performer-input');
    const name = input.value.trim();
    
    if (name && !manualPerformers.includes(name)) {
        manualPerformers.push(name);
        addChipToList('performers', name);
        input.value = '';
    }
}

function addManualTag() {
    const input = document.getElementById('manual-tag-input');
    const tag = input.value.trim();
    
    if (tag && !manualTags.includes(tag)) {
        manualTags.push(tag);
        addChipToList('tags', tag);
        input.value = '';
        // Persist new user-created tags to the server
        fetch('/api/tags', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({tag})
        }).then(() => { allTags = []; }); // reset cache so next load gets fresh list
    }
    document.getElementById('tag-suggestions').style.display = 'none';
}

// ─── Tag autocomplete ───
let allTags = [];

async function loadTags() {
    if (allTags.length) return;
    try {
        const res = await fetch('/api/tags');
        const data = await res.json();
        allTags = data.tags || [];
    } catch (_) {}
}

function filterTagSuggestions(query) {
    const ul = document.getElementById('tag-suggestions');
    const input = document.getElementById('manual-tag-input');
    const q = query.trim().toLowerCase();
    
    loadTags().then(() => {
        const matches = allTags.filter(t =>
            !manualTags.includes(t) &&
            (q === '' || t.toLowerCase().includes(q))
        ).slice(0, 20);

        const items = [...matches];
        let html = items.map(t =>
            `<li onclick="pickTag('${t.replace(/'/g, "\\'")}')">${t}</li>`
        ).join('');

        // If the typed text isn't in the list, offer to create it
        const exact = q && !allTags.some(t => t.toLowerCase() === q) && !manualTags.some(t => t.toLowerCase() === q);
        if (exact) {
            const newTag = query.trim();
            html += `<li class="tag-create-new" onclick="pickTag('${newTag.replace(/'/g, "\\'")}')">➕ Create "<strong>${newTag}</strong>"</li>`;
        }

        if (!html) { ul.style.display = 'none'; return; }

        ul.innerHTML = html;
        _positionDropdown(ul, input);
        ul.style.display = 'block';
    });
}

function pickTag(tag) {
    if (!manualTags.includes(tag)) {
        manualTags.push(tag);
        addChipToList('tags', tag);
    }
    document.getElementById('manual-tag-input').value = '';
    document.getElementById('tag-suggestions').style.display = 'none';
    // If it's a new tag not in the curated list, save it
    if (!allTags.includes(tag)) {
        fetch('/api/tags', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({tag})
        }).then(() => { allTags = []; });
    }
}

// ─── Site autocomplete ───
function _positionDropdown(ul, inputEl) {
    const rect = inputEl.getBoundingClientRect();
    ul.style.position = 'fixed';
    ul.style.top = (rect.bottom + 2) + 'px';
    ul.style.left = rect.left + 'px';
    ul.style.width = rect.width + 'px';
    ul.style.zIndex = '9999';
    ul.style.margin = '0';
}

async function searchSiteSuggestions(query) {
    const ul = document.getElementById('site-suggestions');
    const input = document.getElementById('manual-site');
    const q = query.trim();
    // Allow empty query — returns locally cached known sites
    if (q.length > 0 && q.length < 2) { ul.style.display = 'none'; return; }
    
    try {
        const url = q ? `/api/search-sites?q=${encodeURIComponent(q)}` : '/api/search-sites';
        const res = await fetch(url);
        if (!res.ok) { ul.style.display = 'none'; return; }
        const data = await res.json();
        const sites = data.sites || [];
        
        if (!sites.length) { ul.style.display = 'none'; return; }
        
        ul.innerHTML = sites.map(s => {
            const network = (typeof s.network === 'string') ? s.network : (s.network && s.network.name) || '';
            const label = network ? `${s.name} <span style="opacity:.6;font-size:11px;">(${network})</span>` : s.name;
            const safeName = s.name.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
            const safeNet = network.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
            return `<li onclick="pickSite('${safeName}', '${safeNet}')">${label}</li>`;
        }).join('');
        _positionDropdown(ul, input);
        ul.style.display = 'block';
    } catch (_) {
        ul.style.display = 'none';
    }
}

function pickSite(name, network) {
    document.getElementById('manual-site').value = name;
    document.getElementById('site-suggestions').style.display = 'none';
    // Save to known sites cache
    fetch('/api/known-sites', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, network: network || ''})
    }).catch(() => {});
}

function addChipToList(type, text) {
    const listId = type === 'performers' ? 'manual-performers-list' : 'manual-tags-list';
    const list = document.getElementById(listId);

    // Build chip with DOM APIs — never inject performer/tag text via innerHTML
    // to prevent XSS from TPDB-supplied or user-entered values.
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.appendChild(document.createTextNode(text));

    const removeBtn = document.createElement('span');
    removeBtn.className = 'chip-remove';
    removeBtn.textContent = '×';
    // Use a closure so text never appears in an HTML/JS attribute context.
    removeBtn.addEventListener('click', () => removeChip(type, text));
    chip.appendChild(removeBtn);

    list.appendChild(chip);
}

function removeChip(type, text) {
    if (type === 'performers') {
        manualPerformers = manualPerformers.filter(p => p !== text);
        renderChips('performers', manualPerformers);
    } else {
        manualTags = manualTags.filter(t => t !== text);
        renderChips('tags', manualTags);
    }
}

function renderChips(type, items) {
    const listId = type === 'performers' ? 'manual-performers-list' : 'manual-tags-list';
    const list = document.getElementById(listId);
    list.innerHTML = '';
    items.forEach(item => addChipToList(type, item));
}

async function generateThumbnails() {
    if (!currentManualFile) return;
    
    const grid = document.getElementById('manual-thumbnails-grid');
    grid.innerHTML = `<div class="thumbnail-loading">${t('thumbnail.generating')}</div>`;
    
    try {
        const response = await fetch('/api/extract-thumbnails', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({file_path: currentManualFile.path})
        });
        
        if (!response.ok) throw new Error('Failed to extract thumbnails');
        
        const data = await response.json();
        generatedThumbnails = data.thumbnails;
        
        // Render thumbnails
        grid.innerHTML = data.thumbnails.map((thumb, idx) => `
            <div class="thumbnail-option" onclick="selectThumbnail(${idx})">
                <img src="${thumb.data}" alt="Thumbnail ${idx + 1}">
                <div class="timestamp">${formatTimestamp(thumb.timestamp)}</div>
            </div>
        `).join('');
        
    } catch (error) {
        grid.innerHTML = `<div style="color: var(--error); text-align: center;">Error: ${escapeHtml(error.message)}</div>`;
    }
}

function selectThumbnail(index) {
    selectedThumbnailIndex = index;
    
    // Update UI
    document.querySelectorAll('.thumbnail-option').forEach((el, idx) => {
        el.classList.toggle('selected', idx === index);
    });
}

function formatTimestamp(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

async function saveManualMetadata() {
    if (!currentManualFile) return;
    
    const title = document.getElementById('manual-title').value.trim();
    if (!title) {
        alert('Please enter a scene title');
        return;
    }
    
    const metadata = {
        file_path: currentManualFile.path,
        title: title,
        site: document.getElementById('manual-site').value.trim() || null,
        performers: manualPerformers,
        release_date: document.getElementById('manual-date').value || null,
        tags: manualTags,
        quality: document.getElementById('manual-quality').value || null,
        thumbnail_index: selectedThumbnailIndex
    };

    // Capture display name before closing modal
    const displayName = currentManualFile.name || currentManualFile.path.split('/').pop();

    // Close modal immediately — job continues in background
    manualEditModal.classList.add('hidden');
    showBgJob(displayName);

    // Large files on slow storage can take several minutes to re-mux via FFmpeg.
    // Use a 15-minute AbortController timeout so the browser does not silently
    // drop the connection, which would surface as a generic "NetworkError".
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15 * 60 * 1000);

    try {
        const response = await fetch('/api/save-manual-metadata', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(metadata),
            signal: controller.signal
        });
        
        clearTimeout(timeoutId);

        if (!response.ok) {
            let detail = 'Failed to save metadata';
            try { const j = await response.json(); detail = j.detail || detail; } catch (_) {}
            throw new Error(detail);
        }

        const data = await response.json();
        
        // Create a manual match result and add it to matchedResults
        const manualMatch = {
            original: currentManualFile,
            match: {
                id: 'manual',
                title: metadata.title,
                site: metadata.site,
                performers: metadata.performers,
                release_date: metadata.release_date,
                tags: metadata.tags,
                thumbnail_url: data.thumbnail_url,
                manual_entry: true
            },
            confidence: 100
        };
        
        // Update or add to matchedResults
        const existingIndex = matchedResults.findIndex(r => r.original.path === currentManualFile.path);
        if (existingIndex >= 0) {
            matchedResults[existingIndex] = manualMatch;
        } else {
            matchedResults.push(manualMatch);
        }
        
        // Refresh display
        displayMatches();

        hideBgJob();
        showToast('Metadata saved', displayName, 'success', 3000);
        
    } catch (error) {
        clearTimeout(timeoutId);
        hideBgJob();
        if (error.name === 'AbortError') {
            showToast('Save timed out', 'The file may be too large or the server is busy. Try again.', 'error', 5000);
            showStatus(t('error.save_timeout'), 'error');
        } else if (!navigator.onLine || error.message === 'Failed to fetch' || error.message.includes('NetworkError')) {
            showToast('Connection error', 'Could not reach the server. Check that the container is running.', 'error', 5000);
            showStatus(t('error.save_network'), 'error');
        } else {
            showToast('Save failed', error.message, 'error', 5000);
            showStatus(t('error.save_generic', { message: error.message }), 'error');
        }
    }
}

// ═══ Background Job Indicator ═══
function showBgJob(filename) {
    document.getElementById('bg-job-filename').textContent = filename;
    document.getElementById('bg-job-indicator').classList.remove('hidden');
}

function hideBgJob() {
    document.getElementById('bg-job-indicator').classList.add('hidden');
}

// ═══ Toast Notifications ═══
function showToast(title, message, type = 'info', duration = 3000) {
    const icons = { success: '✅', error: '❌', info: 'ℹ️' };
    const container = document.getElementById('toast-container');

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <div class="toast-body">
            <div class="toast-title">${escapeHtml(title)}</div>
            ${message ? `<div class="toast-message">${escapeHtml(message)}</div>` : ''}
        </div>
    `;

    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('toast-out');
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ═══ Status Bar ═══
function showStatus(message, type = 'info') {
    statusBar.classList.remove('hidden');
    statusText.textContent = message;
    
    if (type === 'error') {
        statusText.style.color = 'var(--error)';
    } else if (type === 'success') {
        statusText.style.color = 'var(--success)';
    } else {
        statusText.style.color = 'var(--text-secondary)';
    }
}
