/* To Scale — API dashboard.
   ---------------------------------------------------------------------------
   There is no login here. The API key is the identity: it is created by one
   POST, kept in this browser's localStorage, and sent as X-API-Key on every
   call. No cookie, no session, nothing to reset.

   The one deliberate exception is the dataset download, which goes through a
   plain navigation with the key in the query string instead of fetch(). A
   fetch of a multi-hundred-megabyte CSV has to hold the entire file in the
   tab's memory before the browser will save it, which kills a phone; a
   navigation streams it to disk. The cost -- the key lands in browser history
   -- is accepted for one read-only key over already-public filings.
*/
(function () {
  "use strict";

  var KEY_STORE = "toscale.api_key";
  var CFG = window.TO_SCALE || {};

  function $(id) { return document.getElementById(id); }

  /* localStorage throws outright in a locked-down browser rather than merely
     returning null, so every touch is guarded. A dashboard that cannot
     remember the key must still work for the length of one visit. */
  var memory = null;
  function getKey() {
    if (memory) return memory;
    try { return localStorage.getItem(KEY_STORE) || ""; } catch (e) { return ""; }
  }
  function setKey(k) {
    memory = k || null;
    try { k ? localStorage.setItem(KEY_STORE, k) : localStorage.removeItem(KEY_STORE); }
    catch (e) { /* held in `memory` for this page view only */ }
  }

  function note(el, text, kind) {
    if (!el) return;
    el.textContent = text || "";
    el.className = "formnote" + (kind ? " " + kind : "");
  }

  function show(el, on) { if (el) el.hidden = !on; }

  /* Read the server's message rather than inventing one. Every error this API
     raises carries a `detail` that already says what to do next; replacing it
     with "Something went wrong" would throw away the only useful part. */
  function detailOf(payload, fallback) {
    if (payload && typeof payload.detail === "string") return payload.detail;
    return fallback;
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = opts.headers || {};
    var key = getKey();
    if (key) headers["X-API-Key"] = key;
    if (opts.body) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    });
  }

  // ---- rendering -----------------------------------------------------------

  function renderKey(key) {
    $("key-value").textContent = key;
    $("pay-key").textContent = key;
    var curl = $("curl-example");
    if (curl) {
      curl.textContent =
        'curl -H "X-API-Key: ' + key + '" \\\n  ' +
        location.origin + "/api/company/JPM";
    }
    show($("get-key"), false);
    ["key-panel", "status-panel", "download-panel"].forEach(function (id) {
      show($(id), true);
    });
  }

  function renderNoKey() {
    show($("get-key"), true);
    ["key-panel", "status-panel", "download-panel", "upgrade-panel"]
      .forEach(function (id) { show($(id), false); });
  }

  function renderStatus(s) {
    var pro = s.tier === "pro";
    $("s-tier").textContent = pro ? "Pro" : "Free";
    $("s-tier-sub").textContent = s.calls_limit
      ? s.calls_limit.toLocaleString() + " calls/month"
      : "no monthly limit";

    $("s-calls").textContent =
      s.calls_used_this_month.toLocaleString() + " / " +
      s.calls_limit.toLocaleString();
    $("s-calls-sub").textContent = s.calls_remaining.toLocaleString() +
      " left in " + s.month;

    $("s-dl").textContent = s.has_paid_download ? "Unlocked" : "Locked";
    $("s-dl-sub").textContent = s.has_paid_download
      ? "yours to download"
      : "one-time $" + CFG.datasetPrice;
    $("s-dl").className = "tval " + (s.has_paid_download ? "good" : "muted");

    var pct = s.calls_limit
      ? Math.min(100, Math.round((s.calls_used_this_month / s.calls_limit) * 100))
      : 0;
    var bar = $("s-bar");
    bar.style.width = pct + "%";
    bar.className = pct >= 100 ? "full" : (pct >= 80 ? "warn" : "");

    show($("upgrade-panel"), !pro);
    note($("status-note"), "");
  }

  function refresh() {
    if (!getKey()) { renderNoKey(); return Promise.resolve(); }
    return api("/api/user/status").then(function (r) {
      if (r.ok) { renderStatus(r.data); return; }
      /* A key the server does not know is a dead key -- most likely one typed
         in by hand, or one from a database that has since been reset. Clearing
         it puts the page back to a state the visitor can act on instead of
         looping on an error they cannot fix. */
      if (r.status === 401) {
        setKey("");
        renderNoKey();
        note($("reg-note"), "That key is not recognised. Register again below.", "bad");
        return;
      }
      note($("status-note"), detailOf(r.data, "Could not read your status."), "bad");
    }).catch(function () {
      note($("status-note"), "Could not reach the server.", "bad");
    });
  }

  // ---- actions -------------------------------------------------------------

  function register(ev) {
    ev.preventDefault();
    var email = ($("email").value || "").trim();
    if (!email) return;
    var btn = $("reg-btn");
    btn.disabled = true;
    note($("reg-note"), "Creating your key…");
    api("/api/auth/register", { method: "POST", body: { email: email } })
      .then(function (r) {
        btn.disabled = false;
        if (r.ok && r.data.api_key) {
          setKey(r.data.api_key);
          renderKey(r.data.api_key);
          note($("reg-note"), "");
          return refresh();
        }
        note($("reg-note"), detailOf(r.data, "Could not create a key."), "bad");
      })
      .catch(function () {
        btn.disabled = false;
        note($("reg-note"), "Could not reach the server.", "bad");
      });
  }

  function pasteKey() {
    var k = window.prompt("Paste your API key");
    if (k === null) return;
    k = k.trim();
    if (!k) return;
    setKey(k);
    renderKey(k);
    refresh();
  }

  function copyKey() {
    var key = getKey();
    var done = function () { note($("copy-note"), "Copied.", "good"); };
    var failed = function () {
      note($("copy-note"), "Copy it by hand — this browser blocked the clipboard.", "bad");
    };
    /* navigator.clipboard is undefined on a page served over plain http, which
       is exactly what a local test run is, so the selection fallback is not
       decoration. */
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(key).then(done, failed);
      return;
    }
    try {
      var r = document.createRange();
      r.selectNodeContents($("key-value"));
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(r);
      document.execCommand("copy") ? done() : failed();
    } catch (e) { failed(); }
  }

  function forgetKey() {
    setKey("");
    renderNoKey();
    note($("reg-note"), "Key forgotten on this device. It still works elsewhere.");
  }

  function openPayModal(message) {
    $("pay-body").textContent = message;
    $("pay-key").textContent = getKey();
    show($("pay-modal"), true);
    $("pay-close").focus();
  }

  function download() {
    var btn = $("dl-btn");
    btn.disabled = true;
    note($("dl-note"), "Checking your access…");
    /* Entitlement is checked with a cheap status read first, so an unpaid
       visitor gets the payment instructions instead of a navigation that
       lands them on a bare JSON 402. */
    api("/api/user/status").then(function (r) {
      btn.disabled = false;
      if (!r.ok) {
        note($("dl-note"), detailOf(r.data, "Could not check your access."), "bad");
        return;
      }
      if (!r.data.has_paid_download) {
        note($("dl-note"), "");
        openPayModal(
          "The full dataset is a one-time $" + CFG.datasetPrice + ". " +
          "Send it by e-transfer or PayPal to " +
          (CFG.adminEmail || "the site owner") + ", then email the same " +
          "address with your API key. It is unlocked by hand, usually within " +
          "24 hours."
        );
        return;
      }
      note($("dl-note"), "Starting the download. It is a large file — leave this tab open.");
      window.location.href =
        "/api/download-dataset?api_key=" + encodeURIComponent(getKey());
    }).catch(function () {
      btn.disabled = false;
      note($("dl-note"), "Could not reach the server.", "bad");
    });
  }

  // ---- wiring --------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    $("reg-form").addEventListener("submit", register);
    $("paste-key").addEventListener("click", pasteKey);
    $("copy-key").addEventListener("click", copyKey);
    $("forget-key").addEventListener("click", forgetKey);
    $("dl-btn").addEventListener("click", download);
    $("pay-close").addEventListener("click", function () {
      show($("pay-modal"), false);
      $("dl-btn").focus();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !$("pay-modal").hidden) {
        show($("pay-modal"), false);
      }
    });

    var key = getKey();
    if (key) { renderKey(key); refresh(); } else { renderNoKey(); }
  });
})();
