# Word Add-in — Setup Guide (sideload)

This assumes the app is **already deployed and running** — the `nda-api` app's public
origin serves the Word add-in at `/addin/` and the engine `/v1` API on the **same
origin** (`app/api/routes_addin.py`; there is no separate proxy). All that's left is to
point the manifest at your domain, give the add-in its API key, and sideload it into Word.

```
Word (desktop / web)
   │  loads taskpane.html + calls /v1   (same origin, X-API-Key)
   ▼
https://<APP_DOMAIN>          (the nda-api public FQDN)
   ├── /addin/*   →  Word add-in static files
   └── /v1/*      →  engine API
```

Throughout, `APP_DOMAIN` = the api app's public FQDN. Get it with:

```bash
APP_DOMAIN=$(az containerapp show -n nda-api -g <rg> \
  --query properties.configuration.ingress.fqdn -o tsv)
```

---

## Step 1 — Stamp your domain into the manifest

`manifest.cloud.xml` ships with `__APP_DOMAIN__` placeholders. Generate the real
manifest you'll sideload (leave the template untouched so it stays reusable):

```bash
cd word-addin

sed "s/__APP_DOMAIN__/${APP_DOMAIN}/g" manifest.cloud.xml > manifest.prod.xml
```

`manifest.prod.xml` now points the taskpane, icons, and AppDomain at
`https://<APP_DOMAIN>/addin/...`. (`manifest.azure.xml` in the repo is a stamped
example for the dev environment — regenerate your own rather than reusing it.)

> Keep the `<Id>` GUID stable across updates — it's the add-in's identity. Only bump
> `<Version>` when you re-publish.

## Step 2 — Give the add-in its API key

The add-in reads its key from `/addin/config.js`, which the **api app synthesizes per
request** from its `ENGINE_API_KEY` setting (Key Vault secret `engine-api-key`) — the
secret lives in the deployment env, never in git or the image. The committed
`word-addin/config.js` is a no-op stub used only by the local dev server and must NOT
be edited to hold a real key.

So all you do is make sure `ENGINE_API_KEY` is set on the **api** app. Verify it is
served:

```bash
curl -s https://${APP_DOMAIN}/addin/config.js
# -> window.AMP_CONFIG = { apiBase: "", apiKey: "<your key>" };
```

If that returns `apiKey: ""`, the `ENGINE_API_KEY` secret ref is missing on the api
app — seed the Key Vault secret, wire the ref, and roll a revision (`docs/AZURE.md §3.2`).

Same-origin hosting means **no CORS setup is needed** — the pane and `/v1` share one
origin.

## Step 3 — Sideload the manifest into Word

Pick your platform:

**Word on the web** (simplest):
1. Open a doc at office.com → **Word**.
2. **Home ▸ Add-ins ▸ More Add-ins ▸ My Add-ins ▸ Upload My Add-in**.
3. Select `manifest.prod.xml`.

**Word on Mac:**
```bash
cp manifest.prod.xml \
  ~/Library/Containers/com.microsoft.Word/Data/Documents/wef/
```
Create the `wef` folder if missing, then restart Word.

**Word on Windows:** File ▸ Options ▸ Trust Center ▸ Trust Center Settings ▸
**Trusted Add-in Catalogs** → add a shared folder's UNC path → tick *Show in Menu* →
restart Word → **Insert ▸ My Add-ins ▸ Shared Folder ▸ Amperesand NDA Review**.

**Org-wide rollout (recommended):** Microsoft 365 admin center ▸ **Settings ▸
Integrated apps ▸ Upload custom apps** → upload `manifest.prod.xml` → assign to
users. No per-machine sideloading.

## Step 4 — Use it

1. In Word: **Home ▸ Amperesand ▸ NDA Review** → the task pane opens.
2. Open an NDA, choose **Quick** or **Deep**, click **Review this NDA**.
3. The pane calls `POST https://<APP_DOMAIN>/v1/reviews` with `X-API-Key`, shows the
   risk tier + findings, and **Apply redlines** writes tracked changes + comments.

Quick sanity checks:
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://${APP_DOMAIN}/addin/taskpane.html   # expect 200
curl -s -o /dev/null -w "%{http_code}\n" https://${APP_DOMAIN}/healthz               # expect 200
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Add-in button missing after sideload | Wrong manifest path or Word not restarted. Re-check Step 3; on Mac confirm the file is in the `wef` folder. |
| Pane loads, review returns **401** | The key served by `/addin/config.js` ≠ a key the engine accepts. Match `ENGINE_API_KEY` (or an `ENGINE_SERVICE_KEYS` entry) and roll a revision. |
| Review returns **503** on `/v1` | No LLM provider key configured (`503 no_provider`) — seed `openrouter-api-key` (`docs/CREDENTIALS.md`). |
| Blank pane / "can't load" | Manifest still contains `__APP_DOMAIN__`, or icons 404. Regenerate `manifest.prod.xml` (Step 1) and confirm `assets/icon-16/32/80.png` exist. |
| "Add-in not from a trusted source" | Use the M365 admin-center upload, or a properly trusted catalog. |
| Review starts then times out | Deep mode runs minutes (pane allows 10; deep goes async via `/v1/reviews?async=true` + job polling). Check the worker logs: `az containerapp logs show -n nda-worker -g <rg> --follow`. |

## Updating later

1. Edit files under `word-addin/`.
2. Bump `<Version>` in `manifest.cloud.xml` (keep the same `<Id>`).
3. Rebuild + roll the image (the api app serves the statics from it):
   `az acr build … && az containerapp update -n nda-api …` (`docs/AZURE.md §5`).
4. Re-upload `manifest.prod.xml` **only** if the manifest itself changed (URLs,
   version, permissions). Pure HTML/JS/CSS edits need no re-sideload.
