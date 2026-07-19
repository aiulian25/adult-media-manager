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

// Resolved once the FIRST locale file has loaded. Anything that renders
// translated text OUTSIDE data-i18n (e.g. the update banner, which is built
// with textContent at startup) must await this — otherwise it races the
// locale fetch and bakes raw keys into the DOM.
let _i18nReadyResolve;
const _i18nReady = new Promise((resolve) => { _i18nReadyResolve = resolve; });

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
    _i18nReadyResolve();
}

// Three flat themes share one token contract in style.css. Applying sets a
// data-theme hook on <html>; the CSS re-points tokens per theme (no per-theme
// JS). 'default' is Legacy (purple). Older builds shipped extra themes
// (midnight-teal/ember/daylight) — a stored value from those degrades to
// Legacy and is rewritten so the picker and server stay consistent.
const ALLOWED_THEMES = ['default', 'dark', 'light'];
function applyTheme(theme) {
    const t = ALLOWED_THEMES.includes(theme) ? theme : 'default';
    if (t !== theme && localStorage.getItem('amm_theme') === theme) {
        localStorage.setItem('amm_theme', t);   // migrate retired theme id
    }
    document.documentElement.setAttribute('data-theme', t);
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

// ─── Utility: Get intelligent default browse path ────────────────────────────
/**
 * Choose sensible default browse path based on platform.
 * Native (DEB/AppImage): user's actual home directory from Electron.
 * Docker/browser: /media (most likely mounted volume root).
 */
function _getDefaultBrowsePath() {
    if (typeof window !== 'undefined' && window.electronAPI?.isElectron) {
        // homedir is injected by the preload script — falls back to /home if unavailable
        return window.electronAPI.homedir || '/home';
    }
    // Docker: "/" resolves to the virtual roots view (the list of mounted folders
    // the server allows), so the picker always shows the user's mounts instead of
    // a hard-coded "/media" that may not exist in their compose file.
    return '/';
}

// ─────────────────────────────────────────────────────────────────────────────

// State
let scannedFiles = [];
let matchedResults = [];
let currentBrowsePath = _getDefaultBrowsePath();
let browseHistory = [];              // stack of visited paths for back navigation
let browseSelectionMode = 'folder';  // 'folder' or 'files'
let selectedFiles = [];
let selectedScannedIndices = new Set();   // indices into scannedFiles
let selectedMatchIndices   = new Set();   // indices into matchedResults

// Elements
const scanPath = document.getElementById('scan-path');
const btnScan = document.getElementById('btn-scan');
const btnStopScan = document.getElementById('btn-stop-scan');
const btnMatch = document.getElementById('btn-match');
const btnRename = document.getElementById('btn-rename');
const btnBrowse = document.getElementById('btn-browse');
const btnHistory = document.getElementById('btn-history');
const btnLibrary = document.getElementById('btn-library');
const recursive = document.getElementById('recursive');
const skipOrganized = document.getElementById('skip-organized');
const action = document.getElementById('action');
const template = document.getElementById('template');
const flatRename = document.getElementById('flat-rename');
const embedModeSel = document.getElementById('embed-mode');

// Metadata write modes exposed in the UI: 'embed' (Remux + .nfo, default),
// 'remux_only' (Remux, no .nfo), 'smart' (Both — in-place + .nfo),
// 'embed_only' (Embedded only), 'nfo_only' (Sidecar only).
const _UI_EMBED_MODES = ['embed', 'remux_only', 'smart', 'embed_only', 'nfo_only'];

/** Currently-selected metadata write mode; defaults to 'embed' (Remux + .nfo). */
function _getEmbedMode() {
    const v = embedModeSel && embedModeSel.value;
    return _UI_EMBED_MODES.includes(v) ? v : 'embed';
}

/** Apply a persisted default metadata mode to the toolbar picker + cache it. */
function _applyEmbedMode(mode) {
    const m = _UI_EMBED_MODES.includes(mode) ? mode : 'embed';
    localStorage.setItem('amm_embed_mode', m);
    if (embedModeSel) embedModeSel.value = m;
    return m;
}

// ─── Live template preview ────────────────────────────────────────────────────
// Shows an example output path under the template field as the user types or
// picks a preset. Uses the server's /api/preview-paths (the SAME formatter the
// real rename uses) against a representative scanned/matched file — never a
// client-side reimplementation, so the preview can't drift from the backend.
const templatePreviewEl = document.getElementById('template-preview');
const templateWarningEl = document.getElementById('template-warning');
let _templatePreviewTimer = null;

/** Render (or clear) the template warning line below the preview. */
function _setTemplateWarning(text) {
    if (!templateWarningEl) return;
    if (text) {
        templateWarningEl.textContent = text;
        templateWarningEl.classList.add('is-shown');
    } else {
        templateWarningEl.textContent = '';
        templateWarningEl.classList.remove('is-shown');
    }
}

// Canonical list of valid {placeholder} names, fetched once from /api/templates
// (the formatter's TEMPLATE_VARS). Kept server-sourced so the client list can
// never drift from what apply_template actually resolves. Null until loaded —
// while null we skip unknown-var warnings to avoid false positives.
let _validTemplateVars = null;
async function loadTemplateVars() {
    try {
        const res = await fetch('/api/templates');
        if (!res.ok) return;
        const data = await res.json();
        if (Array.isArray(data.variables)) _validTemplateVars = new Set(data.variables);
        // Preset buttons come from the SAME response (server TEMPLATES dict) —
        // one source of truth, so backend preset changes appear here without
        // HTML edits (F13). On failure the cluster simply stays hidden and the
        // plain template input remains fully usable.
        if (data.templates && typeof data.templates === 'object') {
            _renderPresetButtons(data.templates);
        }
    } catch (_) { /* offline / non-fatal — unknown-var warnings stay disabled */ }
}

/** Build the preset buttons from the server's TEMPLATES dict (name → template). */
function _renderPresetButtons(templates) {
    const cluster = document.querySelector('.template-presets');
    if (!cluster) return;
    cluster.querySelectorAll('.preset-btn').forEach(b => b.remove());
    const entries = Object.entries(templates).filter(([, v]) => typeof v === 'string' && v);
    if (!entries.length) return;
    for (const [key, value] of entries) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'preset-btn';
        btn.dataset.template = value;
        btn.title = value;                       // tooltip shows the real template
        const i18nKey = 'toolbar.preset_' + key;
        const label = t(i18nKey);
        btn.textContent = label === i18nKey ? key.replace(/_/g, ' ') : label;
        if (template && template.value === value) btn.classList.add('active');
        btn.addEventListener('click', () => {
            template.value = value;
            document.querySelectorAll('.preset-btn').forEach(b => b.classList.toggle('active', b === btn));
            updateTemplatePreview();             // immediate preview on preset pick
        });
        cluster.appendChild(btn);
    }
    cluster.hidden = false;
}

