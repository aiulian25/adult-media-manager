// ═══ Browse Modal ═══
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

        const note = `<div class="history-note">${escapeHtml(t('history.revert_note'))}</div>`;
        const rows = data.entries.map(entry => `
            <div class="history-item">
                <div class="history-head">
                    <span class="history-action">${escapeHtml(entry.action.toUpperCase())}</span>
                    <span class="history-time">${escapeHtml(entry.timestamp)}</span>
                    ${entry.revertible
                        ? `<button class="glass-btn history-revert-btn" data-id="${escapeHtml(entry.id)}">${escapeHtml(t('history.revert'))}</button>`
                        : ''}
                </div>
                <div class="history-path">${escapeHtml(t('history.from'))} ${escapeHtml(entry.old_path)}</div>
                <div class="history-path">${escapeHtml(t('history.to'))} ${escapeHtml(entry.new_path)}</div>
                ${entry.error ? `<div style="color:var(--error);font-size:11px;margin-top:4px;">${escapeHtml(entry.error)}</div>` : ''}
            </div>
        `).join('');
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
            showToast(t('history.revert_title'), t('history.reverted_ok'), 'success');
        } else {
            showToast(t('history.revert_title'), _revertCodeMsg(data.code), 'info');
        }
        loadHistory();   // refresh — a successful revert adds an "undo" entry
    } catch (error) {
        showToast(t('history.revert_title'), error.message, 'error');
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = t('history.revert'); }
    }
}

