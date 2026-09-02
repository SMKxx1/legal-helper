/* Legal Helper — Word task-pane add-in.
 * Reads the open document, sends it to the API, renders the review, and applies
 * the provider-neutral redline_plan as Word tracked changes + comments.
 */
"use strict";

const DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const els = {};
let lastReview = null;
let renderedFindings = []; // the sorted findings currently shown (carry _applied/_i state)
// Cache BOTH depth results so the user can switch the displayed review without re-running. Each
// entry is { body, elapsed } for that mode; null until that mode has run.
let reviews = { quick: null, deep: null };
let viewMode = null; // which cached review ("quick"|"deep") is currently displayed
let reviewing = false; // mutex: one engine review run at a time
let applying = false; // mutex: never run applyOne/applyAll concurrently against the doc
let initialized = false; // set once Office.onReady wiring succeeds
let applyAllQueue = Promise.resolve(); // serializes apply operations against the doc
// Deep mode fans out per-clause findings + whole-doc recall + ensemble verify +
// Opus cross-clause/tiebreak, so it legitimately runs for several minutes on a
// full document. Quick mode is a single fast pass. Timeouts are per-mode.
const REVIEW_TIMEOUT_MS = { quick: 180000, deep: 600000 };

// RAG tier → pill text + plain-language label shown in the summary panel.
const RAG = {
  red: { cls: "red", pill: "RED", label: "High risk" },
  yellow: { cls: "yellow", pill: "AMBER", label: "Medium risk" },
  green: { cls: "green", pill: "GREEN", label: "Low risk" },
};

// API origin resolution order:
//   1. a user override saved in this browser (the Sign-in screen's "Server base URL" field),
//   2. the origin the add-in was served from (the same server hosts the API),
//   3. local-dev fallback.
// All add-in state lives in localStorage under "lh.*" (plan §4.4): lh.token, lh.serverBase,
// lh.mode, lh.ourSide.
const servedOrigin =
  typeof location !== "undefined" && /^https?:/.test(location.origin) ? location.origin : "";
const cfg = {
  get apiBase() {
    return (localStorage.getItem("lh.serverBase") || servedOrigin || "https://localhost:8000").trim();
  },
  set apiBase(v) {
    localStorage.setItem("lh.serverBase", (v || "").trim());
  },
  get token() {
    return localStorage.getItem("lh.token") || "";
  },
  set token(v) {
    if (v) localStorage.setItem("lh.token", v);
    else localStorage.removeItem("lh.token");
  },
  get mode() {
    return localStorage.getItem("lh.mode") || "deep";
  },
  get ourSide() {
    return localStorage.getItem("lh.ourSide") || "";
  },
  set ourSide(v) {
    localStorage.setItem("lh.ourSide", (v || "").trim());
  },
};

/* ===== auth: sign in, the OpenRouter key gate, and the bearer token on every fetch =====
 * Three screens, mutually exclusive: #signin-screen (no token yet) -> #addkey-screen (signed in,
 * GET /api/me says has_key=false) -> #review-section (signed in AND has a key). The ⚙ settings
 * panel (account summary, change key, sign out) is only reachable from the last one. */
let currentUser = null; // the `user` object from /api/auth/login or /api/me, once signed in

// Every authenticated request goes through here: attaches the bearer token, and on 401 drops the
// (now-invalid) session and returns the pane to Sign in — plan §4.4 "On any 401 ... return to
// Sign in." `skipAuthReset` lets the sign-in form's own login POST reuse this without recursing.
async function apiFetch(path, opts, { skipAuthReset } = {}) {
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {});
  if (cfg.token) headers["Authorization"] = "Bearer " + cfg.token;
  const resp = await fetch(cfg.apiBase.replace(/\/$/, "") + path, { ...opts, headers });
  if (resp.status === 401 && !skipAuthReset) {
    signOut("Your session expired — sign in again.");
  }
  return resp;
}

function showScreen(name) {
  const screens = {
    signin: els["signin-screen"],
    signup: els["signup-screen"],
    addkey: els["addkey-screen"],
    ready: els["ready-screen"],
  };
  Object.keys(screens).forEach((k) => screens[k] && screens[k].classList.toggle("hidden", k !== name));
  els["settings-toggle"].classList.toggle("hidden", name !== "ready");
  if (name !== "ready") {
    els.settings.classList.add("hidden");
    els["settings-toggle"].setAttribute("aria-expanded", "false");
  } else {
    switchTab(currentTab); // land on (or return to) whichever tab was last active
  }
}

/* ===== tabs: Review / History / Usage (only reachable once signed in with a key) ===== */
let currentTab = "review";
function switchTab(name) {
  currentTab = name;
  ["review", "history", "usage"].forEach((t) => {
    els[t + "-section"].classList.toggle("hidden", t !== name);
  });
  els.tabs.querySelectorAll(".tab-btn").forEach((b) => {
    b.classList.toggle("is-active", b.dataset.tab === name);
  });
  if (name === "history") loadHistory();
  if (name === "usage") loadUsage();
}

function signOut(message) {
  currentUser = null;
  cfg.token = "";
  showScreen("signin");
  setAuthStatus("signin-status", message || "", !!message);
}

function setAuthStatus(elId, msg, isError) {
  const el = els[elId];
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("error", !!isError);
}

function renderSettingsSummary() {
  if (!currentUser) return;
  els["settings-account"].textContent = currentUser.display_name || currentUser.username;
  els["settings-key"].textContent = currentUser.key_label
    ? `Key •••• ${currentUser.key_last4} (${currentUser.key_label})`
    : `Key •••• ${currentUser.key_last4 || "????"}`;
}

// After a successful login (or on boot, with a stored token): route to Add-key or Ready.
function routeSignedInUser(user) {
  currentUser = user;
  if (!user.has_key) {
    showScreen("addkey");
    setAuthStatus("addkey-status", "");
  } else {
    renderSettingsSummary();
    showScreen("ready");
    setStatus("Ready — pick a depth and review the open document.");
  }
}

// Self-service registration. The server validates the OpenRouter key against OpenRouter before
// it creates the account, so a 422 here usually means the key is wrong — not the password.
async function doSignUp() {
  const username = els["signup-username"].value.trim();
  const password = els["signup-password"].value;
  const apiKey = els["signup-key"].value.trim();
  const displayName = els["signup-display"].value.trim();
  if (!username || !password || !apiKey) {
    setAuthStatus("signup-status", "Username, password and OpenRouter key are all required.", true);
    return;
  }
  els["signup-btn"].disabled = true;
  setAuthStatus("signup-status", "Checking your key with OpenRouter…");
  try {
    const resp = await apiFetch(
      "/api/auth/register",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username,
          password,
          api_key: apiKey,
          display_name: displayName || undefined,
        }),
      },
      { skipAuthReset: true },
    );
    const body = parseJsonSafe(await resp.text());
    if (!resp.ok) {
      const code = body && body.error && body.error.code;
      const msg =
        code === "username_taken"
          ? "That username is taken — pick another."
          : code === "invalid_openrouter_key"
            ? "OpenRouter rejected that key. Check it at openrouter.ai/keys."
            : code === "signup_disabled"
              ? "Sign-up is closed on this server."
              : code === "too_many_signups"
                ? "Too many accounts created from here. Try again later."
                : (body && body.error && body.error.message) || "Could not create the account.";
      setAuthStatus("signup-status", msg, true);
      return;
    }
    cfg.token = body.token;
    els["signup-password"].value = "";
    els["signup-key"].value = "";
    setAuthStatus("signup-status", "");
    routeSignedInUser(body.user); // has_key is true, so this lands straight on Ready
  } catch (e) {
    setAuthStatus("signup-status", `Could not reach the server: ${e.message}`, true);
  } finally {
    els["signup-btn"].disabled = false;
  }
}

async function doSignIn() {
  const server = els["signin-server"].value.trim();
  if (server) cfg.apiBase = server;
  const username = els["signin-username"].value.trim();
  const password = els["signin-password"].value;
  if (!username || !password) {
    setAuthStatus("signin-status", "Enter a username and password.", true);
    return;
  }
  els["signin-btn"].disabled = true;
  setAuthStatus("signin-status", "Signing in…");
  try {
    const resp = await apiFetch(
      "/api/auth/login",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) },
      { skipAuthReset: true },
    );
    const body = parseJsonSafe(await resp.text());
    if (!resp.ok) {
      const msg =
        resp.status === 429
          ? "Too many failed sign-ins — try again in a few minutes."
          : (body && body.error && body.error.message) || "Sign-in failed.";
      setAuthStatus("signin-status", msg, true);
      return;
    }
    cfg.token = body.token;
    els["signin-password"].value = "";
    setAuthStatus("signin-status", "");
    routeSignedInUser(body.user);
  } catch (e) {
    setAuthStatus(
      "signin-status",
      "Could not reach " + cfg.apiBase + " — check the server base URL.",
      true,
    );
  } finally {
    els["signin-btn"].disabled = false;
  }
}

async function doSaveKey() {
  const apiKey = els["addkey-key"].value.trim();
  if (!apiKey) {
    setAuthStatus("addkey-status", "Paste your OpenRouter API key.", true);
    return;
  }
  els["addkey-btn"].disabled = true;
  setAuthStatus("addkey-status", "Validating with OpenRouter…");
  try {
    const resp = await apiFetch("/api/me/openrouter-key", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
    });
    const body = parseJsonSafe(await resp.text());
    if (!resp.ok) {
      setAuthStatus(
        "addkey-status",
        (body && body.error && body.error.message) || "Could not save that key.",
        true,
      );
      return;
    }
    els["addkey-key"].value = "";
    currentUser = Object.assign({}, currentUser, {
      has_key: true,
      key_last4: body.key_last4,
      key_label: body.key_label,
    });
    renderSettingsSummary();
    showScreen("ready");
    setStatus("Key saved — ready to review.");
  } catch (e) {
    setAuthStatus("addkey-status", "Could not reach the server.", true);
  } finally {
    els["addkey-btn"].disabled = false;
  }
}

async function doSignOut() {
  try {
    await apiFetch("/api/auth/logout", { method: "POST" }, { skipAuthReset: true });
  } catch (e) {
    /* best-effort — the client-side token is dropped either way */
  }
  signOut("");
}

// On boot with a stored token, confirm it still works before showing Ready/Add-key.
async function bootAuth() {
  if (!cfg.token) {
    showScreen("signin");
    return;
  }
  try {
    const resp = await apiFetch("/api/me", { method: "GET" }, { skipAuthReset: true });
    if (!resp.ok) {
      signOut("");
      return;
    }
    routeSignedInUser(parseJsonSafe(await resp.text()));
  } catch (e) {
    // Server unreachable — stay on Sign in rather than getting stuck on a blank pane.
    showScreen("signin");
    setAuthStatus("signin-status", "Could not reach " + cfg.apiBase + ".", true);
  }
}

