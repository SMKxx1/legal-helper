/* Bot access-control console (PLAN §3.4 rework). CSP forbids inline JS, so all behaviour lives here.
 * Drives the require_admin JSON endpoints:
 *   GET/POST /api/admin/allowlist, DELETE /api/admin/allowlist/{id}
 *   GET /api/admin/pending
 *   GET/PUT /api/admin/admin-routing
 * User-controlled values are written with textContent (never innerHTML) so a crafted principal/label
 * can never inject markup. */
(function () {
  "use strict";
  if (!document.getElementById("access-page")) { return; }

  var statusEl = document.getElementById("access-status");

  function flash(msg, isError) {
    if (!statusEl) { return; }
    statusEl.textContent = msg;
    statusEl.hidden = false;
    statusEl.className = "adm-note" + (isError ? " adm-note-error" : " adm-note-ok");
  }

  function cell(text) {
    var td = document.createElement("td");
    td.textContent = text == null ? "" : String(text);
    return td;
  }

  function emptyRow(tbody, span, text) {
    var tr = document.createElement("tr");
    var td = document.createElement("td");
    td.colSpan = span;
    td.className = "adm-empty";
    td.textContent = text;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  // ---- Allowlist ----------------------------------------------------------
  function loadAllowlist() {
    var tbody = document.getElementById("allowlist-rows");
    return adm.jfetch("/api/admin/allowlist")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        tbody.textContent = "";
        var rows = (data && data.allowlist) || [];
        if (!rows.length) { emptyRow(tbody, 6, "No allowlist entries yet."); return; }
        rows.forEach(function (row) {
          var tr = document.createElement("tr");
          tr.appendChild(cell(row.principal_type));
          tr.appendChild(cell(row.principal_key));
          tr.appendChild(cell(row.role));
          tr.appendChild(cell(row.label || "—"));
          tr.appendChild(cell(row.added_by || "—"));
          var actions = document.createElement("td");
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "adm-btn adm-btn-ghost adm-btn-sm";
          btn.textContent = "Remove";
          btn.setAttribute("data-remove", row.id);
          actions.appendChild(btn);
          tr.appendChild(actions);
          tbody.appendChild(tr);
        });
      })
      .catch(function () { tbody.textContent = ""; emptyRow(tbody, 6, "Couldn't load the allowlist."); });
  }

  function addAllowlist(e) {
    e.preventDefault();
    var body = {
      principal_type: document.getElementById("al-type").value,
      principal_key: document.getElementById("al-key").value.trim(),
      role: document.getElementById("al-role").value,
      label: document.getElementById("al-label").value.trim()
    };
    if (!body.principal_key) { flash("Enter a Slack user ID or email.", true); return; }
    adm.jfetch("/api/admin/allowlist", { method: "POST", body: JSON.stringify(body) })
      .then(function (r) {
        if (!r.ok) { return adm.errText(r, "Couldn't add that entry.").then(function (m) { throw new Error(m); }); }
        document.getElementById("al-key").value = "";
        document.getElementById("al-label").value = "";
        flash("Saved.", false);
        return loadAllowlist();
      })
      .catch(function (err) { flash(err.message || "Couldn't add that entry.", true); });
  }

  function onAllowlistClick(e) {
    var id = e.target && e.target.getAttribute && e.target.getAttribute("data-remove");
    if (!id) { return; }
    adm.jfetch("/api/admin/allowlist/" + encodeURIComponent(id), { method: "DELETE" })
      .then(function (r) {
        if (!r.ok) { return adm.errText(r, "Couldn't remove that entry.").then(function (m) { throw new Error(m); }); }
        flash("Removed.", false);
        return loadAllowlist();
      })
      .catch(function (err) { flash(err.message || "Couldn't remove that entry.", true); });
  }

  // ---- Pending ------------------------------------------------------------
  function loadPending() {
    var tbody = document.getElementById("pending-rows");
    return adm.jfetch("/api/admin/pending")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        tbody.textContent = "";
        var rows = (data && data.pending) || [];
        if (!rows.length) { emptyRow(tbody, 6, "No pending requests."); return; }
        rows.forEach(function (row) {
          var tr = document.createElement("tr");
          tr.appendChild(cell(row.requester));
          tr.appendChild(cell(row.channel));
          tr.appendChild(cell(row.intent));
          tr.appendChild(cell(row.status));
          tr.appendChild(cell(row.has_document ? "yes" : "—"));
          tr.appendChild(cell(row.created_at ? row.created_at.replace("T", " ").slice(0, 16) : ""));
          tbody.appendChild(tr);
        });
      })
      .catch(function () { tbody.textContent = ""; emptyRow(tbody, 6, "Couldn't load pending requests."); });
  }

  // ---- Routing ------------------------------------------------------------
  function loadRouting() {
    return adm.jfetch("/api/admin/admin-routing")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        document.getElementById("routing-channel").value = (data && data.nda_admin_slack_channel) || "";
        document.getElementById("routing-email").value = (data && data.nda_admin_email) || "";
      })
      .catch(function () { /* leave inputs blank */ });
  }

  function saveRouting(e) {
    e.preventDefault();
    var body = {
      nda_admin_slack_channel: document.getElementById("routing-channel").value.trim(),
      nda_admin_email: document.getElementById("routing-email").value.trim()
    };
    adm.jfetch("/api/admin/admin-routing", { method: "PUT", body: JSON.stringify(body) })
      .then(function (r) {
        if (!r.ok) { return adm.errText(r, "Couldn't save routing.").then(function (m) { throw new Error(m); }); }
        flash("Routing saved.", false);
      })
      .catch(function (err) { flash(err.message || "Couldn't save routing.", true); });
  }

  function init() {
    document.getElementById("allowlist-form").addEventListener("submit", addAllowlist);
    document.getElementById("allowlist-rows").addEventListener("click", onAllowlistClick);
    document.getElementById("routing-form").addEventListener("submit", saveRouting);
    loadRouting();
    loadAllowlist();
    loadPending();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
