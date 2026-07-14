// ═══ Browse Modal ═══

// Keyboard file-selection state (files mode). browseFocusIdx is the row the
// arrow keys move and Space toggles; browseAnchorIdx is the fixed end of a
// Shift range. Both index into window._browsePaths and reset on every reload.
let browseFocusIdx  = -1;
let browseAnchorIdx = -1;

async function openBrowseModal() {
    browseModal.classList.remove('hidden');
    // Derive a directory to start from. If scanPath contains files or a
    // comma-separated list, walk up to the parent directory so the browse
    // API never receives a file path.
    let startPath = (scanPath.value || '').split(',')[0].trim() || _getDefaultBrowsePath();
    const lastSlash = startPath.lastIndexOf('/');
    const lastName  = startPath.substring(lastSlash + 1);
    if (lastName.includes('.')) {
        // Looks like a filename — use its parent directory
        startPath = startPath.substring(0, lastSlash) || _getDefaultBrowsePath();
    }
    currentBrowsePath = startPath || _getDefaultBrowsePath();
    browseHistory = [];
    selectedFiles = [];
    // Reflect the persisted "Show hidden" choice in the checkbox before loading.
    const hiddenCb = document.getElementById('browse-show-hidden');
    if (hiddenCb) hiddenCb.checked = _browseShowHidden();
    updateBrowseSelectionMode(browseSelectionMode);
    loadBrowseDirectory(currentBrowsePath);
}

// Whether the picker should include dot-files/dirs. Persisted so the choice
// survives reopening the modal and restarting the app.
function _browseShowHidden() {
    return localStorage.getItem('amm_browse_show_hidden') === '1';
}

