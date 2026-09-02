# Legal Helper — Word Add-in

A Microsoft Word task-pane add-in that reviews the open document against the Legal Helper playbook via the API and applies findings as **tracked changes + comments**.

No build step — plain HTML/CSS/JS, Office.js from the CDN. Serves over HTTPS; sideload the manifest in Word.

---

## What it does

1. **Sign in** with a username and password
   - Save your OpenRouter API key once (encrypted server-side; persists next login)
   - Key is never sent to the browser; only last 4 digits are shown for confirmation

2. **Choose review depth**
   - **Quick:** single whole-document pass (~30 seconds, synchronous)
   - **Deep:** thorough analysis + coverage check (1–3 minutes, asynchronous)
   - Choice auto-saves to user preferences

3. **Review this document**
   - Reads open `.docx`, uploads it, waits for findings
   - Shows: risk tier, findings with suggested edits, required clause checklist
   - Each finding displays an **inline word-level redline** (red for deletions, green for insertions)
   - Expanding a finding briefly **highlights that clause in the document**

4. **Apply redlines** (all at once, or per finding)
   - Turns on Track Changes
   - Finds the exact span in the document, replaces with suggested text
   - Attaches a margin comment with the finding details
   - Only the differing words are struck/inserted (looks like a human redline)

5. **View history and download originals**
   - **History** tab lists all past reviews
   - Click a review to re-open it (shows findings and applied redlines)
   - **Original .docx** link downloads the reviewed document from the bucket

---

## UI overview

### Sign-in screen
Username / password fields. On first login, displays "Key not set" message.

### Key settings
Paste your OpenRouter API key. Shows last-4 digits after successful save. ⚙ gear icon to delete or update.

### Review controls
- Dropdown: Quick / Deep mode selector (saved immediately)
- Button: "Review this document" (uploads and starts the review)
- Quick: displays findings in <60s
- Deep: shows polling status ("Running coverage check...") with elapsed time counter

### Findings pane
- Risk tier: High / Medium / Low
- Adherence score: 0–100%
- List of findings (agent, clause, risk, description)
- For each finding: toggle to expand, view suggested text, click "Apply" or "Apply all"

### History tab
- Table of past reviews (filename, date, risk tier, cost)
- Click a review to see the full details and download the original `.docx`
- Delete button to remove from history and the bucket

### Usage tab
- Total reviews and cost this month
- Tokens used and remaining budget
- Cost breakdown by agent/model
- Monthly spend limit and remaining budget

---

## Settings (⚙ in the pane header)

- **API base URL** — backend origin; leave empty if same-origin (production default), or set to `http://localhost:8000` in dev
- **API key** — legacy field kept for testing; sign-in (recommended) is the normal flow

All settings persist in browser local storage (`lh.*` keys).

**Timeouts:**
- Quick mode: 3 minutes
- Deep mode: 10 minutes
- Both have exponential backoff polling (2s, 4s, 8s, ...)

---

## Local development

### 1. Start the backend API

```bash
cd ..                    # root of repo
make install
make run                 # serves on http://localhost:8000
```

### 2. Install trusted localhost certificate

```bash
npx office-addin-dev-certs install   # one-time; creates ~/.office-addin-dev-certs/
```

### 3. Start the add-in dev server

```bash
cd word-addin
npm install              # once
node dev-server.mjs      # starts HTTPS server on https://localhost:3000
                         # proxies /api and /healthz to http://localhost:8000
```

### 4. Sideload the manifest

The dev server outputs:
```
https://localhost:3000 (add-in HTML + CSS/JS)
Manifest: https://localhost:3000/manifest.dev.xml
```

**Word on the web:**
1. Insert → Get Add-ins (or Insert → Add-ins)
2. Upload My Add-in
3. Select `word-addin/manifest.dev.xml` from your computer
4. Legal Helper pane appears in the task pane

**Word on Mac:**
1. Copy `word-addin/manifest.dev.xml` to:
   ```
   ~/Library/Containers/com.microsoft.Word/Data/Documents/wef/
   ```
2. Restart Word
3. Home → Add-ins → Legal Helper → Review this document

**Word on Windows (Office 2019+):**
1. Copy `word-addin/manifest.dev.xml` to:
   ```
   %LOCALAPPDATA%\Microsoft\Office\16.0\Wef\
   ```
2. Restart Word
3. Home → Add-ins → Legal Helper → Review this document

### 5. Test locally

Sign in as `alice.tan` (password shown in `make run` output, or empty).

Try these sample documents from `../../samples/`:
- `nda_missing_governing_law.docx` — Quick review detects missing clause
- `msa_uncapped_liability.docx` — Deep review flags uncapped liability
- `letter_not_a_contract.docx` — Classifier recognizes as non-contract

