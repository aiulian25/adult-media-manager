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
            flat: flatRename.checked,
            performer_limit: _performerLimit()
        }));

    // Natural/alphabetical processing order ("2 Alex" before "10 Brandi"),
    // regardless of scan or selection order — the preview modal, the results
    // list, and the embed queue (server re-sorts per phase) all follow it.
    const _natCmp = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
    operations.sort((a, b) => _natCmp.compare(a.old_path, b.old_path));
    
    if (operations.length === 0) {
        showStatus(t('status.no_rename'), 'error');
        return;
    }
    
    const actionType = action.value;
    const embedMode  = _getEmbedMode();

    if (actionType === 'test') {
        // Test mode: just run and display — no pre-flight needed
        _doRename(operations, actionType, embedMode);
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
            // Trimmed copies — `operations` itself still carries the full
            // file_data for the /api/rename call further down, which needs it.
            body: JSON.stringify({
                operations: operations.slice(0, 5).map(op => ({
                    ...op, file_data: previewFileData(op.file_data),
                })),
            })
        });
        if (pvRes.ok) {
            const pvData = await pvRes.json();
            // Hard-block only when the template produces an empty filename component
            // (truly broken template).  same_as_source just means the file is already
            // at the correct destination — those are silently skipped during the rename.
            const degenerate = pvData.previews.find(p => p.degenerate);
            if (degenerate) {
                showStatus(t('rename.template_empty_error'), 'error');
                progressFill.style.width = '0%';
                btnRename.disabled = false;
                return;
            }
            // Warn (but don't block) if every sampled file is already at its destination.
            const allSame = pvData.previews.length > 0 && pvData.previews.every(p => p.same_as_source);
            if (allSame) {
                showStatus(t('rename.already_at_dest_note'), 'info');
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
        _showRenamePreviewModal(preview.results, operations, actionType, embedMode);
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
function _showRenamePreviewModal(testResults, operations, actionType, embedMode = 'embed') {
    const modal     = document.getElementById('preview-modal');
    const list      = document.getElementById('preview-modal-list');
    const notes     = document.getElementById('preview-modal-notes');
    const footnote  = document.getElementById('preview-modal-footnote');
    const summary   = document.getElementById('preview-modal-summary');
    const btnOk     = document.getElementById('preview-modal-confirm');
    const btnCancel = document.getElementById('preview-modal-cancel');
    const btnClose  = document.getElementById('preview-modal-close');

    // Partition results into four buckets:
    //  • skips       — old_path === new_path (already at destination, no-op)
    //  • policySkips — collision policy "skip" refused the target (⏭, neutral)
    //  • moves       — success and actually changing path
    //  • errors      — failed for a real reason
    const skips       = testResults.filter(r =>  r.success && r.old_path === r.new_path);
    const moves       = testResults.filter(r =>  r.success && r.old_path !== r.new_path);
    const policySkips = testResults.filter(r => !r.success &&  r.skipped);
    const errors      = testResults.filter(r => !r.success && !r.skipped);
    // Whole-batch preflight callouts (F10): names the suffix policy changed
    // and names shortened to the 255-byte filesystem budget.
    const collisions  = moves.filter(r => r.collision_resolved);
    const truncated   = testResults.filter(r => r.success && r.truncated);

    const verbKey = { move: 'rename.will_move', copy: 'rename.will_copy',
                      hardlink: 'rename.will_hardlink', symlink: 'rename.will_symlink' }[actionType]
                    || 'rename.will_move';
    // Everything below is built with createElement/textContent — paths come
    // from the filesystem and the metadata APIs, and never touch an HTML or
    // attribute parser on their way to the screen. Same discipline as
    // _showUnmatchedPanel and addChipToList in manual.js.
    const el = (tag, cls, text) => {
        const node = document.createElement(tag);
        if (cls)  node.className = cls;
        if (text != null) node.textContent = text;
        return node;
    };

    // Summary: status pills, so what needs attention reads at a glance,
    // plus the reassurance that this dialog has not written anything yet.
    summary.replaceChildren();
    if (moves.length)       summary.append(el('span', 'pm-pill pm-pill-move', t(verbKey, { n: moves.length })));
    if (errors.length)      summary.append(el('span', 'pm-pill pm-pill-fail', t('rename.will_fail', { n: errors.length })));
    if (policySkips.length) summary.append(el('span', 'pm-pill pm-pill-skip', t('rename.will_skip_exists', { n: policySkips.length })));
    if (skips.length)       summary.append(el('span', 'pm-pill pm-pill-skip', t('rename.will_skip', { n: skips.length })));
    // The reassurance only makes sense when Proceed will actually do something;
    // otherwise say plainly that there is nothing to do.
    summary.append(el('span', 'pm-reassure',
        moves.length ? t('preview_modal.reassure') : t('rename.nothing_to_do')));

    // Rows worth reading: everything that is actually changing. Files already
    // at their destination collapse into the footnote below.
    const rows = testResults.filter(r => r.old_path !== r.new_path);

    // When every displayed path lives in one folder — the common case — print
    // that folder once and show bare filenames, so the part that changes is
    // the part you read. The moment any directory differs (moving into
    // {site}/{performer}/ folders) full paths come back: a preview must never
    // hide a directory change.
    const dirOf    = (p) => { const i = (p || '').lastIndexOf('/'); return i >= 0 ? p.slice(0, i + 1) : ''; };
    const basename = (p) => (p || '').split('/').pop();
    const dirs = new Set();
    rows.forEach(r => {
        dirs.add(dirOf(r.old_path));
        if (r.new_path) dirs.add(dirOf(r.new_path));
    });
    const onlyDir   = dirs.size === 1 ? [...dirs][0] : '';
    const sharedDir = (onlyDir && rows.length > 1) ? onlyDir : '';
    const shown     = (p) => (sharedDir ? basename(p) : (p || ''));

    // Preflight callouts (F10) and the shared folder sit ABOVE the scroll
    // region, so a collision at batch position 250 cannot scroll out of view.
    notes.replaceChildren();
    const addCallout = (glyph, title, names) => {
        const box  = el('div', 'pm-callout');
        box.append(el('span', 'pm-callout-glyph', glyph));
        const body = el('div', 'pm-callout-body');
        body.append(el('b', null, title));
        body.append(el('span', 'pm-callout-names', names.map(r => basename(r.new_path)).join(' · ')));
        box.append(body);
        notes.append(box);
    };
    if (collisions.length) addCallout('🔀', t('rename.preview_collisions', { n: collisions.length }), collisions);
    if (truncated.length)  addCallout('✂️', t('rename.preview_truncated',  { n: truncated.length  }), truncated);
    if (sharedDir) {
        const line = el('div', 'pm-base');
        line.append(document.createTextNode(t('preview_modal.folder_label') + ' '));
        line.append(el('b', null, sharedDir));
        notes.append(line);
    }

    // One row per change, same anatomy as an embed-queue row:
    // 16px status glyph · faint old name over primary new name · muted reason.
    list.replaceChildren();
    rows.forEach(r => {
        let glyph, glyphCls, toText, inert = false, detail = '', detailCls = '';
        if (!r.success && r.skipped) {
            // Policy skip: neutral, not an error — the collision policy chose
            // to leave this file alone because the target name already exists.
            glyph = '⏭'; glyphCls = 'pm-glyph-skip';
            toText = t('preview_modal.unchanged'); inert = true;
            detail = t('rename.skipped_exists'); detailCls = 'pm-detail-skip';
        } else if (!r.success) {
            glyph = '✕'; glyphCls = 'pm-glyph-fail';
            toText = t('preview_modal.not_renamed'); inert = true;
            detail = r.error || ''; detailCls = 'pm-detail-fail';
        } else {
            glyph = '✓'; glyphCls = 'pm-glyph-move';
            toText = shown(r.new_path);
            if (r.collision_resolved) {
                detail = t('preview_modal.auto_numbered'); detailCls = 'pm-detail-skip';
            }
        }

        const row = el('div', 'pm-row');
        row.append(el('span', `pm-glyph ${glyphCls}`, glyph));
        const paths = el('div', 'pm-paths');
        paths.append(el('span', 'pm-from', shown(r.old_path)));
        paths.append(el('span', `pm-to${inert ? ' is-inert' : ''}`, toText));
        row.append(paths);
        if (detail) {
            const note = el('span', `pm-detail ${detailCls}`, detail);
            note.title = detail;   // the full reason when the column ellipsises
            row.append(note);
        }
        list.append(row);
    });
    list.hidden = rows.length === 0;

    if (skips.length) {
        footnote.replaceChildren();
        footnote.append(document.createTextNode(`⏭ ${t('rename.skips_exact', { n: skips.length })}`));
        footnote.append(document.createElement('br'));
        footnote.append(el('b', null, t('rename.skip_advice')));
        footnote.append(document.createTextNode(' '));
        footnote.append(el('code', null, '{site}.{scene}.{quality}'));
        footnote.append(document.createTextNode(' · '));
        footnote.append(el('code', null, '{site} - {performer} - {scene}'));
        footnote.hidden = false;
    } else {
        footnote.replaceChildren();
        footnote.hidden = true;
    }

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
        _doRename(actionableOps, actionType, embedMode);
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
// R2: remember the active embed job so a page refresh can re-attach to its
// durable progress (the backend persists the job; see /api/embed-status).
const EMBED_JOB_KEY    = 'amm_embed_job';

/**
 * Persist the remaining operations to localStorage so the user can resume
 * after a page refresh.  Cleared automatically when the queue drains.
 * @param {Array}  remaining  - operations not yet attempted
 * @param {string} actionType - 'move'|'copy'|'hardlink'
 */
function _saveRenameQueue(remaining, actionType, embedMode = 'embed') {
    if (remaining.length === 0) {
        localStorage.removeItem(RENAME_QUEUE_KEY);
    } else {
        localStorage.setItem(RENAME_QUEUE_KEY, JSON.stringify({ operations: remaining, actionType, embedMode }));
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
async function _sendChunk(chunk, actionType, embedMode = 'embed', embedJobId = null) {
    const res = await fetch('/api/rename', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            operations: chunk, action: actionType, embed_mode: embedMode,
            on_conflict: document.getElementById('conflict-policy')?.value || 'suffix',
            // Chunk 2+ reports into chunk 1's embed job so the progress banner
            // counts the WHOLE batch, not just the last chunk (the "0 of 5" bug).
            embed_job_id: embedJobId,
        })
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
async function _doRename(operations, actionType, embedMode = 'embed') {
    btnRename.disabled = true;
    _eqReset();   // a new batch gets a fresh queue panel + auto-collapse policy

    // ── Test mode or small batch: single request, original behaviour ──────────
    if (actionType === 'test' || operations.length <= LARGE_BATCH) {
        showStatus(t(actionType === 'test' ? 'status.previewing' : 'status.renaming'));
        progressFill.style.width = '70%';
        try {
            const data = await _sendChunk(operations, actionType, embedMode);
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
    _saveRenameQueue(operations, actionType, embedMode);

    const total         = operations.length;
    const chunks        = [];
    for (let i = 0; i < total; i += CHUNK_SIZE) chunks.push(operations.slice(i, i + CHUNK_SIZE));

    let allResults     = [];
    let processed      = 0;
    let embedJobId     = null;   // chunk 1 creates it; later chunks extend it

    for (let ci = 0; ci < chunks.length; ci++) {
        const chunk = chunks[ci];
        showStatus(t('status.renaming_chunk', { chunk: ci + 1, total_chunks: chunks.length, done: processed + chunk.length, total }));
        progressFill.style.width = `${Math.round(((ci) / chunks.length) * 100)}%`;

        try {
            const data = await _sendChunk(chunk, actionType, embedMode, embedJobId);
            allResults = allResults.concat(data.results);
            if (data.embed_job_id) embedJobId = data.embed_job_id;

            // Prune the persisted queue after each successful chunk
            const remaining = operations.slice(processed + chunk.length);
            _saveRenameQueue(remaining, actionType, embedMode);

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
    _applyRenameResults(allResults, embedJobId);
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

    // Built with DOM APIs + addEventListener, NOT innerHTML with an inline
    // onclick carrying a JSON-serialised filename: that put user/API-supplied
    // text into an HTML attribute + JS context at once (one stray quote away
    // from breaking out). Same discipline as addChipToList in manual.js.
    const remaining = matchedResults.length;
    const panel = document.createElement('div');
    panel.id = 'unmatched-panel';
    panel.className = 'glass-panel unmatched-section';

    // Header in the embed-queue idiom: glyph · title · muted count · action.
    const header = document.createElement('div');
    header.className = 'um-head';
    const glyph = document.createElement('span');
    glyph.className = 'um-glyph';
    glyph.textContent = '⚠';
    header.appendChild(glyph);
    const title = document.createElement('b');
    title.className = 'um-title';
    title.textContent = t('rename.unmatched_section');
    header.appendChild(title);
    const count = document.createElement('span');
    count.className = 'um-count';
    count.textContent = t('rename.unmatched_title', { n: remaining });
    header.appendChild(count);
    const spacer = document.createElement('span');
    spacer.className = 'um-spacer';
    header.appendChild(spacer);
    const editAll = document.createElement('button');
    editAll.type = 'button';
    editAll.className = 'glass-btn btn-primary um-cta';
    editAll.textContent = `✏️ ${t('rename.unmatched_edit_all')}`;
    editAll.addEventListener('click', () => {
        displayMatches();
        statusBar.classList.add('hidden');
        document.getElementById('unmatched-panel')?.remove();
    });
    header.appendChild(editAll);
    panel.appendChild(header);

    // One row per unmatched file — same anatomy as an embed-queue row:
    // 16px glyph · mono name · 30px icon action. Capped height so a large
    // unmatched set can't push the embed queue and results off-screen.
    const list = document.createElement('div');
    list.className = 'um-list';
    matchedResults.forEach((r) => {
        const item = document.createElement('div');
        item.className = 'um-row';
        const g = document.createElement('span');
        g.className = 'um-row-glyph';
        g.textContent = '⚠';
        item.appendChild(g);
        const name = document.createElement('span');
        name.className = 'um-name';
        name.textContent = r.original.filename;
        name.title = r.original.path || r.original.filename;
        item.appendChild(name);
        const edit = document.createElement('button');
        edit.type = 'button';
        edit.className = 'glass-btn um-edit';
        edit.textContent = '✏️';
        edit.title = t('rename.unmatched_edit_one');
        edit.setAttribute('aria-label', t('rename.unmatched_edit_one'));
        edit.addEventListener('click', () => openManualEditModal(r.original));
        item.appendChild(edit);
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
// F13: poll pacing. After the fast window the poller DEGRADES to a 30-second
// slow poll instead of quitting — the backend job store is durable and a single
// remux is allowed up to an hour, so the UI must not abandon a live job.
// Module-level `let`s so tests can shrink them.
// F1: 1 s (was 2 s) — the backend no longer blocks its event loop on embed
// bookkeeping, so a 1 s repaint is honest real-time rather than a queued poll.
let EMBED_POLL_INTERVAL_MS = 1000;
// F2: the window counts QUIET polls, not elapsed ones. It used to be a plain
// stopwatch (`polls >= LIMIT`), so a batch that was remuxing perfectly happily
// dropped to one repaint per 30 s at the 10-minute mark — bars and counter
// crawling exactly when a long job most needs to look alive. Now only a job
// that reports nothing new for 600 consecutive polls slows down, and the first
// change snaps it straight back. Each poll is a microsecond in-memory dict read
// server-side (see embed_status in main.py), so staying fast costs nothing.
let EMBED_POLL_FAST_LIMIT  = 600;    // consecutive UNCHANGED polls (= 10 quiet min)
let EMBED_POLL_SLOW_MS     = 30000;

/**
 * Fingerprint of everything the user can SEE moving in an embed-status payload:
 * the done counter and each file's status + progress.
 *
 * `elapsed` is deliberately excluded — it ticks on every poll by definition, so
 * including it would make every job look busy and the slow poll unreachable.
 * `warnings` is covered by `done` (the server appends a warning only in
 * _job_progress, which increments done in the same call).
 */
function _embedProgressSig(job) {
    const files = Array.isArray(job.files) ? job.files : [];
    return job.done + '/' + job.total + '|' +
        files.map(f => f.status + ':' + f.progress).join(',');
}

// ── Embed queue panel ─────────────────────────────────────────────────────────
// Self-managing per-file progress view (mockup B): appears above the Rename
// Results the moment embedding starts, highlights + auto-scrolls to the active
// file, and on completion collapses itself to a one-line summary — unless
// there are issues, in which case it stays open with them pinned first.
// Zero required clicks; the chevron only exists for manual peeking.
let _eqLastJob = null;       // F6: last /api/embed-status payload seen
let _eqOpen = true;          // current expanded state
let _eqUserToggled = false;  // once the user touches the chevron, we obey them

const _EQ_GLYPH = { pending: '⏳', done: '✓', warning: '⚠', error: '✕', duplicate: '⧉' };

function _eqReset() {
    _eqLastJob = null;
    document.getElementById('embed-queue-panel')?.remove();
    _eqOpen = true;
    _eqUserToggled = false;
}

// Compact duration: 42s · 3m 12s · 1h 04m — shown in the queue header while
// running (ticking) and frozen on completion ("how long did that take?").
function _fmtEqDuration(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + 's';
    const m = Math.floor(sec / 60), r = sec % 60;
    if (m < 60) return m + 'm ' + String(r).padStart(2, '0') + 's';
    const h = Math.floor(m / 60);
    return h + 'h ' + String(m % 60).padStart(2, '0') + 'm';
}

// Build ONE queue row (factory shared by full renders and the reconciler).
function _eqBuildRow(f) {
    const row = document.createElement('div');
    row.className = 'eq-row' + (f.status === 'embedding' ? ' is-active' : '');
    row.dataset.name = f.name;
    row.dataset.status = f.status;
    const st = document.createElement('span');
    if (f.status === 'embedding') {
        st.className = 'eq-spin eq-spin-sm';
    } else {
        st.className = 'eq-st eq-' + f.status;
        st.textContent = _EQ_GLYPH[f.status] || '';
    }
    st.title = t('embed.queue_st_' + f.status);
    row.appendChild(st);
    if (f.status === 'embedding') {
        // Underline bar (option A): 2px under the name, driven by REAL
        // two-phase progress (ffmpeg out_time, then copy-back bytes). When
        // the server can't measure (no duration yet), spinner only — the bar
        // never shows an invented number.
        const wrap = document.createElement('span');
        wrap.className = 'eq-fnwrap';
        const fn = document.createElement('span');
        fn.className = 'eq-fn';
        fn.textContent = f.name;
        wrap.appendChild(fn);
        if (Number.isFinite(f.progress)) {
            const bar = document.createElement('span');
            bar.className = 'eq-ubar';
            const fill = document.createElement('i');
            fill.style.width = f.progress + '%';
            bar.appendChild(fill);
            wrap.appendChild(bar);
            row.dataset.hasbar = '1';
        }
        row.appendChild(wrap);
    } else {
        const fn = document.createElement('span');
        fn.className = 'eq-fn';
        fn.textContent = f.name;
        row.appendChild(fn);
    }
    if (Number.isFinite(f.size)) {
        const sz = document.createElement('span');
        sz.className = 'eq-size';
        sz.textContent = formatFileSize(f.size);
        row.appendChild(sz);
    }
    if (f.detail && f.status !== 'pending' && f.status !== 'embedding') {
        const d = document.createElement('span');
        d.className = 'eq-detail';
        d.textContent = f.detail;
        d.title = f.detail;
        row.appendChild(d);
    }
    return row;
}

function _renderEmbedQueue(job) {
    let files = Array.isArray(job.files) ? job.files : null;
    if (!files || !files.length) return;   // restart-resumed job — no per-file data
    // F6 (defensive): a completed job must never render a spinner. The server
    // sweeps stragglers in _job_finish, but an older backend — or a job whose
    // per-file states were restored without them — could still report complete
    // with a non-terminal row, and polling has stopped by then, so nothing
    // would ever repaint it. Resolve for rendering only; the underlying job
    // object is left untouched.
    if (job.complete) {
        files = files.map(f => (f.status === 'embedding' || f.status === 'pending')
            ? { ...f, status: 'warning', progress: null,
                detail: f.detail || t('embed.queue_state_unknown') }
            : f);
    }
    let panel = document.getElementById('embed-queue-panel');
    const issues = files.filter(f => f.status === 'warning' || f.status === 'error' || f.status === 'duplicate');
    const open = _eqUserToggled ? _eqOpen : (job.complete ? issues.length > 0 : true);
    _eqOpen = open;
    const ordered = job.complete && issues.length
        ? [...issues, ...files.filter(f => !issues.includes(f))]
        : files;

    // ── Fast path: same rows in the same order → update in place, so the
    // underline bar's CSS width transition animates smoothly between polls
    // instead of being reset by a rebuild. Any structural change falls
    // through to the full rebuild below.
    if (panel && panel.dataset.open === String(open) && open) {
        const list = panel.querySelector('.eq-list');
        const rows = list ? [...list.children] : [];
        const sameShape = rows.length === ordered.length &&
            rows.every((r, i) => r.dataset.name === ordered[i].name);
        if (sameShape) {
            _eqUpdateHead(panel, job, issues);
            ordered.forEach((f, i) => {
                const row = rows[i];
                const barState = Number.isFinite(f.progress) && f.status === 'embedding' ? '1' : '';
                if (row.dataset.status !== f.status ||
                        (row.dataset.hasbar || '') !== barState) {
                    const nr = _eqBuildRow(f);
                    // finishing flash: embedding → terminal state
                    if (row.dataset.status === 'embedding' && f.status !== 'embedding') {
                        nr.classList.add('eq-row-flash');
                    }
                    row.replaceWith(nr);
                } else if (f.status === 'embedding' && barState) {
                    row.querySelector('.eq-ubar > i').style.width = f.progress + '%';
                }
            });
            const active = list.querySelector('.eq-row.is-active');
            if (active) queueMicrotask(() => active.scrollIntoView({ block: 'nearest' }));
            return;
        }
    }

    // ── Full rebuild (first render, collapse toggle, order change) ──
    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'embed-queue-panel';
        panel.className = 'glass-panel embed-queue';
        resultsContainer.prepend(panel);
    }
    panel.dataset.open = String(open);
    panel.textContent = '';
    const head = document.createElement('div');
    head.className = 'eq-head';
    panel.appendChild(head);
    _eqUpdateHead(panel, job, issues);
    const fold = head.querySelector('.eq-fold');
    fold.addEventListener('click', () => {
        _eqUserToggled = true;
        _eqOpen = !_eqOpen;
        _renderEmbedQueue(job);
    });
    if (!open) return;

    const list = document.createElement('div');
    list.className = 'eq-list';
    let activeRow = null;
    ordered.forEach(f => {
        const row = _eqBuildRow(f);
        list.appendChild(row);
        if (f.status === 'embedding' && !activeRow) activeRow = row;
    });
    panel.appendChild(list);
    if (activeRow) queueMicrotask(() => activeRow.scrollIntoView({ block: 'nearest' }));
}

// (Re)paint the queue header: state icon, count, job bar, summary, chevron.
function _eqUpdateHead(panel, job, issues) {
    const head = panel.querySelector('.eq-head');
    head.textContent = '';
    const icon = document.createElement('span');
    if (job.complete) {
        icon.className = 'eq-st ' + (issues.length ? 'eq-warning' : 'eq-done');
        icon.textContent = issues.length ? '⚠' : '✓';
    } else {
        icon.className = 'eq-spin';
    }
    head.appendChild(icon);
    const count = document.createElement('b');
    count.textContent = `${job.done} / ${job.total}`;
    head.appendChild(count);
    if (Number.isFinite(job.elapsed)) {
        const el = document.createElement('span');
        el.className = 'eq-elapsed';
        el.textContent = _fmtEqDuration(job.elapsed);
        el.title = t('embed.queue_elapsed');
        head.appendChild(el);
    }
    const bar = document.createElement('div');
    bar.className = 'eq-bar';
    const fill = document.createElement('i');
    fill.style.width = job.total > 0 ? `${Math.round((job.done / job.total) * 100)}%` : '100%';
    bar.appendChild(fill);
    head.appendChild(bar);
    if (job.complete && !_eqOpen) {
        const files = Array.isArray(job.files) ? job.files : [];
        const sum = document.createElement('span');
        sum.className = 'eq-summary';
        sum.textContent = t('embed.queue_summary', { n: files.filter(f => f.status === 'done').length })
            + (issues.length ? ` · ${t('embed.queue_issues', { n: issues.length })}` : '');
        head.appendChild(sum);
    }
    const fold = document.createElement('button');
    fold.type = 'button';
    fold.className = 'eq-fold';
    fold.textContent = _eqOpen ? '▾' : '▸';
    fold.title = t(_eqOpen ? 'embed.queue_hide' : 'embed.queue_show');
    head.appendChild(fold);
}

async function _pollEmbedStatus(jobId, total) {
    // Tolerate transient connection failures (e.g. the server restarting
    // mid-embed → ERR_CONNECTION_REFUSED). The backend persists the job
    // durably, so once it's back the next poll re-attaches. Only give up after
    // several consecutive failures so a brief hiccup doesn't silently abort.
    const MAX_NET_FAILS = 8;  // ~16 s of unreachable server before giving up
    let netFails = 0;
    let slowNotified = false;  // one-time "still embedding" notice on escalation
    // F2: consecutive polls whose payload was byte-identical to the previous
    // one. Reset to 0 by any observable change, so the fast window only closes
    // on a genuinely stalled job.
    let quietPolls = 0;
    let lastSig = null;

    // Mark embedding as active — enables beforeunload guard and banner
    _embedInProgress = true;
    _setEmbedBanner(t('embed.banner', { done: 0, total }));
    document.title = `⏳ ${t('embed.title_progress', { done: 0, total })}`;
    // Persist so a refresh can re-attach (R2). Cleared in _finishEmbedPolling.
    try { localStorage.setItem(EMBED_JOB_KEY, JSON.stringify({ jobId, total })); } catch {}

    async function tick() {
        try {
            const res = await fetch(`/api/embed-status/${encodeURIComponent(jobId)}`);
            if (!res.ok) {
                // 404 = job expired or invalid; stop silently
                _finishEmbedPolling(null);
                return;
            }
            netFails = 0;  // reachable again — reset the failure streak
            const job = await res.json();
            // F2: did anything the user can see actually move since last poll?
            const sig = _embedProgressSig(job);
            quietPolls = (sig === lastSig) ? quietPolls + 1 : 0;
            lastSig = sig;
            const statusText = t('status.embedding', { done: job.done, total: job.total });
            showStatus(statusText, 'info');
            progressFill.style.width =
                job.total > 0 ? `${Math.round((job.done / job.total) * 100)}%` : '100%';

            // Keep the banner and title in sync with progress
            _setEmbedBanner(t('embed.banner', { done: job.done, total: job.total }));
            document.title = `⏳ ${t('embed.title_progress', { done: job.done, total: job.total })}`;
            _eqLastJob = job;         // F6: last known state for the final repaint
            _renderEmbedQueue(job);   // per-file queue panel (self-managing)

            if (job.complete) {
                // R2: a job the server flipped to "interrupted" (restart killed
                // the FFmpeg work) is terminal — tell the user rather than
                // silently stopping. NFO sidecars written before the restart are
                // already durable; only the in-container embed may be incomplete.
                if (job.status === 'interrupted') {
                    showToast(t('embed.interrupted_title'), t('embed.interrupted'), 'info', 6000);
                }
                _finishEmbedPolling(job.warnings);
            } else {
                // F13/F2: degrade to the slow poll only once the job has gone
                // QUIET for the whole window — never abandon a live job, and
                // never throttle one that is still visibly working. Any change
                // resets quietPolls to 0, so a batch that resumes goes straight
                // back to the fast cadence (and can notify again if it stalls
                // a second time).
                const slow = quietPolls >= EMBED_POLL_FAST_LIMIT;
                if (!slow) slowNotified = false;
                if (slow && !slowNotified) {
                    slowNotified = true;
                    showStatus(t('embed.long_running'), 'info');
                }
                setTimeout(tick, slow ? EMBED_POLL_SLOW_MS : EMBED_POLL_INTERVAL_MS);
            }
        } catch {
            // Transient network error (server restarting / momentarily
            // unreachable). Keep retrying up to MAX_NET_FAILS so a brief outage
            // doesn't abandon a still-running, durably-persisted job. Retries
            // stay at the FAST interval even in slow mode — re-attaching
            // quickly matters more than politeness when the server just came back.
            netFails++;
            if (netFails >= MAX_NET_FAILS) {
                _finishEmbedPolling(null);
            } else {
                _setEmbedBanner(t('embed.reconnecting'));
                setTimeout(tick, EMBED_POLL_INTERVAL_MS);
            }
        }
    }

    // Fire the FIRST poll now, not an interval from now. The queue panel can
    // only render once a payload with `files` has arrived, so waiting here made
    // every batch stare at an empty banner for a second — and with R1 a batch of
    // small files can be finished before that first poll ever ran. Safe against
    // a 404 race: /api/rename and /api/save-manual-metadata both register the
    // job (_job_create) BEFORE they return the id we are polling with.
    tick();
}

/**
 * Called when embed polling ends. Appends warnings to the results panel
 * and resets the status bar.
 *
 * @param {Array|null} warnings  - Array of {path, warning} or null
 */
function _finishEmbedPolling(warnings) {
    // F6: polling stops here — this is the LAST chance to repaint. Force one
    // final render marked complete so no row is left spinning, including on the
    // paths that never saw a completed job (404/expired, network give-up).
    if (_eqLastJob) {
        try { _renderEmbedQueue({ ..._eqLastJob, complete: true }); } catch {}
        _eqLastJob = null;
    }
    // Dismiss the sticky banner and release the beforeunload guard
    _clearEmbedBanner();
    statusBar.classList.add('hidden');
    progressFill.style.width = '0%';
    // R2: job is terminal — drop the resume handle so a later refresh won't
    // re-attach to a finished job.
    try { localStorage.removeItem(EMBED_JOB_KEY); } catch {}

    // Surface unmatched files at the top so user sees them without scrolling
    _showUnmatchedPanel();

    if (!warnings || warnings.length === 0) return;

    // Append a warning section below the existing results panel
    const extra = document.createElement('div');
    extra.className = 'glass-panel';
    extra.style.cssText = 'padding:16px;margin-top:12px;border:1px solid rgba(240,165,0,.35)';
    extra.innerHTML = `
        <h4 style="margin:0 0 10px;color:var(--warning,#f0a500)">
            ⚠ ${escapeHtml(t('rename.embed_warnings_title', { n: warnings.length }))}
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

// Rename Results in the embed-queue idiom (thin mono rows, glyph state, muted
// detail on the right) — one visual system with the panel above it instead of
// the old bordered-card look. Full paths live in tooltips; rows show basenames.
function displayRenameResults(results) {
    const ok      = results.filter(r => r.success && !r.skipped).length;
    const skipped = results.filter(r => r.skipped).length;
    const failed  = results.filter(r => !r.success && !r.skipped).length;

    const panel = document.createElement('div');
    panel.className = 'glass-panel rename-results';

    const head = document.createElement('div');
    head.className = 'rr-head';
    const title = document.createElement('b');
    title.textContent = t('rename.results_title');
    head.appendChild(title);
    const sum = document.createElement('span');
    sum.className = 'rr-sum';
    sum.textContent = [`✓ ${ok}`, skipped ? `⏭ ${skipped}` : '', failed ? `✕ ${failed}` : '']
        .filter(Boolean).join(' · ');
    sum.classList.toggle('has-fail', failed > 0);
    head.appendChild(sum);
    panel.appendChild(head);

    const list = document.createElement('div');
    list.className = 'eq-list rename-results-list';
    const base = p => (p || '').split('/').pop();
    results.forEach(result => {
        const skip = !!result.skipped;
        const row = document.createElement('div');
        row.className = 'eq-row';
        const st = document.createElement('span');
        st.className = 'eq-st ' + (skip ? 'eq-pending' : result.success ? 'eq-done' : 'eq-error');
        st.textContent = skip ? '⏭' : (result.success ? '✓' : '✕');
        st.title = result.action ? result.action.toUpperCase() : '';
        row.appendChild(st);
        const paths = document.createElement('span');
        paths.className = 'rr-paths';
        const from = document.createElement('span');
        from.className = 'rr-from';
        from.textContent = base(result.old_path);
        from.title = result.old_path || '';
        paths.appendChild(from);
        if (result.new_path && !skip) {
            const to = document.createElement('span');
            to.className = 'rr-to';
            to.textContent = base(result.new_path);
            to.title = result.new_path;
            paths.appendChild(to);
        }
        row.appendChild(paths);
        // One muted detail slot, priority: error > skip reason > embed warning
        // > companions — same right-aligned treatment as the queue's details.
        const detailText = result.error ? result.error
            : skip ? t('rename.skipped_exists')
            : result.embed_warning ? `⚠ ${result.embed_warning}`
            : result.companions_moved ? t('rename.companions_moved', { n: result.companions_moved })
            : '';
        if (detailText) {
            const d = document.createElement('span');
            d.className = 'eq-detail' + (result.error ? ' rr-err' : '');
            d.textContent = detailText;
            d.title = detailText;
            row.appendChild(d);
        }
        list.appendChild(row);
    });
    panel.appendChild(list);

    resultsContainer.innerHTML = '';
    resultsContainer.appendChild(panel);
}

