# Amperesand NDA Review — Word Add-in

A Word task-pane add-in that reviews the open NDA against Amperesand's playbook via
the engine `/v1` API and applies the redlines as **tracked changes + comments**.

No build step — plain HTML/CSS/JS + Office.js from the CDN. You only need to serve
this folder over HTTPS and sideload the manifest.

## What it does
1. **Pick a review depth** → a **Quick / Deep** selector sits above the review
   button. **Quick** is a single whole-document pass on Sonnet (~30s); **Deep** is a
   whole-document pass on Opus plus a coverage check for missing required clauses (a
   few minutes, most thorough). The choice is saved as soon as you pick it.
2. **Review this NDA** → reads the open document as `.docx`, uploads it to
   `POST /v1/reviews` (tagged `source_channel=word`), and shows the risk tier,
   findings, missing required clauses, and cross-clause flags. **Quick** posts
   synchronously (the finished review comes straight back). **Deep** can run for
   minutes — longer than the platform's ingress request timeout — so it submits
   **asynchronously** (`POST /v1/reviews?async=1` → `202` + a job id) and **polls**
   `GET /v1/reviews/jobs/{id}` with backoff until the job is `done`, then renders the
   inlined review (a cache hit still returns the finished review immediately, no
   polling). A live elapsed-time counter shows while it runs. Each finding renders an
   **inline word-level redline** of the suggested change — deletions struck in pastel
   red, insertions in pastel green — computed client-side with the *same* token diff
   the apply path uses, so the preview matches the tracked changes you'll get.
   Expanding a finding briefly **flashes that clause in the document** so you can see
   where it applies.
3. **Apply redlines** (all, or per finding) → turns on Track Changes and applies
   each `redline_plan` edit (find the verbatim span → replace with the suggested
   language → attach a margin comment). Only the differing words are struck/inserted
   (not the whole clause), so the tracked change reads like a human redline. You
   review/accept the tracked changes.

## Configure (⚙ in the pane)
- **Engine API base URL** — the api app's public origin (dev: `https://localhost:8000`);
  served same-origin in production, so it can usually stay empty
- **API key** — sent as `X-API-Key` if the engine has `ENGINE_API_KEY` set

The review-depth selector lives on the main pane (not in ⚙). All settings
persist in the add-in's local storage. Client request timeout is per-mode
(Quick 3 min, Deep 10 min).

## Serve + sideload (dev)
1. Serve this folder over HTTPS on port 3000 (the URLs in `manifest.xml`). Easiest:
   ```bash
   npx office-addin-dev-certs install        # one-time trusted localhost cert
   npx http-server . -p 3000 -S -C ~/.office-addin-dev-certs/localhost.crt \
       -K ~/.office-addin-dev-certs/localhost.key
   ```
   (Add 32×32 / 80×80 PNGs under `assets/` as `icon-32.png` / `icon-80.png`, or
   edit the icon URLs out of the manifest.)
2. Sideload `manifest.xml`:
   - **Windows:** share a folder as a trusted catalog (File ▸ Options ▸ Trust Center ▸
     Trusted Add-in Catalogs), or use `npx office-addin-debugging start manifest.xml`.
   - **Mac:** copy `manifest.xml` to `~/Library/Containers/com.microsoft.Word/Data/Documents/wef/`.
   - **Word on the web:** Insert ▸ Add-ins ▸ Upload My Add-in ▸ pick `manifest.xml`.
3. In Word: **Home ▸ Amperesand ▸ NDA Review** opens the pane.

## Production (same-origin with the engine)
The add-in ships **same-origin** with the engine — the FastAPI **api** app serves this
folder under `/addin/*` and exposes `/v1` on the **same host** (`app.api.routes_addin`).
So there is **no CORS setup** and the `SameSite=Lax` session cookie works unchanged.

- **Origin / base URL.** The origin is the **api app's own public URL** (whatever the
  deployment exposes — e.g. an Azure Container Apps FQDN). The add-in resolves its API
  base to the origin it was served from, so `/v1` calls land on the same host. Substitute
  that FQDN for `__APP_DOMAIN__` in `manifest.cloud.xml` (see below) — this replaces the
  old Caddy/Railway domain; there is no separate proxy service anymore.
- **`/addin/config.js`** is **synthesized per request by the api app** from the
  `ENGINE_API_KEY` setting (`Cache-Control: no-store`), so the key lives in the
  deployment's env/secret store — never in git. When `ENGINE_API_KEY` is unset the
  synthesized config still parses with an empty `apiKey`, so the add-in loads in its
  not-configured state (it simply sends no `X-API-Key`) instead of breaking. The
  committed `config.js` stub is a no-op used only by the local `dev-server.mjs`.

To point the manifest at your domain and sideload it, follow
**[`SETUP.md`](./SETUP.md)** (manifest stamping, `ENGINE_API_KEY`, sideload/M365 rollout).
Do not edit the dev `manifest.xml` for production.
