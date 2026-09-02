# Deploying Legal Helper to Railway

This guide walks through the deployment click-path from `LEGAL_HELPER_PLAN.md` §7. By the end, you will have a public Legal Helper deployment with a sideloaded Word add-in.

**Estimated time:** 15–20 minutes.

---

## Prerequisites

- A GitHub account with a fork or private clone of `SMKxx1/legal-helper`
- A Railway account (free tier is sufficient)
- An OpenRouter account with API key (for live reviews)
- Microsoft Word (desktop or web)

---

## Step-by-step

### 1. Prepare your repo

Clone and test locally:

```bash
git clone https://github.com/YOUR-GITHUB/legal-helper
cd legal-helper
make install
make run
```

Visit `http://localhost:8000/healthz` — should return `{"status":"ok"}`.
Verify `.env` is in `.gitignore` and `.env.example` is committed.

Push to a private GitHub repository:

```bash
gh repo create legal-helper --private --source=. --remote=origin --push
```

### 2. Create a Railway project

1. Go to [Railway.app](https://railway.app)
2. **New Project** → **Empty Project**
3. Name it `legal-helper`

**Screenshot:** Railway dashboard with new empty project.

---

### 3. Add PostgreSQL database

1. **`+ Create`** → **Database** → **PostgreSQL**
2. Keep the default name (`Postgres`)
3. Wait for the database to initialize (≈1 minute)

At this point, you should see the `Postgres` service on the canvas.

**Screenshot:** Railway project canvas with Postgres service added.

---

### 4. Add an object bucket

1. **`+ Create`** → **Bucket**
2. Name it `documents`
3. Choose a region closest to your classroom (⚠ cannot change later)
4. Confirm creation

**Screenshot:** Rail way project canvas with Postgres and bucket services.

---

### 5. Add the FastAPI app service

1. **`+ Create`** → **GitHub Repo**
2. Search for and select `legal-helper`
3. Select branch `main`
4. Railway auto-detects the Dockerfile at the root; proceed

Railway begins building the image. While it builds, set environment variables:

**Screenshot:** Railway build log showing "Building from Dockerfile..."

### 6. Configure environment variables

Click the `legal-helper` service box on the canvas, then **Variables**.

Set each variable below. For S3 values, use **Railway reference syntax** (click the "Add reference" icon):

| Variable | Value | Notes |
|---|---|---|
| `APP_ENV` | `prod` | |
| `PORT` | `8000` | Railway injects this; uvicorn must bind it |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | Reference variable |
| `APP_SECRET_KEY` | (generate) | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SEED_DEMO_DATA` | `true` | Seeds synthetic users on first boot |
| `DEMO_USER_PASSWORD` | (choose) | Password for demo users (e.g., `LegalHelper2026!`) |
| `ADDIN_ID` | (generate) | `python -c "import uuid; print(uuid.uuid4())"` |
| `S3_ENDPOINT` | `${{documents.ENDPOINT}}` | Reference variable |
| `S3_BUCKET` | `${{documents.BUCKET}}` | Reference variable |
| `S3_ACCESS_KEY_ID` | `${{documents.ACCESS_KEY_ID}}` | Reference variable |
| `S3_SECRET_ACCESS_KEY` | `${{documents.SECRET_ACCESS_KEY}}` | Reference variable |
| `S3_REGION` | `${{documents.REGION}}` | Reference variable |
| `MODEL_CLASSIFIER` | `anthropic/claude-haiku-4-5` | |
| `MODEL_QUICK` | `anthropic/claude-sonnet-4-6` | |
| `MODEL_DEEP` | `anthropic/claude-opus-4-8` | Optional: budget-conscious? Use `MODEL_DEEP=anthropic/claude-sonnet-4-6` |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | |
| `PROVIDER_TIMEOUT_S` | `150` | Opus can take up to 3 minutes; 150s is safe |
| `REVIEW_CONCURRENCY` | `2` | Parallel in-process reviews |
| `MAX_UPLOAD_MB` | `10` | |
| `MAX_DOC_CHARS` | `120000` | |
| `MAX_MONTHLY_COST_USD` | `5` | Per-user budget guardrail |
| `MAX_DOCS_PER_USER` | `20` | Bucket retention cap |

**Screenshot:** Variables panel with all values set.

---

### 7. Configure healthcheck

In the `legal-helper` service, go to **Settings** → **Healthcheck path** and set it to `/healthz`.

**Screenshot:** Settings panel with healthcheck configured.

---

### 8. Generate a public domain

1. In the `legal-helper` service, go to **Settings** → **Networking**
2. **Generate Domain**
3. Copy the public URL (e.g., `https://legal-helper-prod.railway.app`)

Railway provisions TLS automatically.

**Screenshot:** Networking panel with generated domain.

---

### 9. Verify deployment

Open your new domain:
- `https://<your-domain>/` — landing page, capability states, manifest download
- `https://<your-domain>/healthz` — should return `{"status":"ok"}`

Check the deployment logs: **Deployments** → (latest) → **View Logs**. You should see:
```
[...] Running migration [...]
[...] Seeding demo data [...]
[...] Uvicorn running on 0.0.0.0:8000 [...]
```

**Screenshot:** Deployment logs showing successful startup.

---

### 10. Sideload the add-in manifest

Download the dynamic manifest:
```bash
curl -s https://<your-domain>/manifest.xml > manifest.xml
```

**Word on the web:**
1. Open Word online
2. **Insert** → **Add-ins** → **Upload My Add-in** → select `manifest.xml`
3. A task pane titled "Legal Helper" appears

**Word on Mac:**
1. Copy `manifest.xml` to `~/Library/Containers/com.microsoft.Word/Data/Documents/wef/`
2. Restart Word
3. **Home** → **Add-ins** → **Legal Helper** → **Review this document**

**Word on Windows:**
1. Copy `manifest.xml` to `%LOCALAPPDATA%\Microsoft\Office\16.0\Wef\`
2. Restart Word
3. **Home** → **Add-ins** (or **Insert** → **Add-ins** depending on Office version) → select **Legal Helper**

**Screenshot:** Word add-in pane open, showing sign-in screen.

---

### 11. Smoke test

Run the deployment validation script from your repo:

```bash
python backend/scripts/smoke.py https://<your-domain> alice.tan <DEMO_USER_PASSWORD>
```

All checks should pass:
- ✓ /healthz returns 200 on 10/10 checks
- ✓ /api/status is reachable
- ✓ /api/me returns 401 without token
- ✓ Login succeeded
- ✓ /api/me returned user alice.tan
- ✓ /api/me/usage p95 < 500ms

Optional: include a live review (requires your OpenRouter key):
```bash
python backend/scripts/smoke.py https://<your-domain> alice.tan <DEMO_USER_PASSWORD> \
  --with-review --openrouter-key sk-or-v1-...
```

---

### 12. Demonstrate in Word

Sign in as `alice.tan` with password `<DEMO_USER_PASSWORD>`.

Paste your personal OpenRouter key into the settings panel (⚙).

Upload a sample document:
- `samples/nda_missing_governing_law.docx` — Quick review (~30s, detects missing governing law)
- `samples/msa_uncapped_liability.docx` — Deep review (~2–3 min with Opus)
- `samples/letter_not_a_contract.docx` — Should classify as non-contract

Click **Review this document** → findings appear → click **Apply all** to apply redlines.

Open the **History** tab to see past reviews and download the original document.

**Screenshot:** Legal Helper pane with findings and tracked changes applied.

---

## Cost monitoring

Watch your spend in Railway:
- **Project settings** → **Usage** (estimated credit consumption)
- Model choices dominate: Opus > Sonnet > Haiku
- For a budget-conscious demo, use `MODEL_DEEP=anthropic/claude-sonnet-4-6` and set `MAX_MONTHLY_COST_USD=3`

---

## Redeploy

Push a commit to `main`:
```bash
git add .
git commit -m "Update something"
git push
```

Railway auto-builds and redeploys. Verify in the **Deployments** view that the new version is live.

Users, reviews, and bucket objects persist across redeployments.

---

## Teardown

After the workshop:

1. **Railway project** → **Settings** → **Delete Project**
2. GitHub repository → **Settings** → **Delete** (if it was only for the demo)
3. Rotate `APP_SECRET_KEY` and `DEMO_USER_PASSWORD` if the deployment stays up beyond the workshop

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Build fails with "Dockerfile not found" | Ensure `Dockerfile` is at the repo root, not in `backend/` |
| Database connection timeout | Postgres service is still initializing; wait 1–2 min and retry |
| `/manifest.xml` returns 404 | The app service is still building; check logs |
| Add-in won't sideload | Verify the manifest domain matches your Railway URL |
| Login fails with "Unknown user" | Ensure `SEED_DEMO_DATA=true` was set and the deploy completed successfully |
| Review upload fails with 401 | Token is invalid; sign out and sign back in |
| Review runs slow | Opus takes 1–3 minutes; "Deep" reviews are asynchronous with polling |

---

## Next steps

- **Modify the playbook:** edit `playbook/legal_helper_playbook.json`
- **Add a custom user:** `python -m app.seed_demo --add-user <name>` and then manually set a password (or have the presenter do it live)
- **Extend the agents:** see `backend/app/agents/`
- **Deploy elsewhere:** the `Dockerfile` and `docker-compose.yml` (for local multi-container dev) make it portable to any container platform

