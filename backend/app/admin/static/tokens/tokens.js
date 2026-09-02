/* Token-registry admin behaviour (P5 wave B, agent 2).
 *
 * CSP-clean: external file, addEventListener only, no inline handlers. Cookie-session auth carries
 * the double-submit CSRF token in the X-CSRF-Token header (read from the non-HttpOnly `csrf` cookie).
 * Three flows: create (list page), edit-metadata (detail page), usage-gated delete with typed
 * force-confirmation (detail page).
 */
(function () {
  "use strict";

  function getCookie(name) {
    var m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  function jsonFetch(url, method, body) {
    return fetch(url, {
      method: method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": getCookie("csrf")
      },
      body: body ? JSON.stringify(body) : undefined
    });
  }

  function setMsg(el, text, isError) {
    if (!el) { return; }
    el.textContent = text || "";
    el.className = isError ? "adm-error" : "adm-ok";
  }

  function errorText(payload) {
    if (payload && payload.error && payload.error.message) { return payload.error.message; }
    return "Something went wrong.";
  }

  function fieldValue(form, name) {
    var el = form.querySelector('[name="' + name + '"]');
    return el ? el.value : "";
  }

  // ---- Create (list page) --------------------------------------------------
  function wireCreate() {
    var form = document.getElementById("tok-create-form");
    if (!form) { return; }
    var msg = document.getElementById("tok-create-msg");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      setMsg(msg, "", false);
      var body = {
        name: fieldValue(form, "name").trim(),
        label: fieldValue(form, "label"),
        help_text: fieldValue(form, "help_text"),
        data_type: fieldValue(form, "data_type"),
        party: fieldValue(form, "party"),
        fallback_text: fieldValue(form, "fallback_text")
      };
      jsonFetch("/api/admin/tokens", "POST", body).then(function (resp) {
        return resp.json().then(function (payload) {
          if (resp.ok && payload.token) {
            window.location.href = "/admin/tokens/" + encodeURIComponent(payload.token.name);
          } else {
            setMsg(msg, errorText(payload), true);
          }
        });
      }).catch(function () { setMsg(msg, "Network error.", true); });
    });
  }

  // ---- Lazy usage (list page) ---------------------------------------------
  // "View usage" loads one token's usage report on demand (/api/admin/tokens/{name}/usage), so the
  // list page itself does NO template .docx scanning on load (that was the slow path).
  function renderUsage(u) {
    var tvs = (u && u.template_versions) || [];
    var fbs = (u && u.form_bindings) || [];
    var total = tvs.length + fbs.length;
    if (total === 0) { return "Not used in any template."; }
    var parts = tvs.map(function (tv) {
      var v = tv.template_name + " (" + tv.variant_code + ", v" + tv.version_no + ")";
      return tv.is_current ? v : v + " [old]";
    });
    return "Used in " + total + " place" + (total === 1 ? "" : "s") + ": " + parts.join("; ");
  }

  function wireUsage() {
    var table = document.querySelector(".adm-table");
    if (!table) { return; }
    table.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button.tok-usage-btn");
      if (!btn) { return; }
      var name = btn.getAttribute("data-token");
      var out = btn.parentElement.querySelector(".tok-usage-out");
      btn.disabled = true;
      if (out) { out.textContent = "Loading…"; out.className = "tok-usage-out adm-muted"; }
      jsonFetch("/api/admin/tokens/" + encodeURIComponent(name) + "/usage", "GET").then(function (resp) {
        return resp.json().then(function (payload) {
          if (resp.ok && payload.usage) {
            if (out) { out.textContent = renderUsage(payload.usage); out.className = "tok-usage-out"; }
            btn.remove();
          } else {
            if (out) { setMsg(out, errorText(payload), true); }
            btn.disabled = false;
          }
        });
      }).catch(function () {
        if (out) { setMsg(out, "Network error.", true); }
        btn.disabled = false;
      });
    });
  }

  // ---- Edit metadata (detail page) ----------------------------------------
  function wireEdit() {
    var form = document.getElementById("tok-edit-form");
    if (!form) { return; }
    var id = form.getAttribute("data-token-id");
    var msg = document.getElementById("tok-edit-msg");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      setMsg(msg, "", false);
      var body = {
        label: fieldValue(form, "label"),
        help_text: fieldValue(form, "help_text"),
        data_type: fieldValue(form, "data_type"),
        party: fieldValue(form, "party"),
        fallback_text: fieldValue(form, "fallback_text")
      };
      jsonFetch("/api/admin/tokens/" + encodeURIComponent(id), "PATCH", body).then(function (resp) {
        return resp.json().then(function (payload) {
          if (resp.ok) { setMsg(msg, "Saved.", false); }
          else { setMsg(msg, errorText(payload), true); }
        });
      }).catch(function () { setMsg(msg, "Network error.", true); });
    });
  }

  // ---- Usage-gated delete (detail page) -----------------------------------
  function wireDelete() {
    var btn = document.getElementById("tok-delete");
    if (!btn) { return; }
    var id = btn.getAttribute("data-token-id");
    var name = btn.getAttribute("data-token-name");
    var inUse = btn.getAttribute("data-in-use") === "1";
    var msg = document.getElementById("tok-delete-msg");
    var confirmInput = document.getElementById("tok-confirm-input");

    btn.addEventListener("click", function () {
      setMsg(msg, "", false);
      var force = inUse;
      var confirm = confirmInput ? confirmInput.value.trim() : "";
      if (inUse && confirm !== name) {
        setMsg(msg, "Type the token name exactly to confirm force-delete.", true);
        return;
      }
      btn.disabled = true;
      jsonFetch("/api/admin/tokens/" + encodeURIComponent(id) + "/delete", "POST", {
        force: force, confirm: confirm
      }).then(function (resp) {
        return resp.json().then(function (payload) {
          if (resp.ok && payload.deleted) {
            window.location.href = "/admin/tokens";
          } else if (resp.ok && !payload.deleted) {
            setMsg(msg, "This token is still in use — force-delete required.", true);
            btn.disabled = false;
          } else {
            setMsg(msg, errorText(payload), true);
            btn.disabled = false;
          }
        });
      }).catch(function () { setMsg(msg, "Network error.", true); btn.disabled = false; });
    });
  }

  function start() { wireCreate(); wireUsage(); wireEdit(); wireDelete(); }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