async function loadBrowseDirectory(path) {
    document.getElementById('browse-path').textContent = path;
    document.getElementById('browse-list').innerHTML = '<div style="text-align:center;padding:20px;">Loading...</div>';

    try {
        const response = await fetch(`/api/browse?path=${encodeURIComponent(path)}&show_hidden=${_browseShowHidden()}`);
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

        // Show files if in file selection mode. Clicks go through a single
        // delegated handler (onBrowseFileClick) so Shift-range and Ctrl-toggle
        // work; the checkbox is a visual indicator only (pointer-events:none)
        // so a click anywhere on the row is handled the same way.
        if (browseSelectionMode === 'files') {
            html += files.map((item, i) => {
                const isSelected = selectedFiles.some(f => f.path === item.path);
                return `
                    <div class="browse-item file ${isSelected ? 'selected' : ''}" data-fidx="${i}">
                        <input type="checkbox" ${isSelected ? 'checked' : ''} tabindex="-1"
                               style="pointer-events:none;">
                        📄 ${escapeHtml(item.name)}
                    </div>
                `;
            }).join('');
        }

        // A fresh listing invalidates the old focus/anchor indices.
        browseFocusIdx  = -1;
        browseAnchorIdx = -1;

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

    // Keyboard-selection hint is only relevant when picking files.
    const hint = document.getElementById('browse-kbd-hint');
    if (hint) hint.style.display = (mode === 'files') ? 'block' : 'none';
    
    // Reload current directory to show/hide files
    if (currentBrowsePath) {
        loadBrowseDirectory(currentBrowsePath);
    }
}

// ── Index-based file selection (supports checkbox click, Shift-range and
//    keyboard navigation, all sharing one source of truth) ──────────────────

/** True if the file at index `idx` (into _browsePaths) is currently selected. */
function _isFileIdxSelected(idx) {
    const p = (window._browsePaths || [])[idx];
    return p != null && selectedFiles.some(f => f.path === p);
}

/** Select/deselect the file at `idx` and sync its row's checkbox + highlight. */
function _setFileIdxSelected(idx, selected) {
    const p = (window._browsePaths || [])[idx];
    if (p == null) return;
    const at = selectedFiles.findIndex(f => f.path === p);
    if (selected && at === -1)      selectedFiles.push({ path: p });
    else if (!selected && at !== -1) selectedFiles.splice(at, 1);

    const row = document.querySelector(`.browse-item.file[data-fidx="${idx}"]`);
    if (row) {
        row.classList.toggle('selected', selected);
        const cb = row.querySelector('input[type="checkbox"]');
        if (cb) cb.checked = selected;
    }
}

function _toggleFileIdx(idx) { _setFileIdxSelected(idx, !_isFileIdxSelected(idx)); }

/** Select every file between two indices inclusive (Shift-range). */
function _selectFileRange(a, b) {
    const lo = Math.min(a, b), hi = Math.max(a, b);
    for (let i = lo; i <= hi; i++) _setFileIdxSelected(i, true);
    updateSelectionCounter();
}

/** Select all files in the current listing (Ctrl/Cmd+A). */
function _selectAllFiles() {
    const n = (window._browsePaths || []).length;
    for (let i = 0; i < n; i++) _setFileIdxSelected(i, true);
    updateSelectionCounter();
}

/** Move the keyboard focus ring to file `idx`, scrolling it into view. */
function _setBrowseFocus(idx) {
    const n = (window._browsePaths || []).length;
    if (n === 0) { browseFocusIdx = -1; return; }
    idx = Math.max(0, Math.min(n - 1, idx));
    document.querySelectorAll('.browse-item.file.focused')
        .forEach(el => el.classList.remove('focused'));
    const row = document.querySelector(`.browse-item.file[data-fidx="${idx}"]`);
    if (row) {
        row.classList.add('focused');
        row.scrollIntoView({ block: 'nearest' });
    }
    browseFocusIdx = idx;
}

/** Delegated click on a file row — handles plain, Ctrl (toggle) and Shift (range). */
function onBrowseFileClick(e, idx) {
    if (e.shiftKey && browseAnchorIdx >= 0) {
        _selectFileRange(browseAnchorIdx, idx);
    } else {
        _toggleFileIdx(idx);
        browseAnchorIdx = idx;
        updateSelectionCounter();
    }
    _setBrowseFocus(idx);
}

// Back-compat shim: select/deselect a single file by its path.
function toggleFileSelection(filePath) {
    const idx = (window._browsePaths || []).indexOf(filePath);
    if (idx === -1) return;
    _toggleFileIdx(idx);
    browseAnchorIdx = idx;
    _setBrowseFocus(idx);
    updateSelectionCounter();
}

// Keyboard selection (files mode): ↑/↓ move focus, Space toggles, Shift+↑/↓
// extends a range, Ctrl/Cmd+A selects all, Home/End jump, Enter confirms.
function onBrowseKeydown(e) {
    if (browseModal.classList.contains('hidden')) return;
    if (browseSelectionMode !== 'files') return;
    const n = (window._browsePaths || []).length;
    if (n === 0) return;

    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const step = e.key === 'ArrowDown' ? 1 : -1;
        const next = browseFocusIdx < 0 ? 0 : browseFocusIdx + step;
        if (e.shiftKey) {
            if (browseAnchorIdx < 0) browseAnchorIdx = browseFocusIdx < 0 ? 0 : browseFocusIdx;
            _selectFileRange(browseAnchorIdx, Math.max(0, Math.min(n - 1, next)));
        }
        _setBrowseFocus(next);
    } else if (e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault();
        if (browseFocusIdx < 0) _setBrowseFocus(0);
        _toggleFileIdx(browseFocusIdx);
        browseAnchorIdx = browseFocusIdx;
        updateSelectionCounter();
    } else if ((e.ctrlKey || e.metaKey) && (e.key === 'a' || e.key === 'A')) {
        e.preventDefault();
        _selectAllFiles();
        browseAnchorIdx = 0;
        _setBrowseFocus(n - 1);
    } else if (e.key === 'Home') {
        e.preventDefault(); _setBrowseFocus(0);
    } else if (e.key === 'End') {
        e.preventDefault(); _setBrowseFocus(n - 1);
    } else if (e.key === 'Enter') {
        if (selectedFiles.length > 0) { e.preventDefault(); selectBrowseFolder(); }
    }
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

// Wire the delegated file-row click handler and the keyboard listener once.
// File rows carry data-fidx (no inline onclick) so Shift/Ctrl modifiers reach
// onBrowseFileClick; directory rows keep their own inline navigateDir handler.
(function initBrowseInput() {
    const list = document.getElementById('browse-list');
    if (list) {
        list.addEventListener('click', (e) => {
            const row = e.target.closest('.browse-item.file');
            if (!row || !list.contains(row)) return;
            const idx = parseInt(row.dataset.fidx, 10);
            if (!Number.isNaN(idx)) onBrowseFileClick(e, idx);
        });
    }
    document.addEventListener('keydown', onBrowseKeydown);

    // "Show hidden" toggle: persist the choice and reload the current directory.
    const hiddenCb = document.getElementById('browse-show-hidden');
    if (hiddenCb) {
        hiddenCb.addEventListener('change', () => {
            localStorage.setItem('amm_browse_show_hidden', hiddenCb.checked ? '1' : '0');
            if (currentBrowsePath) loadBrowseDirectory(currentBrowsePath);
        });
    }
})();

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

        const note = `<div class="history-note">${escapeHtml(t('history.revert_note'))}</div>`;
        const rows = data.entries.map(entry => {
            // Grouped rename (F10): companion rows fold into the "+N companions"
            // chip on their primary (video) row — reverting it restores the set.
            if (entry.group_id && !entry.group_primary) return '';
            const chip = entry.companions > 0
                ? `<span class="history-companions-chip">${escapeHtml(t('history.companions_chip', { n: entry.companions }))}</span>`
                : '';
            return `
            <div class="history-item">
                <div class="history-head">
                    <span class="history-action">${escapeHtml(entry.action.toUpperCase())}</span>
                    ${chip}
                    <span class="history-time">${escapeHtml(entry.timestamp)}</span>
                    ${entry.revertible
                        ? `<button class="glass-btn history-revert-btn" data-id="${escapeHtml(entry.id)}">${escapeHtml(t('history.revert'))}</button>`
                        : ''}
                </div>
                <div class="history-path">${escapeHtml(t('history.from'))} ${escapeHtml(entry.old_path)}</div>
                <div class="history-path">${escapeHtml(t('history.to'))} ${escapeHtml(entry.new_path)}</div>
                ${entry.error ? `<div style="color:var(--error);font-size:11px;margin-top:4px;">${escapeHtml(entry.error)}</div>` : ''}
            </div>
        `;
        }).join('');
        document.getElementById('history-list').innerHTML = note + rows;

    } catch (error) {
        document.getElementById('history-list').innerHTML = `<div style="color:var(--error);text-align:center;padding:20px;">Error: ${escapeHtml(error.message)}</div>`;
    }
}

