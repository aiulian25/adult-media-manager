// ═══ Right-click "Copy" context menu (shared: browser + Electron) ═══
//
// Why this exists: Electron (deb/AppImage) shows NO context menu by default, so
// right-click did nothing there. The browser (Docker) has its own menu, but we
// render our own everywhere so the behaviour is identical across all three build
// targets from ONE implementation — no platform-specific code.
//
// Scope is deliberately minimal: copy the current text selection. The menu only
// appears when there IS a selection; right-clicking elsewhere is left untouched.
//
// Copy uses the async Clipboard API when available in a secure context, falling
// back to document.execCommand('copy'). The fallback matters because a Docker
// instance reached over a plain-HTTP LAN IP (e.g. http://192.168.x.x:8887) is NOT
// a secure context, so navigator.clipboard is unavailable there; localhost,
// 127.0.0.1 and the Electron app are secure and use the modern API.
(function initCopyContextMenu() {
    let menuEl = null;

    // Return the selected text, handling both a normal document selection and a
    // selection inside an <input>/<textarea> (whose text is not in getSelection()).
    function getSelectedText() {
        const el = document.activeElement;
        if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') &&
            typeof el.selectionStart === 'number' &&
            el.selectionStart !== el.selectionEnd) {
            return el.value.substring(el.selectionStart, el.selectionEnd);
        }
        const sel = window.getSelection();
        return sel ? sel.toString() : '';
    }

    async function copyText(text) {
        if (!text) return false;
        // Preferred path: async Clipboard API (requires a secure context).
        if (navigator.clipboard && window.isSecureContext) {
            try { await navigator.clipboard.writeText(text); return true; }
            catch (_) { /* fall through to the legacy path */ }
        }
        // Legacy fallback for non-secure contexts (LAN-IP Docker over HTTP).
        try {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.top = '-1000px';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand('copy');
            document.body.removeChild(ta);
            return ok;
        } catch (_) { return false; }
    }

    function closeMenu() {
        if (menuEl) { menuEl.remove(); menuEl = null; }
        document.removeEventListener('mousedown', onDocDown, true);
        document.removeEventListener('keydown', onKey, true);
        document.removeEventListener('scroll', closeMenu, true);
        window.removeEventListener('blur', closeMenu);
        window.removeEventListener('resize', closeMenu);
    }
    function onDocDown(e) { if (menuEl && !menuEl.contains(e.target)) closeMenu(); }
    function onKey(e) { if (e.key === 'Escape') closeMenu(); }

    function showMenu(x, y, text) {
        closeMenu();
        menuEl = document.createElement('div');
        menuEl.className = 'context-menu glass-panel';

        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'context-menu-item';
        item.textContent = (typeof t === 'function' ? t('ctx.copy') : 'Copy');
        item.addEventListener('click', async () => {
            const ok = await copyText(text);
            closeMenu();
            if (ok && typeof showToast === 'function') {
                showToast(typeof t === 'function' ? t('ctx.copied') : 'Copied', '', 'success', 1500);
            }
        });
        menuEl.appendChild(item);

        document.body.appendChild(menuEl);

        // Clamp to the viewport so the menu never opens off-screen.
        const r = menuEl.getBoundingClientRect();
        const left = Math.max(6, Math.min(x, window.innerWidth  - r.width  - 6));
        const top  = Math.max(6, Math.min(y, window.innerHeight - r.height - 6));
        menuEl.style.left = `${Math.round(left)}px`;
        menuEl.style.top  = `${Math.round(top)}px`;

        // Defer the dismiss listeners so the opening right-click doesn't close it.
        setTimeout(() => {
            document.addEventListener('mousedown', onDocDown, true);
            document.addEventListener('keydown', onKey, true);
            document.addEventListener('scroll', closeMenu, true);
            window.addEventListener('blur', closeMenu);
            window.addEventListener('resize', closeMenu);
        }, 0);
    }

    document.addEventListener('contextmenu', (e) => {
        const text = getSelectedText();
        // No selection → don't hijack the event (browser keeps its default menu;
        // Electron simply shows nothing, as before).
        if (!text || !text.trim()) { closeMenu(); return; }
        e.preventDefault();
        showMenu(e.clientX, e.clientY, text);
    });
})();