/** Return the list of {placeholder} names in `tpl` not recognised by the formatter. */
function _unknownTemplateVars(tpl) {
    if (!_validTemplateVars) return [];
    const found = [...String(tpl).matchAll(/\{(\w+)\}/g)].map(m => m[1]);
    // De-dupe while preserving first-seen order.
    return [...new Set(found)].filter(name => !_validTemplateVars.has(name));
}

/** Current {performers} cap from the toolbar select (F9). 0 = "All". */
function _performerLimit() {
    const el = document.getElementById('performer-limit');
    const n = el ? parseInt(el.value, 10) : 3;
    return Number.isFinite(n) ? n : 3;
}

/** Pick a representative operation for the preview, or null when no data yet. */
function _samplePreviewOp() {
    // Prefer a real matched result (has scene_data → most accurate preview).
    const matched = matchedResults.find(r => r && r.match && r.original && r.original.path);
    if (matched) {
        return { old_path: matched.original.path, scene_data: matched.match,
                 file_data: matched.original, performer_limit: _performerLimit() };
    }
    // Otherwise a scanned file (detector metadata only).
    const scanned = scannedFiles.find(f => f && f.path);
    if (scanned) {
        return { old_path: scanned.path, scene_data: {}, file_data: scanned,
                 performer_limit: _performerLimit() };
    }
    return null;
}

function updateTemplatePreview() {
    if (!templatePreviewEl) return;

    // Unknown-variable detection needs no sample data — validate the raw template
    // against the server's canonical list, so the user is warned even before a
    // scan. Unknown vars take priority over the same-as-source notice below.
    const unknown = _unknownTemplateVars(template.value);
    const unknownMsg = unknown.length
        ? t('template.unknown_vars', { vars: unknown.map(v => '{' + v + '}').join(', ') })
        : '';

    const op = _samplePreviewOp();
    if (!op) {
        templatePreviewEl.textContent = t('template.preview_none');
        templatePreviewEl.className = 'template-preview is-hint';
        _setTemplateWarning(unknownMsg);
        return;
    }
    const body = {
        operations: [{
            old_path: op.old_path,
            scene_data: op.scene_data,
            file_data: op.file_data,
            template: template.value,
            flat: flatRename.checked,
        }],
    };
    fetch('/api/preview-paths', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
            const p = data && data.previews && data.previews[0];

            // Warning line: unknown vars first, then "no change" (same as source).
            if (unknownMsg)               _setTemplateWarning(unknownMsg);
            else if (p && p.same_as_source) _setTemplateWarning(t('template.same_as_source'));
            else                          _setTemplateWarning('');

            if (!p) { templatePreviewEl.textContent = ''; templatePreviewEl.className = 'template-preview'; return; }
            if (p.degenerate) {
                templatePreviewEl.textContent = t('template.preview_empty');
                templatePreviewEl.className = 'template-preview is-error';
                return;
            }
            // Show the path relative to its parent for brevity (it's just an example).
            templatePreviewEl.textContent = t('template.preview_prefix') + ' ' + p.new_path;
            templatePreviewEl.className = 'template-preview';
        })
        .catch(() => {
            templatePreviewEl.textContent = '';
            templatePreviewEl.className = 'template-preview';
            _setTemplateWarning(unknownMsg);
        });
}

/** Debounced wrapper for the typing path. */
function _scheduleTemplatePreview() {
    clearTimeout(_templatePreviewTimer);
    _templatePreviewTimer = setTimeout(updateTemplatePreview, 300);
}
const resultsContainer = document.getElementById('results-container');
const statusBar = document.getElementById('status-bar');
const statusText = document.getElementById('status-text');
const progressFill = document.getElementById('progress-fill');

// Home / reset button
document.getElementById('btn-home').addEventListener('click', resetToHome);

/**
 * Build a centered empty/zero-result state. `icon` is trusted markup (an emoji
 * we control); `title`/`subtitle` are treated as untrusted text and escaped,
 * so interpolated values like a filesystem path or a server error message can
 * never inject HTML.
 */
function _emptyStateHtml(icon, title, subtitle) {
    return `
        <div class="empty-state">
            <div class="empty-icon">${icon}</div>
            <div class="empty-title">${escapeHtml(title)}</div>
            <div class="empty-subtitle">${escapeHtml(subtitle)}</div>
        </div>`;
}

function _renderEmptyState(icon, title, subtitle) {
    resultsContainer.innerHTML = _emptyStateHtml(icon, title, subtitle);
}

/**
 * Windowed list rendering (review item R3 / U1). Appends the first `cap` items as
 * built nodes, then a single "Show all N more" button that appends the rest on
 * demand — so a large result set doesn't build its whole DOM up front. Shared by
 * the scan and match lists so both window the same way from ONE implementation.
 *
 * @param {Element}  container - element to append rows (and the button) into
 * @param {Array}    items     - arbitrary items passed to buildNode
 * @param {Function} buildNode - (item) => Node, builds one row
 * @param {number}   cap       - how many to render up front
 * @param {Function} [onExpand]- called after the remainder is appended
 */
