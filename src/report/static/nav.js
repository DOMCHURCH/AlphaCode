/* BalanceProof — navigation: the mobile collapse, and why Dashboard is greyed.
   ---------------------------------------------------------------------------
   Both jobs are enhancements. The bar is rendered by the server, complete and
   correct, before this file loads: the links are already visible and already
   say whether there is a session. What follows only makes the bar smaller on a
   narrow screen and explains one disabled control.

   That is why the toggle button ships hidden and is revealed here. The usual
   arrangement -- collapse with CSS, open with JS -- leaves the menu permanently
   shut if the script fails to arrive, which on a phone with a bad connection is
   exactly when it does.
*/
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function wireToggle() {
    var btn = $("nav-toggle");
    var links = $("nav-links");
    if (!btn || !links) return;

    // The script is here, so the collapsed layout is now safe to use.
    document.documentElement.classList.add("nav-js");
    btn.hidden = false;

    function setOpen(open) {
      links.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.classList.toggle("open", open);
    }

    btn.addEventListener("click", function () {
      setOpen(!links.classList.contains("open"));
    });

    // A menu that stays open after you have chosen from it reads as broken.
    links.addEventListener("click", function (ev) {
      if (ev.target.closest("a")) setOpen(false);
    });

    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && links.classList.contains("open")) {
        setOpen(false);
        btn.focus();
      }
    });
  }

  function wireDisabledDashboard() {
    var btn = $("nav-dash-off");
    var note = $("nav-note");
    if (!btn) return;
    /* `title` is the whole explanation on a desktop and no explanation at all
       on a phone, where nothing hovers. Pressing it says the same thing in a
       place a thumb can reach. */
    btn.addEventListener("click", function () {
      if (!note) return;
      note.textContent = "Please log in to access your dashboard.";
      note.hidden = false;
      window.clearTimeout(note._t);
      note._t = window.setTimeout(function () { note.hidden = true; }, 4000);
    });
  }

  /* ---- the sign-in offer -------------------------------------------------
     Shown once, then not again until the reader has used the service another
     hundred times -- pages read, keyless API calls and MCP calls together.

     THE COUNT IS THE SERVER'S AND THE DISMISSAL IS OURS. The count has to come
     from the server because a browser cannot see API or MCP traffic; the
     dismissal has to stay here because it belongs to one person at one
     keyboard, and keeping it per address would let one person in an office
     silence the offer for the whole floor.

     Storage can throw outright -- a private window, or a browser set to block
     site data -- and the fallback is to say nothing. An offer that cannot
     remember being dismissed is an offer that reappears on every page load,
     which is the one behaviour worse than never showing it. */
  var STORE_KEY = "bp.prompt_after";
  var _prompt = null;
  var _opener = null;

  function readStore(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return undefined; }
  }

  function writeStore(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* no memory, no nag */ }
  }

  function closePrompt() {
    if (!_prompt || _prompt.hidden) return;
    _prompt.hidden = true;
    document.removeEventListener("keydown", onPromptKey, true);
    if (_opener && _opener.focus) _opener.focus();
  }

  /* Escape and the backdrop are both "no", the same as the X. A dialog with
     one way out is a trap, and this one is an offer -- it should be easier to
     leave than to accept. */
  function dismissPrompt(state) {
    /* Guarded because Escape stays wired to the document after the panel
       closes. Without this, every later Escape rewrites the watermark -- and
       once the count has moved on, each rewrite pushes the next offer another
       hundred away, so a reader who likes pressing Escape would never be
       asked again. */
    if (!_prompt || _prompt.hidden) return;
    /* The first "no" is answered with the discount, once, when there is one
       to offer. The second -- from the discount view -- is the real no. */
    if (state.offer && !_dealShown) { showDeal(state); return; }
    writeStore(STORE_KEY, String(state.count + state.interval));
    answer("no", state.count);
    closePrompt();
  }

  var _dealShown = false;

  /* The address's answer, kept on the server as well as here, so a "no" on a
     laptop is not asked again in the phone's browser on the same network or in
     a private window. Fire-and-forget: the local copy already holds. */
  function answer(value, count) {
    try {
      window.fetch("/api/signin-prompt/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer: value, count: count || 0 }),
        keepalive: true
      }).catch(function () {});
    } catch (e) { /* no fetch, no memory */ }
  }

  function showDeal(state) {
    _dealShown = true;
    var ask = $("sp-ask");
    var deal = $("sp-deal");
    var code = $("sp-code-text");
    var claim = $("sp-claim");
    var copy = $("sp-copy");
    var sheet = _prompt.querySelector(".sp-sheet");
    if (!ask || !deal) { _dealShown = true; return; }
    if (code) code.textContent = state.offer.code;
    ask.hidden = true;
    deal.hidden = false;
    if (sheet) sheet.setAttribute("aria-labelledby", "sp-deal-title");
    if (claim) {
      claim.focus();
      claim.addEventListener("click", function () {
        /* Read at checkout by the server, so every buy button on the site
           applies the discount without each one knowing about it. Thirty
           days: long enough to come back after comparing, short enough not
           to linger. */
        var secure = window.location.protocol === "https:" ? "; Secure" : "";
        document.cookie = "bp_offer=" + encodeURIComponent(state.offer.code) +
          "; Path=/; Max-Age=2592000; SameSite=Lax" + secure;
        writeStore(STORE_KEY, "yes");
        answer("yes", state.count);
      });
    }
    if (copy && navigator.clipboard) {
      copy.addEventListener("click", function () {
        navigator.clipboard.writeText(state.offer.code).then(function () {
          copy.textContent = "Copied";
        }).catch(function () {});
      });
    } else if (copy) {
      copy.hidden = true;
    }
  }

  function onPromptKey(ev) {
    if (ev.key !== "Tab") return;
    /* Only what is on screen: the sheet holds two views and the hidden one's
       controls are still in the DOM. */
    var f = Array.prototype.filter.call(_prompt.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    ), function (el) { return el.offsetParent !== null; });
    if (!f.length) return;
    var lo = f[0];
    var hi = f[f.length - 1];
    if (ev.shiftKey && (document.activeElement === lo || !_prompt.contains(document.activeElement))) {
      ev.preventDefault(); hi.focus();
    } else if (!ev.shiftKey && document.activeElement === hi) {
      ev.preventDefault(); lo.focus();
    }
  }

  function showPrompt(state) {
    _opener = document.activeElement;
    _prompt.hidden = false;
    document.addEventListener("keydown", onPromptKey, true);

    var close = $("sp-close");
    var note = $("sp-note");
    var form = $("sp-form");
    var email = $("sp-email");
    var send = $("sp-send");

    /* The sheet is aria-modal, so assistive technology is now being told the
       rest of the page does not exist. Leaving focus out there would strand a
       screen-reader user in content their reader has just hidden. Focus goes
       to the CLOSE button rather than the email field: the first thing offered
       should be the way out, and landing in a text input reads as a demand. */
    if (close) close.focus();

    if (close) close.addEventListener("click", function () { dismissPrompt(state); });
    ["sp-no", "sp-no-deal"].forEach(function (id) {
      var b = $(id);
      if (b) b.addEventListener("click", function () { dismissPrompt(state); });
    });
    _prompt.addEventListener("click", function (ev) {
      if (ev.target === _prompt) dismissPrompt(state);
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && !_prompt.hidden) dismissPrompt(state);
    });

    function say(msg) {
      if (!note) return;
      note.textContent = msg;
      note.hidden = false;
    }

    if (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var address = (email && email.value || "").trim();
        if (!address || address.indexOf("@") < 0) {
          say("That does not look like an email address.");
          return;
        }
        if (send) { send.disabled = true; send.textContent = "Sending…"; }
        window.fetch("/api/auth/magic-link", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: address })
        }).then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (body) {
            /* The endpoint answers the same way whatever the address, so there
               is nothing here to tell one apart from another. 429 and 503 are
               the two it can genuinely refuse on, and both deserve a sentence
               rather than a silent no-op. */
            if (r.status === 429) {
              say("Too many links requested just now. Try again shortly.");
            } else if (!r.ok) {
              say(body.message || body.detail ||
                "Could not send just now. Try /login instead.");
            } else {
              say(body.message || "Check your email for the link.");
              if (form) form.hidden = true;
              writeStore(STORE_KEY, "yes");
              answer("yes", state.count);
              /* Accepting is not dismissing, so it is recorded as the "yes" it
                 is: re-asking somebody who just asked for a link, on this
                 browser or another one on the same address, reads as not
                 having listened. */
            }
          });
        }).catch(function () {
          say("Could not reach the server. Try /login instead.");
        }).then(function () {
          if (send) { send.disabled = false; send.textContent = "Get my free key"; }
        });
      });
    }

    /* Signing in on another tab makes this one stale. `auth.js` broadcasts on
       this key; a panel still offering a sign-in to somebody who just signed
       in is the kind of thing that reads as broken. */
    window.addEventListener("storage", function (ev) {
      if (ev.key === "toscale:signed-in") closePrompt();
    });
  }

  function wireSigninPrompt() {
    _prompt = $("signin-prompt");
    if (!_prompt || !window.fetch) return;

    window.fetch("/api/signin-prompt", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (state) {
        if (!state || state.signed_in || !state.login_enabled) return;
        /* The address's own answer first: a "yes" is final, a "no" holds
           until another interval of use has passed. */
        if (state.answered === "yes") return;
        if (state.after !== null && state.after !== undefined &&
            state.count < state.after) return;
        var after = readStore(STORE_KEY);
        if (after === undefined) return;          // storage blocked: say nothing
        if (after === "yes") return;
        if (after !== null && state.count < Number(after)) return;

        /* NOT at DOMContentLoaded. The page is still drawing, the hero is the
           argument, and a panel over it reads as a paywall on a site whose
           whole claim is that reading is free. Whichever comes first: the
           reader has gone past the fold, or ten seconds have passed. */
        var fired = false;
        function go() {
          if (fired) return;
          fired = true;
          window.removeEventListener("scroll", onScroll);
          window.clearTimeout(timer);
          showPrompt(state);
        }
        function onScroll() {
          if (window.pageYOffset > window.innerHeight * 0.5) go();
        }
        var timer = window.setTimeout(go, 10000);
        window.addEventListener("scroll", onScroll, { passive: true });
      })
      .catch(function () { /* no offer is better than a broken one */ });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireToggle();
    wireDisabledDashboard();
    wireSigninPrompt();
  });
})();
