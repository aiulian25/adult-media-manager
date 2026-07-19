// ═══ Scan Folder (streaming + cancellable) ═══
//
// The scan streams results over SSE so the UI can render incrementally AND the
// user can STOP a long scan at any time (Stop button). Closing the EventSource
// disconnects the request; the server detects it between files and stops walking,
// keeping whatever was already scanned — those partial results are shown with a
// clear notice that not every file was scanned/matched.
let _scanEventSource = null;   // active SSE handle, or null
let _scanStopped = false;      // true when the user pressed Stop
let _scanResolve = null;       // resolves the scanFolder() promise on stop

// Toggle the Stop button in for the Scan button while a scan is running.
function _setScanRunning(running) {
    if (btnStopScan) btnStopScan.classList.toggle('hidden', !running);
    if (btnScan)     btnScan.classList.toggle('hidden', running);
    if (btnScan)     btnScan.disabled = running;
}

// Stop the in-flight scan, keeping the results received so far.
function stopScan() {
    if (!_scanEventSource) return;
    _scanStopped = true;
    try { _scanEventSource.close(); } catch (_) { /* ignore */ }
    _scanEventSource = null;
    _finishScan({ stopped: true, path: scanPath.value.trim() });
    if (_scanResolve) { _scanResolve(); _scanResolve = null; }
}

async function scanFolder() {
    const path = scanPath.value.trim();
    if (!path) {
        showStatus(t('error.no_path'), 'error');
        return;
    }

    showStatus(t('status.scanning'));
    progressFill.style.width = '0%';
    btnScan.disabled = true;
    btnMatch.disabled = true;
    _scanStopped = false;
    scannedFiles = [];

    // Stage 1: register a server-side session (POST avoids URL-size limits and
    // runs path validation up front, returning 422 on a bad/missing path).
    let sessionId;
    try {
        const sessResp = await fetch('/api/scan-session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                path,
                recursive: recursive.checked,
                skip_organized: skipOrganized ? skipOrganized.checked : false,
                include_hidden: document.getElementById('include-hidden')?.checked || false,
            }),
        });
        if (!sessResp.ok) {
            let errMsg = 'Scan failed';
            try {
                const err = await sessResp.json();
                if (Array.isArray(err.detail)) {
                    errMsg = err.detail.map(e => e.msg || JSON.stringify(e)).join('; ');
                } else if (typeof err.detail === 'string') {
                    errMsg = err.detail;
                } else if (err.detail) {
                    errMsg = JSON.stringify(err.detail);
                }
            } catch (_) {
                errMsg = await sessResp.text().catch(() => errMsg);
            }
            throw new Error(errMsg);
        }
        sessionId = (await sessResp.json()).session_id;
    } catch (error) {
        showStatus(t('error.rename_failed', { message: error.message }), 'error');
        progressFill.style.width = '0%';
        btnScan.disabled = false;
        return;
    }

    // Stage 2: open the SSE stream and accumulate results as they arrive.
    _setScanRunning(true);
    await new Promise((resolve) => {
        _scanResolve = resolve;
        const es = new EventSource(`/api/scan-stream?session_id=${encodeURIComponent(sessionId)}`);
        _scanEventSource = es;

        es.addEventListener('progress', (e) => {
            const d = JSON.parse(e.data);
            const pct = d.total > 0 ? Math.round((d.done / d.total) * 100) : 0;
            progressFill.style.width = `${pct}%`;
            const short = d.filename && d.filename.length > 60
                ? '…' + d.filename.slice(-57) : (d.filename || '');
            showStatus(t('status.scanning_file', { done: d.done, total: d.total, filename: short }));
        });

        es.addEventListener('result', (e) => {
            const d = JSON.parse(e.data);
            if (d.file) scannedFiles.push(d.file);
        });

        es.addEventListener('done', (e) => {
            es.close();
            _scanEventSource = null;
            const d = JSON.parse(e.data);
            _finishScan({ stopped: !!d.stopped, path, skipped: d.skipped });
            _scanResolve = null;
            resolve();
        });

        es.addEventListener('error', (e) => {
            es.close();
            _scanEventSource = null;
            _scanResolve = null;
            // A user-initiated Stop closes the stream itself (handled in stopScan),
            // so a stray error after that is expected — ignore it.
            if (_scanStopped) { resolve(); return; }
            let serverDetail = null;
            if (e.data) { try { serverDetail = JSON.parse(e.data).detail; } catch (_) {} }
            // If results already streamed before the connection dropped, keep them
            // and treat it like a stop; otherwise surface the error state.
            if (scannedFiles.length > 0) {
                _finishScan({ stopped: true, path });
            } else {
                _finishScan({ error: serverDetail || 'connection error', path });
            }
            resolve();
        });
    });
}