function _renderWindowed(container, items, buildNode, cap, onExpand) {
    if (!container) return;
    const head = items.slice(0, cap);
    const frag = document.createDocumentFragment();
    head.forEach(it => frag.appendChild(buildNode(it)));
    container.appendChild(frag);

    const rest = items.slice(cap);
    if (rest.length === 0) return;

    const btn = document.createElement('button');
    btn.className = 'glass-btn show-all-btn';
    btn.textContent = t('scan.show_all', { count: rest.length });
    btn.addEventListener('click', () => {
        btn.remove();
        const f2 = document.createDocumentFragment();
        rest.forEach(it => f2.appendChild(buildNode(it)));
        container.appendChild(f2);
        if (onExpand) onExpand();
    });
    container.appendChild(btn);
}

function resetToHome() {
    // Abort any in-flight scan stream and restore the Scan/Stop buttons.
    if (typeof _scanEventSource !== 'undefined' && _scanEventSource) {
        _scanStopped = true;
        try { _scanEventSource.close(); } catch (_) { /* ignore */ }
        _scanEventSource = null;
        if (typeof _scanResolve !== 'undefined' && _scanResolve) { _scanResolve(); _scanResolve = null; }
    }
    if (typeof _setScanRunning === 'function') _setScanRunning(false);

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
    _renderEmptyState('🎬', t('home.empty_title'), t('home.empty_subtitle'));

    // No files loaded — reset the template preview to its hint state.
    if (typeof updateTemplatePreview === 'function') updateTemplatePreview();
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
    // Initialise i18n + theme before anything else so the first paint already
    // reflects the user's saved choices. localStorage is a fast cache; the
    // server (settings.json) is the durable source of truth and is reconciled
    // immediately after, so a fresh browser/profile still picks up the choice.
    applyTheme(localStorage.getItem('amm_theme') || 'default');
    _applyEmbedMode(localStorage.getItem('amm_embed_mode') || 'embed');
    await loadI18n();
    applyI18nToDOM();
    reconcilePrefsFromServer();   // fire-and-forget; updates if server differs

    // Template preset buttons are rendered (and wired) by _renderPresetButtons
    // once /api/templates answers — see loadTemplateVars below (F13).

    // Insert-variable chips — drop a {token} at the caret in the template field,
    // then re-run the live preview via the same 'input' path typing uses.
    document.querySelectorAll('#insert-chips .tvar-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const token = `{${chip.dataset.var}}`;
            const start = template.selectionStart ?? template.value.length;
            const end   = template.selectionEnd   ?? template.value.length;
            template.value = template.value.slice(0, start) + token + template.value.slice(end);
            const caret = start + token.length;
            template.focus();
            try { template.setSelectionRange(caret, caret); } catch (_) {}
            document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
            updateTemplatePreview();
        });
    });

    // Live template preview: update as the user types / toggles flat mode.
    template.addEventListener('input', _scheduleTemplatePreview);
    flatRename.addEventListener('change', updateTemplatePreview);
    // {performers} cap (F9) — re-preview so the "et al" result is visible.
    document.getElementById('performer-limit')
        ?.addEventListener('change', updateTemplatePreview);
    updateTemplatePreview();                   // initial state (shows hint until a scan)
    // Load the valid-variable list, then re-run so unknown-var warnings apply.
    loadTemplateVars().then(() => updateTemplatePreview());

    // Scan button
    btnScan.addEventListener('click', scanFolder);

    // Stop-scan button (visible only while a scan is streaming)
    if (btnStopScan) btnStopScan.addEventListener('click', stopScan);

    // Match button — carries two functions: start a match, or cancel the running
    // one (the button turns into "Cancel" while matching streams). onMatchClick
    // dispatches based on state; identical across Docker/deb/AppImage.
    btnMatch.addEventListener('click', onMatchClick);
    
    // Rename button
    btnRename.addEventListener('click', renameFiles);
    
    // Browse button — on the Electron desktop builds (deb/AppImage) we open the
    // native GTK picker via a tiny folder/files menu (showNativePicker): familiar
    // chooser, multi-select, hidden-file toggle, starts where the user expects.
    // GTK can't combine file + directory selection in one dialog, hence the menu.
    // Docker/browser builds have no electronAPI.pickPaths, so they fall back to
    // the in-app /api/browse modal, which browses the (sandboxed) server filesystem
    // and lets the user pick EITHER a folder OR individual files.
    btnBrowse.addEventListener('click', () => {
        if (_hasNativePicker()) showNativePicker(btnBrowse);
        else openBrowseModal();
    });
    
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

    // Library modal (catalog stats + duplicates)
    btnLibrary.addEventListener('click', openLibraryModal);
    const _libClose = () => document.getElementById('library-modal').classList.add('hidden');
    document.getElementById('library-close').addEventListener('click', _libClose);
    document.getElementById('library-ok').addEventListener('click', _libClose);
    
    document.getElementById('history-undo').addEventListener('click', undoLastRename);

    // Delegated handler for the per-row "Revert" buttons (rows are re-rendered
    // on every history load, so a single listener on the container is robust).
    document.getElementById('history-list').addEventListener('click', (e) => {
        const btn = e.target.closest('.history-revert-btn');
        if (btn && btn.dataset.id) revertHistoryEntry(btn.dataset.id, btn);
    });

    // Manual edit modal
    document.getElementById('manual-edit-close').addEventListener('click', () => {
        manualEditModal.classList.add('hidden');
    });
    
    document.getElementById('manual-edit-cancel').addEventListener('click', () => {
        manualEditModal.classList.add('hidden');
    });
    
    document.getElementById('manual-edit-save').addEventListener('click', saveManualMetadata);
    // Live-validate the release date as the user changes it.
    document.getElementById('manual-date').addEventListener('input', _refreshManualDateError);
    document.getElementById('manual-add-performer').addEventListener('click', addManualPerformer);
    document.getElementById('manual-add-tag').addEventListener('click', addManualTag);
    // Fetch metadata from a pasted StashDB scene URL/UUID
    const stashFetchBtn = document.getElementById('manual-stashdb-fetch');
    if (stashFetchBtn) stashFetchBtn.addEventListener('click', fetchStashDBScene);
    const stashUrlInput = document.getElementById('manual-stashdb-url');
    if (stashUrlInput) stashUrlInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); fetchStashDBScene(); }
    });
    // Fetch metadata from a pasted ThePornDB scene URL/slug
    const tpdbFetchBtn = document.getElementById('manual-tpdb-fetch');
    if (tpdbFetchBtn) tpdbFetchBtn.addEventListener('click', fetchTPDBScene);
    const tpdbUrlInput = document.getElementById('manual-tpdb-url');
    if (tpdbUrlInput) tpdbUrlInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); fetchTPDBScene(); }
    });
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
            _doRename(savedQueue.operations, savedQueue.actionType, savedQueue.embedMode || 'embed');
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

    // R2 — Resume embed progress: if a metadata-embedding job was running when the
    // page was refreshed (or the server restarted), re-attach to it. The backend
    // persists job state, so polling resumes the banner/progress; a finished or
    // interrupted job ends immediately, and an expired one (404) clears silently.
    let savedEmbed = null;
    try { savedEmbed = JSON.parse(localStorage.getItem(EMBED_JOB_KEY)); } catch {}
    if (savedEmbed && savedEmbed.jobId) {
        _pollEmbedStatus(savedEmbed.jobId, savedEmbed.total || 0);
    }
});

