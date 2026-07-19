// ═══ Manual date validation ═══
// The release date is optional, but when present it must be a real calendar
// date within a sane range (no typos like year 0205, no future releases). The
// <input type="date"> already enforces YYYY-MM-DD; this adds the range/real-date
// guard and an inline message. Pure client-side UX — the value is non-secret and
// is XML-escaped / path-sanitised downstream, so no server change is required.
const _MANUAL_DATE_MIN = '1900-01-01';

/** @returns {{valid: boolean, error: string}} — empty value is valid (optional field). */
function _validateManualDate(value) {
    const v = (value || '').trim();
    if (!v) return { valid: true, error: '' };

    // Must be an exact ISO calendar date (round-trips through Date without rollover).
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
    if (m) {
        const [y, mo, d] = [+m[1], +m[2], +m[3]];
        const dt = new Date(Date.UTC(y, mo - 1, d));
        const real = dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d;
        if (real) {
            const todayIso = new Date().toISOString().slice(0, 10);
            if (v < _MANUAL_DATE_MIN || v > todayIso) {
                return { valid: false, error: t('manual.date_range', { min: _MANUAL_DATE_MIN, max: todayIso }) };
            }
            return { valid: true, error: '' };
        }
    }
    return { valid: false, error: t('manual.date_invalid') };
}

/** Show/clear the inline date error; returns whether the date is valid. */
function _refreshManualDateError() {
    const input = document.getElementById('manual-date');
    const errEl = document.getElementById('manual-date-error');
    const { valid, error } = _validateManualDate(input ? input.value : '');
    if (errEl) {
        errEl.textContent = valid ? '' : error;
        errEl.classList.toggle('is-shown', !valid);
    }
    if (input) input.classList.toggle('input-error', !valid);
    return valid;
}

// ═══ Manual Edit Modal ═══

