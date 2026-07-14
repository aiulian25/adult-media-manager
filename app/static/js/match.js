// ═══ Match Files (streaming + cancellable) ═══
//
// Matching streams results over SSE so the UI fills in incrementally AND the user
// can CANCEL a long match at any time: the Match button becomes a Cancel button
// while matching runs — a second function on the same button (onMatchClick). Cancel
// closes the EventSource; the server detects the disconnect between files, cancels
// the still-pending lookups, and flushes whatever it already matched to the cache
// (see match_stream's is_disconnected check), so partial results are kept, never
// lost. Identical across Docker/deb/AppImage — pure SSE + FastAPI, no platform code.
let _matchEventSource = null;   // active SSE handle, or null when idle
let _matchInProgress  = false;  // true between start and done/error/cancel
let _matchStopped     = false;  // true when the user pressed Cancel
let _matchResolve     = null;   // resolves the matchFiles() promise on cancel
let _matchOrdered     = null;   // result slots, lifted so Cancel can show partials

// Dispatch the Match button click: start a match, or cancel the running one. The
// single button carries both functions (start ⇄ cancel); wired in core.js.
function onMatchClick() {
    if (_matchInProgress) cancelMatch();
    else                  matchFiles();
}

// Swap the Match button between its "Match" and "Cancel" faces. It stays clickable
// in both states; the data-i18n attr is swapped too so a mid-match language change
// still localises the label correctly.
function _setMatchRunning(running) {
    _matchInProgress = running;
    if (!btnMatch) return;
    if (running) {
        btnMatch.dataset.i18n = 'nav.cancel_match';
        btnMatch.textContent  = t('nav.cancel_match');
        btnMatch.classList.add('btn-cancel');
        btnMatch.disabled = false;   // must stay clickable so the user can cancel
    } else {
        btnMatch.dataset.i18n = 'nav.match';
        btnMatch.textContent  = t('nav.match');
        btnMatch.classList.remove('btn-cancel');
    }
}

// Cancel an in-flight match, keeping the results matched so far. Closing the
// EventSource disconnects the request (this does NOT fire an SSE error event); the
// server stops between files and flushes the cache, and we publish the partials.
function cancelMatch() {
    if (!_matchEventSource) return;
    _matchStopped = true;
    try { _matchEventSource.close(); } catch (_) { /* ignore */ }
    _matchEventSource = null;
    _finishMatch(true);
    if (_matchResolve) { _matchResolve(); _matchResolve = null; }
}

// Publish whatever is in the ordered slots after a Cancel and restore the button.
// The normal "done" path finalises inline (it has the server's matched/total
// counts); this handles only the cancelled case so partial work isn't discarded.
function _finishMatch(stopped) {
    const ordered = _matchOrdered || [];
    matchedResults = ordered.filter(Boolean);
    _setMatchRunning(false);
    if (stopped) {
        const matched = matchedResults.filter(m => m.match).length;
        progressFill.style.width = '0%';
        showStatus(t('status.match_stopped', { matched, total: ordered.length }), 'info');
        displayMatches();
        btnRename.disabled = matched === 0;
        setTimeout(() => {
            statusBar.classList.add('hidden');
            progressFill.style.width = '0%';
        }, 2500);
    }
}

// Client-side chunk size (F11). Mirrors the server's _SSE_MAX_FILES DoS cap
// (main.py) — keep the two in sync. `let` (not const) so tests can shrink it.
let MATCH_CHUNK_SIZE = 500;

