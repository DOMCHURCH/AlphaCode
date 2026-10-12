/* BalanceProof -- the free-key form, wherever it appears.

   Shared by the home page (hero and closing call to action) and the sign-up
   prompt on company pages and blog posts (nudge.js), so all of them take a key
   the same way and show the same success state. Moved here unchanged from
   home.js.
*/
/* The free-key forms (the hero's and the closing call to action's) and every
   Copy button on the page.

   `POST /api/auth/register` makes the account and returns the key once, so the
   key is rendered the moment it arrives, with a Copy button and a curl command
   that already has it in. That is the same thing the dashboard's own sign-up
   does, and the key goes into the same browser store, so /dashboard on this
   device finds it too.

   Progressive enhancement, like the rest of the page. With this script blocked
   the form is a GET to /dashboard, which has the sign-up form, and the Copy
   buttons stay hidden: nothing here is needed to read the page or to use the
   tabs, which are CSS. `textContent` throughout: the key is a credential going
   into the page, and there is no version of this where it should be parsed. */
(function () {
  "use strict";

  var KEY_STORE = "toscale.api_key";   // the dashboard's own store; keep in step

  /* Windows, where `curl` in PowerShell is Invoke-WebRequest and only
     `curl.exe` takes curl's flags. userAgentData first (Chromium), then the
     older platform and user-agent strings. A wrong guess costs nothing: the
     command still works in cmd and PowerShell, and every tab is a click away. */
  function onWindows() {
    try {
      var uad = navigator.userAgentData;
      if (uad && uad.platform) return /windows/i.test(uad.platform);
    } catch (e) { /* fall through */ }
    return /win/i.test(navigator.platform || "") || /windows/i.test(navigator.userAgent || "");
  }
  var CURL = onWindows() ? "curl.exe" : "curl";

  function legacyCopy(text) {
    var box = document.createElement("textarea");
    box.value = text;
    box.setAttribute("readonly", "");
    box.style.position = "fixed";
    box.style.opacity = "0";
    document.body.appendChild(box);
    box.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(box);
    return ok;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return legacyCopy(text); }
      );
    }
    return Promise.resolve(legacyCopy(text));
  }

  function wireCopy(btn) {
    var label = btn.textContent;
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var scope = btn.closest("[data-keyout]") || document;
      var from = scope.querySelector(btn.getAttribute("data-copy-from"));
      if (!from) return;
      copyText(from.textContent).then(function (ok) {
        btn.textContent = ok ? "Copied" : "Copy failed";
        setTimeout(function () { btn.textContent = label; }, 1800);
      });
    });
  }

  function detailOf(data, fallback) {
    return data && typeof data.detail === "string" ? data.detail : fallback;
  }

  /* Somebody who has just taken a key must not be offered one a moment later by
     the sign-in sheet (nav.js). That sheet already honours its own "yes" flag,
     which is what it writes when a reader claims the offer, so this sets the
     same one for the next page load. This page's own copy of the sheet has
     already read its state and is waiting on a scroll or a timer, so it is
     taken out of the page: the sheet then does its showing into a node nobody
     can see, and nothing in its code has to change. */
  function quietSigninSheet() {
    try { localStorage.setItem("bp.prompt_after", "yes"); } catch (e) { /* no memory, no nag */ }
    var sheet = document.getElementById("signin-prompt");
    if (sheet) sheet.remove();
  }
  function showKey(key) {
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-keyslot]"),
      function (slot) {
        var out = slot.querySelector("[data-keyout]");
        var form = slot.querySelector("[data-keyform]");
        var ticker = out.getAttribute("data-ticker") || "AAPL";
        out.querySelector("[data-keyvalue]").textContent = key;
        /* One line, no backslash continuation: it has to paste into bash, cmd
           and PowerShell alike -- and `curl.exe` on Windows, where PowerShell's
           `curl` is Invoke-WebRequest. Copy copies exactly what is shown. */
        out.querySelector("[data-keycurl]").textContent =
          CURL + ' -H "X-API-Key: ' + key + '" ' +
          location.origin + "/api/company/" + ticker;
        form.hidden = true;
        out.hidden = false;
      }
    );
  }

  function initForm(form) {
    var note = form.querySelector("[data-keynote]");
    var btn = form.querySelector("button[type=submit]");

    function say(text, kind, linkHref, linkText) {
      note.textContent = text || "";
      note.className = "formnote" + (kind ? " " + kind : "");
      if (linkHref) {
        var a = document.createElement("a");
        a.href = linkHref;
        a.textContent = linkText;
        note.appendChild(a);
      }
    }

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var email = (form.elements.email.value || "").trim();
      if (!email) return;
      btn.disabled = true;
      say("Creating your key…");
      fetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ email: email, accept_terms: true }),
        credentials: "same-origin"
      })
        .then(function (res) {
          return res.json().catch(function () { return {}; })
            .then(function (data) { return { ok: res.ok, status: res.status, data: data }; });
        })
        .then(function (r) {
          btn.disabled = false;
          if (r.ok && r.data && r.data.api_key) {
            try { localStorage.setItem(KEY_STORE, r.data.api_key); } catch (e) { /* private window */ }
            /* Signed up: the prompt on company pages and posts has done its job. */
            try { localStorage.setItem("bp.nudge", "signed-up"); } catch (e) { /* storage blocked */ }
            say("");
            showKey(r.data.api_key);
            quietSigninSheet();
            var heading = document.querySelector("[data-keyout]:not([hidden]) .keyout-h");
            if (heading) heading.focus();
            return;
          }
          if (r.status === 409) {
            /* The key is never sent back for an address that already has one:
               that would turn this open endpoint into a lookup. A sign-in link
               to the inbox is the way back in. */
            say("That address already has a key, and keys are shown only once. ",
                "bad", "/login", "Sign in to replace it");
            return;
          }
          say(detailOf(r.data, "That did not work. Please try again."), "bad");
        })
        .catch(function () {
          btn.disabled = false;
          say("Could not reach the server. Check your connection.", "bad");
        });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    /* The code card opens on the PowerShell tab on Windows. The tabs are radio
       inputs, so this only changes which one starts checked. */
    var ps = document.getElementById("cc-t-ps");
    if (ps && onWindows()) ps.checked = true;
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-copy-from]"), wireCopy
    );
    Array.prototype.forEach.call(
      document.querySelectorAll("form[data-keyform]"), initForm
    );
  });
})();
