#!/usr/bin/env node
/**
 * UI geometry assertions — headless, run against a live AMM server.
 *
 *   npm run check:ui                       # assumes http://127.0.0.1:8000
 *   AMM_URL=http://127.0.0.1:8931 npm run check:ui
 *
 * Requires a Chrome/Chromium binary and puppeteer-core:
 *   npm install --no-save puppeteer-core@23
 *   CHROME_PATH=/usr/bin/google-chrome npm run check:ui
 *
 * Why this file exists
 * --------------------
 * The three toolbar nav icons shipped for several releases rendering 8px WIDE
 * by 20px tall — squashed, not small. `.fusion-bar .glass-btn { padding: 5px
 * 10px }` had the same specificity as `.fusion-nav .glass-btn { padding: 0 }`
 * and sat later in the file, so it won and left an 8px content box that
 * flex-shrunk the icon. Three separate "make the icon bigger" fixes changed
 * numbers on the rule that was already losing, so nothing moved on screen.
 *
 * A cascade collision is invisible in source review and obvious in a rendered
 * box. These assertions read the rendered box.
 */

const URL = process.env.AMM_URL || 'http://127.0.0.1:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome';

let puppeteer;
try {
    puppeteer = require('puppeteer-core');
} catch {
    console.error('SKIP: puppeteer-core is not installed.\n' +
                  '      npm install --no-save puppeteer-core@23');
    process.exit(0);
}

// Every icon-only control: the rendered glyph must stay square (a squashed
// glyph means something is eating the content box) and must fill most of its
// button (a tiny glyph means the size never took effect).
const ICON_BUTTONS = [
    { id: 'btn-library',  label: 'Library'  },
    { id: 'btn-history',  label: 'History'  },
    { id: 'btn-settings', label: 'Settings' },
];

const failures = [];
const check = (ok, message) => { if (!ok) failures.push(message); };

(async () => {
    const browser = await puppeteer.launch({
        executablePath: CHROME,
        args: ['--no-sandbox', '--disable-dev-shm-usage'],
    });
    const page = await browser.newPage();
    await page.setViewport({ width: 1400, height: 900 });
    await page.goto(URL, { waitUntil: 'networkidle0' });

    const measured = await page.evaluate((buttons) => buttons.map(({ id, label }) => {
        const btn = document.getElementById(id);
        if (!btn) return { id, label, missing: true };
        const svg = btn.querySelector('svg');
        const b = btn.getBoundingClientRect();
        const s = svg ? svg.getBoundingClientRect() : null;
        return {
            id, label,
            btnW: +b.width.toFixed(1),  btnH: +b.height.toFixed(1),
            svgW: s ? +s.width.toFixed(1)  : null,
            svgH: s ? +s.height.toFixed(1) : null,
            flex: svg ? getComputedStyle(svg).flexGrow + '/' + getComputedStyle(svg).flexShrink : null,
        };
    }), ICON_BUTTONS);

    for (const m of measured) {
        if (m.missing) { check(false, `${m.label}: #${m.id} not found in the DOM`); continue; }
        if (m.svgW === null) { check(false, `${m.label}: button has no <svg>`); continue; }

        // 1. Square. This is the assertion that would have caught the 8x20 bug.
        check(Math.abs(m.svgW - m.svgH) <= 1,
            `${m.label}: glyph is ${m.svgW}x${m.svgH} — not square. Something is ` +
            `constraining the content box (check for a padding rule that outranks ` +
            `.fusion-bar .fusion-nav .glass-btn).`);

        // 2. Actually fills the button rather than sitting in it as a dot.
        const fill = m.svgH / m.btnH;
        check(fill >= 0.7,
            `${m.label}: glyph fills only ${(fill * 100).toFixed(0)}% of its ` +
            `${m.btnH}px button (want >= 70%).`);

        // 3. flex-shrink must be 0, so a future padding rule overflows visibly
        //    instead of silently squashing the glyph back into a sliver.
        check(m.flex === '0/0',
            `${m.label}: svg flex is ${m.flex} — needs flex: none so it can never ` +
            `be shrunk by the flex container.`);

        console.log(`  ${m.label.padEnd(9)} button ${m.btnW}x${m.btnH}  glyph ${m.svgW}x${m.svgH}  ` +
                    `fill ${(fill * 100).toFixed(0)}%  flex ${m.flex}`);
    }

    await browser.close();

    if (failures.length) {
        console.error('\nFAIL — UI geometry:');
        failures.forEach(f => console.error('  ✗ ' + f));
        process.exit(1);
    }
    console.log('\nOK — all icon buttons are square, filled and unshrinkable.');
})().catch(err => {
    console.error('ERROR: ' + err.message);
    process.exit(1);
});