async function matchFiles() {
    // Only match files the user has checked (default: all)
    const filesToMatch = selectedScannedIndices.size > 0
        ? [...selectedScannedIndices].sort((a,b) => a-b).map(i => scannedFiles[i])
        : scannedFiles;

    if (filesToMatch.length === 0) return;

    const total = filesToMatch.length;
    showStatus(t('status.matching_start', { total }));
    progressFill.style.width = '0%';
    btnRename.disabled = true;
    _matchStopped = false;

    // Ordered result slots across ALL chunks; each chunk writes at its base
    // offset. Lifted to module scope so cancelMatch() can publish partials.
    const ordered = new Array(total).fill(null);
    _matchOrdered = ordered;

    const datasource = document.getElementById('datasource').value;
    // "Re-match" forces a fresh API query, ignoring the persistent cache (D3).
    const refresh = !!document.getElementById('ignore-cache')?.checked;

    // Sequential sessions of ≤500 files (F11): the server caps each session as
    // a DoS guard, so the client paces itself instead of silently losing file
    // 501+. Cancel stops the current stream AND the remaining chunks.
    const chunks = Math.ceil(total / MATCH_CHUNK_SIZE);
    _setMatchRunning(true);
    let matchedTotal = 0;
    let failed = false;

    for (let c = 0; c < chunks && !_matchStopped && !failed; c++) {
        const base = c * MATCH_CHUNK_SIZE;
        const chunkFiles = filesToMatch.slice(base, base + MATCH_CHUNK_SIZE);
        const out = await _streamOneMatchSession(chunkFiles, base, ordered, {
            datasource, refresh, total, chunks, chunk: c + 1,
        });
        if (out.error) failed = true;
        else matchedTotal += out.matched;
    }

    if (_matchStopped) return;      // cancelMatch → _finishMatch published partials
    matchedResults = ordered.filter(Boolean);
    if (failed) {
        // Keep what completed instead of discarding finished chunks.
        displayMatches();
        _setMatchRunning(false);
        btnRename.disabled = matchedResults.filter(m => m.match).length === 0;
        return;
    }
    progressFill.style.width = '100%';
    showStatus(t('status.matched', { matched: matchedTotal, total }), 'success');
    displayMatches();
    _setMatchRunning(false);
    btnRename.disabled = matchedResults.filter(m => m.match).length === 0;
    setTimeout(() => {
        statusBar.classList.add('hidden');
        progressFill.style.width = '0%';
    }, 2000);
}

