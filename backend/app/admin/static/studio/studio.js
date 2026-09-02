/* Template studio editor (PLAN §3.7). CSP forbids inline JS; all behaviour is wired here via
 * addEventListener + event delegation, so a re-rendered doc/checklist/findmap needs no re-binding.
 *
 * Core interaction: highlight text in the document → click a palette token → the server replaces the
 * exact character span with {{token}} (studio.tokenize_ops). Offsets are recovered with the
 * PART-LENGTH model: every child of a .stu-seg is a part node (<span class="stu-run">, or an atomic
 * <span class="stu-tok"> chip rendering a friendly label) carrying data-plen = the characters it
 * occupies in docview.paragraph_text. A boundary offset = the sum of data-plen over the parts before
 * it, plus the intra-run text offset (a run's rendered text IS its underlying text, so that maps
 * 1:1); a boundary inside a token chip snaps to the chip's start/end — never textContent, which now
 * differs from paragraph_text wherever a chip renders its label.
 * A stale-view 409 (studio_stale_view) triggers ONE auto re-extract + retry, then surfaces.
 */
(function () {
  "use strict";

  var root = document.getElementById("stu-root");
  if (!root || !window.adm) { return; }

  var versionId = root.dataset.versionId;
  var state = { hash: root.dataset.viewHash };

  var doc = document.getElementById("stu-doc");
  var palette = document.getElementById("stu-palette");
  var findmap = document.getElementById("stu-findmap");
  var checklist = document.getElementById("stu-checklist");
  var flash = document.getElementById("stu-flash");

  function setFlash(msg, isErr) {
    if (!flash) { return; }
    flash.textContent = msg;
    flash.hidden = !msg;
    flash.classList.toggle("stu-flash-error", !!isErr);
  }
  function clearFlash() { setFlash("", false); }
  function setDisabled(id, off) {
    var el = document.getElementById(id);
    if (el) { el.disabled = !!off; }
  }

  /* ---- selection → (locator, start, end) --------------------------------- */
  function segOf(node) {
    while (node && node !== doc) {
      if (node.nodeType === 1 && node.classList && node.classList.contains("stu-seg")) {
        return node;
      }
      node = node.parentNode;
    }
    return null;
  }
  function plenOf(el) {
    var v = el && el.dataset ? parseInt(el.dataset.plen, 10) : NaN;
    return isNaN(v) ? 0 : v;
  }
  function segPlen(seg) {
    var sum = 0, kids = seg.childNodes;
    for (var i = 0; i < kids.length; i++) {
      if (kids[i].nodeType === 1) { sum += plenOf(kids[i]); }
    }
    return sum;
  }
  function offsetInSeg(seg, node, off, isEnd) {
    // The char offset in paragraph_text of the boundary (node, off): sum data-plen across the
    // seg's part nodes before the boundary — NEVER Range.toString() across parts, because token
    // chips render a friendly label whose length differs from the underlying "{{name}}".
    if (node === seg) {
      // Boundary between the seg's children (e.g. triple-click): off is a child index.
      var head = 0, kids = seg.childNodes, n = Math.min(off, kids.length);
      for (var k = 0; k < n; k++) { if (kids[k].nodeType === 1) { head += plenOf(kids[k]); } }
      return head;
    }
    var child = node; // climb to the seg's direct child containing the boundary
    while (child.parentNode && child.parentNode !== seg) { child = child.parentNode; }
    if (child.parentNode !== seg) {
      // Boundary outside this seg (e.g. a drag past the paragraph into inter-seg whitespace):
      // clamp to the seg's start or end by document position — never guess an inner offset.
      var pos = seg.compareDocumentPosition(node);
      return (pos & Node.DOCUMENT_POSITION_PRECEDING) ? 0 : segPlen(seg);
    }
    var sum = 0, parts = seg.childNodes;
    for (var i = 0; i < parts.length && parts[i] !== child; i++) {
      if (parts[i].nodeType === 1) { sum += plenOf(parts[i]); }
    }
    if (child.nodeType !== 1) { return sum; } // stray text node: occupies no underlying chars
    if (child.classList.contains("stu-tok")) {
      // Atomic chip: a boundary inside it snaps to the chip's start (selection start) or its
      // end (selection end) — never mid-token, whatever the label length.
      if (node === child) { return sum + (off > 0 ? plenOf(child) : 0); }
      return sum + (isEnd ? plenOf(child) : 0);
    }
    // Run part: its rendered text length == data-plen, so a Range INSIDE it maps 1:1.
    var r = document.createRange();
    r.setStart(child, 0);
    r.setEnd(node, off);
    return sum + r.toString().length;
  }
  function currentSelection() {
    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) { return null; }
    var rng = sel.getRangeAt(0);
    var startSeg = segOf(rng.startContainer);
    if (!startSeg) { return null; }
    var endSeg = segOf(rng.endContainer) || startSeg;
    var start = offsetInSeg(startSeg, rng.startContainer, rng.startOffset, false);
    var end = offsetInSeg(endSeg, rng.endContainer, rng.endOffset, true);
    if (startSeg === endSeg && start > end) { var t = start; start = end; end = t; }
    return {
      locator: startSeg.dataset.locator,
      endLocator: endSeg.dataset.locator,
      start: start,
      end: end
    };
  }

  /* ---- render a returned state payload ----------------------------------- */
  function applyState(st) {
    state.hash = st.content_hash;
    root.dataset.viewHash = st.content_hash;
    if (doc) { doc.innerHTML = st.doc_html; }
    if (checklist) { checklist.innerHTML = st.checklist_html; }
    if (findmap) { findmap.innerHTML = st.findmap_html; }
    setDisabled("stu-undo", !st.can_undo);
    setDisabled("stu-redo", !st.can_redo);
    setDisabled("stu-publish", !st.publishable);
    root.dataset.publishable = st.publishable ? "1" : "0";
  }

  /* ---- op poster with the single stale-view re-extract + retry ----------- */
  function op(url, body, allowRetry) {
    var payload = body === undefined ? "{}" : JSON.stringify(body);
    return adm.jfetch(url, { method: "POST", body: payload }).then(function (resp) {
      if (resp.ok) {
        return resp.json().then(function (st) { applyState(st); clearFlash(); });
      }
      return resp.json().then(function (err) {
        var code = err && err.error && err.error.code;
        if (code === "studio_stale_view" && allowRetry) {
          return refreshThenRetry(url, body);
        }
        var msg = (err && err.error && err.error.message) || "Action failed.";
        setFlash(msg, true);
      });
    }).catch(function () { setFlash("Could not reach the server.", true); });
  }

  function refreshThenRetry(url, body) {
    return adm.jfetch("/admin/studio/" + versionId + "/state")
      .then(function (resp) { return resp.json(); })
      .then(function (st) {
        applyState(st);
        var retryBody = body ? Object.assign({}, body, { view_hash: state.hash }) : body;
        return op(url, retryBody, false); // retry EXACTLY once, then surface
      });
  }

  /* ---- tokenize on palette click (with an active selection) -------------- */
  function tokenize(token) {
    var selo = currentSelection();
    if (!selo) { return false; }
    op("/admin/studio/" + versionId + "/tokenize", {
      locator: selo.locator,
      end_locator: selo.endLocator,
      start: selo.start,
      end: selo.end,
      token: token,
      view_hash: state.hash
    }, true);
    window.getSelection().removeAllRanges();
    return true;
  }

  /* ---- token details panel ---------------------------------------------- */
  function showDetails(chip) {
    var panel = document.getElementById("stu-details");
    if (!panel) { return; }
    document.getElementById("stu-details-name").textContent = chip.dataset.label || chip.dataset.token;
    document.getElementById("stu-details-code").textContent = chip.dataset.placeholder;
    document.getElementById("stu-details-help").textContent = chip.dataset.help || "No description.";
    document.getElementById("stu-details-type").textContent = chip.dataset.type || "text";
    document.getElementById("stu-details-party").textContent = chip.dataset.party || "internal";
    panel.dataset.placeholder = chip.dataset.placeholder;
    panel.hidden = false;
  }

  if (palette) {
    palette.addEventListener("click", function (ev) {
      var chip = ev.target.closest(".stu-chip");
      if (!chip) { return; }
      if (!tokenize(chip.dataset.token)) { showDetails(chip); }
    });
  }

  var copyBtn = document.getElementById("stu-details-copy");
  if (copyBtn) {
    copyBtn.addEventListener("click", function () {
      var panel = document.getElementById("stu-details");
      var text = panel ? (panel.dataset.placeholder || "") : "";
      if (navigator.clipboard && text) {
        navigator.clipboard.writeText(text).then(function () {
          setFlash("Copied " + text, false);
        }).catch(function () { setFlash("Copy failed.", true); });
      }
    });
  }

  /* ---- find & map accept (delegated: findmap re-renders on every op) ----- */
  if (findmap) {
    findmap.addEventListener("click", function (ev) {
      var btn = ev.target.closest(".stu-fm-accept");
      if (!btn) { return; }
      var li = btn.closest(".stu-fm-item");
      if (!li) { return; }
      op("/admin/studio/" + versionId + "/map", {
        view_hash: state.hash,
        mappings: [{
          locator: li.dataset.locator,
          start: parseInt(li.dataset.start, 10),
          end: parseInt(li.dataset.end, 10),
          token_name: btn.dataset.token
        }]
      }, false);
    });
  }

  /* ---- undo / redo / publish -------------------------------------------- */
  var undoBtn = document.getElementById("stu-undo");
  if (undoBtn) {
    undoBtn.addEventListener("click", function () {
      if (!this.disabled) { op("/admin/studio/" + versionId + "/undo", {}, false); }
    });
  }
  var redoBtn = document.getElementById("stu-redo");
  if (redoBtn) {
    redoBtn.addEventListener("click", function () {
      if (!this.disabled) { op("/admin/studio/" + versionId + "/redo", {}, false); }
    });
  }

  var publishBtn = document.getElementById("stu-publish");
  if (publishBtn) {
    publishBtn.addEventListener("click", function () {
      if (this.disabled) { return; }
      publishBtn.disabled = true;
      adm.jfetch("/admin/studio/" + versionId + "/publish", { method: "POST", body: "{}" })
        .then(function (resp) {
          if (resp.ok) { window.location.href = "/admin/templates"; return; }
          return resp.json().then(function (err) {
            publishBtn.disabled = false;
            setFlash((err && err.error && err.error.message) || "Publish failed.", true);
          });
        }).catch(function () {
          publishBtn.disabled = false;
          setFlash("Could not reach the server.", true);
        });
    });
  }
})();
