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
  /* The ticker in the worked examples. Must match `api_tab.TICKER`: the
     server ships the placeholder version of both snippets and this script
     replaces them, so a disagreement shows as the example silently
     changing company the moment a key loads. */
  var EXAMPLE_TICKER = "AAPL";

  var session = null;   // the account from /api/auth/me, or null
  /* The address the key in hand belongs to. Sent with a checkout so that a
     reader who pasted a key WITHOUT signing in still buys for their own
     account: with no session and no address, Stripe asks for one, and whatever
     they type there is the account that gets the grant -- which is how you buy
     Pro for an address you do not own. The server prefers its own session over
     this, so it can only ever narrow the answer, never widen it. */
  var accountEmail = "";
  // One checkout poll per page view, however many times status re-renders.
  var checkoutWatched = false;

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

  /* Which tab to open with. A #hash WINS over the remembered one, because a
     hash is a request made just now and the stored tab is a preference from
     some previous visit -- and /dashboard#billing is the no-script fallback
     behind the home page's buy buttons, so it has to land on billing rather
     than on whatever tab was last used. */
  function storedTab() {
    var hash = (location.hash || "").replace(/^#/, "");
    if (TABS.indexOf(hash) !== -1) return hash;
    try { return localStorage.getItem(TAB_STORE) || "api"; } catch (e) { return "api"; }
  }

  // ---- rendering ------------------------------------------------------------

  /* The two worked examples on the API tab, filled in with the reader's own
     key and this deployment's host.

     Done in the browser rather than server-side, deliberately and in both
     directions. The page is rendered before anyone is identified, so a key
     written into the HTML would be written for whoever loaded the page; and
     `request.url` inside the container is the internal http hop rather than
     the https origin the reader is on, so an example built from it hands
     somebody a command that answers with a redirect instead of JSON.
     `location.origin` is, by definition, the address that just worked.

     `textContent`, never innerHTML: the key is a credential going into a
     <pre>, and there is no version of this where it should be parsed. */
  function fillExamples(key) {
    var url = location.origin + "/api/company/" + EXAMPLE_TICKER;
    var k = key || "YOUR_KEY";
    var curl = $("curl-example");
    if (curl) {
      curl.textContent = 'curl -H "X-API-Key: ' + k + '" \\\n  ' + url;
    }
    var py = $("py-example");
    if (py) {
      py.textContent =
        "import requests\n\n" +
        'headers = {"X-API-Key": "' + k + '"}\n' +
        'response = requests.get("' + url + '", headers=headers)\n' +
        "data = response.json()\n" +
        'print(data["assets"])';
    }
  }

  function renderKey(key) {
    $("key-value").textContent = key;
    $("pay-key").textContent = key;
    fillExamples(key);
    show($("get-key"), false);
    show($("tabs"), true);
    selectTab(storedTab());
    // Rotating a key requires proving you own the address, not holding the key.
    show($("regen-box"), !!session);
    show($("regen-hint"), !session && CFG.loginEnabled);
    show($("copy-key"), true);
    note($("key-note"), "");
  }

  /* Signed in, but the key itself is not on this device.

     The server stores a hash, so /api/auth/me can only say WHICH key this
     account holds, never what it is. Showing the prefix beats showing a dash:
     it lets somebody check that the key in their deploy config is the one this
     account is on, which is the actual question people open this page with. */
  function renderKeyPrefix(prefix, lastUsed) {
    var shown = prefix ? prefix + "\u2026" : "\u2014";
    $("key-value").textContent = shown;
    $("pay-key").textContent = shown;
    fillExamples("");
    show($("get-key"), false);
    show($("tabs"), true);
    selectTab(storedTab());
    show($("regen-box"), true);
    show($("regen-hint"), false);
    // Nothing worth copying: the box holds eight characters and an ellipsis.
    show($("copy-key"), false);
    note(
      $("key-note"),
      "Keys are stored hashed and are shown once, when issued. This is not "
        + "stored in this browser. " + lastUsedText(lastUsed)
        + " Press Regenerate to issue a new one \u2014 the old key stops "
        + "working immediately."
    );
  }

  function lastUsedText(iso) {
    if (!iso) return "It has never been used.";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return "Last used: " + d.toLocaleDateString() + ".";
  }

  /* One entry point for "the session answered".

     The server can no longer correct what this browser holds -- /me returns a
     digest-derived prefix, not the key -- so the prefix IS the check. Without
     it, regenerating on one device leaves every other device rendering the
     dead key as live, curl examples and all, with nothing to notice it by. */
  function renderSession(data) {
    var full = getKey();
    if (full && data.api_key_prefix && full.slice(0, 8) === data.api_key_prefix) {
      renderKey(full);
      return;
    }
    if (full) setKey("");   // it belongs to a key this account no longer has
    renderKeyPrefix(data.api_key_prefix, data.api_key_last_used);
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
      : "one-time " + CFG.datasetPrice;
    $("s-dl").className = "tval " + (s.has_paid_download ? "good" : "muted");

    var pct = s.calls_limit
      ? Math.min(100, Math.round((s.calls_used_this_month / s.calls_limit) * 100))
      : 0;
    var bar = $("s-bar");
    bar.style.width = pct + "%";
    bar.className = pct >= 100 ? "full" : (pct >= 80 ? "warn" : "");

    // A pasted key never goes through renderKey, so the examples are filled
    // here too. Without this that reader is shown "YOUR_KEY" next to a working
    // quota, which reads as the key not having been accepted.
    fillExamples(getKey());
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
    /* Only on the FIRST render, and only when the URL says a payment just
       happened -- otherwise every status refresh would start its own poll. */
    if (!checkoutWatched) { checkoutWatched = true; watchForCheckout(s); }
    /* Neither line names a price or a billing period for a Pro account, and
       that is deliberate: there are two Pro plans now, monthly and annual, and
       the status payload cannot tell them apart -- it carries one tier. Saying
       "$49/month" to somebody who paid $490 for a year is a worse answer than
       saying nothing, and "billed monthly" is simply false for half of them.
       The expiry date on the Account tab is the honest, plan-agnostic fact. */
    $("a-plan").textContent = planName;
    $("a-plan-sub").textContent = pro
      ? "renews through Stripe"
      : "no charge";

    $("b-plan").textContent = planName;
    $("b-plan-sub").textContent = pro
      ? s.calls_limit.toLocaleString() + " calls/month"
      : "$0 — " + s.calls_limit.toLocaleString() + " calls/month";
    $("b-dataset").textContent = s.has_paid_download ? "Purchased" : "Not purchased";
    $("b-dataset-sub").textContent = s.has_paid_download
      ? "download it from the API tab"
      : "one-time " + CFG.datasetPrice;
    $("b-dataset").className = "tval " + (s.has_paid_download ? "good" : "muted");

    accountEmail = s.email || "";
    /* BOTH Pro buttons go dead once the account is Pro, not just the monthly
       one. There is a single Pro tier and the annual plan grants exactly it,
       so an enabled "Go Pro annually" next to an account that already has Pro
       is a second subscription for access it already holds. Switching between
       billing periods is a change to an existing subscription and belongs in
       Stripe's portal, not in a second checkout. */
    setDisabled(["buy-pro", "buy-pro-annual"], pro);
    setDisabled(["buy-data"], !!s.has_paid_download);

    /* Only for accounts Stripe actually holds a customer for. Everyone else
       would get a 409, and a button that fails for most of the people who can
       see it teaches them not to trust the rest of the page. */
    show($("manage-billing"), !!s.has_billing);
    /* A paused account is the one case where this button is the whole point of
       the page, so it says so rather than leaving them to guess why their key
       started answering 402. */
    if (s.api_access_paused) {
      note($("portal-note"),
        "API access is paused after a failed payment. Update your card to " +
        "resume it — nothing has been deleted.", "bad");
    }

    renderWhoami(s);
    note($("status-note"), "");
  }

  // ---- loading --------------------------------------------------------------

  function load() {
    /* Ask about the session first. It is the stronger claim about WHO this
       is -- but it no longer carries the key, so what is in localStorage is
       the only readable copy and must survive this call. */
    return api("/api/auth/me")
      .then(function (r) {
        if (r.ok) {
          session = r.data;
          /* Deliberately does NOT call setKey. This response carries no key
             to store, and clearing the stored one here would throw away the
             only copy the person has. */
          renderSession(r.data);
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
    trapFocus($("pay-modal"), $("pay-close"));
  }

  /* A dialog that says aria-modal="true" has to behave like one.

     It did not: focus moved to the close button and then Tab walked straight
     out into the nav links behind a 45%-opaque overlay, where the focus ring
     cannot be seen and the controls are not supposed to be reachable. And on
     close, focus fell to <body> rather than back to the button that opened it,
     so a keyboard user landed at the top of the document every time.

     One handler on the dialog, removed when it closes. Escape closes, because
     a modal that can only be dismissed by finding one button is a trap of a
     different kind. */
  var _trapped = null;

  function trapFocus(modal, first) {
    var opener = document.activeElement;
    var focusable = modal.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), ' +
      'select, textarea, [tabindex]:not([tabindex="-1"])'
    );
    function onKey(e) {
      if (e.key === "Escape") { closeModal(modal); return; }
      if (e.key !== "Tab" || !focusable.length) return;
      var lo = focusable[0], hi = focusable[focusable.length - 1];
      // Wrap at both ends, and catch focus that is already outside.
      if (e.shiftKey && (document.activeElement === lo || !modal.contains(document.activeElement))) {
        e.preventDefault(); hi.focus();
      } else if (!e.shiftKey && document.activeElement === hi) {
        e.preventDefault(); lo.focus();
      }
    }
    modal.addEventListener("keydown", onKey);
    _trapped = { modal: modal, onKey: onKey, opener: opener };
    (first || focusable[0] || modal).focus();
  }

  function closeModal(modal) {
    modal.hidden = true;
    if (!_trapped || _trapped.modal !== modal) return;
    _trapped.modal.removeEventListener("keydown", _trapped.onKey);
    // Back to whatever opened it, so a keyboard user resumes where they were.
    if (_trapped.opener && _trapped.opener.focus) _trapped.opener.focus();
    _trapped = null;
  }

  /* Why a checkout could not be opened, in the reader's terms.

     Never a redirect. Bouncing somebody to /login when a purchase fails is the
     bug this replaced: it loses the click, tells them nothing, and is
     indistinguishable from being signed out when they are not. Every branch
     here ends in a sentence in the modal and a button they can press again. */
  function checkoutProblem(r, what) {
    var detail = (r.data && r.data.detail) || "";
    if (r.status === 429) {
      // The server's message already carries the wait in seconds.
      return detail || "Too many checkouts have been started just now. " +
        "Give it a minute and try again.";
    }
    if (r.status === 503) return payText(what, "not on card yet");
    if (r.status === 502) {
      return "Stripe could not open a checkout just now. Try again in a " +
        "moment; if it keeps happening, email " +
        (CFG.adminEmail || "the site owner") + ".";
    }
    return detail || "Something went wrong opening the checkout.";
  }

  /* plan -> the button that starts it. A LOOKUP rather than a ternary, which
     is what this was: `plan === "pro" ? "buy-pro" : "buy-data"` quietly made
     the dataset button the else-branch for every plan that was not "pro", so
     adding the annual plan would have disabled and re-enabled the wrong
     button on every annual checkout. */
  var BUY_BTN = {
    pro: "buy-pro",
    pro_annual: "buy-pro-annual",
    dataset: "buy-data"
  };

  function setDisabled(ids, on) {
    ids.forEach(function (id) { var b = $(id); if (b) b.disabled = on; });
  }

  function startCheckout(plan, what) {
    var btn = $(BUY_BTN[plan]);
    if (!btn) return;
    btn.disabled = true;
    api("/api/billing/checkout", {
      method: "POST",
      body: { plan: plan, email: accountEmail || undefined }
    }).then(function (r) {
      if (r.ok && r.data && r.data.url) {
        // Straight to Stripe. Not window.open: a popup blocker eats it, and
        // this is a navigation the reader asked for.
        window.location.href = r.data.url;
        return;
      }
      btn.disabled = false;
      openPayModal(checkoutProblem(r, what));
    }).catch(function () {
      btn.disabled = false;
      openPayModal("Could not reach the server. Check your connection and " +
        "try again.");
    });
  }

  /* Cancelling, changing a card, switching between the monthly and annual
     plans, and pulling invoices all live on Stripe's own portal rather than
     being rebuilt here. Everything the reader changes there arrives back as a
     webhook this service already handles, so the two sides cannot drift.

     The button navigates rather than opening a tab, for the same reason
     `startCheckout` does: a popup blocker eats `window.open`, and this is a
     navigation the reader asked for. */
  function openPortal() {
    var btn = $("open-portal");
    btn.disabled = true;
    note($("portal-note"), "Opening Stripe…");
    api("/api/billing/portal", { method: "POST" }).then(function (r) {
      if (r.ok && r.data && r.data.url) {
        window.location.href = r.data.url;
        return;
      }
      btn.disabled = false;
      /* 401 means the cookie went stale while the tab sat open. Saying so is
         more useful than a generic failure, because the fix is a sign-in and
         nothing about the subscription is wrong. */
      if (r.status === 401) {
        note($("portal-note"), "Your session expired. Sign in again to manage billing.", "bad");
        return;
      }
      note($("portal-note"),
        detailOf(r.data, "Could not open the billing portal."), "bad");
    }).catch(function () {
      btn.disabled = false;
      note($("portal-note"), "Could not reach the server.", "bad");
    });
  }

  /* Only reached when the deployment has no Stripe configuration at all, so
     it must not promise a card form that cannot open. */
  function payText(what, price) {
    return (
      what + " cannot be bought by card on this deployment (" + price +
      "). Email " + (CFG.adminEmail || "the site owner") + " with your API " +
      "key and it will be sorted out by hand."
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
        openPayModal(payText("The full dataset", "a one-time " + CFG.datasetPrice));
        return;
      }
      /* The download authenticates with the KEY, not the session cookie, and
         the key is only here if this browser stored it when it was issued.
         Saying so beats sending `api_key=` and letting it 401. */
      if (!getKey()) {
        note(
          $("dl-note"),
          "This browser does not have your key — it is stored hashed and "
            + "shown only when issued. Press Regenerate above to issue a new "
            + "one, then download.",
          "bad"
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

  /* ---- waiting for the webhook ------------------------------------------
     Arriving from a paid checkout is a RACE the buyer always used to lose
     quietly: the browser redirect and Stripe's webhook are two independent
     trips, the page loaded once, and if the redirect won there was no poll and
     no retry -- so a successful purchase showed pre-purchase state until
     somebody thought to reload. A first-time buyer never even saw the grant
     banner, because it needs a prior visit to have seeded localStorage.

     So when `?checkout=success` is on the URL, say plainly that the payment
     landed, then re-read status until the grant appears. Bounded: eight tries
     over about twenty seconds, then a sentence naming what to do instead. It
     never claims failure -- fulfilment is at-least-once and may simply be
     slow, and telling somebody their payment did not work when it did is the
     one message worse than saying nothing. */
  var WAIT_TRIES = 8;
  var WAIT_MS = 2500;

  function announce(text) {
    $("banner-text").textContent = text;
    show($("grant-banner"), true);
  }

  function awaitGrant(before, tries) {
    api("/api/user/status").then(function (r) {
      if (!r.ok) return;
      var now = r.data.tier + "|" + (r.data.has_paid_download ? "d" : "-");
      if (now !== before) {
        renderStatus(r.data);
        announce(
          r.data.has_paid_download && before.slice(-1) !== "d"
            ? "Payment received — the full dataset is unlocked. Download it below."
            : "Payment received — Pro is active. You have " +
              r.data.calls_limit.toLocaleString() + " API calls a month."
        );
        return;
      }
      if (tries > 1) {
        setTimeout(function () { awaitGrant(before, tries - 1); }, WAIT_MS);
        return;
      }
      announce(
        "Payment received. Stripe has not confirmed it to us yet — this is " +
        "normally seconds. Reload in a minute, and if it is still not here " +
        "email " + (CFG.adminEmail || "the site owner") + "."
      );
    }).catch(function () { /* a dropped poll is not worth a message */ });
  }

  function watchForCheckout(s) {
    if (location.search.indexOf("checkout=success") === -1) return;
    var now = s.tier + "|" + (s.has_paid_download ? "d" : "-");
    announce("Payment received. Unlocking your account…");
    setTimeout(function () { awaitGrant(now, WAIT_TRIES); }, WAIT_MS);
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
      startCheckout("pro", "Pro");
    });
    $("buy-pro-annual").addEventListener("click", function () {
      startCheckout("pro_annual", "Pro annual");
    });
    $("open-portal").addEventListener("click", openPortal);
    $("buy-data").addEventListener("click", function () {
      startCheckout("dataset", "The full dataset");
    });
    $("pay-close").addEventListener("click", function () {
      closeModal($("pay-modal"));
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