// Run ONE match session (≤500 files) and stream its results into `ordered`
// starting at `baseIndex`. Resolves {matched} on done, {matched: 0} on cancel,
// {error: true} on failure (error already shown). Factored out of matchFiles
// so the chunk loop (F11) reuses the exact session + SSE flow.
function _streamOneMatchSession(files, baseIndex, ordered, ctx) {
    return new Promise(async (resolve) => {
        // Stage 1: POST the file list to register a server-side session.
        // This avoids URL query-string size limits that would break large batches.
        let sessionId;
        try {
            const sessResp = await fetch('/api/match-session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ files, datasource: ctx.datasource,
                                       auto_match: true, refresh: ctx.refresh }),
            });
            if (!sessResp.ok) {
                const err = await sessResp.json().catch(() => ({}));
                showStatus(t('error.match_failed', { message: err.detail || sessResp.statusText }), 'error');
                resolve({ matched: 0, error: true });
                return;
            }
            sessionId = (await sessResp.json()).session_id;
        } catch (err) {
            showStatus(t('error.match_failed', { message: err.message }), 'error');
            resolve({ matched: 0, error: true });
            return;
        }

        // Stage 2: open the SSE stream using only the opaque session token.
        _matchResolve = () => resolve({ matched: 0 });   // cancelMatch path
        const es = new EventSource(`/api/match-stream?session_id=${encodeURIComponent(sessionId)}`);
        _matchEventSource = es;

        es.addEventListener('progress', (e) => {
            const d = JSON.parse(e.data);
            const done = baseIndex + d.done;
            const pct = Math.round((done / ctx.total) * 100);
            progressFill.style.width = `${pct}%`;
            if (ctx.chunks > 1) {
                showStatus(t('status.matching_chunk',
                    { done, total: ctx.total, chunk: ctx.chunk, chunks: ctx.chunks }));
            } else {
                // Show truncated filename so long paths don't overflow the bar
                const short = d.filename.length > 60
                    ? '…' + d.filename.slice(-57)
                    : d.filename;
                showStatus(t('status.matching_file', { done, total: ctx.total, filename: short }));
            }
        });

        es.addEventListener('result', (e) => {
            const d = JSON.parse(e.data);
            ordered[baseIndex + d.index] = d.match;
        });

        es.addEventListener('done', (e) => {
            es.close();
            _matchEventSource = null;
            _matchResolve = null;
            const d = JSON.parse(e.data);
            resolve({ matched: d.matched });
        });

        es.addEventListener('error', (e) => {
            es.close();
            _matchEventSource = null;
            _matchResolve = null;
            // A user Cancel closes the stream via cancelMatch() (which already
            // finalised); browsers may still fire a generic error on close, so
            // swallow it here instead of flashing a spurious "connection error".
            if (_matchStopped) { resolve({ matched: 0 }); return; }
            // Otherwise it's a real failure; the server may also emit a structured
            // error event with a detail payload.
            let msg = 'Match failed — connection error';
            if (e.data) {
                try { msg = JSON.parse(e.data).detail || msg; } catch { /* ignore */ }
            }
            showStatus(t('error.match_failed', { message: msg }), 'error');
            progressFill.style.width = '0%';
            resolve({ matched: 0, error: true });
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
                // Scan-entry fields (duration/quality/codec) → NFO runtime +
                // streamdetails (F5), matching what a rename's Phase-2 writes.
                file_data: result.original,
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
                body: JSON.stringify({ file_path: r.original.path, scene_data: r.match, file_data: r.original }),
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

/**
 * Classify a 0–100 confidence into a band for human-readable interpretation.
 * Returns { key, label, cls } where cls drives the colour (CSS) and label is
 * localised. Bands: High ≥ 80, Medium 50–79, Low < 50.
 */
function _confidenceBand(pct) {
    if (pct >= 80) return { key: 'high',   label: t('match.band_high'),   cls: 'conf-high' };
    if (pct >= 50) return { key: 'medium', label: t('match.band_medium'), cls: 'conf-medium' };
    return { key: 'low', label: t('match.band_low'), cls: 'conf-low' };
}

/**
 * Provenance badge for a match: user-confirmed > cached (D3) > fingerprint (U4).
 * Returns an HTML string (possibly empty).
 */
function _matchBadge(result) {
    if (result.user_confirmed) {
        return `<span class="conf-verified" title="${escapeHtml(t('match.confirmed_hint'))}">★ ${escapeHtml(t('match.confirmed'))}</span>`;
    }
    if (result.cached || result.match_method === 'cache') {
        return `<span class="conf-cached" title="${escapeHtml(t('match.cached_hint'))}">⚡ ${escapeHtml(t('match.cached'))}</span>`;
    }
    if (result.match_method === 'fingerprint') {
        return `<span class="conf-verified" title="${escapeHtml(t('match.fingerprint_hint'))}">✓ ${escapeHtml(t('match.fingerprint'))}</span>`;
    }
    return '';
}

/**
 * Evidence-coverage note (D7): "based on title + duration only" so a high % on a
 * sparse match reads as honest agreement among the *available* fields rather than
 * an unqualified score. Returns an HTML string (empty when no field info).
 */
function _evidenceNote(result) {
    const fields = result.match_fields;
    if (!Array.isArray(fields) || fields.length === 0) return '';
    const names = fields.map(f => t('match.field_' + f));
    return `<span class="conf-evidence" title="${escapeHtml(t('match.based_on_hint'))}">${
        escapeHtml(t('match.based_on', { fields: names.join(', ') }))
    }</span>`;
}

// Cap the up-front match rows like the scanned list (R3/U1). Matches are already
// server-capped at 500 (_SSE_MAX_FILES), so this mainly avoids a large DOM build;
// selection is tracked in selectedMatchIndices (a Set), never the DOM, so capping
// never changes which files get renamed.
const MATCH_RENDER_CAP = 300;

/**
 * Build one match row by cloning the #tpl-match-row / #tpl-match-nomatch template
 * (review item R3) instead of concatenating a giant HTML string. User-supplied
 * text is set via textContent (XSS-safe by construction); the badge/evidence
 * helpers return already-escaped HTML and go into dedicated slots; handlers are
 * wired with addEventListener (no inline on* / JSON string-escaping).
 * @param {{result: object, index: number}} item
 * @returns {Node}
 */
function _buildMatchRow({ result, index }) {
    const orig = result.original || {};

    if (!result.match) {
        const node = document.getElementById('tpl-match-nomatch')
            .content.firstElementChild.cloneNode(true);
        node.id = `match-item-${index}`;
        node.querySelector('.match-cb').dataset.index = index;
        // F15: a failed provider call is NOT "no match" — say the lookup
        // errored (auth/rate-limit/network/internal) so the user knows the
        // result is unknown rather than absent. Unknown kinds render as
        // "internal" so a future kind never shows a raw i18n key.
        if (result.lookup_error) {
            const kind = ['auth', 'rate_limit', 'network', 'internal']
                .includes(result.lookup_error) ? result.lookup_error : 'internal';
            node.querySelector('[data-nomatch]').textContent =
                `⚠ ${t('match.lookup_error_' + kind)}`;
        } else {
            node.querySelector('[data-nomatch]').textContent = `❌ ${t('match.no_match')}`;
        }
        // Show the file size even with no match so duplicates can be compared and
        // the user can decide which copy to keep.
        const nmSize = formatFileSize(orig.size);
        node.querySelector('[data-original]').textContent =
            `${orig.filename || ''}${nmSize ? ' · ' + nmSize : ''}`;
        const editBtn = node.querySelector('[data-edit]');
        editBtn.textContent = `✏️ ${t('match.edit_manually')}`;
        editBtn.addEventListener('click', () => openManualEditModal(orig));
        _wireRemoveButton(node, orig);
        return node;
    }

    const m = result.match;
    const node = document.getElementById('tpl-match-row')
        .content.firstElementChild.cloneNode(true);
    node.id = `match-item-${index}`;
    node.dataset.index = index;

    const cb = node.querySelector('.match-cb');
    cb.dataset.index = index;
    // Derive checked from the selection Set (not hardcoded) so rows revealed
    // later via "Show all" reflect any prior Select-All / deselect (mirrors scan).
    cb.checked = selectedMatchIndices.has(index);
    cb.addEventListener('change', () => toggleMatchFile(index, cb.checked));

    // Thumbnail (optional)
    if (m.thumbnail_url) {
        const wrap = node.querySelector('[data-thumb]');
        const img = wrap.querySelector('img');
        img.src = m.thumbnail_url;
        img.alt = m.title || '';
        wrap.hidden = false;
    }

    // Title + optional (Manual) / (Your pick) tag
    node.querySelector('[data-title]').textContent = m.title || '';
    if (m.manual_entry) {
        const tag = node.querySelector('[data-manual]');
        tag.textContent = ` ${t('match.manual_badge')}`;
        tag.hidden = false;
    } else if (result.match_method === 'user_pick') {
        // Alternative chosen by the user (F3) — mark it like a manual entry.
        const tag = node.querySelector('[data-manual]');
        tag.textContent = ` ${t('match.picked_badge')}`;
        tag.hidden = false;
    }

    // Meta: site / performers / date
    node.querySelector('[data-site]').textContent = m.site || '';
    if (m.performers && m.performers.length) {
        const el = node.querySelector('[data-performers]');
        el.textContent = m.performers.join(', ');
        el.hidden = false;
    }
    if (m.release_date) {
        const el = node.querySelector('[data-date]');
        el.textContent = m.release_date;
        el.hidden = false;
    }

    // Tags (first 5)
    if (m.tags && m.tags.length) {
        const tagsEl = node.querySelector('[data-tags]');
        m.tags.slice(0, 5).forEach(tagText => {
            const span = document.createElement('span');
            span.className = 'tag';
            span.textContent = tagText;
            tagsEl.appendChild(span);
        });
        tagsEl.hidden = false;
    }

    // Original filename + derived file tech (resolution/source/group from the
    // detector, D4) surfaced as a tie-breaker hint.
    // Include the file size in the tech line so duplicates are easy to compare.
    const tech = [orig.quality, orig.source, orig.group, formatFileSize(orig.size)]
        .filter(Boolean).join(' · ');
    node.querySelector('[data-original]').textContent =
        `${t('match.original_label')}: ${orig.filename || ''}${tech ? ' · ' + tech : ''}`;

    // Confidence: badge slot + band + bar + evidence note
    const band = _confidenceBand(result.confidence);
    const badgeHtml = _matchBadge(result);
    if (badgeHtml) {
        const slot = node.querySelector('[data-badge]');
        slot.innerHTML = badgeHtml;   // helper output is pre-escaped
        slot.hidden = false;
    }
    const bandEl = node.querySelector('[data-band]');
    bandEl.classList.add(band.cls);
    bandEl.textContent = `${band.label} · ${result.confidence}%`;
    const bar = node.querySelector('[data-bar]');
    bar.classList.add(band.cls);
    bar.style.setProperty('--confidence', `${result.confidence}%`);
    const evidenceHtml = _evidenceNote(result);
    if (evidenceHtml) {
        const slot = node.querySelector('[data-evidence]');
        slot.innerHTML = evidenceHtml;   // helper output is pre-escaped
        slot.hidden = false;
    }

    // Actions: edit + write-NFO (keep #nfo-btn-${index} id — writeNfo/writeAllNfos use it)
    const editBtn = node.querySelector('[data-edit]');
    editBtn.textContent = `✏️ ${t('match.edit_btn')}`;
    editBtn.addEventListener('click', () => openManualEditModal(orig));
    const nfoBtn = node.querySelector('[data-nfo]');
    nfoBtn.id = `nfo-btn-${index}`;
    nfoBtn.textContent = `📄 ${t('match.write_nfo')}`;
    nfoBtn.addEventListener('click', () => writeNfo(index));
    _wireRemoveButton(node, orig);

    // Alternatives (F3): the server returns up to 5 deduped candidates per
    // match — surface them behind a toggle so a wrong best-match is one click
    // to fix instead of a full Manual Edit round trip.
    const alts = result.alternatives;
    if (Array.isArray(alts) && alts.length) {
        const altsToggle = node.querySelector('[data-alts-toggle]');
        const altsPanel = node.querySelector('[data-alts]');
        altsToggle.textContent = `▾ ${t('match.alternatives', { n: alts.length })}`;
        altsToggle.hidden = false;
        altsToggle.addEventListener('click', () => {
            if (altsPanel.hidden && !altsPanel.childElementCount) {
                _fillAlternatives(altsPanel, index);   // lazy build on first open
            }
            altsPanel.hidden = !altsPanel.hidden;
        });
    }

    return node;
}

// Build the compact alternative sub-rows for match `index` (F3). textContent
// only — same XSS discipline as the main row builder.
function _fillAlternatives(panel, index) {
    const r = matchedResults[index];
    if (!r || !Array.isArray(r.alternatives)) return;
    r.alternatives.forEach((alt, altIdx) => {
        const row = document.createElement('div');
        row.className = 'match-alt-row';
        const info = document.createElement('span');
        info.className = 'match-alt-info';
        info.textContent = [alt.title, alt.site, alt.release_date]
            .filter(Boolean).join(' · ');
        const useBtn = document.createElement('button');
        useBtn.className = 'glass-btn match-action-btn match-alt-use';
        useBtn.textContent = t('match.use_this');
        useBtn.addEventListener('click', () => useAlternative(index, altIdx));
        row.appendChild(info);
        row.appendChild(useBtn);
        panel.appendChild(row);
    });
}

// Swap an alternative in as the row's match (F3). The previous best stays
// pickable (swap, not discard); the row becomes user-confirmed at 100% — a
// pick is ground truth, not a cascade score — and the choice is persisted
// fire-and-forget so a later re-scan/match returns it as a confirmed cache hit.
function useAlternative(index, altIdx) {
    const r = matchedResults[index];
    if (!r || !Array.isArray(r.alternatives) || !r.alternatives[altIdx]) return;
    const alt = r.alternatives[altIdx];
    const prev = r.match;
    r.alternatives = r.alternatives.slice();
    r.alternatives[altIdx] = prev;
    r.match = alt;
    r.user_confirmed = true;
    r.match_method = 'user_pick';
    r.cached = false;
    r.confidence = 100;
    r.coverage = null;
    r.match_fields = [];

    if (r.original && r.original.oshash) {
        fetch('/api/confirm-match', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ oshash: r.original.oshash, scene: alt }),
        }).catch(() => { /* fire-and-forget — the swap already applied locally */ });
    }

    displayMatches(false);   // preserve selection + filter, re-render the rows
}

