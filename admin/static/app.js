/* Astra Host admin console — progressive enhancements only.
   Everything works without JS; this adds instant filtering, copy,
   confirms, toasts and live instance metrics. */
(function () {
  "use strict";

  /* ── sidebar (mobile) ─────────────────────────────── */
  var toggle = document.getElementById("navToggle");
  var sidebar = document.getElementById("sidebar");
  var scrim = document.getElementById("scrim");
  function setNav(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", open);
    if (scrim) scrim.hidden = !open;
  }
  if (toggle) toggle.addEventListener("click", function () { setNav(!sidebar.classList.contains("open")); });
  if (scrim) scrim.addEventListener("click", function () { setNav(false); });

  /* ── toasts ───────────────────────────────────────── */
  document.querySelectorAll("[data-toast]").forEach(function (el) {
    var close = el.querySelector(".toast-x");
    if (close) close.addEventListener("click", function () { el.remove(); });
    setTimeout(function () { if (el.parentNode) el.remove(); }, 8000);
  });

  /* ── clock ────────────────────────────────────────── */
  var clock = document.querySelector("[data-clock]");
  if (clock) {
    var tick = function () {
      var d = new Date();
      var p = function (n) { return (n < 10 ? "0" : "") + n; };
      clock.textContent = p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds()) + " UTC";
    };
    tick();
    setInterval(tick, 1000);
  }

  /* ── instant table filtering ──────────────────────── */
  document.querySelectorAll("[data-filter-input]").forEach(function (input) {
    var targetId = input.getAttribute("data-filter-target");
    var table = targetId
      ? document.getElementById(targetId)
      : (input.closest(".card") || document).querySelector("[data-filter-table]");
    if (!table || !table.tBodies.length) return;
    var rows = Array.prototype.slice.call(table.tBodies[0].rows);
    var run = function () {
      var q = (input.value || "").trim().toLowerCase();
      rows.forEach(function (row) {
        var placeholder = row.cells.length < 2; // "No rows" state row
        var hit = !q ? true : (placeholder ? false : row.innerText.toLowerCase().indexOf(q) !== -1);
        row.classList.toggle("hidden-row", !hit);
      });
    };
    input.addEventListener("input", run);
  });

  /* ── copy buttons ─────────────────────────────────── */
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy") || "";
      var done = function () {
        var old = btn.textContent;
        btn.textContent = "Copied ✓";
        setTimeout(function () { btn.textContent = old; }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {});
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); done(); } catch (e) {}
        document.body.removeChild(ta);
      }
    });
  });

  /* ── destructive-action confirms ──────────────────── */
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    var handler = function (ev) {
      var msg = el.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) {
        ev.preventDefault();
        ev.stopPropagation();
      }
    };
    if (el.tagName === "FORM") el.addEventListener("submit", handler);
    else el.addEventListener("click", handler);
  });

  /* ── live instance metrics ────────────────────────── */
  var live = location.pathname.match(/^\/vps\/([^\/]+)\/?$/);
  var cpuEl = document.querySelector('[data-live="cpu"]');
  if (live && cpuEl) {
    var memEl = document.querySelector('[data-live="mem"]');
    var pidEl = document.querySelector('[data-live="pid"]');
    var statusEl = document.querySelector('[data-live="status"]');
    setInterval(function () {
      fetch("/api/vps/" + encodeURIComponent(live[1]) + "/stats", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data || !data.ok) return;
          var s = data.stats || {};
          if (s.cpu_percent !== undefined) cpuEl.textContent = s.cpu_percent + "%";
          if (s.mem_used_mb !== undefined && memEl) memEl.textContent = s.mem_used_mb + " MB";
          if (s.pid && pidEl) pidEl.textContent = s.pid;
          if (s.status && statusEl) statusEl.textContent = s.status;
        })
        .catch(function () {});
    }, 15000);
  }
})();