// Finalise a scan run: render results (or the right empty/error/stopped state),
// reset the buttons, and — when the scan was cut short — explain that not every
// file was scanned (so not all will be matched or available for manual edit).
function _finishScan({ stopped = false, error = null, path = '', skipped = null } = {}) {
    _setScanRunning(false);
    btnScan.disabled = false;
    progressFill.style.width = '100%';

    // "K skipped (not media/hidden/unreadable)" note (F9) — explains the gap when
    // a drop/scan surfaces fewer files than expected. Appended to the found line.
    const nSkipped = skipped
        ? (skipped.non_media || 0) + (skipped.hidden || 0) + (skipped.unreadable || 0)
        : 0;
    const skipSuffix = nSkipped > 0 ? ' · ' + t('status.scan_skipped', { count: nSkipped }) : '';

    if (error) {
        showStatus(t('status.scan_failed'), 'error');
        _renderEmptyState('⚠️', t('empty.scan_error_title'),
                          t('empty.scan_error_subtitle', { error }));
        btnMatch.disabled = true;
        progressFill.style.width = '0%';
        return;
    }

    if (scannedFiles.length === 0) {
        if (stopped) {
            showStatus(t('status.scan_stopped', { count: 0 }), 'info');
            _renderEmptyState('🛑', t('empty.scan_stopped_title'),
                              t('empty.scan_stopped_subtitle'));
        } else if (skipOrganized && skipOrganized.checked) {
            showStatus(t('status.found', { count: 0 }) + skipSuffix, 'info');
            _renderEmptyState('✅', t('empty.all_organized_title'),
                              t('empty.all_organized_subtitle', { path }));
        } else {
            showStatus(t('status.found', { count: 0 }) + skipSuffix, 'info');
            _renderEmptyState('🔍', t('empty.scan_title'),
                              t('empty.scan_subtitle', { path }));
        }
        btnMatch.disabled = true;
        progressFill.style.width = '0%';
        return;
    }

    displayScannedFiles();
    btnMatch.disabled = false;

    if (stopped) {
        // Partial results: tell the user the scan was cut short and that any
        // unscanned files won't be matched or available for manual edit.
        showStatus(t('status.scan_stopped', { count: scannedFiles.length }), 'info');
        _renderScanStoppedNotice(scannedFiles.length);
        progressFill.style.width = '0%';
    } else {
        showStatus(t('status.found', { count: scannedFiles.length }) + skipSuffix, 'success');
        setTimeout(() => {
            statusBar.classList.add('hidden');
            progressFill.style.width = '0%';
        }, nSkipped > 0 ? 4500 : 2000);   // linger longer when there's a skip note
    }
}

// Prepend a dismissible warning banner above the (partial) scanned list.
function _renderScanStoppedNotice(count) {
    if (!resultsContainer || resultsContainer.querySelector('.scan-stopped-notice')) return;
    const banner = document.createElement('div');
    banner.className = 'glass-panel scan-stopped-notice';
    banner.innerHTML = `
        <span class="scan-stopped-icon">🛑</span>
        <div>
            <div class="scan-stopped-title">${escapeHtml(t('scan.stopped_banner_title', { count }))}</div>
            <div class="scan-stopped-sub">${escapeHtml(t('scan.stopped_banner_sub'))}</div>
        </div>`;
    resultsContainer.insertBefore(banner, resultsContainer.firstChild);
}

// Cap how many file rows are injected into the DOM up front. A recursive scan
// of a large library can return thousands of files; rendering them all at once
// builds a huge DOM and can freeze the tab. The rest render on demand via a
// "Show all" button. Selection is tracked in selectedScannedIndices (a Set),
// NOT in the DOM, so capping never changes which files get matched.
const SCAN_RENDER_CAP = 300;