// Provider identity of the last fetched scene (F7). Set by _fetchSceneFromUrl,
// reset on every modal open, forwarded by saveManualMetadata so the NFO gets a
// real <uniqueid type="stashdb|tpdb"> and the confirmed cache entry keeps the
// provider linkage. Stays null for hand-typed entries → id remains "manual".
let manualSceneId = null;
let manualSceneSource = null;
function openManualEditModal(fileData) {
    currentManualFile = fileData;
    manualPerformers = [];
    manualTags = [];
    generatedThumbnails = [];
    selectedThumbnailIndex = null;
    manualSceneId = null;        // provider identity never leaks between files (F7)
    manualSceneSource = null;
    
    // Populate form
    document.getElementById('manual-edit-filename').textContent = fileData.filename;
    document.getElementById('manual-title').value = fileData.scene_title || fileData.clean_name || '';
    document.getElementById('manual-site').value = fileData.site || '';
    const manualDate = document.getElementById('manual-date');
    manualDate.value = fileData.release_date || '';
    manualDate.max = new Date().toISOString().slice(0, 10);   // no future releases
    _refreshManualDateError();                                // validate any pre-filled value
    document.getElementById('manual-quality').value = fileData.quality || '';
    const descEl = document.getElementById('manual-description');
    if (descEl) descEl.value = '';
    
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
    
    // Reset the StashDB / ThePornDB URL fields + status for this file
    ['manual-stashdb-url', 'manual-tpdb-url'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    _setSceneFetchStatus('manual-stashdb-status', '', null);
    _setSceneFetchStatus('manual-tpdb-status', '', null);

    // Show thumbnails section
    document.getElementById('manual-thumbnails-section').style.display = 'block';
    document.getElementById('manual-thumbnails-grid').innerHTML = '';

    manualEditModal.classList.remove('hidden');
}

// ─── Fetch metadata from a pasted scene URL (StashDB or ThePornDB) ───
// Shows an inline status (no native alerts). Populates title/site/date and
// REPLACES the performer/tag chips with the fetched scene's values so the form
// reflects exactly what was fetched (the user can still tweak before saving).
// One shared implementation drives both sources; the per-source endpoint, DOM
// ids and i18n keys are passed in.
function _setSceneFetchStatus(statusId, msg, kind /* 'ok' | 'error' | null */) {
    const el = document.getElementById(statusId);
    if (!el) return;
    el.textContent = msg || '';
    el.classList.toggle('is-error', kind === 'error');
    el.classList.toggle('is-ok', kind === 'ok');
}

async function _fetchSceneFromUrl(cfg) {
    const input = document.getElementById(cfg.inputId);
    const btn = document.getElementById(cfg.btnId);
    const url = (input ? input.value : '').trim();
    if (!url) {
        _setSceneFetchStatus(cfg.statusId, t(cfg.needUrlKey), 'error');
        if (input) input.focus();
        return;
    }

    const origLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = t(cfg.fetchingKey); }
    _setSceneFetchStatus(cfg.statusId, t(cfg.fetchingKey), null);

    try {
        const res = await fetch(cfg.endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
        if (!res.ok) {
            let detail = t(cfg.failedKey);
            try {
                const j = await res.json();
                // Typed lookup failure (roadmap-2 F15): {code: auth|rate_limit|
                // network} maps through the same keys the match rows use, so
                // "API key rejected" reads identically on both surfaces.
                if (j.detail && typeof j.detail === 'object' && j.detail.code) {
                    const known = ['auth', 'rate_limit', 'network'];
                    const kind = known.includes(j.detail.code) ? j.detail.code : 'internal';
                    detail = t('match.lookup_error_' + kind);
                } else if (j.detail) {
                    detail = j.detail;
                }
            } catch (_) {}
            throw new Error(detail);
        }
        const data = await res.json();
        const scene = data.scene || {};

        // Populate scalar fields
        if (scene.title) document.getElementById('manual-title').value = scene.title;
        if (scene.site) document.getElementById('manual-site').value = scene.site;
        const dateInput = document.getElementById('manual-date');
        if (dateInput) { dateInput.value = scene.release_date || ''; _refreshManualDateError(); }
        // Synopsis + provider identity (F7) — the endpoint already returns
        // them; keep them so Save writes <plot> and a real <uniqueid>.
        const descEl = document.getElementById('manual-description');
        if (descEl) descEl.value = scene.description || '';
        manualSceneId = scene.id || null;
        manualSceneSource = scene.source || null;

        // Replace performer + tag chips with the fetched values
        manualPerformers = Array.isArray(scene.performers) ? [...scene.performers] : [];
        manualTags = Array.isArray(scene.tags) ? [...scene.tags] : [];
        renderChips('performers', manualPerformers);
        renderChips('tags', manualTags);

        _setSceneFetchStatus(cfg.statusId, t(cfg.loadedKey, { title: scene.title || '' }), 'ok');
    } catch (err) {
        _setSceneFetchStatus(cfg.statusId, err.message || t(cfg.failedKey), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = origLabel || t(cfg.fetchKey); }
    }
}

function fetchStashDBScene() {
    return _fetchSceneFromUrl({
        endpoint: '/api/stashdb/scene',
        inputId: 'manual-stashdb-url', btnId: 'manual-stashdb-fetch', statusId: 'manual-stashdb-status',
        needUrlKey: 'manual.stashdb_need_url', fetchingKey: 'manual.stashdb_fetching',
        failedKey: 'manual.stashdb_failed', loadedKey: 'manual.stashdb_loaded', fetchKey: 'manual.stashdb_fetch',
    });
}

function fetchTPDBScene() {
    return _fetchSceneFromUrl({
        endpoint: '/api/tpdb/scene',
        inputId: 'manual-tpdb-url', btnId: 'manual-tpdb-fetch', statusId: 'manual-tpdb-status',
        needUrlKey: 'manual.tpdb_need_url', fetchingKey: 'manual.tpdb_fetching',
        failedKey: 'manual.tpdb_failed', loadedKey: 'manual.tpdb_loaded', fetchKey: 'manual.tpdb_fetch',
    });
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
        showToast('Title required', 'Please enter a scene title.', 'error');
        document.getElementById('manual-title').focus();
        return;
    }

    // Block save on an invalid release date (range / non-real date).
    if (!_refreshManualDateError()) {
        const dateInput = document.getElementById('manual-date');
        showToast(t('manual.date_invalid_title'), _validateManualDate(dateInput.value).error, 'error');
        dateInput.focus();
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
        thumbnail_index: selectedThumbnailIndex,
        embed_mode: _getEmbedMode(),
        // F7: synopsis + provider identity (null for hand-typed entries —
        // the backend then keeps id="manual" and writes no <uniqueid>).
        description: document.getElementById('manual-description')?.value.trim() || null,
        scene_id: manualSceneId,
        source: manualSceneSource
    };

    // Capture display name before closing modal
    const displayName = currentManualFile.name || currentManualFile.path.split('/').pop();

    // Close modal immediately — job continues in background
    manualEditModal.classList.add('hidden');
    showBgJob(displayName);

    // The request now only writes the NFO sidecar + selects a thumbnail and
    // returns immediately; the heavy FFmpeg container remux runs in the
    // background (tracked via /api/embed-status). A short timeout is enough to
    // catch a hung connection without the old multi-minute wait.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 60 * 1000);

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

        // NFO sidecar is written → metadata is durable immediately. If a
        // container embed was requested it now runs in the background; reuse the
        // shared embed-status poller (banner + progress) instead of blocking.
        if (data.embed_job_id) {
            showToast(t('manual.saved_title'), t('manual.saved_embedding'), 'success', 3000);
            _pollEmbedStatus(data.embed_job_id, 1);
        } else {
            showToast(t('manual.saved_title'), displayName, 'success', 3000);
        }

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