// ═══ Settings (language + theme + API keys) ═══

/**
 * After the localStorage-cached first paint, reconcile UI preferences with the
 * server's durable copy (settings.json). If the server has a different choice
 * (e.g. saved from another browser, or after clearing localStorage), adopt it.
 * Non-blocking and silent on failure — the cached values remain in effect.
 */
async function reconcilePrefsFromServer() {
    try {
        const resp = await fetch('/api/settings');
        if (!resp.ok) return;
        const data = await resp.json();

        const serverTheme = data.theme || 'default';
        if (serverTheme !== (localStorage.getItem('amm_theme') || 'default')) {
            localStorage.setItem('amm_theme', serverTheme);
        }
        applyTheme(localStorage.getItem('amm_theme'));

        const serverLocale = data.locale;
        if (serverLocale && serverLocale !== (localStorage.getItem('amm_locale') || 'en')) {
            localStorage.setItem('amm_locale', serverLocale);
            await loadI18n();
            applyI18nToDOM();
            // Re-render the textContent-built banner in the new language
            // (data-i18n re-application above doesn't reach it).
            _maybeShowUpdateBanner();
        }

        // Default metadata write mode (persisted server-side; survives restarts).
        if (data.embed_mode) _applyEmbedMode(data.embed_mode);
        const fpCb0 = document.getElementById('settings-contribute-fp');
        if (fpCb0) fpCb0.checked = data.contribute_fingerprints === true;
    } catch (_) {
        // Non-fatal — keep the cached values.
    }
}

function _applySettingsStatus(data, isNative = false) {
    // data: { tpdb: {active, source}, stashdb: {active, source} }
    for (const [key, info] of Object.entries(data)) {
        if (!info) continue;
        const badge  = document.getElementById(`settings-${key}-badge`);
        const source = document.getElementById(`settings-${key}-source`);
        const inp    = document.getElementById(`settings-${key}-key`);
        const row    = badge?.closest('.settings-key-row');
        if (!badge) continue;

        // Remove-saved-key control (roadmap-2 F14): only meaningful for keys
        // stored via Settings — env-sourced keys are immutable by design and
        // unset keys have nothing to remove.
        const clearBtn = document.getElementById(`settings-${key}-clear`);
        if (clearBtn) clearBtn.hidden = info.source !== 'settings';

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

    // Clear inputs on every open for security (never pre-fill key values), and
    // re-mask any field left revealed from a previous session so the next key the
    // user types isn't shown in cleartext by default.
    ['settings-tpdb-key', 'settings-stashdb-key'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.value = ''; el.disabled = false; el.type = 'password'; }
        const eyeBtn = document.querySelector(`.settings-eye-btn[data-target="${id}"]`);
        if (eyeBtn) {
            eyeBtn.setAttribute('aria-pressed', 'false');
            eyeBtn.innerHTML =
                '<svg class="eye-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
                '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
        }
    });

    // Pre-fill the preference selects from the cached values so they show the
    // current choice instantly; the server fetch below corrects them if needed.
    const langSel  = document.getElementById('settings-lang');
    const themeSel = document.getElementById('settings-theme');
    const embedSel = document.getElementById('settings-embed-mode');
    if (langSel)  langSel.value  = localStorage.getItem('amm_locale') || 'en';
    if (themeSel) themeSel.value = localStorage.getItem('amm_theme')  || 'default';
    if (embedSel) embedSel.value = localStorage.getItem('amm_embed_mode') || 'embed';

    // Software update card fills in asynchronously — never blocks the modal.
    refreshUpdateInfo().then(renderUpdateCard);

    // Fetch current status from the server
    try {
        const resp = await fetch('/api/settings');
        if (resp.ok) {
            const data = await resp.json();
            _applySettingsStatus(data, isNative);
            // Server is authoritative for the saved preferences.
            if (langSel  && data.locale) langSel.value  = data.locale;
            if (themeSel && data.theme)  themeSel.value = data.theme;
            if (embedSel && data.embed_mode) embedSel.value = data.embed_mode;
            const fpCb = document.getElementById('settings-contribute-fp');
            if (fpCb) fpCb.checked = data.contribute_fingerprints === true;
        }
    } catch (_) {
        // Non-fatal — badges stay in default state
    }
}

/* ── Software update: banner, Settings card, restart prompt (F17) ──
   One state machine feeds three surfaces:
     - a dismissible banner in the main window (announces a release once,
       dismiss remembered per-version),
     - the "Software update" card in Settings (idle → downloading → ready →
       installing → done, plus manual-fallback and error states),
     - a themed in-app restart prompt.
   Trust model: the renderer passes NO arguments to download/install/restart —
   the Electron main process owns asset selection, sha256 verification and
   what a restart actually does. */
let updInfo = null;       // last /api/version payload
let updPhase = 'idle';    // idle | downloading | ready | manual | installing | done | error
let updResult = null;     // downloadUpdate() result ({name, pkgType, …})
let updError = '';
let updNote = '';         // transient note (e.g. cancelled authorization)

function updCanAutoDl() {
    return !!(window.electronAPI && typeof window.electronAPI.downloadUpdate === 'function');
}
function updCanInstall() {
    return !!(window.electronAPI && typeof window.electronAPI.installUpdate === 'function');
}
function _updIsElectron() {
    return !!(window.electronAPI && window.electronAPI.isElectron);
}

