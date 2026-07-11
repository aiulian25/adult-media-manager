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
async function _sendChunk(chunk, actionType, embedMode = 'embed') {
    const res = await fetch('/api/rename', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ operations: chunk, action: actionType, embed_mode: embedMode })
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
    let lastEmbedJobId = null;

    for (let ci = 0; ci < chunks.length; ci++) {
        const chunk = chunks[ci];
        showStatus(t('status.renaming_chunk', { chunk: ci + 1, total_chunks: chunks.length, done: processed + chunk.length, total }));
        progressFill.style.width = `${Math.round(((ci) / chunks.length) * 100)}%`;

        try {
            const data = await _sendChunk(chunk, actionType, embedMode);
            allResults = allResults.concat(data.results);
            if (data.embed_job_id) lastEmbedJobId = data.embed_job_id;

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
    // Tolerate transient connection failures (e.g. the server restarting
    // mid-embed → ERR_CONNECTION_REFUSED). The backend persists the job
    // durably, so once it's back the next poll re-attaches. Only give up after
    // several consecutive failures so a brief hiccup doesn't silently abort.
    const MAX_NET_FAILS = 8;  // ~16 s of unreachable server before giving up
    let polls = 0;
    let netFails = 0;

    // Mark embedding as active — enables beforeunload guard and banner
    _embedInProgress = true;
    _setEmbedBanner(t('embed.banner', { done: 0, total }));
    document.title = `⏳ Embedding (0/${total}) — Adult Media Manager`;
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
            document.title = `⏳ Embedding (${job.done}/${job.total}) — Adult Media Manager`;

            if (job.complete || polls >= MAX_POLLS) {
                // R2: a job the server flipped to "interrupted" (restart killed
                // the FFmpeg work) is terminal — tell the user rather than
                // silently stopping. NFO sidecars written before the restart are
                // already durable; only the in-container embed may be incomplete.
                if (job.status === 'interrupted') {
                    showToast(t('embed.interrupted_title'), t('embed.interrupted'), 'info', 6000);
                }
                _finishEmbedPolling(job.warnings);
            } else {
                setTimeout(tick, INTERVAL_MS);
            }
        } catch {
            // Transient network error (server restarting / momentarily
            // unreachable). Keep retrying up to MAX_NET_FAILS so a brief outage
            // doesn't abandon a still-running, durably-persisted job.
            netFails++;
            if (netFails >= MAX_NET_FAILS || polls >= MAX_POLLS) {
                _finishEmbedPolling(null);
            } else {
                _setEmbedBanner(t('embed.reconnecting'));
                setTimeout(tick, INTERVAL_MS);
            }
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
                            ${result.companions_moved ? `<div style="color: var(--text-secondary); font-size: 11px; margin-top: 4px;">${escapeHtml(t('rename.companions_moved', { n: result.companions_moved }))}</div>` : ''}
                            ${result.error ? `<div style="color: var(--error); font-size: 12px; margin-top: 4px;">${escapeHtml(result.error)}</div>` : ''}
                            ${result.embed_warning ? `<div style="color: var(--warning, #f0a500); font-size: 11px; margin-top: 4px;">⚠️ ${escapeHtml(result.embed_warning)}</div>` : ''}
                        </div>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

