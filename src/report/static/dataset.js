/* To Scale — the /dataset download page.
   ---------------------------------------------------------------------------
   A deliberately small script, and not a second dashboard. Everything it does
   is one of three things:

     1. find the key this browser already has, so a returning buyer does not
        have to paste one;
     2. check entitlement BEFORE navigating, so somebody who has not bought the
        dataset gets a sentence and a buy button rather than a bare JSON 402;
     3. start a Checkout Session for the dataset when they have not.

   It shares localStorage with the dashboard on purpose -- same key, same
   store. A key pasted on one page is a key the other page knows about, which
   is what somebody arriving here from an emailed link expects.

   The download NAVIGATES with the key in the query string rather than fetching
   with a header, for the same reason the dashboard does: a fetch of a
   multi-hundred-megabyte CSV has to hold the whole file in the tab's memory
   before the browser will offer to save it, which kills a phone. A navigation
   streams it to disk.
*/
(function () {
  "use strict";

  var KEY_STORE = "toscale.api_key";
  var CFG = window.TO_SCALE || {};

  function $(id) { return document.getElementById(id); }

  var memory = null;
  function storedKey() {
    if (memory) return memory;
    try { return localStorage.getItem(KEY_STORE) || ""; } catch (e) { return ""; }
  }
  function remember(k) {
    memory = k || null;
    try { if (k) localStorage.setItem(KEY_STORE, k); }
    catch (e) { /* held for this page view only */ }
  }

  /* The key in play: whatever is typed in the box, else whatever this browser
     already had. Typed wins, because somebody who just pasted a key is telling
     us which account they mean. */
  function currentKey() {
    var typed = ($("dl-key").value || "").trim();
    return typed || storedKey();
  }

  function note(text, kind) {
    var el = $("ds-note");
    el.textContent = text || "";
    el.className = "formnote" + (kind ? " " + kind : "");
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = {};
    var key = currentKey();
    if (key) headers["X-API-Key"] = key;
    if (opts.body) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin"   // a session cookie is a stronger claim
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    });
  }

  function openModal(text) {
    $("pay-body").textContent = text;
    $("pay-modal").hidden = false;
    $("pay-close").focus();
  }

  // ---- the download ---------------------------------------------------------

  function download() {
    var btn = $("ds-btn");
    btn.disabled = true;
    note("Checking your access…");
    api("/api/user/status").then(function (r) {
      btn.disabled = false;
      if (!r.ok) {
        note(
          (r.data && r.data.detail) ||
          "Could not check your access. Sign in, or paste your API key above.",
          "bad"
        );
        $("ds-buy").hidden = false;
        return;
      }
      remember(currentKey());
      if (!r.data.has_paid_download) {
        /* Not an error and not phrased as one. This is the ordinary state for
           somebody who has not bought it yet, and the useful next thing is the
           buy button rather than a rebuke. */
        note("This account has not bought the dataset yet.");
        $("ds-buy").hidden = false;
        return;
      }
      $("ds-buy").hidden = true;
      note("Starting the download. It is a large file — leave this tab open " +
        "while it streams.");
      window.location.href =
        "/api/download-dataset?api_key=" + encodeURIComponent(currentKey());
    }).catch(function () {
      btn.disabled = false;
      note("Could not reach the server. Check your connection.", "bad");
    });
  }

  // ---- buying it ------------------------------------------------------------

  function buy() {
    var btn = $("ds-buy-btn");
    btn.disabled = true;
    note("Opening a secure checkout on Stripe…");
    api("/api/billing/checkout", {
      method: "POST",
      body: { plan: "dataset" }
    }).then(function (r) {
      if (r.ok && r.data && r.data.url) {
        window.location.href = r.data.url;
        return;
      }
      btn.disabled = false;
      note("");
      if (r.status === 503) {
        openModal(
          "The dataset (" + (CFG.datasetPrice || "one-time") + ") cannot be " +
          "bought by card on this deployment. Email " +
          (CFG.adminEmail || "the site owner") + " and it will be sorted out " +
          "by hand."
        );
        return;
      }
      openModal(
        (r.data && r.data.detail) ||
        "Could not open a checkout just now. Please try again."
      );
    }).catch(function () {
      btn.disabled = false;
      note("Could not reach the server. Check your connection.", "bad");
    });
  }

  // ---- wiring ---------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    var field = $("dl-key");
    if (!field) return;                // not the dataset page; nothing to wire

    /* Prefill from storage, and hide the box entirely when a session is
       already answering for this browser -- asking somebody who is signed in
       to paste their own key is asking them to go and find it. */
    var known = storedKey();
    if (known) field.value = known;
    fetch("/api/auth/me", { credentials: "same-origin" })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (me) {
        if (!me || !me.api_key) return;
        remember(me.api_key);
        field.value = me.api_key;
        $("dl-key-field").hidden = true;
      })
      .catch(function () { /* the pasted-key path still works */ });

    $("ds-btn").addEventListener("click", download);
    $("ds-buy-btn").addEventListener("click", buy);
    $("pay-close").addEventListener("click", function () {
      $("pay-modal").hidden = true;
    });
  });
})();