// Wire the per-row "Remove" button. Removing a file hides it from the app for
// this session — it is NOT deleted from disk — so it is no longer matched,
// renamed, or available for manual edit. Shared by the matched and no-match rows.
function _wireRemoveButton(node, orig) {
    const removeBtn = node.querySelector('[data-remove]');
    if (!removeBtn) return;
    removeBtn.textContent = `✕ ${t('match.remove')}`;
    removeBtn.title = t('match.remove_hint');
    removeBtn.addEventListener('click', () => removeMatchedFile(orig && orig.path));
}

// Drop a file from the match results AND the scanned list by path, so it stops
// being displayed, matched, renamed, or editable. Does NOT touch the file on
// disk. Selection is preserved across the re-index by remembering paths, so
// removing one row doesn't disturb which other rows stay selected (mirrors the
// post-rename prune in rename.js). A re-scan brings the file back if still there.
function removeMatchedFile(path) {
    if (!path) return;

    // Remember selected match paths (minus the one being removed) to restore after.
    const selMatchPaths = new Set(
        [...selectedMatchIndices].map(i => matchedResults[i] && matchedResults[i].original
            ? matchedResults[i].original.path : null).filter(Boolean)
    );
    selMatchPaths.delete(path);

    // Same for the scanned-list selection, so a later re-Match stays consistent.
    const selScanPaths = new Set(
        [...selectedScannedIndices].map(i => scannedFiles[i] ? scannedFiles[i].path : null).filter(Boolean)
    );
    selScanPaths.delete(path);

    matchedResults = matchedResults.filter(r => !(r.original && r.original.path === path));
    scannedFiles   = scannedFiles.filter(f => f.path !== path);

    selectedMatchIndices = new Set(
        matchedResults.map((r, i) => (r.original && selMatchPaths.has(r.original.path)) ? i : null)
                      .filter(i => i !== null)
    );
    selectedScannedIndices = new Set(
        scannedFiles.map((f, i) => selScanPaths.has(f.path) ? i : null).filter(i => i !== null)
    );

    displayMatches(false);   // re-render the match view without the removed row
    if (typeof showToast === 'function') {
        showToast(t('match.removed'), '', 'info', 1500);
    }
}

