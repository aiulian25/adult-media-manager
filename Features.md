# Features.md — Adult Media Manager

## App Summary (from the scan, not the README)
Adult Media Manager is a single-worker FastAPI backend (`docker-entrypoint.sh:69 --workers 1`) serving a vanilla-JS web UI, packaged four ways (Docker, deb, rpm, AppImage via Electron) that share one codebase and differ only by env/packaging. It detects scene metadata from filenames (`app/core/detector.py`), matches against TPDB/StashDB by text and by content fingerprint (`app/api/*.py`, `_match_one_*`), then renames files by a naming template (`app/core/formatter.py`) and embeds metadata via ffmpeg remux / mkvpropedit / AtomicParsley plus a Jellyfin NFO sidecar (`app/main.py:_run_embed_phase`, `write_nfo`). Persistence is a SQLite catalog for incremental rescans + duplicate detection (`app/core/catalog.py`) alongside atomic-write JSON stores for match cache, history, settings and learned aliases. The entire HTTP API is **anonymous and unrate-limited**, and in Docker it binds `0.0.0.0` — the single largest risk surface, and the reason security is prioritized below.

## Implementation Order (dependency-aware)
1. **F1 (embed-loop offload)** — ✅ DONE. Pure correctness, no dependencies; de-risks every future progress/streaming feature by proving the event loop stays free. Measurement during implementation showed it is NOT sufficient for the reported freeze, which promoted F6 to the top of the queue.
1b. **F6 (terminal-state reconciliation)** — NEXT, and now the prime suspect for the user-reported "stuck at 3rd file": the client stops polling on `complete` and can never repaint a leftover spinner. Depends on nothing; S-sized.
2. **F2 (auth + bind + rate-limit + security headers)** — highest security value; independent of F1; must land before any feature that widens the API. Do this second so the app is safe to expose while the rest is built.
3. **F3 (per-file embed audit trail in the catalog)** — ✅ DONE. Hooked F1's `_embed_one` choke point (and the manual-save path); additive `embed_log` table with bounded retention. Unblocks F4, which reads these columns.
4. **F4 (library export / signed shareable report)** — depends on F3's audit columns and on F2's auth primitive (the signed-link scheme reuses F2's secret).
5. **F5 (dependency pinning + reproducible build)** — ✅ DONE. Independent supply-chain hardening; froze the already-bundled known-good versions with hashes across all four wheel targets, so no runtime behavior changed.

---

### F1: Non-blocking embed pipeline (fixes real-time progress stall) — ✅ DONE (v1.12.10)
> **Implemented 2026-07-27.** All five steps applied: `_refresh_fingerprint_after_embed` offloads both
> `_match_cache_rekey` and `catalog.update_fingerprint`; `_embed_one` offloads `write_nfo` and
> `catalog.mark_organized` (via a kwargs-preserving local closure); `_job_progress` keeps the in-memory
> counter synchronous (the completion check depends on it) and fire-and-forgets only the sqlite mirror
> through `run_in_executor`, with a snapshot of the warnings list so the executor thread never iterates a
> list a coroutine may append to, and a `get_running_loop()` guard so the helper still works if ever called
> from a sync context. Client poll 2000 ms → 1000 ms, and `EMBED_POLL_FAST_LIMIT` 300 → 600 so the
> F13 10-minute fast window is preserved rather than silently halved.
>
> **Measured outcome (honest):** the formerly-blocking work costs **~42 ms per file** on local SSD with a
> 20 000-entry cache — `_match_cache_rekey` full-file JSON rewrite **39.8 ms**, `write_nfo` 1.8 ms,
> both sqlite writes ≈0.1 ms, `JobStore.progress` ≈0.0 ms. With the default `AMM_MATCH_CACHE_MAX=50000`
> and 3–4 concurrent embeds that is roughly 0.3–0.5 s of loop block per completion wave, and multiples of
> that on slow/NAS-backed `DATA_DIR`. So F1 removes real, measurable jitter and is correct on principle
> (blocking I/O on a `--workers 1` event loop), **but a before/after A-B test on local SSD showed no
> multi-second stall in either build** (worst poll latency 2 ms before, 3 ms after; `done` curve
> `[0,0,3,6,7]` in both). **F1 alone therefore does not explain the reported "stuck at the 3rd file" freeze
> — see F6 for the outstanding hypothesis and the diagnostic needed.**
>
> **Verification:** 7-file `embed`-mode batch against a real uvicorn server with a seeded 20 000-entry
> (5.58 MB) `match_cache.json` and a real per-file oshash so the rekey actually fires — 7/7 embedded, 7 NFOs
> written, `jobs.db` mirror `done=7 status=complete` (proves fire-and-forget persistence loses nothing).
> Regression suites re-run green: chunk-shared jobs (25 files/3 chunks), embed-queue per-file states,
> queue file sizes + manual-save entry, and ffmpeg progress accuracy (12 live ticks).
- **Evidence:** SCAN_NOTES #2 (whole section) and #54. `app/main.py:2831` `_refresh_fingerprint_after_embed` → `_match_cache_rekey` (`app/main.py:2752`) → `_match_cache_store.mutate` rewrites the whole `match_cache.json` synchronously on the event loop; `app/main.py:2754` `catalog.update_fingerprint`, `app/main.py:2846` `write_nfo`, `app/main.py:2859` `catalog.mark_organized`, and `_job_progress`→`JobStore.progress` (`app/core/jobs.py:95`) are all synchronous blocking I/O run directly on the loop inside `_embed_one`. `_embed_sem` default is 3 (`app/main.py:2676`). The scan path already offloads the identical write with `await asyncio.to_thread(catalog.upsert_scanned, …)` at `app/main.py:1251` — proving the pattern and the asymmetry.
- **What it does:** Makes the embed queue update every ~1s in real time and never freeze on the in-flight files. In the app's own terms: the "Embedding metadata — N of M" queue panel and its per-file underline progress bars keep advancing while ffmpeg remuxes to the NAS.
- **Current vs new:** Today, when 3 files finish embedding near-simultaneously, each coroutine performs a full-file `match_cache.json` rewrite + 2 sqlite writes + an NFO write on the single event loop (`app/main.py:2752-2870`); the `/api/embed-status` poller served by the same `--workers 1` loop can't respond, so the queue appears stuck at the 3rd file even though the files finished on disk (SCAN_NOTES #2). After this feature, all four blocking operations run in `asyncio.to_thread`, the loop stays free, and polls return immediately.
- **Effort:** S
- **Implementation steps:**
  1. In `app/main.py`, function `_refresh_fingerprint_after_embed` (starts L2733): wrap the two blocking calls — change `catalog.update_fingerprint(str(path), new_oshash, path.stat().st_size)` (L2754) to `await asyncio.to_thread(catalog.update_fingerprint, str(path), new_oshash, path.stat().st_size)`, and change the internal `_match_cache_rekey(old_oshash, new_oshash, delete_old=delete_old)` call (L2752) to `await asyncio.to_thread(_match_cache_rekey, old_oshash, new_oshash, delete_old=delete_old)` (make `_refresh_fingerprint_after_embed` remain `async`; it already is).
  2. In `_embed_one` (L2807): change `write_nfo(result.new_path, meta)` (L2846) to `await asyncio.to_thread(write_nfo, result.new_path, meta)`.
  3. In `_embed_one` (L2857-2868): wrap `catalog.mark_organized(...)` in `await asyncio.to_thread(catalog.mark_organized, ...)` (pass the same keyword args positionally-safe: define a small local `def _mark(): catalog.mark_organized(...)` then `await asyncio.to_thread(_mark)` to keep kwargs).
  4. In `_job_progress` (L203) the sqlite write is synchronous but tiny; leave the counter update in-loop (needed synchronously so `job["done"]` is correct for the completion check at L2890), but change `JobStore.progress` persistence to fire-and-forget: in `app/core/jobs.py:progress` wrap the `self._conn.execute(...)+commit()` body so callers can `asyncio.to_thread` it — OR simpler and sufficient: in `_job_progress`, after `job["done"] += 1`, replace `_job_store.progress(...)` with `asyncio.get_running_loop().run_in_executor(None, _job_store.progress, job_id, job["done"], list(job["warnings"]))`. Keep it best-effort (JobStore already swallows errors).
  5. Reduce client repaint latency: in `app/static/js/rename.js:454` change `let EMBED_POLL_INTERVAL_MS = 2000;` to `1000`. (Real-time enough without hammering; loop is now free.)
  - **Test after each step:** `cd /home/iulian/projects/pron/adult-media-manager && bundled-python/bin/python3 -m py_compile app/main.py app/core/jobs.py && node --check app/static/js/rename.js`
  - **Integration test (write a scratch test mirroring the existing pattern in prior sessions):** start the bundled server (`DATA_DIR=<scratch> AMM_NATIVE=1 PYTHONPATH=bundled-packages:. bundled-python/bin/python3 -m uvicorn app.main:app --port 8790`), POST a 7-file `/api/rename` batch with `embed_mode":"nfo_only"`, then poll `/api/embed-status/{job_id}` every 500ms and assert `done` increases monotonically to 7 with no >2s gap between increments.
- **i18n:** none — no new strings.
- **Packaging:** none — pure application code; identical on Docker/deb/rpm/AppImage.
- **Acceptance criteria:**
  - `grep -n "await asyncio.to_thread(write_nfo" app/main.py` returns the L2846 site.
  - During a 7-file `nfo_only` batch, consecutive `/api/embed-status` polls at 500ms never return the same `done` for more than ~1.5s while work remains (proves loop is not blocked).
  - `bundled-python/bin/python3 -m py_compile app/main.py` exits 0.

---

### F2: Optional API authentication + safe bind + rate limiting + security headers
- **Evidence:** SCAN_NOTES #32 (no `Depends`/token/login anywhere in `app/main.py`), #33 (`Dockerfile:18`, `docker-compose.yml:15`, `docker-entrypoint.sh:70` bind `0.0.0.0`), #34 (`app/main.py:1548` `p.unlink()` in the anonymous `resolve_duplicates`), #35 (anonymous `rename_files`), #36 (no rate limiting; `app/main.py:2109` fans out 5 upstream calls), #44 (no CSP/security-header middleware — only the Cache-Control one at `app/main.py:106`), #37 (`app/main.py:240` keys stored plaintext).
- **What it does:** Adds an OPT-IN shared-token gate so a LAN-exposed Docker instance is no longer an anonymous "delete/move my files" API, plus a per-IP rate limit and baseline security headers. Off by default so existing localhost/desktop users are unaffected; on when `AMM_AUTH_TOKEN` is set.
- **Current vs new:** Today, when any device on the network opens `http://<host>:8887`, it can call `/api/catalog/resolve-duplicates` and permanently `unlink()` files (`app/main.py:1548`) with no credential (SCAN_NOTES #34). After this feature, when `AMM_AUTH_TOKEN` is set every `/api/*` request must carry `Authorization: Bearer <token>` (or a `amm_token` cookie set once via a tiny login POST); missing/wrong token → 401; the static UI shell still loads so the user can enter the token; and all responses carry `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`.
- **Effort:** M
- **Implementation steps:**
  1. `app/main.py` after `APP_VERSION` (L87): add `_AUTH_TOKEN = os.getenv("AMM_AUTH_TOKEN", "").strip()` and a constant `_AUTH_COOKIE = "amm_token"`.
  2. Add an HTTP middleware right after the existing `_no_stale_ui_cache` (L106) named `_auth_and_headers`. Logic, in order: (a) always set `response.headers["X-Content-Type-Options"]="nosniff"`, `["X-Frame-Options"]="DENY"`, `["Referrer-Policy"]="no-referrer"`; (b) if `_AUTH_TOKEN` is empty → behave exactly as today (return response); (c) allow unauthenticated GET of `/`, `/static/*`, `/api/health`, and the app icon route so the login screen renders; (d) for every other path, read `request.headers.get("authorization")` (`Bearer <t>`) or `request.cookies.get(_AUTH_COOKIE)`, compare with `hmac.compare_digest` against `_AUTH_TOKEN`; on mismatch return `Response(status_code=401, content='{"detail":"auth required"}', media_type="application/json")` BEFORE calling `call_next`. Import `hmac` at top (add to the L6-16 import block).
  3. Add `POST /api/auth` accepting `{"token": str}`; on `hmac.compare_digest` match, return `Response` setting cookie `_AUTH_COOKIE` with `HttpOnly=True, SameSite="Strict", Secure=False` (LAN HTTP) and a 30-day max-age; on mismatch 401. Rate-limit this route specifically (see step 5).
  4. Rate limiting (no new dependency — use a tiny in-process token bucket, matching the codebase's "stdlib only" ethos, SCAN_NOTES rationale for catalog.py): add `_RATE = {}` dict + `_rate_allow(ip: str, limit=int(os.getenv("AMM_RATE_LIMIT","120")), window=60)` that returns False when an IP exceeds `limit` requests/`window`s (monotonic-based sliding count, evict on read). In `_auth_and_headers`, for `/api/*` paths call `_rate_allow(request.client.host)`; on False return `Response(status_code=429, ...)`. `AMM_RATE_LIMIT=0` disables.
  5. Encrypt keys at rest is OUT OF SCOPE here (documented follow-up) — but add a one-line WARNING log at startup if `_AUTH_TOKEN` is empty AND `AMM_HOST` is `0.0.0.0` AND not native: `print("SECURITY: API is anonymous and bound to 0.0.0.0 — set AMM_AUTH_TOKEN to require a token.")` near the client init (after L387).
  6. Frontend: in `app/static/js/core.js`, add a `_fetchAuthed` wrapper is NOT required (cookie rides automatically); instead add a 401 handler — in the app's central fetch error path, on `resp.status===401` show a small token-entry modal that POSTs `/api/auth` then reloads. Add modal markup to `app/static/index.html` near the settings modal; wire in `core.js` init.
  7. **Config propagation (per the repo's env/compose-sync rule):** add `AMM_AUTH_TOKEN` and `AMM_RATE_LIMIT` (commented, with guidance) to `docker-compose.yml`, `docker-compose.dev.yml`, `.env.example`, `.env`, and the README env table.
  - **Test after step 2/3:** `bundled-python/bin/python3 -m py_compile app/main.py`; then `AMM_AUTH_TOKEN=secret … uvicorn` and `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:PORT/api/catalog/stats` → expect `401`; with `-H "Authorization: Bearer secret"` → expect `200`; `GET /api/health` with no token → `200`.
  - **Test after step 4:** loop 200 rapid `curl` calls; expect some `429`.
- **i18n:** add to ALL of `app/static/locales/{en,de,es,fr,ja,pt}.json`: `auth.title` ("Sign in"), `auth.prompt` ("This instance requires an access token."), `auth.token_label` ("Access token"), `auth.submit` ("Unlock"), `auth.failed` ("Wrong token — try again."), `auth.locked_out` ("Too many attempts — wait a minute."). English values given; translate the rest to match the existing tone in each file.
- **Packaging:** Docker — document `AMM_AUTH_TOKEN` in compose (highest-value target since it binds 0.0.0.0). deb/rpm/AppImage — native mode is localhost single-user, so auth stays optional; no packaging change, only env doc. No format-specific code.
- **Acceptance criteria:**
  - With `AMM_AUTH_TOKEN` unset: `curl /api/catalog/stats` → `200` (backward compatible).
  - With it set: `/api/catalog/stats` no header → `401`; with correct Bearer → `200`; `/api/health` unauth → `200`; `POST /api/auth` correct token → `Set-Cookie: amm_token`.
  - Every response includes `X-Content-Type-Options: nosniff` (`curl -I`).
  - `grep -c AMM_AUTH_TOKEN docker-compose.yml docker-compose.dev.yml .env.example README.md` ≥ 1 each.

---

### F3: Per-file embed audit trail (persisted, not just in-memory) — ✅ DONE (v1.12.10)
> **Implemented 2026-07-27.** All five steps applied, plus three deliberate additions the spec omitted:
>
> 1. **Retention (spec gap).** The spec's table was unbounded, while every other store in AMM is capped
>    (`AMM_HISTORY_MAX`, `AMM_MATCH_CACHE_MAX`). Added `_EMBED_LOG_MAX = 20000` with *amortised* pruning —
>    the oldest rows beyond the cap are trimmed once every `_EMBED_LOG_PRUNE_EVERY = 500` inserts (by
>    `rowid`, monotonic for an append-only table), so the common path stays a single INSERT. `0` = unlimited.
> 2. **Manual-save path also logs.** The spec hooked only `_embed_one`; `_run_manual_embed_job` embeds too,
>    so leaving it out would have made "did this file get remuxed?" blind for every file organised through
>    Manual Edit — exactly the asymmetry class SCAN_NOTES #54–58 flagged. Both paths now write the same trail.
> 3. **Defensive `detail` cap (`_EMBED_LOG_DETAIL_MAX = 500`)** in the writer itself: a durable log must not
>    become where an unbounded ffmpeg stderr lands, even though the caller already truncates to 200.
>
> Also: `elapsed_s` measures the *whole* per-file embed including semaphore wait (real user-visible latency);
> `embed_log_for()` matches the path as stored **and** its resolved form so a symlinked/non-normalised
> variant still finds its history; the read endpoint offloads the query via `asyncio.to_thread` (F1
> discipline) and clamps `limit` to 1–200; and one `Embeds logged` tile was added to the Library modal's
> existing "Data on disk" row (`catalog.embed_log_stats()` → `_store_stats()` → generic `tile()` helper), so
> the trail is discoverable instead of an invisible endpoint.
>
> **Verification (all acceptance criteria met):** 3-file `nfo_only` batch → exactly 3 rows; every row
> `elapsed_s` a positive float, `status ∈ {done,warning,error}`, `mode` correct, `bytes` = real size;
> `EXPLAIN QUERY PLAN` confirms `SEARCH embed_log USING INDEX idx_embed_log_path` (no full scan). Endpoint:
> known path → entries, unknown-but-allowed path → `200` with `[]`, `limit` 0/999 → `422`.
> **Security (run with `AMM_NATIVE` unset, i.e. Docker allowed-roots semantics):** `403` for `/etc/passwd`,
> `/proc/self/environ`, `/var/lib/secret`, `/usr/bin/ffmpeg`, and the traversal `/media/../etc/shadow`
> (proves resolution happens before the check). Warning/error rows persist; a 5000-char detail is stored at
> 500 chars. Retention: 200 inserts against a cap of 50 → 52 rows, newest kept, oldest pruned. A disabled
> catalog (unwritable path) makes `log_embed`/`embed_log_for`/`embed_log_stats` safe no-ops.
> Manual Edit save with a real remux → 1 row, `mode=embed status=done elapsed=0.579s`.
> Library tile renders (`EMBEDS LOGGED · 3`). Regressions re-run green: F1 loop responsiveness, chunk-shared
> jobs, queue states, queue sizes, ffmpeg progress accuracy, natural sort, clear-metadata. All six locales
> hold identical 436-key sets.
- **Evidence:** SCAN_NOTES #7 (data model — duration/quality/tags/mode/timing NOT persisted per file), the job dict is memory-only with `EMBED_JOB_TTL=600` (`app/main.py:181`, #46), and the embed elapsed time is computed but discarded after the TTL (`_job_finish` L234 sets `finished` in-memory only). Catalog schema `app/core/catalog.py:104` has NO embed-outcome columns. F1 makes `_embed_one` the single hook where each file's final state (`done|warning|error`, `detail`, `size`, elapsed) is already known (`app/main.py:2873`).
- **What it does:** Records, per file, the outcome of every embed the app performs — mode used, success/warning/error + the exact ffmpeg/NFO reason, bytes, and how long it took — so the Library modal can show "last organized" history and a user can answer "did this file actually get remuxed, and when?" after the 10-minute job TTL expires.
- **Current vs new:** Today, once a job passes `EMBED_JOB_TTL` (600s, `app/main.py:181`) the per-file states and elapsed time vanish (memory-only job dict, SCAN_NOTES #7); the only durable trace is the NFO on disk and a coarse `organized` boolean in the catalog (`catalog.py:104`). After this feature, each embed writes one row to a new `embed_log` table with `path, mode, status, detail, bytes, elapsed_s, at`, queryable via `/api/catalog/embed-log?path=…`.
- **Effort:** M
- **Implementation steps:**
  1. `app/core/catalog.py:_init_schema` (L100): add `CREATE TABLE IF NOT EXISTS embed_log (path TEXT, mode TEXT, status TEXT, detail TEXT, bytes INTEGER, elapsed_s REAL, at REAL)` + index on `path`.
  2. Add method `Catalog.log_embed(self, path, mode, status, detail, bytes_, elapsed_s)` following the existing best-effort/locked pattern (mirror `mark_organized`): `with self._lock: self._conn.execute("INSERT INTO embed_log …"); self._conn.commit()`. Best-effort (swallow+log), like every other method.
  3. `app/main.py:_run_embed_phase._embed_one` (L2807): capture `_t0 = _time.monotonic()` at entry; after the final status is set (L2873), call `await asyncio.to_thread(catalog.log_embed, str(result.new_path), embed_mode, entry["status"], entry.get("detail"), entry.get("size"), _time.monotonic()-_t0)` (uses F1's to_thread offload — DEPENDS ON F1).
  4. Add `GET /api/catalog/embed-log` (Query `path: str`) → `{"entries": catalog.embed_log_for(path)}` where `embed_log_for` selects the last 20 rows for that resolved path, newest first. Validate `path` with `_is_allowed_path` (403 otherwise) — reuse the existing guard.
  5. Frontend (Library modal, `app/static/js/browse.js` renders `#library-stats`): add a small "Recent embeds" line per store tile is optional; minimal viable is the endpoint + a tooltip. Keep UI change scoped to reading the endpoint on demand.
  - **Test after step 1-3:** `bundled-python/bin/python3 -m py_compile app/main.py app/core/catalog.py`; run a 3-file `nfo_only` rename, then `sqlite3 <DATA_DIR>/catalog.db "SELECT path,status,elapsed_s FROM embed_log"` → 3 rows.
  - **Test after step 4:** `curl "/api/catalog/embed-log?path=<one file>"` → JSON with ≥1 entry; `curl "…?path=/etc/passwd"` → `403`.
- **i18n:** add `library.recent_embeds` ("Recent embeds"), `library.embed_never` ("Not embedded yet") to all six locale files if the UI line is added; none if endpoint-only.
- **Packaging:** none — SQLite is stdlib, schema is additive (`CREATE TABLE IF NOT EXISTS`), identical on all targets; no migration needed (new table).
- **Acceptance criteria:**
  - After a rename+embed, `SELECT COUNT(*) FROM embed_log` equals the number of embedded files.
  - `elapsed_s` is a positive float; `status` ∈ {done,warning,error}.
  - `/api/catalog/embed-log?path=<not-allowed>` returns 403.

---

### F4: Library export + signed, expiring shareable report link
- **Evidence:** SCAN_NOTES #7 ("No structured export (CSV/JSON) of the library ever produced"), #23 (`_store_stats` L1368 already aggregates counts but nothing is exportable), F3 adds per-file embed outcomes. The user brief explicitly requires: "For shareable report links, use signed, expiring access and recommend revocation." F2 introduces the `AMM_AUTH_TOKEN` secret this signing reuses.
- **What it does:** Produces a read-only library report (catalog rows joined with embed_log: path, matched scene id/source, confidence, organized, last embed status+time) as downloadable CSV/JSON, and can mint a **signed, time-expiring** URL for that report so a user can share "here's what my organizer did this week" without exposing the whole app. Links are HMAC-signed with the app secret, carry an `exp` timestamp, and are individually revocable.
- **Current vs new:** Today there is no way to get the library out of the app except reading `catalog.db` by hand; `_store_stats` (L1368) returns only aggregate counts to the Library modal. After this feature, `GET /api/report.csv` (auth-gated by F2) streams the report, and `POST /api/report/link` returns `/r/<token>` where `<token>` = base64url(`exp` + HMAC-SHA256(`exp|nonce`, secret)); `GET /r/<token>` serves the report read-only until `exp`, and `DELETE /api/report/link/<nonce>` revokes it (nonce added to an in-memory+jsonstore denylist).
- **Effort:** L
- **Implementation steps:**
  1. `app/core/catalog.py`: add `report_rows()` returning `SELECT path, canonical_scene_id, source_system, confidence_score, user_confirmed, organized, matched_at FROM files ORDER BY updated_at DESC` LEFT JOIN latest `embed_log` per path (subquery). Best-effort, returns `[]` on failure.
  2. `app/main.py`: add `GET /api/report.csv` and `GET /api/report.json` — auth-gated by F2 middleware automatically. CSV via `csv.writer` into a `StringIO`, returned as `Response(media_type="text/csv", headers={"Content-Disposition":"attachment; filename=amm-report.csv"})`. NO path data leaves beyond what the authed user already sees.
  3. Signing: add `_report_secret()` = `AMM_AUTH_TOKEN` if set else a per-install random secret persisted once to `DATA_DIR/report_secret` (0600). Add `_sign_report(exp:int, nonce:str)->str` = `base64url(f"{exp}.{nonce}." + hmac.new(secret, f"{exp}.{nonce}".encode(), sha256).hexdigest())`. Refuse to mint links when neither `AMM_AUTH_TOKEN` nor a persisted secret exists — never sign with an empty key.
  4. `POST /api/report/link` (auth-gated) body `{"ttl_hours": int (1-168)}` → generate `nonce=uuid4().hex`, `exp=int(time)+ttl*3600`, store nonce in `_report_links_store` (`_JsonStore`, dict nonce→exp), return `{"url": f"/r/{_sign_report(exp,nonce)}", "exp": exp, "nonce": nonce}`.
  5. `GET /r/{token}` (NOT auth-gated — the signature IS the credential; add `/r/` to the F2 middleware allowlist): parse token, `hmac.compare_digest` the signature, reject if `exp < now` (410 Gone), reject if `nonce` not in store or on the revoke denylist (403), else stream the CSV read-only. Rate-limit `/r/*` via F2's limiter.
  6. `DELETE /api/report/link/{nonce}` (auth-gated) → remove nonce from the store (revocation). `GET /api/report/link` lists active links with exp.
  7. Frontend: in the Library modal add an "Export / Share" section: two download buttons (CSV/JSON) and a "Create share link (expires in [24h ▾])" control that shows the URL + a Revoke button per active link.
  - **Test after step 2:** `curl -H "Authorization: Bearer <t>" /api/report.csv` → CSV with a header row.
  - **Test after step 5:** mint a link with `ttl_hours:1`, `curl /r/<token>` → `200` CSV; tamper one char → `403`; set system-independent expiry test by minting with a patched `exp` in the past → `410`.
  - **Test after step 6:** revoke the nonce, re-`curl /r/<token>` → `403`.
- **i18n:** add to all six locales: `report.export` ("Export library"), `report.csv` ("Download CSV"), `report.json` ("Download JSON"), `report.share` ("Create share link"), `report.ttl` ("Link expires in"), `report.revoke` ("Revoke"), `report.copied` ("Link copied"), `report.no_secret` ("Set AMM_AUTH_TOKEN to enable share links."). English given; match each file's tone.
- **Packaging:** none — stdlib `csv`/`hmac`/`hashlib`; `report_secret` lives in `DATA_DIR` (same perms as settings.json). Identical on all four targets.
- **Acceptance criteria:**
  - `GET /r/<valid token>` returns CSV until `exp`; after `exp` → 410; tampered token → 403; revoked nonce → 403.
  - Minting a link when no secret exists returns a clear error (never signs with empty key): `curl` → 409 with `report.no_secret`.
  - Report CSV row count equals `SELECT COUNT(*) FROM files`.
  - Documentation in README recommends revoking links and treating them as bearer credentials.

---

### F5: Pinned dependencies + reproducible-build lockfile — ✅ DONE (v1.12.10)
> **Implemented 2026-07-27.** `requirements.lock` (22 packages = the complete transitive closure,
> 540 sha256 hashes) is now the authoritative install for **both** build paths:
> `Dockerfile` → `pip install --no-cache-dir --require-hashes -r requirements.lock`, and both
> `prepare-build.sh` legs (native cp312 and the cross-staged aarch64 one) → same file, same flag.
> `requirements.txt` keeps only the four direct deps as exact `==` pins and is now documentation.
>
> **The cross-build trap the spec did not mention, and how it was closed:** AMM has **four** wheel
> targets — Docker cp311 x86_64 *and* aarch64 (the release image is multi-arch) plus desktop cp312
> x86_64 *and* aarch64. A lock hashed from one machine would have broken the next arm64 release. I
> resolved the closure independently for all four via `pip install --dry-run --report` with explicit
> `--platform`/`--python-version`, and **all four produced an identical 22-package set at identical
> versions** — so one lock genuinely serves every target. Hashes include *every* file published for
> each pinned version (pip-compile `--generate-hashes` semantics), so whichever wheel pip selects per
> platform is authorised while the **version** stays frozen — deliberately chosen over filtering to
> current platform tags, which would break on a `manylinux2014_*` vs `manylinux_2_17_*` filename change.
>
> **Zero runtime risk — the key de-risking fact:** the pinned versions are *exactly* what
> `bundled-packages/` already contained and what every test this session ran against
> (fastapi 0.140.7, starlette 1.3.1, pydantic 2.13.4, uvicorn 0.51.0, httpx 0.28.1). F5 freezes the
> current known-good set; it does **not** bump anything.
>
> Two additions beyond the spec: `prepare-build.sh`'s package cache key now hashes
> **`requirements.lock`** (previously `requirements.txt`) — otherwise a lock edit would not invalidate
> `bundled-packages/` and the desktop bundle would silently keep stale wheels; and the lock carries an
> in-file bump procedure so the next maintainer does not have to find this document.
>
> **Verification (all acceptance criteria met):**
> `grep -c ">=" requirements.txt` → **0**. Two consecutive `docker build` runs → identical
> `pip freeze` sha256 (`c3d00788…` both; throwaway tags, removed afterwards).
> `--require-hashes` dry-run passes on all four targets; **real** installs pass in `python:3.11-slim`
> for **amd64 and arm64** (emulated) with `fastapi/uvloop/pydantic_core/watchfiles` importing from
> native aarch64 wheels, and natively into `--target` with the bundled cp312 python (22 dists, compiled
> `.so` present → wheels, no sdist builds). **Enforcement proven:** tampering fastapi's hashes yields
> `ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE`.
> The app boots and serves on the lock-installed tree alone (`/api/health`, `/api/version`,
> `/api/templates`, F3's endpoint). Regressions green: F1, F3, F3-security, chunk jobs, queue states.
> `bash -n prepare-build.sh`, `docker build --check`, py_compile all clean.
- **Evidence:** SCAN_NOTES #43 — `requirements.txt` uses `>=` for fastapi/uvicorn/httpx/pydantic with no lockfile; `Dockerfile:43` does `pip install --no-cache-dir -r requirements.txt` (unpinned → non-reproducible image, silent transitive drift on every rebuild). The desktop bundle (`prepare-build.sh`) pins python-build-standalone by exact version/sha but the pip install of these deps is not pinned.
- **What it does:** Freezes the exact dependency versions so a rebuilt image/bundle contains byte-identical dependencies, closing the "a bad upstream release silently ships in the next `build and push`" gap — the same reproducibility discipline the app already applies to its bundled ffmpeg (sha256-pinned) and python runtime.
- **Current vs new:** Today, `docker buildx build` re-resolves `fastapi>=0.100.0` etc. on every release (`requirements.txt`, `Dockerfile:43`), so two builds days apart can embed different dependency trees with no record. After this feature, `requirements.txt` carries exact `==` pins with hashes (or a `requirements.lock`), and CI installs with `--require-hashes`, so a build is reproducible and a compromised upstream version cannot enter silently.
- **Effort:** S
- **Implementation steps:**
  1. In a clean venv matching the image base (`python:3.11-slim`), run `pip install fastapi uvicorn[standard] httpx pydantic` then `pip freeze` to capture the exact resolved versions.
  2. Rewrite `requirements.txt` with `==` pins for the direct deps; add a `requirements.lock` (full transitive tree with `--hash=sha256:…` per line, produced by `pip-compile --generate-hashes` or `pip freeze` + `pip hash`).
  3. `Dockerfile:43`: change to `RUN pip install --no-cache-dir --require-hashes -r requirements.lock`.
  4. `prepare-build.sh` (the deb/AppImage bundled-packages step): install from the same `requirements.lock` so desktop and Docker share one dependency set (SCAN_NOTES #58 asymmetry — keep them identical).
  5. Add a CHANGELOG/README note that dependency bumps are now deliberate (edit the lock, rebuild).
  - **Test:** `docker build -t amm-test .` twice; `docker run --rm amm-test pip freeze | sha256sum` identical across builds.
  - **Test:** `pip install --require-hashes -r requirements.lock` in a fresh venv exits 0.
- **i18n:** none.
- **Packaging:** Docker — `Dockerfile` install line changes. deb/rpm/AppImage — `prepare-build.sh` pip step changes to the lock. No runtime behavior change on any target.
- **Acceptance criteria:**
  - `requirements.txt` contains no `>=` (only `==`): `grep -c ">=" requirements.txt` → 0.
  - Two consecutive `docker build` runs yield identical `pip freeze` output.
  - `pip install --require-hashes -r requirements.lock` succeeds; altering one hash makes it fail (proves enforcement).

---

### F6: Terminal-state reconciliation for the embed queue (the residual "stuck at 3rd file" freeze)
- **Evidence:** F1's A-B measurement above (blocking writes cost ~42 ms/file, not seconds) plus these code facts:
  `app/static/js/rename.js` `_finishEmbedPolling()` STOPS polling the moment a response has
  `job.complete === true`, and it is `_finishEmbedPolling` that calls `_showUnmatchedPanel()` — the very
  "N unmatched file(s) — action required" panel the user reported seeing *while three rows still showed
  spinners*. Because polling has ended, the queue panel keeps whatever the LAST response said forever:
  any `entry["status"]` still `"embedding"` at that instant is frozen on screen permanently, with no
  further poll to correct it. Completion is decided server-side by `job["done"] >= job["total"]`
  (`app/main.py` end of `_run_embed_phase`) while per-file `entry["status"]` is mutated independently in
  `_embed_one` — two separate bookkeeping channels that are never reconciled against each other.
- **What it does:** Guarantees the queue panel can never end a job showing a spinner: on the final poll the
  client resolves any non-terminal row, and the server marks any straggler entry terminal when it finishes
  a job, so "complete" always means every row reads ✓/⚠/✕.
- **Current vs new:** Today, if a job reports `complete` while any entry is still `pending`/`embedding`,
  the client stops polling (`_finishEmbedPolling`) and those rows spin forever even though the files
  finished on disk — matching the report exactly ("stuck at the 3rd embedded file even if writing has
  finished"). After this feature, `_job_finish()` sweeps `job["files"]` and promotes any non-terminal entry
  to `done` (or `warning` with a "state unknown" detail), and `_finishEmbedPolling` performs one final
  render that clears leftover spinners.
- **Effort:** S
- **Implementation steps:**
  1. `app/main.py` `_job_finish()`: before `_job_store.finish(...)`, add a sweep —
     `for e in job.get("files", []): if e.get("status") in ("pending", "embedding"): e["status"] = "done"; e.setdefault("detail", None)`.
     Guard with `if job is not None`. This is the authoritative fix (server owns the truth).
  2. `app/static/js/rename.js` `_finishEmbedPolling(warnings)`: before dismissing the banner, if a
     `job` object from the last poll is available, call `_renderEmbedQueue({...job, complete: true})` once
     more so the panel repaints from the swept states.
  3. Add a one-line guard in `_renderEmbedQueue`: when `job.complete === true`, treat any row whose status
     is `embedding`/`pending` as `done` for rendering purposes (defensive — covers a resumed job whose
     per-file states were never in memory).
  4. **Diagnostic to confirm the hypothesis on the user's NAS before/after:** add a temporary
     `print(f"[embed] finish job={job_id} done={job['done']}/{job['total']} states={[e['status'] for e in job.get('files',[])]}")`
     in `_job_finish`, reproduce the batch on the real NAS, and read `docker compose logs`. If any state is
     `embedding` at finish time, F6 is confirmed as the true cause of the reported freeze.
  - **Test:** unit — build a job dict with `total=3, done=3` and one entry left `"embedding"`, call
    `_job_finish`, assert every entry status is terminal. Integration — poll a 7-file batch and assert the
    LAST response before `complete` has zero `embedding` entries.
- **i18n:** optional `embed.queue_st_unknown` ("state unknown") in all six locale files only if step 1 uses
  `warning` instead of `done` for swept entries; not needed for the `done` variant.
- **Packaging:** none — pure application code, identical on Docker/deb/rpm/AppImage.
- **Acceptance criteria:**
  - `_job_finish` leaves no `pending`/`embedding` entry: unit assert passes.
  - After a completed batch, `curl /api/embed-status/<id> | jq '[.files[].status] | unique'` contains no
    `"embedding"`.
  - The queue panel's auto-collapse ("✓ N embedded") is reached in the UI with no spinner visible.

## Self-audit (Phase 4)
Each feature was checked against the four gates; all pass:
- **F1** — real evidence (`app/main.py:2752/2754/2846/2859`, `jobs.py:95`, asymmetry `main.py:1251`); uses real names (`_embed_one`, `_refresh_fingerprint_after_embed`, `EMBED_POLL_INTERVAL_MS`); README-litmus: it's the reported bug; steps name exact lines. KEEP.
- **F2** — evidence of the absence of auth (grep), the `0.0.0.0` bind (three files), the anonymous `unlink` (`main.py:1548`); uses real middleware pattern (`_no_stale_ui_cache` L106) and the repo's env/compose-sync rule; steps executable. KEEP.
- **F3** — ✅ DONE. Evidence: memory-only job TTL (`main.py:181`), catalog schema without embed columns (`catalog.py:104`), F1's hook point; additive `CREATE TABLE IF NOT EXISTS`, mirrors `mark_organized`. Implementation added retention + manual-path parity + a detail cap that the spec had missed. KEEP.
- **F4** — evidence: no export exists (SCAN_NOTES #7), `_store_stats` aggregates only (L1368); satisfies the brief's signed/expiring/revocable requirement using stdlib hmac + the F2 secret; steps concrete. KEEP.
- **F5** — ✅ DONE. Evidence: `requirements.txt` `>=` + `Dockerfile:43`; mirrors the app's existing sha-pinned ffmpeg discipline. Implementation additionally closed the four-wheel-target hash-coverage gap and repointed prepare-build.sh's cache key at the lock — both omitted by the spec. KEEP.
- **F6** — evidence is F1's own A-B measurement plus the `_finishEmbedPolling`/`_showUnmatchedPanel` coupling that matches the reported symptom precisely; real function names; steps executable; includes the diagnostic that would CONFIRM the hypothesis before shipping a guess. KEEP.
- Rejected during audit (no strong evidence / would fail README-litmus): "AI auto-tagging", "cloud sync", "mobile app" — none cite code and none fit the local-first, single-worker architecture.
