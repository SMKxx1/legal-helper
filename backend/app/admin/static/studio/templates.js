/* Templates list behaviour (CSP forbids inline JS). Two actions, both CSRF-guarded via adm.jfetch:
 *  - upload a .docx for a (template, variant) slot → creates a DRAFT version, then opens the studio;
 *  - "Make current" → rolls a prior version back to current (re-points is_current + emits drift).
 */
(function () {
  "use strict";
  if (!window.adm) { return; }

  function slotMsg(form, text) {
    var el = form.querySelector(".tpl-upload-msg");
    if (!el) { return; }
    el.textContent = text || "";
    el.hidden = !text;
  }

  document.querySelectorAll(".tpl-upload").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      slotMsg(form, "");
      var input = form.querySelector(".tpl-file");
      var btn = form.querySelector("button[type=submit]");
      if (!input || !input.files || input.files.length === 0) {
        slotMsg(form, "Choose a .docx file first.");
        return;
      }
      var fd = new FormData();
      fd.append("file", input.files[0]);
      if (btn) { btn.disabled = true; }
      var url = "/admin/templates/" + encodeURIComponent(form.dataset.templateId) +
        "/" + encodeURIComponent(form.dataset.variant) + "/upload";
      adm.jfetch(url, { method: "POST", body: fd }).then(function (resp) {
        if (resp.ok) {
          return resp.json().then(function (body) {
            window.location.href = body.studio_url || "/admin/templates";
          });
        }
        if (btn) { btn.disabled = false; }
        return adm.errText(resp, "Upload failed.").then(function (m) { slotMsg(form, m); });
      }).catch(function () {
        if (btn) { btn.disabled = false; }
        slotMsg(form, "Could not reach the server.");
      });
    });
  });

  // ---- Bulk upload modal ---------------------------------------------------
  var bulkOpen = document.getElementById("tpl-bulk-open");
  var bulkModal = document.getElementById("tpl-bulk-modal");
  var bulkClose = document.getElementById("tpl-bulk-close");
  var bulkForm = document.getElementById("tpl-bulk-form");
  if (bulkOpen && bulkModal) {
    bulkOpen.addEventListener("click", function () { bulkModal.hidden = false; });
    if (bulkClose) {
      bulkClose.addEventListener("click", function () { bulkModal.hidden = true; });
    }
    bulkModal.addEventListener("click", function (ev) {
      if (ev.target === bulkModal) { bulkModal.hidden = true; }
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && !bulkModal.hidden) { bulkModal.hidden = true; }
    });
  }
  if (bulkForm) {
    var bulkFiles = document.getElementById("tpl-bulk-files");
    var bulkMsg = document.getElementById("tpl-bulk-msg");
    var bulkResults = document.getElementById("tpl-bulk-results");

    function renderBulkResults(results) {
      if (!bulkResults) { return; }
      bulkResults.textContent = "";
      var ok = 0;
      results.forEach(function (r) {
        var li = document.createElement("li");
        li.className = r.ok ? "tpl-bulk-ok" : "tpl-bulk-err";
        var name = document.createElement("code");
        name.textContent = r.filename;
        li.appendChild(name);
        var detail = document.createElement("span");
        if (r.ok) {
          ok++;
          detail.textContent = " → " + r.combo + " · " + r.variant + " · v" + r.version_no;
        } else {
          detail.textContent = " — " + (r.error || "failed");
        }
        li.appendChild(detail);
        bulkResults.appendChild(li);
      });
      var summary = document.createElement("li");
      summary.className = "tpl-bulk-summary";
      summary.textContent = ok + " of " + results.length +
        " uploaded as drafts. Reload the page to see them.";
      bulkResults.appendChild(summary);
    }

    bulkForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (bulkMsg) { bulkMsg.textContent = ""; }
      if (bulkResults) { bulkResults.textContent = ""; }
      if (!bulkFiles || !bulkFiles.files || bulkFiles.files.length === 0) {
        if (bulkMsg) { bulkMsg.textContent = "Choose one or more .docx files."; }
        return;
      }
      var fd = new FormData();
      for (var i = 0; i < bulkFiles.files.length; i++) {
        fd.append("files", bulkFiles.files[i]);
      }
      var bulkVariant = document.getElementById("tpl-bulk-variant");
      fd.append("variant", bulkVariant ? bulkVariant.value : "empty");
      var submitBtn = bulkForm.querySelector("button[type=submit]");
      if (submitBtn) { submitBtn.disabled = true; }
      if (bulkMsg) { bulkMsg.textContent = "Uploading…"; }
      adm.jfetch("/admin/templates/bulk-upload", { method: "POST", body: fd }).then(function (resp) {
        if (submitBtn) { submitBtn.disabled = false; }
        if (!resp.ok) {
          return adm.errText(resp, "Bulk upload failed.").then(function (m) {
            if (bulkMsg) { bulkMsg.textContent = m; }
          });
        }
        return resp.json().then(function (body) {
          if (bulkMsg) { bulkMsg.textContent = ""; }
          renderBulkResults(body.results || []);
        });
      }).catch(function () {
        if (submitBtn) { submitBtn.disabled = false; }
        if (bulkMsg) { bulkMsg.textContent = "Could not reach the server."; }
      });
    });
  }

  document.querySelectorAll(".tpl-rollback").forEach(function (btn) {
    btn.addEventListener("click", function () {
      btn.disabled = true;
      var url = "/admin/templates/versions/" + encodeURIComponent(btn.dataset.versionId) + "/rollback";
      adm.jfetch(url, { method: "POST", body: "{}" }).then(function (resp) {
        if (resp.ok) { window.location.reload(); return; }
        btn.disabled = false;
        return adm.errText(resp, "Rollback failed.").then(function (m) { window.alert(m); });
      }).catch(function () {
        btn.disabled = false;
        window.alert("Could not reach the server.");
      });
    });
  });
})();