let _lastUpdCheck = 0;    // throttles visibility-driven rechecks to 1/hour (F11)

let _toolHealth = null;   // last /api/health "tools" dict (F2 system check)

async function refreshUpdateInfo() {
    try {
        const resp = await fetch('/api/version');
        if (resp.ok) { updInfo = await resp.json(); _lastUpdCheck = Date.now(); }
    } catch (_) { /* offline — card renders nothing */ }
    try {
        const h = await fetch('/api/health');
        if (h.ok) _toolHealth = (await h.json()).tools || null;
    } catch (_) { /* keep the last known tool state */ }
    return updInfo;
}

// F2: one-line system check — names any missing external tool. Empty string
// when everything (or nothing yet) is known-good.
function _toolsMissingHtml() {
    if (!_toolHealth) return '';
    const missing = Object.keys(_toolHealth).filter(k => _toolHealth[k] === false);
    if (!missing.length) return '';
    return `<div class="up-tiny" style="color:var(--error)">⚠ ${
        escapeHtml(t('settings.tools_missing', { tools: missing.join(', ') }))}</div>`;
}

function renderUpdateCard() {
    const slot = document.getElementById('update-card-slot');
    if (!slot) return;                    // Settings not open — state persists for next open
    const v = updInfo;
    if (!v || !v.version) { slot.innerHTML = ''; return; }
    const upd = v.update;
    const relUrl = (upd && upd.url) || v.releases_url || 'https://github.com/aiulian25/adult-media-manager/releases';

    // Up to date — the quiet default. The F2 system-check line rides below.
    if (!upd || !upd.latest) {
        slot.innerHTML = `
            <div class="update-card update-row">
                <span class="update-chip ok">✓ ${escapeHtml(t('update.up_to_date'))}</span>
                <span style="font-weight:650">AMM v${escapeHtml(v.version)}</span>
                <span class="up-tiny" style="flex:1">${escapeHtml(t('update.checks_daily'))}</span>
                <a class="up-tiny" href="${escapeHtml(relUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(t('update.releases_link'))}</a>
            </div>${_toolsMissingHtml()}`;
        return;
    }

    const latest = escapeHtml(upd.latest);
    const whatsNew = `<a href="${escapeHtml(relUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(t('update.whats_new'))}</a>`;

    // Docker / plain browser: same card, pull command instead of buttons.
    if (!updCanAutoDl()) {
        slot.innerHTML = `
            <div class="update-card highlight">
                <h4><span class="update-chip new">${escapeHtml(t('update.chip_new'))}</span> ${escapeHtml(t('update.available_title', { version: upd.latest }))}</h4>
                <div class="up-muted">${escapeHtml(t('update.youre_on', { version: v.version }))} · ${whatsNew}</div>
                ${_updIsElectron() ? '' : `<div class="up-tiny" style="margin-top:6px">${escapeHtml(t('update.docker_hint'))} <code>docker compose pull &amp;&amp; docker compose up -d</code></div>`}
            </div>${_toolsMissingHtml()}`;
        return;
    }

    let html = '';
    if (updPhase === 'downloading') {
        html = `
            <div class="update-card highlight">
                <h4>${escapeHtml(t('update.downloading_title', { version: upd.latest }))}</h4>
                <div class="update-bar"><i id="upd-bar-fill"></i></div>
                <div class="update-row">
                    <span class="up-muted" id="upd-bytes" style="flex:1;margin-top:0">${escapeHtml(t('update.starting'))}</span>
                    <span class="up-muted" id="upd-pct" style="margin-top:0">0%</span>
                </div>
            </div>`;
    } else if (updPhase === 'ready') {
        const appimage = updResult && updResult.pkgType === 'appimage';
        html = `
            <div class="update-card highlight">
                <div class="update-row">
                    <div style="flex:1;min-width:0">
                        <h4><span style="color:var(--success)">✓</span> ${escapeHtml(t('update.verified_title'))}</h4>
                        <div class="up-muted">${escapeHtml(t('update.verified_sub'))} · <span class="mono">${escapeHtml(updResult && updResult.name || '')}</span></div>
                        <div class="up-tiny">${escapeHtml(appimage ? t('update.appimage_note') : t('update.pkexec_note'))}</div>
                        ${updNote ? `<div class="up-tiny" style="color:var(--warning)">${escapeHtml(updNote)}</div>` : ''}
                    </div>
                    <button class="btn btn-primary" id="btn-upd-install" style="flex-shrink:0">${escapeHtml(t('update.install_btn'))}</button>
                </div>
            </div>`;
    } else if (updPhase === 'installing') {
        html = `
            <div class="update-card highlight">
                <h4>${escapeHtml(t('update.installing_title', { version: upd.latest }))}</h4>
                <div class="update-bar indet"><i></i></div>
                <div class="up-muted">${escapeHtml(t('update.installing_sub'))}</div>
            </div>`;
    } else if (updPhase === 'done') {
        html = `
            <div class="update-card highlight">
                <div class="update-row">
                    <div style="flex:1">
                        <h4><span style="color:var(--success)">✓</span> ${escapeHtml(t('update.done_title', { version: upd.latest }))}</h4>
                        <div class="up-muted">${escapeHtml(t('update.done_sub'))}</div>
                    </div>
                    <button class="btn btn-primary" id="btn-upd-restart" style="flex-shrink:0">${escapeHtml(t('update.restart_btn'))}</button>
                </div>
            </div>`;
    } else if (updPhase === 'manual') {
        const r = updResult || {};
        const hint = r.pkgType === 'deb' ? `sudo apt install ./Downloads/${r.name || ''}`
                   : r.pkgType === 'rpm' ? `sudo dnf install ./Downloads/${r.name || ''}`
                   : t('update.manual_appimage');
        html = `
            <div class="update-card">
                <h4><span style="color:var(--success)">✓</span> ${escapeHtml(t('update.verified_title'))}</h4>
                <div class="up-muted">${escapeHtml(t('update.manual_saved', { name: r.name || '' }))}</div>
                <div class="up-tiny">${escapeHtml(t('update.manual_install_with'))} <code>${escapeHtml(hint)}</code></div>
            </div>`;
    } else if (updPhase === 'error') {
        html = `
            <div class="update-card error">
                <div class="update-row">
                    <div style="flex:1;min-width:0">
                        <h4><span style="color:var(--error)">✕</span> ${escapeHtml(t('update.error_title'))}</h4>
                        <div class="up-muted">${escapeHtml(updError || 'unknown error')}</div>
                        ${updResult && updResult.name ? `<div class="up-tiny">${escapeHtml(t('update.error_file_kept', { name: updResult.name }))}</div>` : ''}
                    </div>
                    <button class="glass-btn" id="btn-upd-retry" style="flex-shrink:0">${escapeHtml(t('update.retry_btn'))}</button>
                </div>
            </div>`;
    } else {
        // idle — update available.
        html = `
            <div class="update-card highlight">
                <div class="update-row">
                    <div style="flex:1;min-width:0">
                        <h4><span class="update-chip new">${escapeHtml(t('update.chip_new'))}</span> ${escapeHtml(t('update.available_title', { version: upd.latest }))}</h4>
                        <div class="up-muted">${escapeHtml(t('update.youre_on', { version: v.version }))} · ${whatsNew} · ${escapeHtml(t('update.verified_from_github'))}</div>
                    </div>
                    <button class="btn btn-primary" id="btn-upd-download" style="flex-shrink:0">${escapeHtml(t('update.download_btn'))}</button>
                </div>
            </div>`;
    }
    slot.innerHTML = html;
    document.getElementById('btn-upd-download')?.addEventListener('click', updDownload);
    document.getElementById('btn-upd-retry')?.addEventListener('click', updDownload);
    document.getElementById('btn-upd-install')?.addEventListener('click', updInstall);
    document.getElementById('btn-upd-restart')?.addEventListener('click', () => {
        window.electronAPI.restartApp && window.electronAPI.restartApp();
    });
}