async function undoLastRename() {
    showConfirmModal(t('history.undo_confirm'), async () => {
        try {
            const response = await fetch('/api/history/undo', {method: 'POST'});
            if (!response.ok) throw new Error('Failed to undo');

            const data = await response.json();
            if (data.success) {
                showToast(t('history.undo'), t('undo.success'), 'success');
                loadHistory();
            } else {
                showToast(t('history.undo'), t('undo.none'), 'info');
            }

        } catch (error) {
            showToast(t('history.undo'), error.message, 'error');
        }
    });
}

/** Map a server revert `code` to a localised message. */
function _revertCodeMsg(code) {
    const map = {
        already_reverted: 'history.revert_already',
        source_exists:    'history.revert_source_exists',
        not_revertible:   'history.revert_not_revertible',
        forbidden:        'history.revert_forbidden',
        error:            'history.revert_error',
    };
    return t(map[code] || 'history.revert_error');
}

/** Revert a single history entry by id (per-row Revert button). */
async function revertHistoryEntry(id, btnEl) {
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = t('history.reverting'); }
    try {
        const resp = await fetch('/api/history/revert', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ id }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            showToast(t('history.revert_title'), data.detail || resp.statusText, 'error');
            return;
        }
        if (data.success) {
            // Grouped revert (F10): report how many of the set came back.
            if (Array.isArray(data.group) && data.group.length > 1) {
                const ok = data.group.filter(g => g.ok).length;
                showToast(t('history.revert_title'),
                          t('history.group_reverted', { n: ok, total: data.group.length }),
                          'success');
            } else {
                showToast(t('history.revert_title'), t('history.reverted_ok'), 'success');
            }
        } else {
            showToast(t('history.revert_title'), _revertCodeMsg(data.code), 'info');
        }
        loadHistory();   // refresh — a successful revert adds an "undo" entry
    } catch (error) {
        showToast(t('history.revert_title'), error.message, 'error');
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = t('history.revert'); }
    }
}

// ═══ Library Modal (catalog stats + duplicates) ═══
// Surfaces the catalog data that the backend already computes but nothing
// displayed: /api/catalog/stats (totals) and /api/catalog/duplicates (groups of
// identical or — with AMM_SCAN_PHASH — near-identical files). Read-only; the only
// action is dropping a copy from the current working list (never deletes on disk).
let _libraryGroups = [];   // last-fetched duplicate groups, for in-place re-render

async function openLibraryModal() {
    document.getElementById('library-modal').classList.remove('hidden');
    const statsEl = document.getElementById('library-stats');
    const dupesEl  = document.getElementById('library-dupes');
    statsEl.innerHTML = `<div class="library-loading">${escapeHtml(t('library.loading'))}</div>`;
    dupesEl.innerHTML = '';

    try {
        const [statsResp, dupesResp] = await Promise.all([
            fetch('/api/catalog/stats'),
            fetch('/api/catalog/duplicates'),
        ]);
        if (!statsResp.ok || !dupesResp.ok) throw new Error(t('library.load_failed'));
        const stats = await statsResp.json();
        _libraryGroups = (await dupesResp.json()).groups || [];

        statsEl.innerHTML = _libraryStatTiles(stats);
        _renderLibraryDupes();
    } catch (err) {
        statsEl.innerHTML = `<div class="library-error">${escapeHtml(err.message)}</div>`;
    }
}