// Row markup builders — defined once and reused for the initial render and the
// "Show all" expansion. Checkbox state is derived from the selection Set so
// rows rendered later stay correct after Select-All / deselect actions.
function _scannedNewRowHtml(i, file) {
    const checked = selectedScannedIndices.has(i) ? 'checked' : '';
    return `
        <div class="file-item glass-panel selectable" id="scanned-item-${i}">
            <label class="file-checkbox-wrap">
                <input type="checkbox" class="file-cb" data-index="${i}" ${checked}
                       onchange="toggleScannedFile(${i}, this.checked)">
            </label>
            <div class="file-info">
                <div class="file-name">${escapeHtml(file.filename)}</div>
                <div class="file-meta">
                    ${file.site ? `<span class="badge site-badge">${escapeHtml(file.site)}</span>` : ''}
                    ${file.context_source === 'folder' ? `<span class="badge" title="${escapeHtml(t('scan.from_folder_hint'))}">📁</span>` : ''}
                    ${file.performers && file.performers.length > 0 ? `<span>${file.performers.map(escapeHtml).join(', ')}</span>` : ''}
                    ${file.quality ? `<span class="badge quality-badge">${escapeHtml(file.quality)}</span>` : ''}
                    ${file.release_date ? `<span>${escapeHtml(file.release_date)}</span>` : ''}
                </div>
            </div>
        </div>`;
}

function _scannedOrgRowHtml(i, file) {
    const nfo = file.nfo_metadata || {};
    const checked = selectedScannedIndices.has(i) ? 'checked' : '';
    return `
        <div class="file-item glass-panel" id="scanned-item-${i}"
             style="opacity:.7;border:1px solid rgba(0,255,136,.15);">
            <label class="file-checkbox-wrap">
                <input type="checkbox" class="file-cb" data-index="${i}" ${checked}
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
            <div style="font-size:.72rem;color:rgba(0,255,136,.7);white-space:nowrap;align-self:center;">✅ ${escapeHtml(t('scan.has_nfo'))}</div>
        </div>`;
}

/**
 * Render `entries` ([globalIndex, file] pairs) into `listEl`, capping the
 * initial batch at SCAN_RENDER_CAP and appending a "Show all" button for the
 * remainder. Uses insertAdjacentHTML so each batch is parsed once and appended,
 * rather than re-serialising the whole list.
 */
function _renderScannedRows(listEl, entries, rowHtmlFn) {
    if (!listEl) return;
    const head = entries.slice(0, SCAN_RENDER_CAP);
    listEl.insertAdjacentHTML('beforeend', head.map(([i, f]) => rowHtmlFn(i, f)).join(''));

    const rest = entries.slice(SCAN_RENDER_CAP);
    if (rest.length === 0) return;

    const btn = document.createElement('button');
    btn.className = 'glass-btn show-all-btn';
    btn.textContent = t('scan.show_all', { count: rest.length });
    btn.addEventListener('click', () => {
        btn.remove();
        listEl.insertAdjacentHTML('beforeend', rest.map(([i, f]) => rowHtmlFn(i, f)).join(''));
        _updateScannedUI();   // sync dim/selection state for the newly added rows
    });
    listEl.appendChild(btn);
}

// Directory of a file path (everything before the last "/").
function _dirOf(path) {
    const i = path.lastIndexOf('/');
    return i >= 0 ? path.slice(0, i) : '';
}
// Filename stem (drop the last extension).
function _stemOf(name) {
    const i = name.lastIndexOf('.');
    return i > 0 ? name.slice(0, i) : name;
}
// True when a subtitle entry has a sibling *video* in the same scan/dir — i.e. it
// will be carried along by that video's rename (F2 backend companion move), so it
// need not appear as a standalone match row. Handles multi-part langs
// ("Scene.eng.srt" → tries "Scene.eng" then "Scene") against the video stems.
function _subtitleHasSiblingVideo(sub, videoStemsByDir) {
    const set = videoStemsByDir.get(_dirOf(sub.path));
    if (!set) return false;
    let base = _stemOf(sub.filename);          // strip ".srt"  → "Scene.eng"
    while (base.length) {
        if (set.has(base)) return true;
        const d = base.lastIndexOf('.');
        if (d <= 0) break;
        base = base.slice(0, d);               // "Scene.eng" → "Scene"
    }
    return false;
}