async function updDownload() {
    updPhase = 'downloading'; updError = ''; updNote = '';
    renderUpdateCard();
    window.electronAPI.onUpdateProgress(p => {
        const pct = typeof p === 'number' ? p : (p && p.pct) || 0;
        const fill = document.getElementById('upd-bar-fill');
        if (fill) fill.style.width = pct + '%';
        const lab = document.getElementById('upd-pct');
        if (lab) lab.textContent = pct + '%';
        const bytes = document.getElementById('upd-bytes');
        if (bytes && p && typeof p === 'object' && p.total) {
            bytes.textContent = `${formatFileSize(p.transferred)} / ${formatFileSize(p.total)} · github.com`;
        }
    });
    const r = await window.electronAPI.downloadUpdate();
    updResult = r;
    if (r && r.ok && r.canInstall && updCanInstall()) updPhase = 'ready';
    else if (r && r.ok) updPhase = 'manual';       // no pkexec — verified file revealed
    else { updPhase = 'error'; updError = t('update.download_failed') + ': ' + ((r && r.error) || 'unknown error'); }
    renderUpdateCard();
}

async function updInstall() {
    updPhase = 'installing'; updNote = '';
    renderUpdateCard();
    const r = await window.electronAPI.installUpdate();
    if (r && r.ok) {
        updPhase = 'done';      // main also fires the restart prompt
    } else if (r && r.cancelled) {
        updPhase = 'ready';
        updNote = t('update.auth_cancelled');
    } else {
        updPhase = 'error';
        updError = t('update.install_failed') + ': ' + ((r && r.error) || 'unknown error');
    }
    renderUpdateCard();
}

/* Update banner: one quiet row under the top bar, shown once per release.
   Dismiss is remembered per-version — never nags again until the NEXT one.
   The decision lives apart from the fetch so periodic rechecks (F11) reuse
   it — including the dismiss guard, so they can never re-nag. */
function _maybeShowUpdateBanner() {
    const v = updInfo;
    if (!v || !v.version) return;
    const latest = v.update && v.update.latest;
    if (!latest) return;
    if (localStorage.getItem('amm_dismissed_update') === latest) return;
    document.getElementById('update-banner-title').textContent =
        t('update.available_title', { version: latest });
    document.getElementById('update-banner-sub').textContent = updCanAutoDl()
        ? t('update.banner_sub_native')
        : (_updIsElectron() ? t('update.banner_sub_manual') : t('update.banner_sub_docker'));
    document.getElementById('update-banner').classList.remove('hidden');
}

async function checkUpdateOnStartup() {
    try {
        await refreshUpdateInfo();
        // The banner is textContent-rendered — it must not race the locale
        // load or it displays raw keys (seen live: "update.available_title").
        await _i18nReady;
        _maybeShowUpdateBanner();
    } catch (_) { /* offline — no banner */ }
}

/* In-app restart prompt — shown when the main process reports an installed
   update awaiting a restart (deb/rpm upgrade seen on disk, or a replaced
   AppImage). Asked once; "Restart later" is respected — the Settings card
   keeps the persistent affordance. */
function showRestartModal(info) {
    let overlay = document.getElementById('restart-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'restart-overlay';
        overlay.className = 'modal hidden';
        document.body.appendChild(overlay);
    }
    const spawnMode = info && info.mode === 'spawn';
    const latest = escapeHtml((info && info.latest) || '');
    overlay.innerHTML = `
        <div class="modal-content glass-panel restart-box">
            <div class="restart-head">
                <div class="restart-title">${escapeHtml(spawnMode ? t('update.restart_ready_title') : t('update.restart_installed_title'))}</div>
                <div class="restart-sub">AMM v${latest}</div>
            </div>
            <div class="restart-body">${escapeHtml(spawnMode
                ? t('update.restart_body_spawn', { version: (info && info.latest) || '' })
                : t('update.restart_body_relaunch', { version: (info && info.latest) || '', running: (info && info.running) || '' }))}</div>
            <div class="restart-fine">${escapeHtml(t('update.restart_fine'))}</div>
            <div class="restart-actions">
                <button class="btn btn-secondary" id="restart-later">${escapeHtml(t('update.restart_later'))}</button>
                <button class="btn btn-primary" id="restart-now">${escapeHtml(t('update.restart_btn'))}</button>
            </div>
        </div>`;
    overlay.classList.remove('hidden');
    document.getElementById('restart-later').addEventListener('click', () => overlay.classList.add('hidden'));
    document.getElementById('restart-now').addEventListener('click', () => {
        document.getElementById('restart-now').disabled = true;
        window.electronAPI.restartApp && window.electronAPI.restartApp();
    });
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.classList.add('hidden'); });
}

