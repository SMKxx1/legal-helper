# Legal Helper — Word Add-in

A Word task-pane add-in that reviews the open document against the Legal Helper playbook via the
API and applies the redlines as **tracked changes + comments**.

No build step — plain HTML/CSS/JS + Office.js from the CDN. You only need to serve this folder over
HTTPS and sideload the manifest.

## What it does
1. **Sign in** with a username and password, and save your OpenRouter API key once (kept encrypted
   server-side; next sign-in it's already there).
2. **Pick a review depth** — a **Quick / Deep** selector sits above the review button. **Quick** is a
   single whole-document pass (~30s); **Deep** is a more thorough pass plus a coverage check for
   missing required clauses (a few minutes). The choice is saved as soon as you pick it.
3. **Review this document** — reads the open document as `.docx`, uploads it, and shows the risk
   tier, findings, missing required clauses, and cross-clause flags. **Quick** posts synchronously.
   **Deep** submits asynchronously and polls until done, with a live elapsed-time counter. Each
   finding renders an **inline word-level redline** of the suggested change — deletions struck in
   pastel red, insertions in pastel green. Expanding a finding briefly **flashes that clause in the
   document**.
4. **Apply redlines** (all, or per finding) — turns on Track Changes and applies each edit (find the
   verbatim span → replace with the suggested language → attach a margin comment). Only the differing
   words are struck/inserted, so the tracked change reads like a human redline.

## Configure (⚙ in the pane)
- **API base URL** — the backend's public origin; served same-origin in production, so it can
  usually stay empty in dev too (proxied by `dev-server.mjs`)
- **API key** — a legacy field kept for local testing; sign-in (Phase 1) is the normal path

All settings persist in the add-in's local storage (`lh.*` keys). Client request timeout is per-mode
(Quick 3 min, Deep 10 min).

## Serve + sideload (dev)
1. Serve this folder over HTTPS on port 3000 (the URLs in `manifest.dev.xml`):
   ```bash
   npx office-addin-dev-certs install   # one-time trusted localhost cert
   node dev-server.mjs                  # serves this folder + proxies /api,/healthz,/manifest.xml -> :8000
   ```
   (Add 32×32 / 80×80 PNGs under `assets/` as `icon-32.png` / `icon-80.png`.)
2. Sideload `manifest.dev.xml`:
   - **Windows:** share a folder as a trusted catalog (File ▸ Options ▸ Trust Center ▸
     Trusted Add-in Catalogs), or use `npx office-addin-debugging start manifest.dev.xml`.
   - **Mac:** copy `manifest.dev.xml` to `~/Library/Containers/com.microsoft.Word/Data/Documents/wef/`.
   - **Word on the web:** Insert ▸ Add-ins ▸ Upload My Add-in ▸ pick `manifest.dev.xml`.
3. In Word: **Home ▸ Legal Helper ▸ Review this document** opens the pane.

## Production
The add-in ships **same-origin** with the API — the backend serves this folder under `/addin/*` and
exposes `/api` on the same host, so there is no CORS setup needed and a bearer token reaches the API
unchanged. A later phase adds a dynamically-served `/manifest.xml` (host filled in from the request)
for production sideloading — see [`LEGAL_HELPER_PLAN.md`](../LEGAL_HELPER_PLAN.md).

## Tests
```bash
npm test    # node --test test/**/*.test.js — pure redline/diff + async-transport helpers, no Office.js
```
