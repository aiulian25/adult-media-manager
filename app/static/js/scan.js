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
/** Small DOM helper — rows are built as nodes, never as HTML strings. */
function _sfEl(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
}

/**
 * The quiet right-hand column: what the detector (or the .nfo) found, in the
 * order the rename template reads it. An almost-empty column is a signal —
 * it means this file will probably need a manual match.
 */
function _sfMeta(bits) {
    const meta = _sfEl('span', 'sf-meta');
    if (bits.organized) {
        const tick = _sfEl('span', 'sf-tick', '✓');
        tick.title = t('scan.has_nfo');
        meta.appendChild(tick);
    }
    if (bits.folder) {
        const mark = _sfEl('span', 'sf-folder', '📁');
        mark.title = t('scan.from_folder_hint');
        meta.appendChild(mark);
    }
    if (bits.site)       meta.appendChild(_sfEl('span', 'sf-site', bits.site));
    if (bits.performers) meta.appendChild(_sfEl('span', 'sf-perf', bits.performers));
    if (bits.date)       meta.appendChild(_sfEl('span', 'sf-date', bits.date));
    if (bits.quality)    meta.appendChild(_sfEl('span', 'sf-q', bits.quality));
    if (bits.title)      meta.appendChild(_sfEl('span', 'sf-title', bits.title));
    return meta;
}

/** Checkbox column — the same 16px column the section header's Select All uses. */
function _sfCheckbox(i, file) {
    const wrap = _sfEl('label', 'sf-cb');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'file-cb';
    cb.dataset.index = String(i);
    cb.checked = selectedScannedIndices.has(i);
    cb.setAttribute('aria-label', file.filename);
    wrap.appendChild(cb);
    return wrap;
}

function _scannedNewRow(i, file) {
    const row = _sfEl('div', 'sf-row');
    row.id = `scanned-item-${i}`;
    row.appendChild(_sfCheckbox(i, file));
    row.appendChild(_sfEl('span', 'sf-name', file.filename));
    row.appendChild(_sfMeta({
        folder:     file.context_source === 'folder',
        site:       file.site,
        performers: (file.performers || []).join(', '),
        date:       file.release_date,
        quality:    file.quality,
    }));
    return row;
}

function _scannedOrgRow(i, file) {
    const nfo = file.nfo_metadata || {};
    // Still a checkbox, not a static tick: an already-organised file can be
    // opted back into matching one at a time (Select All deliberately skips
    // them). The ✓ in the meta column is what marks it as organised.
    const row = _sfEl('div', 'sf-row is-organized');
    row.id = `scanned-item-${i}`;
    row.appendChild(_sfCheckbox(i, file));
    row.appendChild(_sfEl('span', 'sf-name', file.filename));
    row.appendChild(_sfMeta({
        organized:  true,
        site:       nfo.site,
        performers: (nfo.performers || []).join(', '),
        date:       nfo.release_date,
        title:      nfo.title,
    }));
    return row;
}

/**
 * Render `entries` ([globalIndex, file] pairs) into `listEl`, capping the
 * initial batch at SCAN_RENDER_CAP and appending a "Show all" button for the
 * remainder. Rows are built as nodes into a fragment, so each batch touches
 * the live DOM once.
 *
 * One delegated change listener covers every row, including rows added later
 * by "Show all" — replaces 300 inline onchange attributes that put a row index
 * through the HTML parser on every render.
 */
function _renderScannedRows(listEl, entries, rowFn) {
    if (!listEl) return;
    const append = (batch) => {
        const frag = document.createDocumentFragment();
        batch.forEach(([i, f]) => frag.appendChild(rowFn(i, f)));
        listEl.appendChild(frag);
    };
    listEl.addEventListener('change', (ev) => {
        const cb = ev.target.closest('.file-cb');
        if (!cb) return;
        toggleScannedFile(parseInt(cb.dataset.index, 10), cb.checked);
    });
    append(entries.slice(0, SCAN_RENDER_CAP));

    const rest = entries.slice(SCAN_RENDER_CAP);
    if (rest.length === 0) return;

    const btn = document.createElement('button');
    btn.className = 'glass-btn sf-showall';
    btn.type = 'button';
    btn.textContent = t('scan.show_all', { count: rest.length });
    btn.addEventListener('click', () => {
        btn.remove();
        append(rest);
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

    // Both sections are sections of ONE full-bleed surface, in the same idiom
    // as the embed queue / unmatched / Rename Results: 16px control column,
    // mono name, quiet meta column, hairline rows, a single 2px divider.
    resultsContainer.replaceChildren();

    // ── Already-organised collapsed section (shell; rows filled below) ──
    if (orgEntries.length > 0) {
        const details = _sfEl('details', 'glass-panel scan-section');
        details.id = 'organized-section';
        const head = _sfEl('summary', 'sf-head');
        const tick = _sfEl('span', 'sf-cb');
        tick.appendChild(_sfEl('span', 'sf-head-glyph', '✓'));
        head.appendChild(tick);
        head.appendChild(_sfEl('b', 'sf-title', t('scan.already_organized')));
        head.appendChild(_sfEl('span', 'sf-count',
            t('scan.organized_count', { n: orgEntries.length })));
        const toggle = _sfEl('span', 'sf-expand');
        toggle.appendChild(_sfEl('span', 'sf-when-closed', `▸ ${t('scan.expand')}`));
        toggle.appendChild(_sfEl('span', 'sf-when-open',   `▾ ${t('scan.collapse')}`));
        head.appendChild(toggle);
        details.appendChild(head);
        const orgList = _sfEl('div', 'sf-list');
        orgList.id = 'organized-file-list';
        details.appendChild(orgList);
        resultsContainer.appendChild(details);
    }

    // ── New files section (shell; rows filled below) ────────────────────
    const section = _sfEl('div', 'glass-panel scan-section');
    section.id = 'scanned-section';
    const head = _sfEl('div', 'sf-head');
    const selWrap = _sfEl('label', 'sf-cb');
    const selAll = document.createElement('input');
    selAll.type = 'checkbox';
    selAll.id = 'select-all-scanned';
    selAll.checked = newEntries.length > 0;
    // The Select All control shares the row checkboxes' column, so it needs a
    // label the pointer can't show: title for the mouse, aria-label for AT.
    selAll.title = t('scan.select_all');
    selAll.setAttribute('aria-label', t('scan.select_all'));
    selAll.addEventListener('change', () => toggleSelectAllScanned(selAll.checked));
    selWrap.appendChild(selAll);
    head.appendChild(selWrap);
    head.appendChild(_sfEl('b', 'sf-title', t('scan.scanned_files')));
    const count = _sfEl('span', 'sf-count',
        t('scan.selected_of', { n: newEntries.length, total: scannedFiles.length }));
    count.id = 'scanned-sel-count';
    head.appendChild(count);
    section.appendChild(head);
    const newList = _sfEl('div', 'sf-list');
    newList.id = 'new-file-list';
    if (newEntries.length === 0) {
        newList.appendChild(_sfEl('div', 'sf-empty', t('scan.all_have_nfo')));
    }
    section.appendChild(newList);
    resultsContainer.appendChild(section);

    // Populate the (now-empty) lists with bounded, on-demand rendering.
    if (newEntries.length > 0) {
        _renderScannedRows(newList, newEntries, _scannedNewRow);
    }
    if (orgEntries.length > 0) {
        _renderScannedRows(document.getElementById('organized-file-list'), orgEntries, _scannedOrgRow);
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
    if (countEl) {
        countEl.textContent = t('scan.selected_of', { n: sel, total: scannedFiles.length });
    }
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

