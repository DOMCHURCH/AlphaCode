/* To Scale — API dashboard.
   ---------------------------------------------------------------------------
   Two ways to be here, and they are not equal:

     session   a signed cookie from an emailed link. /api/auth/me returns the
               account AND its key, so the page can show it.
     pasted    a key in localStorage. Still supported on purpose: someone who
               only wants to make calls should not need an email to read their
               own quota.

   The session wins when both exist. It proves control of the address; a pasted
   key proves only that somebody has the string. So a session for A with B's key
   in localStorage shows A, and overwrites the stored key with A's.

   The one deliberate exception to fetch-with-a-header is the dataset download,
   which navigates with the key in the query string. A fetch of a
   multi-hundred-megabyte CSV must hold the whole file in the tab's memory
   before the browser will save it, which kills a phone; a navigation streams it
   to disk.
*/
(function () {
  "use strict";

  var KEY_STORE = "toscale.api_key";
  var TAB_STORE = "toscale.tab";
  var CFG = window.TO_SCALE || {};
  var TABS = ["search", "api", "account", "billing"];

  var session = null;   // the account from /api/auth/me, or null

  function $(id) { return document.getElementById(id); }
  function show(el, on) { if (el) el.hidden = !on; }

  /* localStorage throws outright in a locked-down browser rather than merely
     returning null, so every touch is guarded. The dashboard must still work
     for the length of one visit without it. */
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

  /* Read the server's message rather than inventing one. Every error this API
     raises carries a `detail` that already says what to do next; replacing it
     with "Something went wrong" throws away the only useful part. */
  function detailOf(payload, fallback) {
    if (payload && typeof payload.detail === "string") return payload.detail;
    if (payload && typeof payload.message === "string") return payload.message;
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
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin"   // the session cookie rides along
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    });
  }

  // ---- tabs -----------------------------------------------------------------

  function selectTab(name) {
    if (TABS.indexOf(name) === -1) name = "api";
    TABS.forEach(function (t) {
      var panel = $("panel-" + t);
      if (panel) panel.hidden = t !== name;
    });
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (b) {
      var on = b.getAttribute("data-tab") === name;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    try { localStorage.setItem(TAB_STORE, name); } catch (e) { /* not fatal */ }
  }

  function storedTab() {
    try { return localStorage.getItem(TAB_STORE) || "api"; } catch (e) { return "api"; }
  }

  // ---- rendering ------------------------------------------------------------

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
    show($("tabs"), true);
    selectTab(storedTab());
    // Rotating a key requires proving you own the address, not holding the key.
    show($("regen-box"), !!session);
    show($("regen-hint"), !session && CFG.loginEnabled);
  }

  function renderSignedOut() {
    show($("get-key"), true);
    show($("tabs"), false);
    TABS.forEach(function (t) { show($("panel-" + t), false); });
    show($("whoami"), false);
    show($("signed-out"), true);
  }

  function renderWhoami(s) {
    if (!session) { show($("whoami"), false); show($("signed-out"), true); return; }
    $("who-email").textContent = s.email;
    $("who-plan").textContent = s.tier === "pro" ? "Pro" : "Free";
    show($("whoami"), true);
    show($("signed-out"), false);
  }

  function renderStatus(s) {
    var pro = s.tier === "pro";
    var planName = pro ? "Pro" : "Free";

    $("s-tier").textContent = s.lapsed ? "Free (expired)" : planName;
    $("s-tier-sub").textContent = s.calls_limit
      ? s.calls_limit.toLocaleString() + " calls/month"
      : "no monthly limit";
    $("s-calls").textContent =
      s.calls_used_this_month.toLocaleString() + " / " + s.calls_limit.toLocaleString();
    $("s-calls-sub").textContent =
      s.calls_remaining.toLocaleString() + " left in " + s.month;
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

    // Quick-start example, with the real key and host filled in.
    var qs = $("qs-curl");
    if (qs) {
      qs.textContent =
        'curl -H "X-API-Key: ' + getKey() + '" \\\n  ' +
        location.origin + "/api/company/JPM";
    }
    $("plan-summary").textContent = pro
      ? "Pro — " + s.calls_limit.toLocaleString() + " calls a month"
      : "Free — " + s.calls_limit.toLocaleString() + " calls a month";

    $("a-email").textContent = s.email;
    $("a-pw").textContent = s.has_password ? "Set" : "Not set";
    $("a-pw-sub").textContent = s.has_password
      ? "you can sign in with it"
      : "magic links only";
    if (s.expires_at) {
      var when = new Date(s.expires_at);
      $("a-expires").textContent = when.toLocaleDateString();
      $("a-expires-sub").textContent = s.lapsed
        ? "expired — renew to restore Pro"
        : (s.days_remaining + " days left");
    } else {
      $("a-expires").textContent = pro ? "Never" : "—";
      $("a-expires-sub").textContent = pro ? "comped account" : "not on Pro";
    }
    // Setting a first password needs no old one; changing needs the current.
    if (session) {
      show($("pw-box"), true);
      show($("pw-hint"), false);
      $("pw-box-title").textContent =
        s.has_password ? "Change your password" : "Set a password";
      show($("curpw-field"), !!s.has_password);
      $("setpw-btn").textContent = s.has_password ? "Change" : "Save";
      /* The nudge, and only for an account that has never set one. Anybody who
         arrived by magic link cannot be told this on /login -- saying it there
         would confirm to a stranger that the address is registered -- so the
         Account tab is where it gets said. */
      show($("nopw-callout"), !s.has_password);
    } else {
      show($("pw-box"), false);
      show($("nopw-callout"), false);
      show($("pw-hint"), CFG.loginEnabled);
    }
    maybeAnnounceGrant(s);
    $("a-plan").textContent = planName;
    $("a-plan-sub").textContent = pro
      ? "billed monthly, arranged by email"
      : "no charge";

    $("b-plan").textContent = planName;
    $("b-plan-sub").textContent = pro
      ? "$" + CFG.proPrice + "/month"
      : "$0 — " + s.calls_limit.toLocaleString() + " calls/month";
    $("b-dataset").textContent = s.has_paid_download ? "Purchased" : "Not purchased";
    $("b-dataset-sub").textContent = s.has_paid_download
      ? "download it from the API tab"
      : "one-time $" + CFG.datasetPrice;
    $("b-dataset").className = "tval " + (s.has_paid_download ? "good" : "muted");

    $("buy-pro").disabled = pro;
    $("buy-data").disabled = !!s.has_paid_download;

    renderWhoami(s);
    note($("status-note"), "");
  }

  // ---- loading --------------------------------------------------------------

  function load() {
    /* Ask about the session first. It is the stronger claim, and it carries the
       key -- so when it answers, whatever is in localStorage is stale by
       definition and gets overwritten. */
    return api("/api/auth/me")
      .then(function (r) {
        if (r.ok) {
          session = r.data;
          setKey(r.data.api_key);
          renderKey(r.data.api_key);
          renderStatus(r.data);
          return;
        }
        session = null;
        return loadByKey();
      })
      .catch(function () { session = null; return loadByKey(); });
  }

  function loadByKey() {
    var key = getKey();
    if (!key) { renderSignedOut(); return Promise.resolve(); }
    renderKey(key);
    return api("/api/user/status").then(function (r) {
      if (r.ok) { renderStatus(r.data); return; }
      /* A key the server does not know is a dead key -- typed in by hand, from
         a database since reset, or just regenerated elsewhere. Clearing it puts
         the page back into a state the visitor can act on instead of looping on
         an error they cannot fix. */
      if (r.status === 401) {
        setKey("");
        renderSignedOut();
        note($("reg-note"), "That key is not recognised. Register or sign in.", "bad");
        return;
      }
      note($("status-note"), detailOf(r.data, "Could not read your status."), "bad");
    }).catch(function () {
      note($("status-note"), "Could not reach the server.", "bad");
    });
  }

  // ---- actions --------------------------------------------------------------

  function register(ev) {
    ev.preventDefault();
    var email = ($("email").value || "").trim();
    if (!email) return;
    var btn = $("reg-btn");
    btn.disabled = true;
    note($("reg-note"), "Creating your key…");
    var box = $("accept-terms");
    api("/api/auth/register", {
      method: "POST",
      body: { email: email, accept_terms: !box || box.checked }
    })
      .then(function (r) {
        btn.disabled = false;
        if (r.ok && r.data.api_key) {
          setKey(r.data.api_key);
          note($("reg-note"), "");
          return load();
        }
        /* 409 is the lost-key case, and the only branch that offers recovery.
           The key is never shown here -- a login link goes to the registered
           inbox -- so offering it for an unregistered address would tell a
           stranger that the address exists. */
        if (r.status === 409) {
          note($("reg-note"), detailOf(r.data, "That address already has a key."), "bad");
          show($("resend-box"), true);
          $("resend-btn").onclick = function () { sendLink(email); };
          return;
        }
        note($("reg-note"), detailOf(r.data, "Could not create a key."), "bad");
      })
      .catch(function () {
        btn.disabled = false;
        note($("reg-note"), "Could not reach the server.", "bad");
      });
  }

  function sendLink(email) {
    var btn = $("resend-btn");
    btn.disabled = true;
    note($("resend-note"), "Sending…");
    api("/api/auth/magic-link", { method: "POST", body: { email: email } })
      .then(function (r) {
        if (r.status === 503) {
          btn.disabled = false;
          note($("resend-note"), detailOf(r.data, "Email is not configured."), "bad");
          return;
        }
        note(
          $("resend-note"),
          detailOf(r.data, "Check your email.") + " The link lasts 15 minutes.",
          "good"
        );
      })
      .catch(function () {
        btn.disabled = false;
        note($("resend-note"), "Could not reach the server.", "bad");
      });
  }

  function pasteKey() {
    var k = window.prompt("Paste your API key");
    if (k === null) return;
    k = k.trim();
    if (!k) return;
    setKey(k);
    load();
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

  function regenerate() {
    if (!window.confirm(
      "Replace your API key? Anything using the old one stops working immediately."
    )) return;
    var btn = $("regen-btn");
    btn.disabled = true;
    note($("regen-note"), "Replacing…");
    api("/api/auth/regenerate-key", { method: "POST", body: {} })
      .then(function (r) {
        btn.disabled = false;
        if (r.ok && r.data.api_key) {
          /* Store the new key BEFORE anything reads it again: the old one is
             already dead server-side, so a refresh in between would 401 and
             sign the page out from under the person who just pressed this. */
          setKey(r.data.api_key);
          renderKey(r.data.api_key);
          note($("regen-note"), "Done. The old key no longer works.", "good");
          return load();
        }
        note($("regen-note"), detailOf(r.data, "Could not replace the key."), "bad");
      })
      .catch(function () {
        btn.disabled = false;
        note($("regen-note"), "Could not reach the server.", "bad");
      });
  }

  function logout() {
    api("/api/auth/logout", { method: "POST", body: {} })
      .then(function () {
        // Both, or "log out" leaves the account readable from the stored key.
        session = null;
        setKey("");
        renderSignedOut();
      })
      .catch(function () { window.location.reload(); });
  }

  function forgetKey() {
    setKey("");
    session = null;
    renderSignedOut();
    note($("reg-note"), "Key forgotten on this device. It still works elsewhere.");
  }

  function openPayModal(message) {
    $("pay-body").textContent = message;
    $("pay-key").textContent = getKey();
    show($("pay-modal"), true);
    $("pay-close").focus();
  }

  function payText(what, price) {
    return (
      what + " is " + price + ", paid by card through Stripe. Email " +
      (CFG.adminEmail || "the site owner") + " with your API key and you will " +
      "be sent a checkout link. Card details are entered on Stripe's page and " +
      "never reach this site."
    );
  }

  function download() {
    var btn = $("dl-btn");
    btn.disabled = true;
    note($("dl-note"), "Checking your access…");
    /* Entitlement is checked with a cheap status read first, so an unpaid
       visitor gets the payment instructions instead of a navigation that lands
       them on a bare JSON 402. */
    api("/api/user/status").then(function (r) {
      btn.disabled = false;
      if (!r.ok) {
        note($("dl-note"), detailOf(r.data, "Could not check your access."), "bad");
        return;
      }
      if (!r.data.has_paid_download) {
        note($("dl-note"), "");
        openPayModal(payText("The full dataset", "a one-time $" + CFG.datasetPrice));
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

  // ---- the grant banner -----------------------------------------------------

  var PLAN_STORE = "toscale.lastplan";

  function maybeAnnounceGrant(s) {
    /* A grant happens out of band -- somebody pays, the operator runs a curl --
       so the only sign it worked would otherwise be a number quietly reading
       differently. This compares against what this browser last saw. */
    var now = s.tier + "|" + (s.has_paid_download ? "d" : "-");
    var before = null;
    try { before = localStorage.getItem(PLAN_STORE); } catch (e) { /* ignore */ }
    try { localStorage.setItem(PLAN_STORE, now); } catch (e) { /* ignore */ }
    if (!before || before === now) return;

    var was = before.split("|");
    var msg = "";
    if (was[0] !== s.tier && s.tier === "pro") {
      msg = "Pro unlocked. You now have " +
        s.calls_limit.toLocaleString() + " API calls a month.";
    } else if (was[1] !== "d" && s.has_paid_download) {
      msg = "Full dataset unlocked. Download it from the API tab.";
    } else if (was[0] === "pro" && s.tier === "free") {
      msg = "Your Pro subscription has ended. You are back on the free tier — " +
        "your key still works.";
    }
    if (!msg) return;
    $("banner-text").textContent = msg;
    show($("grant-banner"), true);
  }

  function savePassword(ev) {
    ev.preventDefault();
    var next = $("newpw").value || "";
    var cur = $("curpw").value || "";
    var changing = !$("curpw-field").hidden;
    var btn = $("setpw-btn");
    btn.disabled = true;
    note($("setpw-note"), "Saving…");
    var path = changing
      ? "/api/auth/change-password" : "/api/auth/set-password";
    var body = changing
      ? { current_password: cur, new_password: next } : { password: next };
    api(path, { method: "POST", body: body })
      .then(function (r) {
        btn.disabled = false;
        if (r.ok) {
          $("newpw").value = ""; $("curpw").value = "";
          note($("setpw-note"), "Saved. You can sign in with it now.", "good");
          return load();
        }
        note($("setpw-note"), detailOf(r.data, "Could not save it."), "bad");
      })
      .catch(function () {
        btn.disabled = false;
        note($("setpw-note"), "Could not reach the server.", "bad");
      });
  }

  // ---- wiring ---------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    $("reg-form").addEventListener("submit", register);
    $("paste-key").addEventListener("click", pasteKey);
    $("copy-key").addEventListener("click", copyKey);
    $("forget-key").addEventListener("click", forgetKey);
    $("dl-btn").addEventListener("click", download);
    $("regen-btn").addEventListener("click", regenerate);
    $("logout-btn").addEventListener("click", logout);
    // The callout's button does not do the work -- the form below it does. It
    // scrolls to it and puts the cursor in it, so the nudge lands somewhere.
    $("nopw-btn").addEventListener("click", function () {
      var box = $("pw-box");
      if (box.scrollIntoView) box.scrollIntoView({ behavior: "smooth", block: "center" });
      $("newpw").focus();
    });
    $("buy-pro").addEventListener("click", function () {
      openPayModal(payText("Pro", "$" + CFG.proPrice + " a month"));
    });
    $("buy-data").addEventListener("click", function () {
      openPayModal(payText("The full dataset", "a one-time $" + CFG.datasetPrice));
    });
    $("pay-close").addEventListener("click", function () {
      show($("pay-modal"), false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !$("pay-modal").hidden) show($("pay-modal"), false);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (b) {
      b.addEventListener("click", function () {
        selectTab(b.getAttribute("data-tab"));
      });
    });

    $("setpw-form").addEventListener("submit", savePassword);
    $("banner-close").addEventListener("click", function () {
      show($("grant-banner"), false);
    });
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-goto]"), function (a) {
        a.addEventListener("click", function (ev) {
          ev.preventDefault();
          selectTab(a.getAttribute("data-goto"));
          window.scrollTo({ top: 0, behavior: "smooth" });
        });
      });

    load();
  });
})();