async function saveSettings() {
    const saveBtn = document.getElementById('settings-save');
    saveBtn.disabled = true;
    saveBtn.textContent = t('settings.saving');

    const tpdbVal    = document.getElementById('settings-tpdb-key')?.value.trim()    || '';
    const stashdbVal = document.getElementById('settings-stashdb-key')?.value.trim() || '';
    const localeVal  = document.getElementById('settings-lang')?.value   || 'en';
    const themeVal   = document.getElementById('settings-theme')?.value  || 'default';
    const embedVal   = document.getElementById('settings-embed-mode')?.value || 'embed';

    try {
        const resp = await fetch('/api/settings', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                tpdb_api_key:    tpdbVal    || null,
                stashdb_api_key: stashdbVal || null,
                locale:          localeVal,
                theme:           themeVal,
                embed_mode:      embedVal,
                // F5: explicit true/false — the server treats null as "keep".
                contribute_fingerprints:
                    document.getElementById('settings-contribute-fp')?.checked === true,
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

        // Apply preferences returned by the server (authoritative). Cache them
        // in localStorage so the next load paints them without a flash.
        const newTheme = data.theme || themeVal;
        localStorage.setItem('amm_theme', newTheme);
        applyTheme(newTheme);

        // Default metadata write mode → cache + apply to the toolbar picker.
        _applyEmbedMode(data.embed_mode || embedVal);

        const newLocale = data.locale || localeVal;
        if (newLocale !== (localStorage.getItem('amm_locale') || 'en')) {
            localStorage.setItem('amm_locale', newLocale);
            await loadI18n();
            applyI18nToDOM();
        }

        const label = data.changed && data.changed.length
            ? `${data.changed.join(' & ')} key${data.changed.length > 1 ? 's' : ''} saved and active.`
            : 'Settings saved.';
        showToast('Settings Saved', label, 'success');

        document.getElementById('settings-modal').classList.add('hidden');

    } catch (err) {
        showToast('Settings Error', err.message, 'error');
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = t('settings.save');
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

    // Remove-saved-key buttons (roadmap-2 F14): confirm, then POST the explicit
    // clear flag — blank-save still means "keep", so removal is its own signal.
    document.querySelectorAll('.settings-key-clear').forEach(btn => {
        btn.addEventListener('click', () => {
            const prov = btn.dataset.clear;                    // 'tpdb' | 'stashdb'
            const label = prov === 'tpdb' ? 'TPDB' : 'StashDB';
            showConfirmModal(t('settings.key_remove_confirm', { label }), async () => {
                try {
                    const resp = await fetch('/api/settings', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(prov === 'tpdb'
                            ? { clear_tpdb: true } : { clear_stashdb: true }),
                    });
                    const data = await resp.json();
                    if (!resp.ok) throw new Error(data.detail || resp.statusText);
                    _applySettingsStatus(
                        { tpdb: data.tpdb, stashdb: data.stashdb },
                        !!(window.electronAPI && window.electronAPI.isElectron));
                    showToast(t('settings.title'),
                              t('settings.key_removed', { label }), 'success');
                } catch (err) {
                    showToast(t('settings.title'), err.message, 'error');
                }
            });
        });
    });

    // Update notifier buttons (F17). Download is server-driven (no client input);
    // restart goes through the preload bridge and only exists in the native build.
    // Software update surfaces (F17): banner + restart prompt wiring.
    document.getElementById('update-banner-view')?.addEventListener('click', () => {
        document.getElementById('update-banner').classList.add('hidden');
        openSettingsModal();
        setTimeout(() => document.getElementById('update-card-slot')
            ?.scrollIntoView({ block: 'nearest' }), 150);
    });
    document.getElementById('update-banner-dismiss')?.addEventListener('click', () => {
        const latest = updInfo && updInfo.update && updInfo.update.latest;
        if (latest) localStorage.setItem('amm_dismissed_update', latest);
        document.getElementById('update-banner').classList.add('hidden');
    });
    if (window.electronAPI && typeof window.electronAPI.onUpdateRestartPending === 'function') {
        window.electronAPI.onUpdateRestartPending(showRestartModal);
    }
    checkUpdateOnStartup();
    // F11: long-lived tabs learn about updates too. A pinned Docker tab never
    // reloads, so re-ask every 6 h, plus on tab refocus when the last check is
    // over an hour old. The server caches the GitHub check for 24 h, so these
    // rechecks add no upstream traffic; the per-version dismiss is enforced
    // inside _maybeShowUpdateBanner, so a dismissed release stays dismissed.
    setInterval(checkUpdateOnStartup, 6 * 3600 * 1000);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible'
            && Date.now() - _lastUpdCheck > 3600 * 1000) {
            checkUpdateOnStartup();
        }
    });

    // Show/hide toggle for each key input. We swap the eye ↔ eye-off icon so the
    // toggle gives clear feedback: key fields open EMPTY (never pre-filled, for
    // security), so without the icon change the button looked dead even when it
    // worked. (Env-managed rows hide the eye entirely via CSS — the real key lives
    // server-side and is never sent to the client, so there is nothing to reveal.)
    const EYE_ICON =
        '<svg class="eye-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
        '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
    const EYE_OFF_ICON =
        '<svg class="eye-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
        '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>' +
        '<line x1="1" y1="1" x2="23" y2="23"/></svg>';
    document.querySelectorAll('.settings-eye-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const inp = document.getElementById(btn.dataset.target);
            if (!inp) return;
            const reveal = inp.type === 'password';
            inp.type = reveal ? 'text' : 'password';
            btn.innerHTML = reveal ? EYE_OFF_ICON : EYE_ICON;
            btn.setAttribute('aria-pressed', String(reveal));
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

    // Decide what to scan from a set of dropped paths: scan EXACTLY what was
    // dropped — a single item as-is, or multiple items as a comma-separated list.
    //
    // We deliberately do NOT collapse multiple files to their common parent
    // directory. Dragging 7 files out of a 100-file folder must scan those 7
    // files, not the whole folder (the previous "they share a parent → scan the
    // parent" heuristic conflated "several files" with "a folder" and rescanned
    // everything). A dropped folder still arrives as a single directory path and
    // is scanned recursively; the backend's scan accepts a comma list that mixes
    // files and directories, so mixed drops work too.
    function _resolveDropPaths(paths) {
        const unique = [...new Set(paths.filter(Boolean))];
        if (unique.length <= 1) {
            return { path: unique[0] || '', isMulti: false };
        }
        return { path: unique.join(','), isMulti: true };
    }

    // Populate the scan-path input and optionally trigger a scan.
    function _loadPath(path, isMulti) {
        const cleanPath = path.replace(/^\/+/, '/').trim();

        // In Electron (DEB/AppImage), getPathForFile() returns the real on-disk path,
        // so any absolute path is valid — trust it and let the server validate.
        // In Docker/browser the FileSystem Entry API returns virtual paths like
        // "/VideoFile.mp4" that don't map to server locations — use the known-prefix
        // regex to detect these and ask the user to correct the path instead.
        const isElectron = typeof window !== 'undefined' && window.electronAPI?.isElectron;
        const looksLikeServerPath = isElectron
            ? cleanPath.startsWith('/')
            : /^\/(mnt|media|data|downloads|organized|home|root|srv|nas|storage|run)\b/.test(cleanPath);

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

    // Expose to the global scope so callers outside this IIFE — notably the
    // native folder-picker handler (Browse button in the Electron deb/AppImage
    // build) — can populate the scan path and trigger a scan. Without this the
    // handler hits a ReferenceError, the path is never set, and pressing Scan
    // reports "Please enter a path".
    window._loadPath = _loadPath;
})();