// Guard so the file can be `require`d in Node (unit tests) without office.js present — in the
// browser office.js loads first (taskpane.html), so Office is defined and this runs unchanged.
if (typeof Office !== "undefined")
  Office.onReady((info) => {
    try {
      if (!info || info.host !== Office.HostType.Word) {
        document.body.innerHTML = "<p style='padding:16px'>This add-in runs in Microsoft Word.</p>";
        return;
      }
      [
        "signin-screen",
        "signin-server",
        "signin-username",
        "signin-password",
        "signin-btn",
        "signin-status",
        "show-signup",
        "signup-screen",
        "signup-username",
        "signup-display",
        "signup-password",
        "signup-key",
        "signup-btn",
        "signup-status",
        "show-signin",
        "addkey-screen",
        "addkey-key",
        "addkey-btn",
        "addkey-status",
        "settings",
        "settings-toggle",
        "settings-account",
        "settings-key",
        "settings-change-key",
        "settings-signout",
        "ready-screen",
        "tabs",
        "review-section",
        "history-section",
        "history-status",
        "history-list",
        "usage-section",
        "usage-status",
        "usage-tiles",
        "usage-tables",
        "prereview",
        "our-side",
        "depth-toggle",
        "depth-caption",
        "review-btn",
        "status",
        "summary",
        "runctl",
        "actions",
        "apply-all-btn",
        "findings",
        "review-footer",
        "meta",
      ].forEach((id) => {
        els[id] = document.getElementById(id);
      });

      els["signin-server"].value = cfg.apiBase;
      els["our-side"].value = cfg.ourSide;
      els["our-side"].addEventListener("change", () => {
        cfg.ourSide = els["our-side"].value;
      });

      // Review-depth toggle — "Deep review" on = deep, off = quick. Persists immediately, keeping the
      // cfg.mode contract (localStorage "lh.mode") unchanged so the rest of the flow is untouched.
      const setDeepUI = (deep) => {
        els["depth-toggle"].classList.toggle("is-on", deep);
        els["depth-toggle"].setAttribute("aria-checked", deep ? "true" : "false");
        els["depth-caption"].textContent = deep ? "few min · most thorough" : "~30s · fast check";
      };
      setDeepUI(cfg.mode !== "quick"); // default: deep on
      els["depth-toggle"].onclick = () => {
        const deep = cfg.mode === "quick"; // flip the persisted choice
        localStorage.setItem("lh.mode", deep ? "deep" : "quick");
        setDeepUI(deep);
        setStatus(
          deep
            ? "Deep mode selected — most thorough (a few minutes per review)."
            : "Quick mode selected — fast check.",
        );
      };

      els.tabs.querySelectorAll(".tab-btn").forEach((b) => {
        b.onclick = () => switchTab(b.dataset.tab);
      });
      els["settings-toggle"].onclick = () => {
        const closed = els.settings.classList.toggle("hidden");
        els["settings-toggle"].setAttribute("aria-expanded", closed ? "false" : "true");
      };
      els["settings-change-key"].onclick = () => {
        els.settings.classList.add("hidden");
        els["settings-toggle"].setAttribute("aria-expanded", "false");
        setAuthStatus("addkey-status", "");
        showScreen("addkey");
      };
      els["settings-signout"].onclick = () => doSignOut();
      els["signin-btn"].onclick = () => doSignIn();
      ["signin-username", "signin-password", "signin-server"].forEach((id) => {
        els[id].addEventListener("keydown", (e) => {
          if (e.key === "Enter") doSignIn();
        });
      });
      els["show-signup"].onclick = (e) => {
        e.preventDefault();
        // carry over a server base typed on the sign-in form before switching screens
        const server = els["signin-server"].value.trim();
        if (server) cfg.apiBase = server;
        setAuthStatus("signup-status", "");
        showScreen("signup");
      };
      els["show-signin"].onclick = (e) => {
        e.preventDefault();
        setAuthStatus("signin-status", "");
        showScreen("signin");
      };
      els["signup-btn"].onclick = () => doSignUp();
      ["signup-username", "signup-display", "signup-password", "signup-key"].forEach((id) => {
        els[id].addEventListener("keydown", (e) => {
          if (e.key === "Enter") doSignUp();
        });
      });
      els["addkey-btn"].onclick = () => doSaveKey();
      els["addkey-key"].addEventListener("keydown", (e) => {
        if (e.key === "Enter") doSaveKey();
      });
      els["review-btn"].onclick = () => runReview(); // uses the current depth-toggle choice
      els["apply-all-btn"].onclick = applyAll;
      initialized = true;
      bootAuth(); // decides Sign in / Add key / Ready from the stored token, if any
    } catch (e) {
      document.body.innerHTML =
        "<p style='padding:16px'>Failed to initialize the add-in: " +
        esc((e && e.message) || e) +
        "</p>";
    }
  });

function setStatus(msg, isError) {
  els.status.textContent = msg || "";
  els.status.classList.toggle("error", !!isError);
}

/* ---- read the open document as .docx bytes ---- */
function getDocBytes() {
  return new Promise((resolve, reject) => {
    Office.context.document.getFileAsync(
      Office.FileType.Compressed,
      { sliceSize: 65536 },
      (res) => {
        if (res.status !== Office.AsyncResultStatus.Succeeded) return reject(res.error);
        const file = res.value,
          count = file.sliceCount,
          slices = new Array(count);
        let got = 0,
          failed = false;
        const finish = () => {
          file.closeAsync(() => {});
          const len = slices.reduce((a, s) => a + s.length, 0);
          const out = new Uint8Array(len);
          let off = 0;
          for (const s of slices) {
            out.set(s, off);
            off += s.length;
          }
          resolve(out);
        };
        const getSlice = (i) =>
          file.getSliceAsync(i, (sres) => {
            if (failed) return;
            if (sres.status !== Office.AsyncResultStatus.Succeeded) {
              failed = true;
              file.closeAsync(() => {});
              return reject(sres.error);
            }
            slices[sres.value.index] = sres.value.data;
            got++;
            if (got === count) finish();
          });
        for (let i = 0; i < count; i++) getSlice(i);
      },
    );
  });
}

/* ---- run the review ----
 * `modeArg` ("quick"|"deep") forces a mode (used by "Re-run in … mode"); omitted, it runs the
 * current depth-toggle choice. The just-computed review is cached under its mode and displayed; any
 * previously-displayed review stays visible while this one computes (and on error). */
async function runReview(modeArg) {
  const mode = modeArg === "quick" || modeArg === "deep" ? modeArg : cfg.mode;
  if (reviewing) return; // one engine run at a time
  reviewing = true;
  els["review-btn"].disabled = true;
  els["depth-toggle"].disabled = true;
  els.runctl.querySelectorAll("button").forEach((b) => {
    b.disabled = true;
  });
  setStatus("Reading document…");
  const controller = new AbortController();
  const timeoutMs = REVIEW_TIMEOUT_MS[mode] || REVIEW_TIMEOUT_MS.deep;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let tick = null;
  try {
    const bytes = await getDocBytes();
    const startedAt = Date.now();
    const tip =
      mode === "deep"
        ? "deep mode runs several passes — this can take a few minutes"
        : "quick mode";
    setStatus(`Reviewing against the playbook… (${tip})`);
    tick = setInterval(() => {
      const s = Math.round((Date.now() - startedAt) / 1000);
      setStatus(`Reviewing against the playbook… ${mode} mode, ${s}s elapsed (${tip})`);
    }, 1000);
    const fd = new FormData();
    fd.append("file", new Blob([bytes], { type: DOCX_MIME }), "document.docx");
    fd.append("mode", mode);
    if (cfg.ourSide) fd.append("our_side", cfg.ourSide);
    const headers = {};
    if (cfg.token) headers["Authorization"] = "Bearer " + cfg.token;
    const base = cfg.apiBase.replace(/\/$/, "");
    // Deep mode fans out several passes and legitimately runs for MINUTES — longer than a typical
    // ingress request timeout, which would kill a held-open synchronous connection mid-review. So
    // deep submits ASYNC (POST /api/reviews -> 202 + id) and POLLS GET /api/reviews/{id} to
    // completion with backoff; quick stays a single synchronous POST. Both are bounded by the
    // per-mode AbortController timeout above (the poll wait aborts with it too).
    const body =
      mode === "deep"
        ? await runAsyncReview(base, fd, headers, controller.signal)
        : await runSyncReview(base, fd, headers, controller.signal);
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
    reviews[mode] = { body, elapsed }; // cache (replaces any prior run of this mode)
    showReview(mode); // swap the displayed review to the one just completed
  } catch (e) {
    let msg;
    if (e && e.name === "AbortError") {
      msg = "Review timed out — try Quick mode or check the engine.";
    } else if (
      e instanceof TypeError ||
      (e && /Failed to fetch|NetworkError|Load failed/i.test(e.message || ""))
    ) {
      msg =
        "Could not reach the engine at " +
        cfg.apiBase +
        " — check it is running and that CORS/HTTPS allow this add-in.";
    } else {
      msg = "Review failed: " + ((e && e.message) || e);
    }
    setStatus(msg, true);
    if (!reviews.quick && !reviews.deep) els.prereview.classList.remove("hidden"); // let them retry
  } finally {
    clearTimeout(timer);
    if (tick) clearInterval(tick);
    reviewing = false;
    els["review-btn"].disabled = false;
    els["depth-toggle"].disabled = false;
    els.runctl.querySelectorAll("button").forEach((b) => {
      b.disabled = false;
    });
  }
}

/* ===== review transport (sync quick / async deep) ================
 * Quick reviews are a single fast pass — one synchronous POST /api/reviews (200, the finished
 * review inline). Deep reviews can outlast a typical ingress request timeout, so they submit
 * ASYNC (POST /api/reviews -> 202 + id) and poll GET /api/reviews/{id} to completion. These share
 * the pure shape/backoff helpers below so both paths (and the node tests) agree. */

// A well-formed, FINISHED review payload — the shape both the sync 200 and a "done" poll carry.
function isReviewBody(body) {
  return !!(
    body &&
    typeof body === "object" &&
    body.id &&
    body.status === "done" &&
    Array.isArray(body.findings)
  );
}

// Parse a response body as JSON, tolerating an empty/garbage body (-> null) so a non-JSON error
// page never throws before we can render "HTTP <status>".
function parseJsonSafe(raw) {
  try {
    return raw ? JSON.parse(raw) : null;
  } catch (_) {
    return null;
  }
}

// Poll backoff: start ~1.5s, grow gently, cap at 5s so a 10-minute deep review is a bounded,
// low-chatter number of GETs (not a tight loop) while still feeling responsive early on.
function nextPollDelayMs(attempt) {
  const base = 1500,
    factor = 1.5,
    cap = 5000;
  return Math.min(cap, Math.round(base * Math.pow(factor, Math.max(0, attempt))));
}

// Interpret one GET /api/reviews/{id} poll. status walks queued -> running -> done|failed; when
// done the poll response IS the finished review (no separate wrapper). Returns
// { state, review?, error? }.
function jobOutcome(job) {
  if (!job || typeof job !== "object") return { state: "pending" };
  const status = String(job.status || "").toLowerCase();
  if (status === "done") return { state: "done", review: isReviewBody(job) ? job : null };
  if (status === "failed") return { state: "failed", error: job.error || "" };
  return { state: "pending" }; // queued | running
}

