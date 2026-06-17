// ═══ Background Job Indicator ═══
function showBgJob(filename) {
    document.getElementById('bg-job-filename').textContent = filename;
    document.getElementById('bg-job-indicator').classList.remove('hidden');
}

function hideBgJob() {
    document.getElementById('bg-job-indicator').classList.add('hidden');
}

// ═══ Toast Notifications ═══
function showToast(title, message, type = 'info', duration = 3000) {
    const icons = { success: '✅', error: '❌', info: 'ℹ️' };
    const container = document.getElementById('toast-container');

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <div class="toast-body">
            <div class="toast-title">${escapeHtml(title)}</div>
            ${message ? `<div class="toast-message">${escapeHtml(message)}</div>` : ''}
        </div>
    `;

    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('toast-out');
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ═══ Human-readable file size ═══
// Binary units (1024) to match what file managers / Synology DSM show, so the
// numbers users compare here line up with their NAS. Returns '' for unknown
// sizes so callers can omit the segment cleanly. Shared by every build target.
function formatFileSize(bytes) {
    if (typeof bytes !== 'number' || !isFinite(bytes) || bytes < 0) return '';
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    const val = bytes / Math.pow(1024, i);
    // Whole numbers for bytes and for values ≥ 100; one decimal otherwise.
    const text = (i === 0 || val >= 100) ? Math.round(val).toString() : val.toFixed(1);
    return `${text} ${units[i]}`;
}

// ═══ Go-to-top button ═══
// The app scrolls the page (no inner-scroll container), so with many scanned/
// matched rows the toolbar scrolls out of reach. A floating button appears once
// the user scrolls down and jumps back to the top. Pure renderer UI — identical
// across Docker/deb/AppImage.
(function initGoToTop() {
    const btn = document.getElementById('go-to-top');
    if (!btn) return;
    const SHOW_AFTER = 400;   // px scrolled before the button appears
    const onScroll = () => {
        btn.classList.toggle('is-visible', window.scrollY > SHOW_AFTER);
    };
    btn.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
})();

// ═══ Status Bar ═══
function showStatus(message, type = 'info') {
    statusBar.classList.remove('hidden');
    statusText.textContent = message;
    
    if (type === 'error') {
        statusText.style.color = 'var(--error)';
    } else if (type === 'success') {
        statusText.style.color = 'var(--success)';
    } else {
        statusText.style.color = 'var(--text-secondary)';
    }
}
