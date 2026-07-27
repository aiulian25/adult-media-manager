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
            body: JSON.stringify({ operations: operations.slice(0, 5) })
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
    const parts = [];
    if (moves.length)  parts.push(t(verbKey, { n: moves.length }));
    if (errors.length) parts.push(t('rename.will_fail', { n: errors.length }));
    if (policySkips.length) parts.push(t('rename.will_skip_exists', { n: policySkips.length }));
    if (skips.length)  parts.push(t('rename.will_skip', { n: skips.length }));
    summary.textContent = parts.join(' · ') || t('rename.nothing_to_do');

    // Only render errors and actual moves inline; collapse skips into a
    // single summary line so they don't drown out the important rows.
    const renderRow = (r) => {
        const samePlace = r.old_path === r.new_path;
        if (samePlace) return ''; // rendered separately below
        // Policy skip (⏭): neutral, not an error — the collision policy chose
        // to leave this file alone because the target name already exists.
        if (!r.success && r.skipped) {
            return `
            <div style="border:1px solid rgba(255,165,0,.3);border-radius:8px;padding:.6rem .9rem;font-size:.8rem;line-height:1.5">
                <div style="color:var(--text-muted)">
                    ⏭ <span style="color:var(--text)">${escapeHtml(r.old_path)}</span><br>
                    ${escapeHtml(t('rename.skipped_exists'))}
                </div>
            </div>`;
        }
        const colour = r.success ? 'rgba(0,255,136,.25)' : 'rgba(255,71,87,.35)';
        return `
            <div style="border:1px solid ${colour};border-radius:8px;padding:.6rem .9rem;font-size:.8rem;line-height:1.5">
                <div style="color:var(--text-muted);margin-bottom:.2rem">
                    ${r.success ? '✅' : '❌'} From: <span style="color:var(--text)">${escapeHtml(r.old_path)}</span>
                </div>
                ${r.new_path ? `<div>→ To: <strong>${escapeHtml(r.new_path)}</strong>${r.collision_resolved ? ' <span title="' + escapeHtml(t('rename.preview_collisions', { n: 1 })) + '">🔀</span>' : ''}</div>` : ''}
                ${r.error    ? `<div style="color:var(--error);margin-top:.2rem">⚠ ${escapeHtml(r.error)}</div>` : ''}
            </div>`;
    };

    // Preflight callout blocks (F10) — shown ABOVE the rows so a collision at
    // batch position 250 is impossible to miss.
    const basename = (p) => (p || '').split('/').pop();
    let html = '';
    if (collisions.length) {
        html += `
            <div style="border:1px solid rgba(255,165,0,.45);border-radius:8px;padding:.6rem .9rem;
                        font-size:.8rem;line-height:1.5">
                🔀 <strong>${escapeHtml(t('rename.preview_collisions', { n: collisions.length }))}</strong><br>
                <span style="color:var(--text-muted)">${collisions.map(r => escapeHtml(basename(r.new_path))).join(' · ')}</span>
            </div>`;
    }
    if (truncated.length) {
        html += `
            <div style="border:1px solid rgba(255,165,0,.45);border-radius:8px;padding:.6rem .9rem;
                        font-size:.8rem;line-height:1.5">
                ✂️ <strong>${escapeHtml(t('rename.preview_truncated', { n: truncated.length }))}</strong><br>
                <span style="color:var(--text-muted)">${truncated.map(r => escapeHtml(basename(r.new_path))).join(' · ')}</span>
            </div>`;
    }
    html += testResults.map(renderRow).join('');

    if (skips.length) {
        html += `
            <div style="border:1px solid rgba(255,165,0,.3);border-radius:8px;padding:.6rem .9rem;
                        font-size:.8rem;color:var(--text-muted);line-height:1.5">
                ⏭ ${escapeHtml(t('rename.skips_exact', { n: skips.length }))}<br>
                <strong style="color:var(--text)">${escapeHtml(t('rename.skip_advice'))}</strong>
                <code>{site}.{scene}.{quality}</code> · <code>{site} - {performer} - {scene}</code>
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

    const remaining = matchedResults.length;
    const panel = document.createElement('div');
    panel.id = 'unmatched-panel';
    panel.className = 'glass-panel';
    panel.style.cssText = 'padding:16px;margin-bottom:16px;border:1px solid rgba(255,71,87,.4);';

    // Header row with count + "Continue editing" button
    const header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;';
    header.innerHTML = `
        <h3 style="margin:0;color:var(--error);">⚠ ${escapeHtml(t('rename.unmatched_title', { n: remaining }))}</h3>
        <button class="glass-btn btn-primary" onclick="displayMatches();statusBar.classList.add('hidden');document.getElementById('unmatched-panel')?.remove();"
                style="font-size:12px;">✏️ ${escapeHtml(t('rename.unmatched_edit_all'))}</button>
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
                    onclick='openManualEditModal(${JSON.stringify(r.original).replace(/'/g, "&#39;")})'>✏️ ${escapeHtml(t('rename.unmatched_edit_one'))}</button>
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
// F13: poll pacing. After the fast window (10 min) the poller DEGRADES to a
// 30-second slow poll instead of quitting — the backend job store is durable
// and a single remux is allowed up to an hour, so the UI must not abandon a
// live job. Module-level `let`s so tests can shrink them.
let EMBED_POLL_INTERVAL_MS = 2000;
let EMBED_POLL_FAST_LIMIT  = 300;    // ticks at fast pace (300 × 2 s = 10 min)
let EMBED_POLL_SLOW_MS     = 30000;