---

## How the clause locator works

When you expand a finding in the pane, it highlights that clause in the Word document:

1. Extract the `span` (suggested text to find) from the finding
2. Use Office.js `Document.body.getRange().getRange("Start")` to get the document range
3. Call `range.search(span, { matchCase: false, matchWholeWord: false })`
4. Highlight the range by adding a border + background color
5. Clear the highlight after 2 seconds

Code: `word-addin/taskpane.js` `flashSpan()` function.

---

## How tracked changes are applied

When you click "Apply" or "Apply all":

1. Turn on Track Changes: `Word.TrackChangesBehavior.enabled`
2. For each finding:
   - Get the document range: `body.getRange()`
   - Search for the exact `span` (case-insensitive)
   - Replace with `suggested` text
   - A tracked change is created automatically
3. Insert a margin comment with the finding ID and description
4. Turn off Track Changes

The result: the document shows the suggested edits with margin comments, and the user can accept/reject each one independently in Word's native UI.

Code: `word-addin/taskpane.js` `applyFinding()` and `applyAll()` functions.

---

## Production sideloading

The production add-in ships **same-origin** with the API:
- Backend serves static files under `/addin/*`
- Backend serves `/api/` on the same origin
- No CORS setup needed; bearer tokens work seamlessly

The dynamic `/manifest.xml` endpoint includes the app's domain (from the request origin), so students can sideload the same manifest URL on Mac, Windows, and web without modification.

Manifest URL: `https://<your-legal-helper-domain>/manifest.xml`

---

## Troubleshooting

| Problem | Solution |
|---|---|
| "Cannot find module" when running `node dev-server.mjs` | Run `npm install` first (installs office-addin-dev-server, etc.) |
| Manifest sideload fails (404) | Ensure `dev-server.mjs` is running and has outputted the manifest URL. Check the URL matches your browser's localhost address. |
| Add-in appears but Sign-in page is blank | Open browser console (F12) and check for JavaScript errors. Ensure `taskpane.js` loaded. |
| "Bearer token invalid" after sign-in | Sign out and sign back in. Local storage may have stale token. |
| "API base URL not reachable" message | Check that backend is running on `http://localhost:8000`. Open `http://localhost:8000/healthz` in a browser (should return `{"status":"ok"}`). |
| Redlines not applying to the document | Document must be open in edit mode (not read-only). Track Changes may already be on; try toggling it. Some Word versions have UI quirks; retry the apply operation. |
| Can't download original document | Document may have expired from the bucket (retention cap: 20 per user, oldest deleted when exceeded). Ask presenter if there's a backup. |
| Deep review stuck on "Running..." | Network issue or backend crash. Check browser console. Refresh and try again. If it persists, backend may be down; check `/healthz`. |
| TypeError: "office.body is undefined" | Office.js failed to initialize. Ensure you're in a real Word (not Notepad). Some browser Word versions have limitations; try desktop Word. |

---

## Manifest files

- `manifest.dev.xml` — development manifest (hardcoded `https://localhost:3000/` URLs)
- No static production manifest in the repo (it's generated dynamically at runtime to include the actual domain)

---

## Browser compatibility

- Word on the web (Edge, Chrome, Safari): full support
- Word on Mac (version 16.48+): full support (Office.js 1.4+)
- Word on Windows (Office 2019, 2021, Microsoft 365): full support
- Requirements: Office.js 1.4 minimum, 1.6 features gated on version

---

## Tests

```bash
npm test    # node --test tests/**/*.test.js
```

Tests use Node's built-in `test` runner. Coverage includes:
- Redline diffing (finding span in document)
- Polling logic (backoff, timeout, status transitions)
- Local storage (settings persistence)
- No Office.js mocking; these are pure utilities

For integration tests (add-in + backend), see `backend/tests/`.

---

## File structure

```
word-addin/
  taskpane.html           Task pane UI (HTML template)
  taskpane.css            Styles
  taskpane.js             Main logic (sign-in, review, apply, polling)
  manifest.dev.xml        Dev manifest
  dev-server.mjs          Local HTTPS dev server + /api proxy
  package.json            Dependencies (office-js, office-addin-dev-server, etc.)
  tests/
    redline.test.js       Redline diffing tests
    polling.test.js       Async polling logic tests
    storage.test.js       Local storage tests
  README.md               This file
```

---

## Next steps

- **Modify the playbook:** edit `../../playbook/legal_helper_playbook.json` and redeploy
- **Add custom users:** run `python -m app.seed_demo --add-user alice` (backend) and set password
- **Extend the UI:** add new tabs or fields to `taskpane.html`
- **Change the manifest:** the dynamic `/manifest.xml` generates from environment variables (see `backend/app/api/routes_addin.py`)