// ═══ Native OS picker (Electron deb/AppImage only) ═══
//
// On the desktop builds the Browse button opens the real GTK file chooser instead
// of the in-app /api/browse modal. GTK can't show files AND directories in one
// dialog on Linux, so we pop a two-item menu and fire the matching native dialog.
// Selected paths feed straight into the scan path and auto-scan — the same code
// path as picking inside the modal. Docker/browser builds lack pickPaths and never
// reach here, so the sandboxed server-side browser stays the only option there.

/** True when running under Electron with the native picker bridge available. */
function _hasNativePicker() {
    return !!(window.electronAPI && typeof window.electronAPI.pickPaths === 'function');
}

/** Open the native dialog for `mode` ("folder" | "files"), then scan the result. */
async function nativePick(mode) {
    let paths = [];
    try {
        paths = await window.electronAPI.pickPaths(mode);
    } catch (err) {
        showStatus((t('error.browse_failed') || 'Picker failed') + ': ' + err.message, 'error');
        return;
    }
    if (!Array.isArray(paths) || paths.length === 0) return;   // cancelled / empty
    // A comma-joined list is the scan contract: a single folder has no comma and
    // takes the directory branch; multiple folders/files are expanded server-side.
    scanPath.value = paths.join(',');
    scanFolder();
}

/** Pop a small folder/files chooser anchored under the Browse button. */
function showNativePicker(anchor) {
    // Clicking Browse again while the menu is open closes it (toggle).
    const existing = document.getElementById('native-pick-menu');
    if (existing) { existing.remove(); return; }

    const menu = document.createElement('div');
    menu.id = 'native-pick-menu';
    menu.className = 'glass-panel native-pick-menu';
    menu.innerHTML = `
        <button class="glass-btn" data-mode="folder">${escapeHtml(t('modal.browse_select_folder') || '📁 Select Folder')}</button>
        <button class="glass-btn" data-mode="files">${escapeHtml(t('modal.browse_select_files') || '📄 Select Files')}</button>
    `;
    document.body.appendChild(menu);

    const r = anchor.getBoundingClientRect();
    menu.style.top  = `${Math.round(r.bottom + 6)}px`;
    menu.style.left = `${Math.round(r.left)}px`;

    const dismiss = () => {
        menu.remove();
        document.removeEventListener('mousedown', onOutside, true);
        document.removeEventListener('keydown', onKey, true);
    };
    const onOutside = (e) => {
        if (!menu.contains(e.target) && e.target !== anchor && !anchor.contains(e.target)) dismiss();
    };
    const onKey = (e) => { if (e.key === 'Escape') dismiss(); };

    menu.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-mode]');
        if (!btn) return;
        dismiss();
        nativePick(btn.dataset.mode);
    });

    // Defer so the click that opened the menu doesn't immediately dismiss it.
    setTimeout(() => {
        document.addEventListener('mousedown', onOutside, true);
        document.addEventListener('keydown', onKey, true);
    }, 0);
}

// ═══ Embed-in-progress guard ═══
// Tracks whether Phase-2 metadata embedding is currently running.
// Used to:
//   1. Show/update the sticky bottom banner.
//   2. Warn before the user closes or navigates away (beforeunload).
//   3. Prefix the window title so it's visible in the taskbar.
let _embedInProgress = false;
const _ORIG_TITLE = document.title;

// Register once at startup. The handler is a no-op when no embed is running.
window.addEventListener('beforeunload', (e) => {
    if (!_embedInProgress) return;
    // Standard pattern: preventDefault + returnValue triggers the browser's
    // built-in "Leave site?" dialog. Custom messages are blocked by all
    // modern browsers for security reasons.
    e.preventDefault();
    e.returnValue = '';
});

function _setEmbedBanner(text) {
    const banner = document.getElementById('embed-banner');
    const label  = document.getElementById('embed-banner-text');
    if (!banner || !label) return;
    label.textContent = text;
    banner.classList.remove('hidden');
}

function _clearEmbedBanner() {
    const banner = document.getElementById('embed-banner');
    if (banner) banner.classList.add('hidden');
    document.title = _ORIG_TITLE;
    _embedInProgress = false;
}