// ── Embed queue panel ─────────────────────────────────────────────────────────
// Self-managing per-file progress view (mockup B): appears above the Rename
// Results the moment embedding starts, highlights + auto-scrolls to the active
// file, and on completion collapses itself to a one-line summary — unless
// there are issues, in which case it stays open with them pinned first.
// Zero required clicks; the chevron only exists for manual peeking.
let _eqOpen = true;          // current expanded state
let _eqUserToggled = false;  // once the user touches the chevron, we obey them

const _EQ_GLYPH = { pending: '⏳', done: '✓', warning: '⚠', error: '✕', duplicate: '⧉' };

function _eqReset() {
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
    const files = Array.isArray(job.files) ? job.files : null;
    if (!files || !files.length) return;   // restart-resumed job — no per-file data
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
    let polls = 0;
    let netFails = 0;
    let slowNotified = false;  // one-time "still embedding" notice on escalation

    // Mark embedding as active — enables beforeunload guard and banner
    _embedInProgress = true;
    _setEmbedBanner(t('embed.banner', { done: 0, total }));
    document.title = `⏳ ${t('embed.title_progress', { done: 0, total })}`;
    // Persist so a refresh can re-attach (R2). Cleared in _finishEmbedPolling.
    try { localStorage.setItem(EMBED_JOB_KEY, JSON.stringify({ jobId, total })); } catch {}

    async function tick() {
        polls++;
        try {
            const res = await fetch(`/api/embed-status/${encodeURIComponent(jobId)}`);
            if (!res.ok) {
                // 404 = job expired or invalid; stop silently
                _finishEmbedPolling(null);
                return;
            }
            netFails = 0;  // reachable again — reset the failure streak
            const job = await res.json();
            const statusText = t('status.embedding', { done: job.done, total: job.total });
            showStatus(statusText, 'info');
            progressFill.style.width =
                job.total > 0 ? `${Math.round((job.done / job.total) * 100)}%` : '100%';

            // Keep the banner and title in sync with progress
            _setEmbedBanner(t('embed.banner', { done: job.done, total: job.total }));
            document.title = `⏳ ${t('embed.title_progress', { done: job.done, total: job.total })}`;
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
                // F13: past the fast window, degrade to the slow poll — never
                // abandon a live job. The one-time notice explains the pace.
                const slow = polls >= EMBED_POLL_FAST_LIMIT;
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

    setTimeout(tick, EMBED_POLL_INTERVAL_MS);
}

/**
 * Called when embed polling ends. Appends warnings to the results panel
 * and resets the status bar.
 *
 * @param {Array|null} warnings  - Array of {path, warning} or null
 */
function _finishEmbedPolling(warnings) {
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