function _libraryStatTiles(s) {
    const tile = (labelKey, val) =>
        `<div class="library-tile"><span class="library-tile-num">${Number(val) || 0}</span>` +
        `<span class="library-tile-label">${escapeHtml(t(labelKey))}</span></div>`;
    return tile('library.tracked', s.total)
         + tile('library.organized', s.organized)
         + tile('library.confirmed', s.confirmed)
         + tile('library.duplicate_groups', s.duplicates);
}

// A duplicate path is only "removable" here if it's actually in the current
// scan/match working list — otherwise Remove would be a confusing no-op, so we
// omit the button and leave the row informational.
function _isInWorkingList(path) {
    try {
        return (typeof matchedResults !== 'undefined' &&
                matchedResults.some(r => r.original && r.original.path === path))
            || (typeof scannedFiles !== 'undefined' &&
                scannedFiles.some(f => f.path === path));
    } catch (_) { return false; }
}

function _renderLibraryDupes() {
    const dupesEl = document.getElementById('library-dupes');
    if (!_libraryGroups.length) {
        dupesEl.innerHTML = `<div class="library-empty">${escapeHtml(t('library.dupes_none'))}</div>`;
        return;
    }
    dupesEl.innerHTML = _libraryGroups.map((g, gi) => {
        const kindLabel = g.kind === 'phash' ? t('library.similar') : t('library.identical');
        const paths = g.paths || [];
        const sizes = g.sizes || [];
        const rows = paths.map((p, i) => {
            const size = formatFileSize(sizes[i]);
            const removeBtn = _isInWorkingList(p)
                ? `<button class="glass-btn library-remove" data-path="${escapeHtml(p)}">✕ ${escapeHtml(t('match.remove'))}</button>`
                : '';
            return `<div class="library-dupe-row">
                        <span class="library-dupe-path" title="${escapeHtml(p)}">${escapeHtml(p)}</span>
                        <span class="library-dupe-size">${escapeHtml(size)}</span>
                        ${removeBtn}
                    </div>`;
        }).join('');
        // Resolve (F16) is offered ONLY for byte-identical groups — pHash groups
        // are different encodes of the same scene, not safe to auto-reclaim
        // (the server re-verifies content anyway and would refuse them).
        const resolveBtn = g.kind === 'oshash' && paths.length > 1
            ? `<button class="glass-btn library-resolve-btn" data-group="${gi}">${escapeHtml(t('library.resolve'))}</button>`
            : '';
        return `<div class="glass-panel library-group" data-group="${gi}">
                    <div class="library-group-head">
                        <span class="library-group-kind library-kind-${escapeHtml(g.kind)}">${escapeHtml(kindLabel)}</span>
                        <span class="library-group-count">${escapeHtml(t('library.copies', { n: g.count }))}</span>
                        ${resolveBtn}
                    </div>
                    ${rows}
                    <div class="library-resolve-panel" id="resolve-panel-${gi}" hidden></div>
                </div>`;
    }).join('');
}

// ── Duplicate resolution (F16) ────────────────────────────────────────────────

/** Open the inline Resolve panel for group `gi`: keep-radios (largest
 *  pre-selected), mode select, live savings line, and — for delete — a typed
 *  confirmation word gating the button. */
