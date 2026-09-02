/* Admin login page (CSP forbids inline JS). Drives the hardened session API (/api/auth/*):
 *  - on load, checks /api/auth/me: an already-signed-in admin who does NOT need a password change is
 *    bounced straight to `?next` (or /admin/); one who must change lands on the change-password form;
 *  - the login form POSTs /api/auth/login; a must_change_password response reveals the change form;
 *  - the change form POSTs /api/auth/password/change (which rotates the session), then proceeds.
 * All failures render the API's envelope message; a bad login is the generic "invalid credentials".
 */
(function () {
  "use strict";

  function nextTarget() {
    try {
      var params = new URLSearchParams(window.location.search);
      var n = params.get("next") || "";
      // Only same-origin admin paths — never an open redirect.
      if (n && n.charAt(0) === "/" && n.indexOf("//") !== 0 && n.indexOf("/admin") === 0) {
        return n;
      }
    } catch (e) { /* ignore */ }
    return "/admin/";
  }

  function csrf() {
    var m = document.cookie.match(/(?:^|; )csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function show(el, on) { if (el) { el.hidden = !on; } }
  function setError(el, msg) {
    if (!el) { return; }
    el.textContent = msg || "";
    el.hidden = !msg;
  }

  async function envMessage(resp, fallback) {
    try {
      var body = await resp.json();
      if (body && body.error && body.error.message) { return body.error.message; }
    } catch (e) { /* ignore */ }
    return fallback;
  }

  var loginForm, changeForm, loginErr, changeErr;

  function revealChange() {
    show(loginForm, false);
    show(changeForm, true);
    var f = document.getElementById("change-old");
    if (f) { f.focus(); }
  }

  function proceed() { window.location.href = nextTarget(); }

  function onLogin(ev) {
    ev.preventDefault();
    setError(loginErr, "");
    var submit = document.getElementById("adm-login-submit");
    if (submit) { submit.disabled = true; }
    var user = document.getElementById("login-user").value;
    var pass = document.getElementById("login-pass").value;
    fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ user_id: user, password: pass })
    }).then(function (resp) {
      if (submit) { submit.disabled = false; }
      if (!resp.ok) {
        return envMessage(resp, "Invalid user ID or password.").then(function (m) {
          setError(loginErr, m);
        });
      }
      return resp.json().then(function (body) {
        if (body && body.must_change_password) { revealChange(); }
        else { proceed(); }
      });
    }).catch(function () {
      if (submit) { submit.disabled = false; }
      setError(loginErr, "Could not reach the server. Please try again.");
    });
  }

  function onChange(ev) {
    ev.preventDefault();
    setError(changeErr, "");
    var submit = document.getElementById("adm-change-submit");
    if (submit) { submit.disabled = true; }
    var oldp = document.getElementById("change-old").value;
    var newp = document.getElementById("change-new").value;
    fetch("/api/auth/password/change", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": csrf()
      },
      body: JSON.stringify({ old_password: oldp, new_password: newp })
    }).then(function (resp) {
      if (submit) { submit.disabled = false; }
      if (!resp.ok) {
        return envMessage(resp, "Could not update the password.").then(function (m) {
          setError(changeErr, m);
        });
      }
      proceed();
    }).catch(function () {
      if (submit) { submit.disabled = false; }
      setError(changeErr, "Could not reach the server. Please try again.");
    });
  }

  function start() {
    loginForm = document.getElementById("adm-login-form");
    changeForm = document.getElementById("adm-change-form");
    loginErr = document.getElementById("adm-login-error");
    changeErr = document.getElementById("adm-change-error");
    if (loginForm) { loginForm.addEventListener("submit", onLogin); }
    if (changeForm) { changeForm.addEventListener("submit", onChange); }

    // Already signed in? Skip the form (or jump to the change step).
    fetch("/api/auth/me", { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (resp) {
        if (!resp.ok) { return null; }
        return resp.json();
      })
      .then(function (me) {
        if (!me) { return; }
        if (me.must_change_password) { revealChange(); }
        else { proceed(); }
      })
      .catch(function () { /* stay on the login form */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
