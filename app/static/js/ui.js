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