// A setTimeout that also rejects (AbortError) if the shared review AbortController fires, so the
// overall per-mode timeout bounds the WAIT between polls too — not just the in-flight fetches.
function abortError() {
  const e = new Error("Aborted");
  e.name = "AbortError";
  return e;
}
function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) return reject(abortError());
    const onAbort = () => {
      clearTimeout(t);
      reject(abortError());
    };
    const t = setTimeout(() => {
      if (signal) signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    if (signal) signal.addEventListener("abort", onAbort, { once: true });
  });
}

// Quick path: one synchronous POST /api/reviews; the finished review comes back inline (200).
// Returns the validated review body or throws a display-ready Error.
async function runSyncReview(base, fd, headers, signal) {
  const resp = await fetch(base + "/api/reviews", {
    method: "POST",
    headers,
    body: fd,
    signal,
  });
  if (resp.status === 401) {
    signOut("Your session expired — sign in again.");
    throw new Error("Sign-in required.");
  }
  const body = parseJsonSafe(await resp.text());
  if (!resp.ok)
    throw new Error((body && body.error && body.error.message) || "HTTP " + resp.status);
  if (!isReviewBody(body)) throw new Error("Malformed response from the engine.");
  return body;
}

// Deep path: POST /api/reviews (mode=deep) answers 202 + { id, status: "queued" } — poll it.
async function runAsyncReview(base, fd, headers, signal) {
  const resp = await fetch(base + "/api/reviews", {
    method: "POST",
    headers,
    body: fd,
    signal,
  });
  if (resp.status === 401) {
    signOut("Your session expired — sign in again.");
    throw new Error("Sign-in required.");
  }
  const body = parseJsonSafe(await resp.text());
  if (!resp.ok)
    throw new Error((body && body.error && body.error.message) || "HTTP " + resp.status);
  const reviewId = body && body.id;
  if (!reviewId) throw new Error("Malformed response from the engine.");
  return pollReview(base, reviewId, headers, signal);
}

// Poll a submitted deep review until it is done (return it) or failed (throw). The elapsed-time
// ticker in runReview keeps the status line moving; `delay` carries the abort.
async function pollReview(base, reviewId, headers, signal) {
  const url = base + "/api/reviews/" + encodeURIComponent(reviewId);
  for (let attempt = 0; ; attempt++) {
    await delay(nextPollDelayMs(attempt), signal); // rejects AbortError on the overall timeout
    const resp = await fetch(url, { headers, signal });
    if (resp.status === 401) {
      signOut("Your session expired — sign in again.");
      throw new Error("Sign-in required.");
    }
    const job = parseJsonSafe(await resp.text());
    if (!resp.ok) throw new Error((job && job.error && job.error.message) || "HTTP " + resp.status);
    const outcome = jobOutcome(job);
    if (outcome.state === "done") {
      if (!outcome.review) throw new Error("Malformed response from the engine.");
      return outcome.review;
    }
    if (outcome.state === "failed")
      throw new Error(outcome.error ? "Review failed: " + outcome.error : "The review job failed.");
    // queued / running -> keep polling
  }
}

/* Swap the displayed review to a cached mode — re-render everything from that cached body (NO
 * re-fetch, NO re-reading the document) and refresh the run-controls row. */
function showReview(mode) {
  const entry = reviews[mode];
  if (!entry) return;
  viewMode = mode;
  lastReview = entry.body;
  els.prereview.classList.add("hidden");
  render(entry.body);
  renderRunCtl();
  setStatus("");
}

/* The post-review controls: "Ran in {mode} mode · {n}s elapsed" plus either a "Re-run in {other}
 * mode" button (only one mode has run) or a Quick/Deep segmented toggle (both have run → switch the
 * cached view without re-fetching). */
function renderRunCtl() {
  const cur = viewMode && reviews[viewMode];
  if (!cur) {
    els.runctl.classList.add("hidden");
    els.runctl.innerHTML = "";
    return;
  }
  const cap = (m) => (m === "deep" ? "Deep" : "Quick");
  const bothRan = !!(reviews.quick && reviews.deep);
  let right;
  if (bothRan) {
    right = `<div class="seg-toggle" role="group" aria-label="View mode">
         <button type="button" class="seg-btn ${viewMode === "quick" ? "is-on" : ""}" data-view="quick">Quick</button>
         <button type="button" class="seg-btn ${viewMode === "deep" ? "is-on" : ""}" data-view="deep">Deep</button>
       </div>`;
  } else {
    const other = viewMode === "deep" ? "quick" : "deep";
    right = `<button type="button" class="rerun-btn" data-rerun="${other}">Re-run in ${cap(other)} mode</button>`;
  }
  els.runctl.innerHTML = `<div class="runctl-row">
       <div class="runctl-meta">Ran in ${cap(viewMode)} mode &middot; ${esc(cur.elapsed)}s elapsed</div>
       ${right}
     </div>`;
  els.runctl.classList.remove("hidden");
  const rb = els.runctl.querySelector("[data-rerun]");
  if (rb) rb.onclick = () => runReview(rb.dataset.rerun);
  els.runctl.querySelectorAll("[data-view]").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.view !== viewMode) showReview(b.dataset.view);
    };
  });
}

function esc(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );
}

// Integer with thousands separators (token counts); falls back to the raw value.
function fmtInt(n) {
  const v = Number(n);
  return Number.isFinite(v) ? Math.round(v).toLocaleString() : String(n == null ? "" : n);
}

// A dollar amount as "$1.23"; a non-numeric value (null cost on a still-running row) renders "—".
function fmtCost(n) {
  const v = Number(n);
  return Number.isFinite(v) ? "$" + v.toFixed(2) : "—";
}