function displayScannedFiles() {
    // Map of directory → set of video stems, so subtitle companions can be hidden
    // when their video is in the same scan (only subtitles are scanned as
    // companions; NFO/artwork are not media and never appear here).
    const videoStemsByDir = new Map();
    scannedFiles.forEach(f => {
        if (f.is_companion) return;            // non-companion scan rows are videos
        const dir = _dirOf(f.path);
        if (!videoStemsByDir.has(dir)) videoStemsByDir.set(dir, new Set());
        videoStemsByDir.get(dir).add(_stemOf(f.filename));
    });

    // Single pass: partition into new vs already-organised, keeping each file's
    // global index. (Replaces a per-row scannedFiles.indexOf() that was O(n²).)
    // Subtitle companions with a sibling video are dropped from BOTH lists — they
    // ride along with the video on rename — but stay in scannedFiles so the
    // "found N" count remains honest (nothing silently disappears).
    const newEntries = [];   // [globalIndex, file]
    const orgEntries = [];
    scannedFiles.forEach((f, i) => {
        if (f.is_companion && _subtitleHasSiblingVideo(f, videoStemsByDir)) return;
        (f.already_organized ? orgEntries : newEntries).push([i, f]);
    });

    // Only new files are selected by default — never touch already-organised ones
    selectedScannedIndices = new Set(newEntries.map(([i]) => i));

    // ── Already-organised collapsed section (shell; rows filled below) ──
    const organizedSection = orgEntries.length === 0 ? '' : `
        <details class="glass-panel" style="padding:16px;margin-bottom:12px;border:1px solid rgba(0,255,136,.25);">
            <summary style="cursor:pointer;display:flex;align-items:center;gap:10px;list-style:none;user-select:none;">
                <span style="font-size:1.1rem;">✅</span>
                <span style="font-weight:600;">${escapeHtml(t('scan.already_organized'))} &nbsp;
                    <span class="selection-count" style="background:rgba(0,255,136,.2);padding:2px 8px;border-radius:12px;">
                        ${orgEntries.length}
                    </span>
                </span>
                <span style="font-size:.78rem;color:var(--text-muted);margin-left:4px;">
                    ${escapeHtml(t('scan.organized_hint'))}
                </span>
                <span style="margin-left:auto;font-size:.8rem;color:var(--text-muted);">▼ ${escapeHtml(t('scan.expand'))}</span>
            </summary>
            <div class="file-list" id="organized-file-list" style="margin-top:12px;"></div>
        </details>
    `;

    // ── New files section (shell; rows filled below) ────────────────────
    const newSection = `
        <div class="glass-panel" style="padding: 20px; margin-bottom: 15px;">
            <div class="selection-header">
                <label class="select-all-label">
                    <input type="checkbox" id="select-all-scanned" ${newEntries.length > 0 ? 'checked' : ''}
                           onchange="toggleSelectAllScanned(this.checked)">
                    <span>${escapeHtml(t('scan.select_all'))}</span>
                </label>
                <h3>${escapeHtml(t('scan.scanned_files'))} &nbsp;<span class="selection-count" id="scanned-sel-count">${newEntries.length}</span> / ${scannedFiles.length} ${escapeHtml(t('scan.selected_suffix'))}</h3>
            </div>
            <div class="file-list" id="new-file-list">
                ${newEntries.length === 0
                    ? `<div style="text-align:center;padding:20px;color:var(--text-muted);">
                           ${escapeHtml(t('scan.all_have_nfo'))}
                       </div>`
                    : ''
                }
            </div>
        </div>
    `;

    resultsContainer.innerHTML = organizedSection + newSection;

    // Populate the (now-empty) lists with bounded, on-demand rendering.
    if (newEntries.length > 0) {
        _renderScannedRows(document.getElementById('new-file-list'), newEntries, _scannedNewRowHtml);
    }
    if (orgEntries.length > 0) {
        _renderScannedRows(document.getElementById('organized-file-list'), orgEntries, _scannedOrgRowHtml);
    }

    // A real file is now available — refresh the live template preview.
    updateTemplatePreview();
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

