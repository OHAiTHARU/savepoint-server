// Small progressive enhancements. Everything works without JS except
// htmx-powered inline actions, which fall back to the version page.
(function () {
  "use strict";

  document.addEventListener("click", function (e) {
    var add = e.target.closest("[data-add-row]");
    if (add) {
      var list = document.querySelector(add.dataset.addRow);
      var tpl = document.querySelector(add.dataset.template);
      if (list && tpl) {
        list.appendChild(tpl.content.cloneNode(true));
        var inputs = list.querySelectorAll("input");
        if (inputs.length) inputs[inputs.length - 2].focus();
      }
      return;
    }
    var remove = e.target.closest("[data-remove-row]");
    if (remove) {
      var row = remove.closest(".root-row");
      var container = row && row.parentElement;
      if (container && container.children.length > 1) row.remove();
      else if (row) row.querySelectorAll("input").forEach(function (i) { i.value = ""; });
      return;
    }
    var copy = e.target.closest("[data-copy]");
    if (copy) {
      var field = document.querySelector(copy.dataset.copy);
      if (!field) return;
      field.select();
      var done = function () { copy.textContent = "Copied"; };
      if (navigator.clipboard) navigator.clipboard.writeText(field.value).then(done, function () { document.execCommand("copy"); done(); });
      else { document.execCommand("copy"); done(); }
    }
  });

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) e.preventDefault();
  });

  // Retention fields only matter for a custom policy.
  function syncRetention(fs) {
    var custom = fs.querySelector('input[name="retention_mode"][value="custom"]');
    var fields = fs.querySelector(".retention-fields");
    if (!custom || !fields) return;
    fields.hidden = !custom.checked;
    fields.querySelectorAll("input").forEach(function (i) { i.disabled = !custom.checked; });
  }
  document.addEventListener("change", function (e) {
    var fs = e.target.closest("[data-retention]");
    if (fs) syncRetention(fs);
  });
  // Machine checkboxes only matter for "Only these machines".
  function syncScope(fs) {
    var only = fs.querySelector('input[name="sync_scope"][value="only"]');
    var picks = fs.querySelector(".machine-picks");
    if (!only || !picks) return;
    picks.hidden = !only.checked;
  }
  document.addEventListener("change", function (e) {
    var fs = e.target.closest("[data-sync-scope]");
    if (fs) syncScope(fs);
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-retention]").forEach(syncRetention);
    document.querySelectorAll("[data-sync-scope]").forEach(syncScope);
  });

  // Close an open "More" menu when clicking elsewhere, or after an htmx action in it.
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.menu[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });

  // Server error fragments land in #flash-area; scroll them into view.
  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.target && e.detail.target.id === "flash-area") {
      e.detail.target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
  document.addEventListener("htmx:responseError", function (e) {
    var xhr = e.detail.xhr;
    if (xhr && xhr.status >= 400 && !xhr.getResponseHeader("HX-Retarget")) {
      var area = document.getElementById("flash-area");
      if (area) {
        var div = document.createElement("div");
        div.className = "flash flash-error";
        div.textContent = "Request failed (" + xhr.status + ").";
        area.appendChild(div);
      }
    }
  });
})();