function _openResolvePanel(gi) {
    const g = _libraryGroups[gi];
    const panel = document.getElementById(`resolve-panel-${gi}`);
    if (!g || !panel) return;
    const paths = g.paths || [];
    const sizes = g.sizes || [];
    let largest = 0;
    sizes.forEach((s, i) => { if ((s || 0) > (sizes[largest] || 0)) largest = i; });

    const radios = paths.map((p, i) => `
        <label class="library-resolve-row">
            <input type="radio" name="resolve-keep-${gi}" value="${escapeHtml(p)}" ${i === largest ? 'checked' : ''}>
            <span class="library-dupe-path" title="${escapeHtml(p)}">${escapeHtml(p)}</span>
            <span class="library-dupe-size">${escapeHtml(formatFileSize(sizes[i]))}</span>
        </label>`).join('');

    const word = t('library.resolve_confirm_word');
    panel.innerHTML = `
        <div class="library-resolve-keep-label">${escapeHtml(t('library.resolve_keep'))}</div>
        ${radios}
        <div class="library-resolve-controls">
            <select class="glass-input resolve-mode" id="resolve-mode-${gi}">
                <option value="hardlink">${escapeHtml(t('library.resolve_hardlink'))}</option>
                <option value="delete">${escapeHtml(t('library.resolve_delete'))}</option>
            </select>
            <span class="library-resolve-savings" id="resolve-savings-${gi}"></span>
        </div>
        <div class="library-resolve-confirm" id="resolve-confirm-wrap-${gi}" hidden>
            <input type="text" class="glass-input" id="resolve-confirm-${gi}"
                   placeholder="${escapeHtml(word)}" autocomplete="off" spellcheck="false">
        </div>
        <div class="library-resolve-actions">
            <button class="btn btn-secondary resolve-cancel" data-group="${gi}">${escapeHtml(t('settings.cancel'))}</button>
            <button class="btn btn-primary resolve-go" id="resolve-go-${gi}" data-group="${gi}">${escapeHtml(t('library.resolve'))}</button>
        </div>`;
    panel.hidden = false;

    const update = () => {
        const keep = panel.querySelector(`input[name="resolve-keep-${gi}"]:checked`)?.value;
        const keepIdx = paths.indexOf(keep);
        const savings = sizes.reduce((a, s, i) => a + (i === keepIdx ? 0 : (s || 0)), 0);
        document.getElementById(`resolve-savings-${gi}`).textContent =
            t('library.resolve_savings', { size: formatFileSize(savings) });
        const mode = document.getElementById(`resolve-mode-${gi}`).value;
        const wrap = document.getElementById(`resolve-confirm-wrap-${gi}`);
        wrap.hidden = mode !== 'delete';
        const goBtn = document.getElementById(`resolve-go-${gi}`);
        const typed = (document.getElementById(`resolve-confirm-${gi}`)?.value || '').trim();
        goBtn.disabled = mode === 'delete' && typed !== word;
    };
    panel.addEventListener('change', update);
    panel.addEventListener('input', update);
    update();
}

async function _resolveGroup(gi) {
    const g = _libraryGroups[gi];
    const panel = document.getElementById(`resolve-panel-${gi}`);
    if (!g || !panel) return;
    const keep = panel.querySelector(`input[name="resolve-keep-${gi}"]:checked`)?.value;
    if (!keep) return;
    const mode = document.getElementById(`resolve-mode-${gi}`).value;
    const remove = (g.paths || []).filter(p => p !== keep);
    const goBtn = document.getElementById(`resolve-go-${gi}`);
    goBtn.disabled = true;
    try {
        const resp = await fetch('/api/catalog/resolve-duplicates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keep, remove, mode }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            const detail = data.detail || {};
            const code = typeof detail === 'object' ? detail.code : null;
            const msg = code === 'not_same_fs'
                ? t('library.resolve_not_same_fs')
                : (typeof detail === 'string' ? detail : (code || resp.statusText));
            showToast(t('library.resolve'), msg, 'error', 8000);
            return;
        }
        showToast(t('library.resolve'),
                  t('library.resolve_done', { size: formatFileSize(data.freed_bytes || 0) }),
                  data.success ? 'success' : 'info', 8000);
        openLibraryModal();   // re-fetch stats + groups; resolved group disappears
    } catch (err) {
        showToast(t('library.resolve'), err.message, 'error', 8000);
    } finally {
        goBtn.disabled = false;
    }
}

// Delegated Remove handler: drop the chosen copy from the working list (reuses
// match.js:removeMatchedFile — hides it from the app, never deletes on disk),
// then re-render so the button disappears for the now-removed path.
(function initLibraryInput() {
    const dupes = document.getElementById('library-dupes');
    if (!dupes) return;
    dupes.addEventListener('click', (e) => {
        const resolveBtn = e.target.closest('.library-resolve-btn');
        if (resolveBtn) { _openResolvePanel(Number(resolveBtn.dataset.group)); return; }
        const cancelBtn = e.target.closest('.resolve-cancel');
        if (cancelBtn) {
            const panel = document.getElementById(`resolve-panel-${cancelBtn.dataset.group}`);
            if (panel) { panel.hidden = true; panel.innerHTML = ''; }
            return;
        }
        const goBtn = e.target.closest('.resolve-go');
        if (goBtn && !goBtn.disabled) { _resolveGroup(Number(goBtn.dataset.group)); return; }
        const btn = e.target.closest('.library-remove');
        if (!btn || !btn.dataset.path) return;
        if (typeof removeMatchedFile === 'function') removeMatchedFile(btn.dataset.path);
        _renderLibraryDupes();
    });
})();