function fmtDate(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/* ===== History tab — last 20 reviews (GET /api/reviews). "Open" re-renders the stored result
 * into the Review tab (untouched by the live quick/deep cache); "Original .docx" streams the
 * presigned download through an authenticated fetch; "Delete" needs a second click to confirm
 * (no native confirm() dialog — some Office hosts block it). ===== */
function setHistoryStatus(msg, isError) {
  const el = els["history-status"];
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("error", !!isError);
}

async function loadHistory() {
  setHistoryStatus("Loading…");
  els["history-list"].innerHTML = "";
  try {
    const resp = await apiFetch("/api/reviews?limit=20", { method: "GET" });
    const body = parseJsonSafe(await resp.text());
    if (!resp.ok) {
      setHistoryStatus((body && body.error && body.error.message) || "Could not load history.", true);
      return;
    }
    renderHistory(Array.isArray(body) ? body : []);
  } catch (e) {
    setHistoryStatus("Could not reach " + cfg.apiBase + ".", true);
  }
}

function renderHistory(rows) {
  if (!rows.length) {
    setHistoryStatus("");
    els["history-list"].innerHTML =
      '<p class="status">No reviews yet — run one from the Review tab.</p>';
    return;
  }
  setHistoryStatus("");
  els["history-list"].innerHTML = rows
    .map((r) => {
      const rag = RAG[(r.risk_tier || "").toLowerCase()];
      const tierHtml =
        r.status === "failed"
          ? '<span class="rag-pill failed">FAILED</span>'
          : rag
            ? `<span class="rag-pill ${rag.cls}">${rag.pill}</span>`
            : "";
      const docBtn = r.document_stored
        ? `<button type="button" class="btn-ghost history-doc" data-id="${r.id}">Original .docx</button>`
        : "";
      const openDisabled = r.status === "done" ? "" : "disabled";
      return `<div class="history-row">
        <div class="history-row-top">
          <span class="history-title">${esc(r.filename || "Untitled")}</span>
          ${tierHtml}
        </div>
        <div class="history-meta">${esc(fmtDate(r.created_at))} &middot; ${esc(r.mode)} &middot; ${fmtCost(r.cost_usd)}</div>
        <div class="history-actions">
          <button type="button" class="btn-ghost history-open" data-id="${r.id}" ${openDisabled}>Open</button>
          ${docBtn}
          <button type="button" class="btn-ghost history-delete" data-id="${r.id}">Delete</button>
        </div>
      </div>`;
    })
    .join("");
  els["history-list"].querySelectorAll(".history-open").forEach((b) => {
    b.onclick = () => openHistoryReview(b.dataset.id, b);
  });
  els["history-list"].querySelectorAll(".history-doc").forEach((b) => {
    b.onclick = () => openOriginalDocument(b.dataset.id, b);
  });
  els["history-list"].querySelectorAll(".history-delete").forEach((b) => {
    b.onclick = () => deleteHistoryReview(b.dataset.id, b);
  });
}

// "Open" — fetch the full stored result and render it into the Review tab via the SAME render()
// the live flow uses. Deliberately does not touch reviews{}/viewMode (the live quick/deep cache):
// this is a read of history, not a new run, so switching back to a live result stays possible.
async function openHistoryReview(id, btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Opening…";
  try {
    const resp = await apiFetch(`/api/reviews/${encodeURIComponent(id)}`, { method: "GET" });
    const body = parseJsonSafe(await resp.text());
    if (!resp.ok || !isReviewBody(body)) {
      setHistoryStatus("Could not open that review.", true);
      return;
    }
    els.prereview.classList.add("hidden");
    render(body);
    els.runctl.classList.add("hidden"); // "re-run in other mode" doesn't apply to a historical view
    els.runctl.innerHTML = "";
    switchTab("review");
    setStatus(`Viewing history — ${body.filename || "this review"}.`);
  } catch (e) {
    setHistoryStatus("Could not reach " + cfg.apiBase + ".", true);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// "Original .docx" — GET .../document is bearer-authenticated and 302s to a presigned bucket URL,
// so it can't be a plain <a href> (no way to attach the header). Fetch it (the browser follows the
// redirect transparently) and hand the resulting bytes to the OS as a normal file open/save.
async function openOriginalDocument(id, btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Opening…";
  try {
    const resp = await apiFetch(`/api/reviews/${encodeURIComponent(id)}/document`, {
      method: "GET",
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank");
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) {
    setHistoryStatus("Could not open the original document.", true);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// Delete needs a second click within 3s to confirm — no native confirm() (some Office hosts block
// it); the button's own label does the asking instead.
async function deleteHistoryReview(id, btn) {
  if (!btn.dataset.confirm) {
    btn.dataset.confirm = "1";
    const original = btn.textContent;
    btn.textContent = "Confirm delete?";
    btn.classList.add("confirm");
    setTimeout(() => {
      if (btn.dataset.confirm) {
        delete btn.dataset.confirm;
        btn.textContent = original;
        btn.classList.remove("confirm");
      }
    }, 3000);
    return;
  }
  delete btn.dataset.confirm;
  btn.disabled = true;
  try {
    const resp = await apiFetch(`/api/reviews/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!resp.ok && resp.status !== 204) {
      setHistoryStatus("Could not delete that review.", true);
      btn.disabled = false;
      return;
    }
    loadHistory();
  } catch (e) {
    setHistoryStatus("Could not reach " + cfg.apiBase + ".", true);
    btn.disabled = false;
  }
}

/* ===== Usage tab — GET /api/me/usage as stat tiles + by-mode/by-model tables ===== */
function setUsageStatus(msg, isError) {
  const el = els["usage-status"];
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("error", !!isError);
}

async function loadUsage() {
  setUsageStatus("Loading…");
  els["usage-tiles"].innerHTML = "";
  els["usage-tables"].innerHTML = "";
  try {
    const resp = await apiFetch("/api/me/usage", { method: "GET" });
    const body = parseJsonSafe(await resp.text());
    if (!resp.ok) {
      setUsageStatus((body && body.error && body.error.message) || "Could not load usage.", true);
      return;
    }
    renderUsage(body || {});
  } catch (e) {
    setUsageStatus("Could not reach " + cfg.apiBase + ".", true);
  }
}

function renderUsage(u) {
  setUsageStatus("");
  const tiles = [
    ["Reviews (total)", fmtInt(u.reviews_total)],
    ["Reviews (this month)", fmtInt(u.reviews_this_month)],
    ["Spend (total)", fmtCost(u.cost_total_usd)],
    ["Spend (this month)", fmtCost(u.cost_this_month_usd)],
  ];
  els["usage-tiles"].innerHTML = tiles
    .map(
      ([label, value]) =>
        `<div class="usage-tile"><span class="num display">${esc(value)}</span><span class="label">${esc(label)}</span></div>`,
    )
    .join("");

  const byMode = u.by_mode || {};
  const quick = byMode.quick || { n: 0, cost_usd: 0 };
  const deep = byMode.deep || { n: 0, cost_usd: 0 };
  const byModel = Array.isArray(u.by_model) ? u.by_model : [];
  const budget = u.budget || {};

  let html = `<div class="section-h">By mode</div>
    <table class="usage-table">
      <thead><tr><th>Mode</th><th>Reviews</th><th>Cost</th></tr></thead>
      <tbody>
        <tr><td>Quick</td><td>${fmtInt(quick.n)}</td><td>${fmtCost(quick.cost_usd)}</td></tr>
        <tr><td>Deep</td><td>${fmtInt(deep.n)}</td><td>${fmtCost(deep.cost_usd)}</td></tr>
      </tbody>
    </table>`;
  if (byModel.length) {
    html += `<div class="section-h">By model</div>
      <table class="usage-table">
        <thead><tr><th>Model</th><th>Calls</th><th>Cost</th></tr></thead>
        <tbody>${byModel
          .map(
            (m) =>
              `<tr><td>${esc(m.model)}</td><td>${fmtInt(m.calls)}</td><td>${fmtCost(m.cost_usd)}</td></tr>`,
          )
          .join("")}</tbody>
      </table>`;
  }
  if (budget.monthly_cap_usd) {
    const remaining = budget.remaining_usd;
    html += `<p class="status">Monthly budget ${fmtCost(budget.monthly_cap_usd)}${remaining != null ? ` &middot; ${fmtCost(remaining)} remaining` : ""}</p>`;
  }
  els["usage-tables"].innerHTML = html;
}

function render(r) {
  /* ---- summary score panel ---- */
  const tier = (r.risk_tier || "green").toLowerCase();
  const rag = RAG[tier] || RAG.green;
  const c = r.counts || {};
  const scoreNum = Number(r.adherence_score);
  const scoreValid = Number.isFinite(scoreNum);
  const pct = scoreValid ? Math.max(0, Math.min(100, Math.round(scoreNum))) : 0;
  const scoreDisplay = scoreValid ? pct : r.adherence_score == null ? "—" : r.adherence_score;
  els.summary.className = "summary";
  els.summary.innerHTML =
    `<div class="summary-top">
       <div>
         <div class="summary-label">Playbook adherence</div>
         <div class="score"><span class="num display">${esc(scoreDisplay)}</span>` +
    (scoreValid ? `<span class="pct display">%</span>` : "") +
    `</div>
       </div>
       <div class="rag">
         <span class="rag-pill ${rag.cls}">${rag.pill}</span>
         <span class="rag-label">${rag.label}</span>
       </div>
     </div>
     <div class="bar"><span style="width:${pct}%"></span></div>
     <div class="summary-foot">
       <span>${esc(r.doc_type || "document")}${r.our_side ? " · " + esc(r.our_side) : ""}</span>
       <span class="counts">
         <span class="c-high">${esc(c.high || 0)} high</span>
         <span class="c-med">${esc(c.medium || 0)} medium</span>
         <span class="c-low">${esc(c.low || 0)} low</span>
       </span>
     </div>`;
  els.summary.classList.remove("hidden");

  /* ---- findings (expandable) ---- */
  const findings = (r.findings || []).slice().sort((a, b) => sevRank(eff(b)) - sevRank(eff(a)));
  renderedFindings = findings; // Apply-all iterates this and shares _applied state
  let html = "";
  if (findings.length) {
    html += `<div class="section-h">Findings (${findings.length})</div>`;
    findings.forEach((f, i) => {
      f._i = i; // stable index → maps a finding to its button
      const sev = eff(f);
      const canApply = canApplyFinding(f);
      // Has a suggestion but can't be auto-redlined (no verbatim anchor) → manual/advisory.
      const isManual = !canApply && !f._applied && !!f.suggested_language;
      const title = f.title || f.clause_heading || "Finding";
      // "Accept edit" = accept the COUNTERPARTY's tracked change already sitting at this finding's
      // location and dismiss our suggestion (see acceptOne). Only offered where WE could also have
      // offered Apply (a verbatim span to locate), and only on WordApi 1.6 hosts that can accept
      // tracked changes from the pane at all.
      const acceptBtn =
        wordApi16() && f.span
          ? `<button type="button" class="btn-ghost accept" data-i="${i}">Accept edit</button>`
          : "";
      const action = f._applied
        ? `<div class="apply-row">${appliedFragmentHTML(f)}<span class="apply-msg" role="status" aria-live="polite"></span></div>`
        : f._dismissed
          ? `<div class="apply-row"><span class="applied">✓ counterparty edit kept — suggestion dismissed</span><span class="apply-msg" role="status" aria-live="polite"></span></div>`
          : canApply
            ? `<div class="apply-row"><button type="button" class="btn-secondary apply" data-i="${i}">Apply redline</button><button type="button" class="btn-ghost copy" data-i="${i}">Copy</button>${acceptBtn}<span class="apply-msg" role="status" aria-live="polite"></span></div>`
            : isManual
              ? `<div class="advisory"><span class="advisory-note">${esc(advisoryNote(f))}</span><button type="button" class="btn-ghost copy" data-i="${i}">Copy suggested text</button></div>`
              : "";
      html +=
        `<div class="finding ${sev}">
        <button type="button" class="finding-head" data-toggle="${i}" aria-expanded="false" aria-controls="fbody-${i}">
          <span class="sev-pill ${sev}">${esc(sev)}</span>` +
        (isManual
          ? `<span class="tag-manual" title="No verbatim text to anchor a tracked change — apply by hand">Manual</span>`
          : "") +
        `<span class="finding-title">${esc(title)}</span>
          <span class="chev" aria-hidden="true">&rsaquo;</span>
        </button>
        <div class="finding-body hidden" id="fbody-${i}">
          <div class="rationale">${esc(f.rationale || "")}</div>` +
        (f.suggested_language
          ? `<div class="redline-cap">Suggested redline
               <span class="swatch"><i class="del" aria-hidden="true"></i>removed</span>
               <span class="swatch"><i class="ins" aria-hidden="true"></i>added</span>
             </div>
             <div class="redline" role="group" aria-label="Suggested redline — removed text struck through, added text highlighted">${renderRedlineHTML(f.span, f.suggested_language)}</div>`
          : "") +
        action +
        `</div>
      </div>`;
    });
  }

  /* ---- missing required clauses ---- */
  const gaps = (r.coverage && r.coverage.absent_required) || [];
  if (gaps.length) {
    html += `<div class="section-h">Missing required clauses (${gaps.length})</div><div class="gaps">`;
    gaps.forEach((g) => {
      html += `<div class="gap-card missing">&#9888; ${esc(g.clause_type)} &mdash; ${esc(g.note || "absent")}</div>`;
    });
    html += `</div>`;
  }
  els.findings.innerHTML = html;

  // expand/collapse a finding; expanding also jumps to + highlights the clause in Word
  els.findings.querySelectorAll("button.finding-head").forEach((h) => {
    h.onclick = () => {
      const body = document.getElementById("fbody-" + h.dataset.toggle);
      const open = h.getAttribute("aria-expanded") === "true";
      h.setAttribute("aria-expanded", open ? "false" : "true");
      if (body) body.classList.toggle("hidden", open);
      if (!open) highlightFinding(findings[+h.dataset.toggle]); // just opened -> take user there
    };
  });
  // apply a single redline
  els.findings.querySelectorAll("button.apply").forEach((b) => {
    b.onclick = () => applyOne(findings[+b.dataset.i], b);
  });
  // copy a finding's suggested language (manual fallback when it can't be auto-redlined)
  els.findings.querySelectorAll("button.copy").forEach((b) => {
    b.onclick = () => copyText(findings[+b.dataset.i].suggested_language, b);
  });
  // accept the counterparty's tracked change at a finding's location + dismiss our suggestion
  // (only rendered on WordApi 1.6 hosts, and only where a span exists to locate)
  els.findings.querySelectorAll("button.accept").forEach((b) => {
    b.onclick = () => acceptOne(findings[+b.dataset.i], b);
  });

  /* ---- apply-all action (count only redlines not yet applied AND not dismissed via Accept edit) ---- */
  const pending = findings.filter((f) => canApplyFinding(f) && !f._applied && !f._dismissed).length;
  els.actions.classList.toggle("hidden", pending === 0);
  if (pending) els["apply-all-btn"].textContent = `Apply all ${pending} redlines (tracked changes)`;

  /* ---- review-details popover ---- */
  const usage = r.usage || {};
  const calls = usage.calls || [];
  els.meta.innerHTML =
    `<div class="info-wrap">
       <button type="button" class="info-btn" id="info-btn" aria-label="Review details" aria-controls="info-pop" aria-expanded="false">i</button>
       <div class="info-pop hidden" id="info-pop" aria-label="Review details">
         <div class="info-h">Review details</div>
         <div class="info-row"><span class="k">Review</span><span class="v">${esc(r.id)}</span></div>` +
    (r.playbook_version
      ? `<div class="info-row"><span class="k">Playbook</span><span class="v">${esc(r.playbook_version)}</span></div>`
      : "") +
    (usage.input_tokens != null
      ? `<div class="info-row"><span class="k">Input tokens</span><span class="v">${esc(fmtInt(usage.input_tokens))}</span></div>`
      : "") +
    (usage.output_tokens != null
      ? `<div class="info-row"><span class="k">Output tokens</span><span class="v">${esc(fmtInt(usage.output_tokens))}</span></div>`
      : "") +
    (usage.cost_usd != null
      ? `<div class="info-row"><span class="k">Cost</span><span class="v">$${esc(usage.cost_usd)}</span></div>`
      : "") +
    calls
      .map(
        (c) =>
          `<div class="info-row"><span class="k">${esc(c.agent)}</span><span class="v">${esc(c.model)} &middot; $${esc(c.cost_usd)}</span></div>`,
      )
      .join("") +
    `</div>
     </div>`;
  els["review-footer"].classList.remove("hidden");
  // Review-details disclosure: revealed on hover or keyboard focus, dismissed on
  // leave/blur, Esc closes. No click toggle — a click focuses the button (which
  // already reveals the popover via onfocus), so a toggle-on-click would close
  // what the focus just opened. button[aria-expanded]+aria-controls = disclosure
  // (not a dialog: no focus trap / focusable content to manage).
  const infoBtn = document.getElementById("info-btn");
  const infoPop = document.getElementById("info-pop");
  const infoWrap = infoBtn.parentElement;
  const showInfo = () => {
    infoPop.classList.remove("hidden");
    infoBtn.setAttribute("aria-expanded", "true");
  };
  const hideInfo = () => {
    infoPop.classList.add("hidden");
    infoBtn.setAttribute("aria-expanded", "false");
  };
  infoWrap.onmouseenter = showInfo;
  infoWrap.onmouseleave = hideInfo;
  infoBtn.onfocus = showInfo;
  infoBtn.onblur = hideInfo;
  infoBtn.onkeydown = (e) => {
    if (e.key === "Escape") infoBtn.blur();
  };
}

const eff = (f) => (f.verified_severity || f.severity || "low").toLowerCase();
const sevRank = (s) => ({ high: 3, medium: 2, low: 1, none: 0 })[s] || 0;
const canApplyFinding = (f) => {
  const s = eff(f);
  // span_faithful === false is the engine's authoritative signal that the model's quoted `span`
  // is NOT a verbatim substring of the document (a hallucinated/paraphrased citation, or language
  // that simply isn't there — e.g. a "clause deleted" finding). There is nothing to anchor a
  // tracked change to, so it cannot auto-apply; we present it as a manual/advisory suggestion
  // instead. This mirrors the engine's own redline_plan gate (`span_faithful is not False`);
  // true/null (whole-doc findings) stay applicable.
  return (
    (s === "high" || s === "medium") &&
    !!f.span &&
    !!f.suggested_language &&
    f.span_faithful !== false
  );
};

// Calm, accurate copy for a finding we can't auto-redline (no verbatim anchor in the document).
// Tailored by change_type so a "missing language" finding reads as guidance, not a failure.
function advisoryNote(f) {
  const ct = (f.change_type || "").toLowerCase();
  if (ct === "deletion" || ct === "absent")
    return "This language isn’t present in the document — add the suggested clause manually.";
  if (f.span_faithful === false)
    return "We couldn’t pin the exact original wording in the document, so this can’t be redlined automatically — review and apply it by hand.";
  return "Apply this suggested change manually.";
}

/* ---- apply redlines as tracked changes + comments ---- */
const wordApi14 = () => Office.context.requirements.isSetSupported("WordApi", "1.4");
// WordApi 1.6 adds Range.getTrackedChanges()/TrackedChangeCollection.acceptAll — needed to ACCEPT a
// tracked change from the pane. Gate the "Accept edit" button on it (older hosts accept in Review).
const wordApi16 = () => Office.context.requirements.isSetSupported("WordApi", "1.6");

async function enableTrackChanges(ctx) {
  // Returns true only if Track Changes is actually on — callers must NOT apply
  // edits to a legal document untracked.
  if (!wordApi14()) return false;
  try {
    ctx.document.changeTrackingMode = Word.ChangeTrackingMode.trackAll;
    await ctx.sync();
    return true;
  } catch (e) {
    return false;
  }
}

// Normalize text for tolerant matching: unify smart quotes/dashes, collapse all
// whitespace (incl. NBSP), lowercase. Lets a model's verbatim `span` match the
// document even when curly quotes, NBSPs, or line wrapping differ.
function normText(s) {
  return String(s == null ? "" : s)
    .replace(/[‘’‚‛′]/g, "'")
    .replace(/[“”„‟″]/g, '"')
    .replace(/[–—−]/g, "-")
    .replace(/\u00a0/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

// Locate the document Range for a model `span`. Returns {range} on a UNIQUE
// match, else {reason}. Two strategies, in order:
//   1. body.search — fast/exact, but capped at 255 chars by Word.
//   2. paragraph matching — tolerates clauses >255 chars and quote/whitespace
//      drift. Returns a precise sub-range (in-paragraph search for short spans,
//      or anchor + Range.expandTo for long ones); when no precise range can be
//      pinned it returns {paragraph, before, after} so the caller rebuilds the
//      paragraph as before + <new> + after (never drops surrounding text).
// Shared by Apply (replace) and — later — scroll-to / highlight.
// Unify one char for tolerant matching (smart quotes/dashes/NBSP -> ASCII).
function unifyChar(ch) {
  if ("‘’‚‛′".indexOf(ch) !== -1) return "'";
  if ("“”„‟″".indexOf(ch) !== -1) return '"';
  if ("–—−".indexOf(ch) !== -1) return "-";
  if (ch === "\u00a0") return " "; // NBSP -> space
  return ch;
}
// normText + a raw-index map[j] = raw index of the j-th normalized char, so a
// normalized match can recover exact document offsets.
function normMap(raw) {
  raw = String(raw == null ? "" : raw);
  let out = "";
  const map = [];
  let prev = true;
  for (let i = 0; i < raw.length; i++) {
    const ch = unifyChar(raw[i]);
    if (/\s/.test(ch)) {
      if (!prev) {
        out += " ";
        map.push(i);
        prev = true;
      }
    } else {
      out += ch.toLowerCase();
      map.push(i);
      prev = false;
    }
  }
  if (out.endsWith(" ")) {
    out = out.slice(0, -1);
    map.pop();
  }
  return { norm: out, map };
}
// Short word-boundary start/end anchors of a long clause (each <=255 chars) so
// two searches + Range.expandTo can select text Word's 255-char search cannot.
function clauseAnchors(s) {
  const K = 180;
  let pre = s.slice(0, K);
  const p2 = pre.lastIndexOf(" ");
  if (p2 >= 60) pre = pre.slice(0, p2);
  let suf = s.slice(Math.max(0, s.length - K));
  const s2 = suf.indexOf(" ");
  if (s2 > -1 && s2 <= suf.length - 60) suf = suf.slice(s2 + 1);
  return { pre, suf };
}
const esc6 = (s) => s.replace(/\^/g, "^^"); // ^ is a Word search control char

async function locateClauseRange(ctx, find) {
  const want = (find || "").trim();
  if (!want) return { reason: "empty span" };

  if (want.length <= 255) {
    const q = want.replace(/\^/g, "^^"); // ^ is a Word search control char (^p, ^t…)
    const hits = ctx.document.body.search(q, { matchCase: false, matchWildcards: false });
    hits.load("items");
    await ctx.sync();
    if (hits.items.length === 1) return { range: hits.items[0] };
    if (hits.items.length > 1) return { reason: "ambiguous (text appears multiple times)" };
    // 0 hits → fall through to paragraph matching (curly quotes / NBSP / wrapping)
  }

  const paras = ctx.document.body.paragraphs;
  paras.load("text");
  await ctx.sync();
  const wantN = normText(want);
  const matches = [];
  for (const p of paras.items) {
    const pn = normText(p.text);
    if (pn && (pn === wantN || pn.indexOf(wantN) !== -1)) matches.push({ p, pn });
  }
  if (matches.length === 0) {
    // The span may straddle paragraph breaks: a per-clause (Deep-mode) finding quotes a verbatim
    // substring of a clause whose paragraphs the engine joined, so no single paragraph contains it.
    // Try a cross-paragraph match before giving up.
    const cross = await locateAcrossParagraphs(ctx, paras.items, wantN);
    if (cross) return cross;
    return { reason: "clause text not found in the document (not a verbatim match)" };
  }
  if (matches.length > 1) return { reason: "ambiguous (the quoted clause appears more than once)" };

  // Recover the clause's EXACT raw offsets within its paragraph (from the
  // document's own text, so quote/whitespace drift can't break the replace).
  const p = matches[0].p;
  const pr = p.text;
  const nm = normMap(pr);
  const idx = nm.norm.indexOf(wantN);
  if (idx === -1) return { range: p.getRange("Content") }; // belt-and-suspenders
  const rawStart = nm.map[idx];
  const rawEnd = nm.map[idx + wantN.length - 1] + 1;
  const before = pr.slice(0, rawStart),
    after = pr.slice(rawEnd);
  const rawSpan = pr.slice(rawStart, rawEnd);
  if (!before.trim() && !after.trim()) return { range: p.getRange("Content") }; // whole paragraph

  // Precise sub-range using the document's own text (immune to quote drift).
  if (rawSpan.length <= 255) {
    const sub = p.search(esc6(rawSpan), { matchCase: false, matchWildcards: false });
    sub.load("items");
    await ctx.sync();
    if (sub.items.length === 1) return { range: sub.items[0] };
  } else {
    const a = clauseAnchors(rawSpan); // long clause: anchor + expandTo
    const s = p.search(esc6(a.pre), { matchCase: false, matchWildcards: false });
    const e = p.search(esc6(a.suf), { matchCase: false, matchWildcards: false });
    s.load("items");
    e.load("items");
    await ctx.sync();
    if (s.items.length === 1 && e.items.length === 1)
      return { range: s.items[0].expandTo(e.items[0]) };
  }
  // No precise sub-range -> caller rebuilds the whole paragraph (keeps before/after).
  return { paragraph: p, before, after };
}

// Plan a CROSS-PARAGRAPH match (pure). A Deep-mode per-clause finding quotes a verbatim substring
// of a clause whose paragraphs the engine joined, so the span can straddle paragraph breaks and no
// single-paragraph search can find it. Concatenate the document's normalized paragraphs (a break ==
// one normalized space, exactly how the span reads once normalized), locate the span across them,
// and map its ends back to (paragraph index, raw offset within that paragraph). Returns
// {startPi,startRaw,endPi,endRaw} for a UNIQUE cross-paragraph hit, {reason} when ambiguous, or null
// when not found or it actually sits inside one paragraph (handled by the single-paragraph path).
function planCrossParagraph(paraTexts, wantN) {
  if (!wantN) return null;
  const segs = [];
  let joined = "";
  for (let pi = 0; pi < paraTexts.length; pi++) {
    const nm = normMap(paraTexts[pi]);
    if (!nm.norm) continue;
    if (joined.length) joined += " ";
    segs.push({ pi, start: joined.length, nm });
    joined += nm.norm;
  }
  const first = joined.indexOf(wantN);
  if (first === -1) return null;
  if (joined.indexOf(wantN, first + 1) !== -1)
    return { reason: "ambiguous (the quoted clause appears more than once)" };
  const last = first + wantN.length - 1;
  const segAt = (pos) => segs.find((s) => pos >= s.start && pos < s.start + s.nm.norm.length);
  const a = segAt(first),
    b = segAt(last);
  if (!a || !b || a.pi === b.pi) return null; // single paragraph, or an endpoint fell on a separator
  return {
    startPi: a.pi,
    startRaw: a.nm.map[first - a.start],
    endPi: b.pi,
    endRaw: b.nm.map[last - b.start] + 1,
  };
}

// Re-pin a span INSIDE a bounded window of paragraphs (the imprecise cross-paragraph window). Same
// normalized concatenation as planCrossParagraph, but scoped to the window and WITHOUT the
// single-paragraph bail — the point is that a span which is ambiguous document-wide can be UNIQUE
// once the search is confined to this window, so a previously un-pinnable window becomes pinnable.
// Returns { startPi, startRaw, endPi, endRaw, startAnchor, endAnchor } (indices are into the passed
// paraTexts) for a unique hit, { reason } when missing/ambiguous, or null when an endpoint landed
// on a paragraph separator. Anchors are short RAW substrings (searched verbatim, so quote/dash
// drift matches the same way body.search does), unique within their own paragraph.
function windowPin(paraTexts, wantN) {
  if (!wantN) return null;
  const segs = [];
  let joined = "";
  for (let pi = 0; pi < paraTexts.length; pi++) {
    const nm = normMap(paraTexts[pi]);
    if (!nm.norm) continue;
    if (joined.length) joined += " ";
    segs.push({ pi, start: joined.length, nm });
    joined += nm.norm;
  }
  const first = joined.indexOf(wantN);
  if (first === -1) return { reason: "span not found inside the window" };
  if (joined.indexOf(wantN, first + 1) !== -1) return { reason: "ambiguous inside the window" };
  const last = first + wantN.length - 1;
  const segAt = (pos) => segs.find((s) => pos >= s.start && pos < s.start + s.nm.norm.length);
  const a = segAt(first),
    b = segAt(last);
  if (!a || !b) return null; // an endpoint fell on a separator
  const startPi = a.pi,
    startRaw = a.nm.map[first - a.start];
  const endPi = b.pi,
    endRaw = b.nm.map[last - b.start] + 1;
  return {
    startPi,
    startRaw,
    endPi,
    endRaw,
    startAnchor: rightAnchor(paraTexts[startPi], startRaw), // begins AT the span start
    endAnchor: leftAnchor(paraTexts[endPi], endRaw), // ends AT the span end
  };
}

// Resolve a cross-paragraph plan to a precise Word Range: a unique anchor at the span's start (in
// its first paragraph) and end (in its last), joined with expandTo across the middle paragraphs.
// Falls back to selecting the whole first..last paragraph window when a unique anchor can't be
// pinned — enough to navigate/highlight (apply then diffs within the window). Returns {range},
// {reason}, or null.
async function locateAcrossParagraphs(ctx, paraItems, wantN) {
  const plan = planCrossParagraph(
    paraItems.map((p) => p.text),
    wantN,
  );
  if (!plan) return null;
  if (plan.reason) return { reason: plan.reason };
  const startP = paraItems[plan.startPi],
    endP = paraItems[plan.endPi];
  const startAnchor = rightAnchor(startP.text, plan.startRaw); // unique, begins at the span start
  const endAnchor = leftAnchor(endP.text, plan.endRaw); // unique, ends at the span end
  if (startAnchor && endAnchor) {
    const s = startP.search(esc6(startAnchor), { matchCase: false, matchWildcards: false });
    const e = endP.search(esc6(endAnchor), { matchCase: false, matchWildcards: false });
    s.load("items");
    e.load("items");
    await ctx.sync();
    if (s.items.length === 1 && e.items.length === 1)
      return { range: s.items[0].expandTo(e.items[0]) };
  }
  // Couldn't pin a unique anchor — select the whole first..last paragraph window so navigation /
  // highlight still lands the user there, but mark it `imprecise` so APPLY refuses it (the window
  // can include unrelated text before/after the span, which must never be redlined automatically).
  // Carry the window's paragraph items + the normalized span so APPLY can retry a WINDOW-SCOPED pin
  // (rescueImpreciseWindow) before it refuses — the window may hold a unique anchor the whole-doc
  // search couldn't isolate. The range itself stays fine for navigation/highlight.
  return {
    range: startP.getRange("Start").expandTo(endP.getRange("End")),
    imprecise: true,
    windowParas: paraItems.slice(plan.startPi, plan.endPi + 1),
    wantN,
  };
}

/* ===== word-level redline diff (pure helpers) =====================
 * Replacing a whole clause makes Word strike the entire old text and insert the
 * entire new text. Instead we diff old vs new at word granularity and edit only
 * the differing runs, so the tracked changes read like a human redline. */
function diffTokenize(s) {
  return String(s == null ? "" : s).match(/\s+|\S+/g) || [];
}
function occurrences(hay, needle) {
  if (!needle) return 0;
  let c = 0,
    i = 0;
  while ((i = hay.indexOf(needle, i)) !== -1) {
    c++;
    i++;
  }
  return c;
}
// Token-level LCS diff -> coalesced ops [{op:'='|'-'|'+', text}].
function lcsOps(a, b) {
  const n = a.length,
    m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const ops = [];
  let i = 0,
    j = 0;
  const push = (op, t) => {
    const l = ops[ops.length - 1];
    if (l && l.op === op) l.text += t;
    else ops.push({ op, text: t });
  };
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      push("=", a[i]);
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      push("-", a[i]);
      i++;
    } else {
      push("+", b[j]);
      j++;
    }
  }
  while (i < n) push("-", a[i++]);
  while (j < m) push("+", b[j++]);
  return ops;
}

/* ===== inline redline preview (task pane) =========================
 * Render the suggested change as a word-level redline using the SAME token-LCS the apply
 * path uses (diffTokenize + lcsOps), so the preview matches the tracked changes "Apply
 * redline" will produce. Deleted words are struck (pastel red), inserted words highlighted
 * (pastel green), unchanged text plain. Returns SAFE HTML: every token is escaped before it
 * is wrapped, so clause text can never inject markup. Changed runs are grouped delete→insert
 * so a substitution reads like a human redline ("hold in <del>strict</del><ins>absolute</ins>
 * confidence") rather than interleaved fragments. */
function renderRedlineHTML(oldText, newText) {
  const a = String(oldText == null ? "" : oldText);
  const b = String(newText == null ? "" : newText);
  if (!a.trim()) return `<ins class="rl-ins">${esc(b)}</ins>`; // pure insertion (e.g. a deleted clause)
  if (a === b) return esc(b);
  const ao = diffTokenize(a),
    bo = diffTokenize(b);
  // Token-LCS is O(n*m); for a huge clause fall back to a single block-level redline.
  if (ao.length * bo.length > 200000)
    return `<del class="rl-del">${esc(a)}</del><ins class="rl-ins">${esc(b)}</ins>`;
  let html = "",
    del = "",
    ins = "";
  const flush = () => {
    if (del) html += `<del class="rl-del">${esc(del)}</del>`;
    if (ins) html += `<ins class="rl-ins">${esc(ins)}</ins>`;
    del = ins = "";
  };
  for (const o of lcsOps(ao, bo)) {
    if (o.op === "=") {
      flush();
      html += esc(o.text);
    } else if (o.op === "-") del += o.text;
    else ins += o.text;
  }
  flush();
  return html || esc(b);
}

// Shortest substring of `t` ending at `pos` (left) / starting at `pos` (right)
// that occurs exactly once in `t` (<=220 chars) -> a reliable search anchor.
function leftAnchor(t, pos) {
  if (pos <= 0) return null;
  for (let len = Math.min(8, pos); len <= Math.min(220, pos); len += 2) {
    const a = t.slice(pos - len, pos);
    if (occurrences(t, a) === 1) return a;
  }
  const a = t.slice(Math.max(0, pos - 220), pos);
  return occurrences(t, a) === 1 ? a : null;
}
function rightAnchor(t, pos) {
  const L = t.length;
  if (pos >= L) return null;
  for (let len = Math.min(8, L - pos); len <= Math.min(220, L - pos); len += 2) {
    const a = t.slice(pos, pos + len);
    if (occurrences(t, a) === 1) return a;
  }
  const a = t.slice(pos, Math.min(L, pos + 220));
  return occurrences(t, a) === 1 ? a : null;
}
// The single common-prefix/suffix-trimmed middle (used when word-by-word isn't apt).
function singleMiddle(oldText, newText) {
  const a = oldText,
    b = newText,
    mn = Math.min(a.length, b.length);
  let cp = 0;
  while (cp < mn && a[cp] === b[cp]) cp++;
  let cs = 0;
  while (cs < mn - cp && a[a.length - 1 - cs] === b[b.length - 1 - cs]) cs++;
  const r = { oldStart: cp, oldEnd: a.length - cs, newRegion: b.slice(cp, b.length - cs) };
  r.startAnchor = r.oldStart > 0 ? leftAnchor(oldText, r.oldStart) : null;
  r.endAnchor = r.oldEnd < oldText.length ? rightAnchor(oldText, r.oldEnd) : null;
  r.ok =
    (r.oldStart === 0 || r.startAnchor != null) &&
    (r.oldEnd === oldText.length || r.endAnchor != null);
  return r;
}
// Plan how to turn oldText into newText. Returns { regions }:
//   []    -> identical (no edit)
//   [r..] -> ordered, non-overlapping regions, each {oldStart,oldEnd,newRegion,
//            startAnchor,endAnchor} that the caller resolves to ranges + replaces
//   null  -> give up granular; caller does one whole-range replace
function planClauseDiff(oldText, newText) {
  if (oldText === newText) return { regions: [] };
  const ops = lcsOps(diffTokenize(oldText), diffTokenize(newText));
  const regions = [];
  let oldPos = 0,
    cur = null;
  for (const o of ops) {
    if (o.op === "=") {
      if (cur) {
        regions.push(cur);
        cur = null;
      }
      oldPos += o.text.length;
    } else {
      if (!cur) cur = { oldStart: oldPos, oldRegion: "", newRegion: "" };
      if (o.op === "-") {
        cur.oldRegion += o.text;
        oldPos += o.text.length;
      } else cur.newRegion += o.text;
    }
  }
  if (cur) regions.push(cur);
  for (const r of regions) {
    r.oldEnd = r.oldStart + r.oldRegion.length;
    r.startAnchor = r.oldStart > 0 ? leftAnchor(oldText, r.oldStart) : null;
    r.endAnchor = r.oldEnd < oldText.length ? rightAnchor(oldText, r.oldEnd) : null;
    r.ok =
      (r.oldStart === 0 || r.startAnchor != null) &&
      (r.oldEnd === oldText.length || r.endAnchor != null);
  }
  const common = ops.reduce((s, o) => s + (o.op === "=" ? o.text.length : 0), 0);
  const ratio = common / Math.max(oldText.length, newText.length, 1);
  // Word-by-word only with real shared structure (in-place edits); a near-total
  // rewrite reads better as one block, but still only the differing middle.
  if (ratio >= 0.5 && regions.length >= 1 && regions.length <= 20 && regions.every((r) => r.ok)) {
    return { regions };
  }
  const sm = singleMiddle(oldText, newText);
  return { regions: sm.ok ? [sm] : null };
}

// Diff-context fallback (pure): when a model's quoted `span` (oldText) is NOT a verbatim match in
// the document — paraphrase, or drift the normalizer doesn't cover — the UNCHANGED context words
// that BRACKET the actual edit usually still are verbatim. Diff span→suggestion and keep only
// changed regions that sit strictly INSIDE stable text (a start AND an end context anchor). Returns
// { ok:true, regions } or { ok:false, reason }. Refuses when the edit touches a span boundary
// (one-sided context can't be pinned uniquely) — so the caller never redlines text it can't
// bracket on both sides. Pure: the caller does the doc search + edit.
function selectContextAnchors(oldText, newText) {
  const plan = planClauseDiff(oldText, newText);
  if (!plan.regions || plan.regions.length === 0)
    return { ok: false, reason: "no changed region to anchor" };
  // Every changed region needs BOTH a start and an end context anchor: a region at the very
  // start/end of the span has one-sided context, which can't be uniquely pinned in the document.
  if (!plan.regions.every((r) => r.startAnchor && r.endAnchor))
    return { ok: false, reason: "edit touches the span edge — no two-sided context to anchor" };
  return { ok: true, regions: plan.regions };
}

// Apply a word-level diff to a located clause Range as granular tracked changes.
// All anchor searches run on the PRISTINE clause (before any edit); edits are
// then applied right-to-left so leftward ranges stay valid. Any anchor that
// doesn't resolve uniquely -> one safe whole-range replace instead.
async function applyDiffToRange(ctx, clause, oldText, newText, comment) {
  const { regions } = planClauseDiff(oldText, newText);
  if (regions && regions.length === 0) return; // identical
  const wholeReplace = async () => {
    const ins = clause.insertText(newText, Word.InsertLocation.replace);
    if (wordApi14()) ins.insertComment(comment || "");
    await ctx.sync();
  };
  if (!regions) {
    await wholeReplace();
    return;
  }

  const built = [];
  for (const r of regions) {
    const sb = r.startAnchor
      ? clause.search(esc6(r.startAnchor), { matchCase: false, matchWildcards: false })
      : null;
    const eb = r.endAnchor
      ? clause.search(esc6(r.endAnchor), { matchCase: false, matchWildcards: false })
      : null;
    if (sb) sb.load("items");
    if (eb) eb.load("items");
    built.push({ r, sb, eb });
  }
  await ctx.sync();
  if (built.some((b) => (b.sb && b.sb.items.length !== 1) || (b.eb && b.eb.items.length !== 1))) {
    await wholeReplace();
    return; // an anchor was ambiguous/missing in the doc
  }
  for (const b of built) {
    const startBound = b.sb ? b.sb.items[0].getRange("End") : clause.getRange("Start");
    const endBound = b.eb ? b.eb.items[0].getRange("Start") : clause.getRange("End");
    b.range = startBound.expandTo(endBound);
  }
  for (let i = built.length - 1; i >= 0; i--) {
    // right-to-left
    built[i].range.insertText(built[i].r.newRegion, Word.InsertLocation.replace);
  }
  if (wordApi14()) clause.insertComment(comment || "");
  await ctx.sync();
}

// Retry pinning an imprecise cross-paragraph window by scoping the search to JUST that window.
// windowPin re-derives short raw anchors at the span's true ends; we search for them inside the
// window's own paragraphs, and a unique hit on BOTH ends gives the exact span range. Returns
// { range } or null — never a partial/ambiguous range (safety: only redline what we pin uniquely).
async function rescueImpreciseWindow(ctx, loc) {
  const paras = loc.windowParas;
  if (!paras || !paras.length || !loc.wantN) return null;
  paras.forEach((p) => p.load("text"));
  await ctx.sync();
  const pin = windowPin(
    paras.map((p) => p.text),
    loc.wantN,
  );
  if (!pin || pin.reason || !pin.startAnchor || !pin.endAnchor) return null;
  const startP = paras[pin.startPi],
    endP = paras[pin.endPi];
  const s = startP.search(esc6(pin.startAnchor), { matchCase: false, matchWildcards: false });
  const e = endP.search(esc6(pin.endAnchor), { matchCase: false, matchWildcards: false });
  s.load("items");
  e.load("items");
  await ctx.sync();
  // startAnchor begins AT the span start, endAnchor ends AT the span end -> the span is exactly
  // [start-of-startHit .. end-of-endHit]. Both must be unique in their paragraph or we bail.
  if (s.items.length === 1 && e.items.length === 1)
    return { range: s.items[0].getRange("Start").expandTo(e.items[0].getRange("End")) };
  return null;
}

// Diff-context fallback: the model's `span` (oldText) isn't verbatim in the doc, but the UNCHANGED
// context bracketing the actual edit usually is. Search the document for each changed region's
// start+end context anchor (both must be UNIQUE), pin the region between them, and redline only that
// bracketed region. Refuses (returns {ok:false}) unless every anchor is unique AND the bracketed
// text is no bigger than the region we expected — anchors that matched in DIFFERENT clauses would
// bracket a huge span, which must never be auto-redlined. Returns {ok, reason?}.
async function applyByContextAnchors(ctx, oldText, newText, comment) {
  const sel = selectContextAnchors(oldText, newText);
  if (!sel.ok) return { ok: false, reason: sel.reason };
  const body = ctx.document.body;
  const built = [];
  for (const r of sel.regions) {
    const sb = body.search(esc6(r.startAnchor), { matchCase: false, matchWildcards: false });
    const eb = body.search(esc6(r.endAnchor), { matchCase: false, matchWildcards: false });
    sb.load("items");
    eb.load("items");
    built.push({ r, sb, eb });
  }
  await ctx.sync();
  for (const b of built)
    if (b.sb.items.length !== 1 || b.eb.items.length !== 1)
      return { ok: false, reason: "context anchor wasn't unique in the document" };
  // startAnchor ends AT the region start, endAnchor begins AT the region end -> the region is
  // [end-of-startHit .. start-of-endHit]. Load each bracketed text to sanity-check its size.
  const edits = [];
  for (const b of built) {
    const range = b.sb.items[0].getRange("End").expandTo(b.eb.items[0].getRange("Start"));
    range.load("text");
    edits.push({ range, r: b.r });
  }
  await ctx.sync();
  const SLACK = 80; // chars of tolerance for drift between the model's span and the doc text
  for (const e of edits) {
    const got = normText(e.range.text).length;
    const want = normText(e.r.oldRegion || "").length;
    if (got > want + SLACK) return { ok: false, reason: "context anchors bracket unexpected text" };
  }
  // Right-to-left so a leftward range stays valid after a rightward replace. Comment on the first
  // (leftmost) inserted range, mirroring applyDiffToRange.
  let firstIns = null;
  for (let i = edits.length - 1; i >= 0; i--) {
    const ins = edits[i].range.insertText(edits[i].r.newRegion, Word.InsertLocation.replace);
    if (i === 0) firstIns = ins;
  }
  if (wordApi14() && firstIns) firstIns.insertComment(comment || "");
  await ctx.sync();
  return { ok: true };
}

// Returns {ok, reason}. On a precise range, applies a word-level redline (only
// the changed words are struck/inserted). Falls back to a whole-paragraph
// rebuild only when no precise range could be pinned. Refuses only what it
// cannot locate uniquely -- never edits the wrong or ambiguous text.
async function applyEdit(ctx, edit) {
  try {
    // Inside the try: the cross-paragraph / context-anchor paths do expandTo + sync that can throw
    // on awkward structures, and that must degrade to a clean "couldn't place" — never an uncaught
    // error (which in Apply-all would abort the whole batch).
    const loc = await locateClauseRange(ctx, edit.find);
    if (loc.range) {
      loc.range.load("text");
      await ctx.sync();
      await applyDiffToRange(
        ctx,
        loc.range,
        loc.range.text,
        edit.replace || "",
        edit.comment || "",
      );
      return { ok: true };
    }
    if (loc.paragraph) {
      const target = loc.paragraph.getRange("Content");
      const ins = target.insertText(
        (loc.before || "") + (edit.replace || "") + (loc.after || ""),
        Word.InsertLocation.replace,
      );
      if (wordApi14()) ins.insertComment(edit.comment || "");
      await ctx.sync();
      return { ok: true };
    }
    if (loc.imprecise) {
      // Found the area but couldn't pin a UNIQUE sub-range across the boundary paragraphs. Before
      // refusing, retry a pin scoped to JUST that window — scoping can make an anchor that was
      // ambiguous document-wide unique — so we redline the exact span instead of nothing.
      const rescued = await rescueImpreciseWindow(ctx, loc);
      if (rescued && rescued.range) {
        rescued.range.load("text");
        await ctx.sync();
        await applyDiffToRange(
          ctx,
          rescued.range,
          rescued.range.text,
          edit.replace || "",
          edit.comment || "",
        );
        return { ok: true };
      }
      // else fall through to the diff-context fallback (still only redlines uniquely-pinned text).
    }
    // Last resort: the span isn't verbatim in the doc (model paraphrase / drift the normalizer
    // doesn't cover). Anchor on the UNCHANGED context that brackets the actual edit and redline only
    // the bracketed region — refuses unless every context anchor is unique.
    const anchored = await applyByContextAnchors(
      ctx,
      edit.find || "",
      edit.replace || "",
      edit.comment || "",
    );
    if (anchored.ok) return { ok: true };
    return { ok: false, reason: loc.reason || anchored.reason || "could not locate the clause" };
  } catch (e) {
    return { ok: false, reason: "Word rejected the edit: " + ((e && e.message) || e) };
  }
}

// The "applied" state shown once a redline is placed: just the green confirmation. Accepting the
// COUNTERPARTY's tracked change is a SEPARATE, pre-apply action (see acceptOne / the "Accept edit"
// button rendered in render()) — it is not offered here post-apply.
function appliedFragmentHTML(f) {
  return `<span class="applied">✓ applied — review the tracked change</span>`;
}

// "Accept edit" = accept what the document CURRENTLY has at this finding's location — i.e. the
// COUNTERPARTY's tracked change — and DISMISS the Review Engine's suggestion for that finding. This
// is deliberately NOT about accepting a tracked change WE just placed (see appliedFragmentHTML above,
// which no longer offers that). Requires WordApi 1.6 (Range.getTrackedChanges()/acceptAll). Never
// accepts on a range we couldn't pin uniquely — an `imprecise` cross-paragraph window is refused
// because it can bracket unrelated tracked changes before/after the actual span.
async function acceptOne(finding, btn) {
  const row = btn.closest && btn.closest(".apply-row");
  const msg = row ? row.querySelector(".apply-msg") : null;
  const say = (t, err) => {
    if (msg) {
      msg.textContent = t || "";
      msg.classList.toggle("error", !!err);
    } else setStatus(t, err);
  };
  if (!wordApi16()) {
    say("Accepting isn’t supported in this Word version — accept it in Word’s Review tab.");
    return;
  }
  if (!finding || !finding.span) {
    say("Nothing to accept for this finding.", true);
    return;
  }
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "Accepting…";
  say("");
  try {
    await Word.run(async (ctx) => {
      const loc = await locateClauseRange(ctx, finding.span);
      // Refuse an imprecise (whole cross-paragraph window) location — it can bracket unrelated
      // tracked changes, so accepting everything in it is not safe. Only a uniquely-pinned range
      // or a whole (non-imprecise) paragraph match is accepted.
      if (loc.imprecise || (!loc.range && !loc.paragraph)) {
        say("Couldn’t find this text — accept it in Word’s Review tab.");
        btn.disabled = false;
        btn.textContent = prev;
        return;
      }
      const range = loc.range || loc.paragraph.getRange("Content");
      const tcs = range.getTrackedChanges();
      tcs.load("items");
      await ctx.sync();
      if (tcs.items.length === 0) {
        say("No tracked change found here — nothing to accept.");
        btn.disabled = false;
        btn.textContent = prev;
        return;
      }
      tcs.acceptAll();
      await ctx.sync();
      finding._dismissed = true;
      if (row) {
        row.innerHTML = `<span class="applied">✓ counterparty edit kept — suggestion dismissed</span>`;
      }
    });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = prev;
    say("Couldn’t find this text — accept it in Word’s Review tab.");
  }
}

async function applyOne(finding, btn) {
  // Feedback goes inline next to the button (the finding is usually scrolled far
  // below the top status bar), falling back to the status bar if the row is gone.
  const row = btn.closest && btn.closest(".apply-row");
  const msg = row ? row.querySelector(".apply-msg") : null;
  const say = (t, err) => {
    if (msg) {
      msg.textContent = t || "";
      msg.classList.toggle("error", !!err);
    } else setStatus(t, err);
  };
  if (!finding || !finding.span || !finding.suggested_language) {
    say("Nothing to apply for this finding.", true);
    return;
  }
  if (applying) return;
  applying = true;
  btn.disabled = true;
  btn.textContent = "Applying…";
  say("");
  try {
    await Word.run(async (ctx) => {
      if (!(await enableTrackChanges(ctx))) {
        say(
          "Track Changes unavailable in this Word version — not applying (would be untracked).",
          true,
        );
        btn.disabled = false;
        btn.textContent = "Apply redline";
        return;
      }
      const sev = eff(finding);
      const res = await applyEdit(ctx, {
        find: finding.span,
        replace: finding.suggested_language,
        comment: `[${sev.toUpperCase()}] ${finding.title || ""} — ${finding.rationale || ""}`,
      });
      if (res.ok) {
        finding._applied = true;
        if (msg) msg.textContent = "";
        btn.outerHTML = appliedFragmentHTML(finding);
      } else {
        btn.disabled = false;
        btn.textContent = "Apply redline";
        // Calm, not a red error: the suggestion is still good, we just couldn't place it. The
        // Copy button beside this lets the user paste it in by hand.
        say("Couldn’t place this automatically — copy the text and apply it by hand.", false);
      }
    });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Apply redline";
    say("Apply failed: " + ((e && e.message) || e), true);
  } finally {
    applying = false;
  }
}

// Copy a finding's suggested language to the clipboard with a transient confirmation on the
// button, so a finding that can't be auto-redlined is still one click from a manual paste. Falls
// back to a hidden-textarea execCommand for Word webviews where navigator.clipboard is blocked.
async function copyText(text, btn) {
  const t = String(text == null ? "" : text);
  let ok = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(t);
      ok = true;
    }
  } catch (e) {
    ok = false; // clipboard API blocked in this webview — fall back below
  }
  if (!ok) {
    try {
      const ta = document.createElement("textarea");
      ta.value = t;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (e2) {
      ok = false;
    }
  }
  if (btn) {
    const prev = btn.dataset.label || btn.textContent;
    btn.dataset.label = prev;
    if (btn._copyTimer) clearTimeout(btn._copyTimer); // a rapid re-click restarts the window cleanly
    btn.textContent = ok ? "Copied ✓" : "Press Ctrl/⌘+C";
    btn.classList.toggle("copied", ok);
    btn._copyTimer = setTimeout(() => {
      btn.textContent = prev;
      btn.classList.remove("copied");
      btn._copyTimer = null;
    }, 1500);
  }
}

/* ---- briefly flash a finding's clause in the document when its card expands ----
 * Navigates to the clause (select → Word scrolls it into view) and pulses a highlight that
 * clears itself after a beat, so the eye lands on the right paragraph. COSMETIC and safe:
 *  - the pulse is applied ONLY when Track Changes is off, so it can never be recorded as a
 *    formatting revision; mid-redline (tracking on) the selection band is the cue instead.
 *  - it never edits content, and never runs while an apply is mutating the document.
 *  - a clause it cannot locate (e.g. one that was deleted) fails SILENTLY — no banner error.
 * Race-safe: rapid expands collapse to the latest one (older flashes skip on a stale seq). */
const FLASH_COLOR = "Yellow"; // a named color — Word Desktop snaps arbitrary #hex to its 16 named highlights
const FLASH_MS = 900;
let highlightQueue = Promise.resolve();
let highlightSeq = 0;
// Span of a highlight a flash set but could not clear in-line (its restore was prevented because
// Track Changes had flipped on, or the run's context died mid-wait). A later flash clears it.
let pendingFlash = null;

function highlightFinding(finding) {
  if (!finding || !finding.span) return Promise.resolve(); // span-less finding → nothing to locate
  const seq = ++highlightSeq;
  const run = () => flashClause(finding.span, seq);
  highlightQueue = highlightQueue.then(run, run); // serialize so flashes never overlap
  return highlightQueue;
}

async function flashClause(span, seq) {
  if (seq !== highlightSeq || applying || typeof Word === "undefined") return;
  await clearPendingFlash(); // remove any highlight an earlier flash couldn't clear
  try {
    await Word.run(async (ctx) => {
      const loc = await locateClauseRange(ctx, span);
      // Precise sub-range when we have one; otherwise the whole matched paragraph.
      const target = loc.range || (loc.paragraph ? loc.paragraph.getRange("Content") : null);
      if (!target) return; // can't locate (deleted/edited clause) — stay quiet
      target.select(); // selects + scrolls Word to the clause
      if (!(await isTrackingOff(ctx))) {
        await ctx.sync(); // tracking on → selection band is the cue; a highlight would be a tracked revision
        return;
      }
      target.font.highlightColor = FLASH_COLOR;
      pendingFlash = span; // we now own a live highlight on this clause
      await ctx.sync();
    });
  } catch (e) {
    return; // cosmetic navigation — never surface as an error
  }
  // Hold the pulse, then clear it in a SEPARATE run so a context death or a Track-Changes flip
  // during the wait can neither strand the highlight nor record its removal as a tracked revision.
  await new Promise((r) => setTimeout(r, FLASH_MS));
  await clearPendingFlash();
}

// True only when Track Changes is confirmed off, so a cosmetic highlight set/cleared here can
// never become a tracked formatting revision. Loads the mode on hosts that support it (WordApi 1.4).
async function isTrackingOff(ctx) {
  if (!wordApi14()) return true;
  ctx.document.load("changeTrackingMode");
  await ctx.sync();
  return ctx.document.changeTrackingMode === Word.ChangeTrackingMode.off;
}

// Clear a highlight a prior flash left set. Re-locates by span in a fresh run (robust to the
// original context dying) and clears only while tracking is off; if tracking is on, it keeps
// `pendingFlash` armed to retry on a later flash rather than record a tracked revision.
async function clearPendingFlash() {
  const span = pendingFlash;
  if (span == null || typeof Word === "undefined") return;
  try {
    await Word.run(async (ctx) => {
      if (!(await isTrackingOff(ctx))) return; // tracking on → leave armed, retry on a later flash
      const loc = await locateClauseRange(ctx, span);
      const target = loc.range || (loc.paragraph ? loc.paragraph.getRange("Content") : null);
      if (target) {
        target.font.highlightColor = null; // documented "remove highlight" (not "" / "NoColor")
        await ctx.sync();
      }
      pendingFlash = null; // cleared, or the clause is gone and can no longer be located to clear
    });
  } catch (e) {
    /* keep pendingFlash armed for a later retry */
  }
}

function applyAll() {
  // Serialize through a promise queue so rapid re-clicks can never race the
  // shared `applying` mutex or run two Word.run batches against the doc at once.
  applyAllQueue = applyAllQueue.then(applyAllInner, applyAllInner);
  return applyAllQueue;
}

// Idempotent: only applies findings not already applied (per-finding or a prior
// Apply-all) and not dismissed (via Accept edit — the user chose to keep the counterparty's
// tracked change there instead), so repeated clicks never duplicate edits or override a dismissal.
async function applyAllInner() {
  if (applying) return;
  const pending = renderedFindings.filter(
    (f) => canApplyFinding(f) && !f._applied && !f._dismissed,
  );
  if (!pending.length) {
    setStatus(
      renderedFindings.some(canApplyFinding)
        ? "All redlines already applied or dismissed — review the tracked changes before accepting."
        : "No redlines to apply.",
    );
    return;
  }
  applying = true;
  els["apply-all-btn"].disabled = true;
  document.querySelectorAll("button.apply").forEach((b) => {
    b.disabled = true;
  });
  setStatus(`Applying ${pending.length} redline(s)…`);
  let applied = 0,
    skipped = 0;
  try {
    await Word.run(async (ctx) => {
      if (!(await enableTrackChanges(ctx))) {
        setStatus(
          "Track Changes unavailable in this Word version — aborting (would be untracked).",
          true,
        );
        return;
      }
      for (const f of pending) {
        const sev = eff(f);
        const res = await applyEdit(ctx, {
          find: f.span,
          replace: f.suggested_language,
          comment: `[${sev.toUpperCase()}] ${f.title || ""} — ${f.rationale || ""}`,
        });
        if (res.ok) {
          f._applied = true;
          applied++;
          markFindingApplied(f);
        } else {
          skipped++;
          markFindingError(f);
        }
      }
    });
    setStatus(
      `Applied ${applied} redline(s)${skipped ? `, ${skipped} couldn’t be placed automatically (open them to copy the text)` : ""}. Review the tracked changes before accepting.`,
    );
  } catch (e) {
    setStatus("Apply failed: " + ((e && e.message) || e), true);
  } finally {
    applying = false;
    els["apply-all-btn"].disabled = false;
    document.querySelectorAll("button.apply").forEach((b) => {
      b.disabled = false;
    });
  }
}

function findingApplyButton(f) {
  return els.findings.querySelector('button.apply[data-i="' + f._i + '"]');
}
function markFindingApplied(f) {
  const b = findingApplyButton(f);
  if (!b) return;
  const row = b.closest(".apply-row"),
    m = row && row.querySelector(".apply-msg");
  if (m) m.textContent = "";
  b.outerHTML = appliedFragmentHTML(f);
}
function markFindingError(f) {
  const b = findingApplyButton(f);
  if (!b) return;
  const row = b.closest(".apply-row"),
    m = row && row.querySelector(".apply-msg");
  if (m) {
    // Calm "note", not a red error — the suggestion is good, we just couldn't auto-place it.
    m.textContent = "Couldn’t place automatically — copy the text and apply it by hand.";
    m.classList.remove("error");
    m.classList.add("note");
  }
}

// Node-only export of the pure redline/diff helpers for unit testing. Browsers have no `module`,
// so this is a no-op in the add-in (the functions stay plain globals there).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    normMap,
    unifyChar,
    planCrossParagraph,
    windowPin,
    diffTokenize,
    lcsOps,
    renderRedlineHTML,
    planClauseDiff,
    selectContextAnchors,
    esc,
    // async deep-review transport (pure shape/backoff/poll-interpretation helpers)
    isReviewBody,
    parseJsonSafe,
    nextPollDelayMs,
    jobOutcome,
  };
}