// ── "Needs review" queue (Suggested-Fields summary) ──────────────────────────
// Drive a review workflow from the normalized confidence (D7) + evidence
// coverage already on each result, so users can batch-confirm the clearly-good
// matches and focus on the ambiguous middle. Categories:
//   unmatched  – no scene matched
//   confirmed  – user already confirmed it (manual/accepted before, R1/D3)
//   high       – fingerprint-verified, OR ≥80% with enough evidence coverage
//   review     – matched but medium/low confidence, or a high % on sparse
//                evidence (low coverage) → warrants a human glance
const MATCH_REVIEW_COVERAGE_MIN = 0.5;
let matchFilter = 'all';   // all | review | high | confirmed | unmatched

function _matchCategory(result) {
    if (!result.match) return 'unmatched';
    if (result.user_confirmed) return 'confirmed';
    if (result.match_method === 'fingerprint') return 'high';  // near-certain
    const lowCoverage =
        typeof result.coverage === 'number' && result.coverage < MATCH_REVIEW_COVERAGE_MIN;
    if (result.confidence >= 80 && !lowCoverage) return 'high';
    return 'review';
}

/** Build the review/category filter bar (select + batch action). */
function _matchFilterBarHtml(counts) {
    const opt = (val, key) =>
        `<option value="${val}" ${matchFilter === val ? 'selected' : ''}>${
            escapeHtml(t(key, { n: counts[val] }))
        }</option>`;
    return `
        <div class="match-filter-bar">
            <label class="match-filter-label" for="match-filter">${escapeHtml(t('match.filter_label'))}</label>
            <select id="match-filter" class="glass-select" onchange="setMatchFilter(this.value)"
                    title="${escapeHtml(t('match.review_hint'))}">
                ${opt('all', 'match.filter_all')}
                ${opt('review', 'match.filter_review')}
                ${opt('high', 'match.filter_high')}
                ${opt('confirmed', 'match.filter_confirmed')}
                ${opt('unmatched', 'match.filter_unmatched')}
            </select>
            <button class="glass-btn match-selecthigh-btn" onclick="selectHighConfidence()"
                    title="${escapeHtml(t('match.select_high_hint'))}">⚡ ${escapeHtml(t('match.select_high'))}</button>
        </div>`;
}

