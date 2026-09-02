/* Shared admin behaviour (CSP forbids inline JS). Exposes window.adm helpers used by every page:
 *  - adm.csrf()      -> the CSRF token (meta tag, falling back to the readable `csrf` cookie),
 *  - adm.jfetch()    -> fetch() with same-origin creds + JSON + the X-CSRF-Token header on writes,
 *  - adm.errText()   -> pull a human message out of the standard {"error":{message}} envelope.
 * Also wires the "Sign out" button present in the shell header.
 */
(function () {
  "use strict";

  function readCookie(name) {
    var m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  function csrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    var v = meta ? (meta.getAttribute("content") || "") : "";
    return v || readCookie("csrf");
  }

  async function errText(resp, fallback) {
    try {
      var body = await resp.json();
      if (body && body.error && body.error.message) { return body.error.message; }
    } catch (e) { /* non-JSON body */ }
    return fallback || "Something went wrong. Please try again.";
  }

  /* JSON fetch helper. `opts.method` defaults to GET; writes carry the CSRF header + JSON body. */
  function jfetch(url, opts) {
    opts = opts || {};
    var method = (opts.method || "GET").toUpperCase();
    var headers = { "Accept": "application/json" };
    if (method !== "GET" && method !== "HEAD") {
      headers["X-CSRF-Token"] = csrf();
      if (opts.body !== undefined && !(opts.body instanceof FormData)) {
        headers["Content-Type"] = "application/json";
      }
    }
    if (opts.headers) {
      Object.keys(opts.headers).forEach(function (k) { headers[k] = opts.headers[k]; });
    }
    return fetch(url, {
      method: method,
      credentials: "same-origin",
      headers: headers,
      body: opts.body
    });
  }

  window.adm = { csrf: csrf, jfetch: jfetch, errText: errText, readCookie: readCookie };

  function wireLogout() {
    var btn = document.getElementById("adm-logout");
    if (!btn) { return; }
    btn.addEventListener("click", function () {
      jfetch("/api/auth/logout", { method: "POST", body: "{}" })
        .then(function () { window.location.href = "/admin/login"; })
        .catch(function () { window.location.href = "/admin/login"; });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireLogout);
  } else {
    wireLogout();
  }
})();