function displayMatches(resetSelection = true) {
    // A full render (new results) selects all matched and shows the unfiltered
    // list; filter/select re-renders pass false to preserve both.
    if (resetSelection) {
        selectedMatchIndices = new Set(
            matchedResults.map((r, i) => r.match ? i : null).filter(i => i !== null)
        );
        matchFilter = 'all';
    }
    const matchedCount = matchedResults.filter(m => m.match).length;

    // Category counts for the filter bar.
    const counts = { all: matchedResults.length, review: 0, high: 0, confirmed: 0, unmatched: 0 };
    matchedResults.forEach(r => { counts[_matchCategory(r)]++; });

    // Header panel (small, static) built once via innerHTML; rows are cloned from
    // <template> and appended with windowing below.
    resultsContainer.innerHTML = `
        <div class="glass-panel match-header-panel">
            <div class="selection-header">
                <label class="select-all-label">
                    <input type="checkbox" id="select-all-matches" ${matchedCount > 0 ? 'checked' : ''}
                           onchange="toggleSelectAllMatches(this.checked)">
                    <span>${escapeHtml(t('match.select_all'))}</span>
                </label>
                <h3>${escapeHtml(t('match.matched_scenes'))} &nbsp;<span class="selection-count" id="match-sel-count">${matchedCount}</span> / ${matchedResults.length} selected</h3>
                <button class="glass-btn match-writeall-btn" id="write-all-nfo-btn" onclick="writeAllNfos()">📄 ${escapeHtml(t('match.write_all_nfos'))}</button>
            </div>
            ${matchedCount > 0 ? _matchFilterBarHtml(counts) : ''}
        </div>`;

    // Zero-result guidance: a single top-level explanation instead of a wall of
    // red "no match" rows.
    if (matchedCount === 0) {
        resultsContainer.insertAdjacentHTML('beforeend',
            `<div class="glass-panel match-zero-panel">${
                _emptyStateHtml('🔍',
                    t('empty.match_title', { total: matchedResults.length }),
                    t('empty.match_subtitle'))
            }</div>`);
    }

    // Apply the active filter (selection is unaffected — only what's shown).
    let items = matchedResults.map((result, index) => ({ result, index }));
    if (matchFilter !== 'all') {
        items = items.filter(it => _matchCategory(it.result) === matchFilter);
    }

    // Empty filtered view (e.g. "Needs review" with nothing to review).
    if (matchedCount > 0 && items.length === 0) {
        resultsContainer.insertAdjacentHTML('beforeend',
            `<div class="glass-panel match-zero-panel">${
                _emptyStateHtml('✅', t('match.filter_empty_title'), t('match.filter_empty_subtitle'))
            }</div>`);
    }

    // Windowed rows from the <template> clone.
    const rowsHost = document.createElement('div');
    resultsContainer.appendChild(rowsHost);
    _renderWindowed(rowsHost, items, _buildMatchRow, MATCH_RENDER_CAP, _updateMatchUI);

    _updateMatchUI();
    updateTemplatePreview();   // matched data available — preview is now most accurate
}

/** Filter the visible match rows by category (selection is preserved). */
function setMatchFilter(value) {
    matchFilter = value;
    displayMatches(false);
}

/** Batch-select only the high-confidence matches for a one-click rename. */
function selectHighConfidence() {
    selectedMatchIndices = new Set(
        matchedResults
            .map((r, i) => (r.match && _matchCategory(r) === 'high') ? i : null)
            .filter(i => i !== null)
    );
    displayMatches(false);   // re-render so checkbox states reflect the new set
    showToast(t('match.selected_high', { n: selectedMatchIndices.size }), '', 'info');
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

