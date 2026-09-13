/* BalanceProof — sign-in and the verify hand-off.
   ---------------------------------------------------------------------------
   Two small jobs on two pages:

   /login        post an address, say "check your mail" whatever comes back
   /auth/verify  spend the token with a POST as soon as the page loads

   The POST on verify is the whole design, not an implementation detail. The
   token arrives in a URL that mail scanners and link prefetchers fetch before
   the recipient sees it; a GET that consumed it would mean the real click
   always landed on a used link. A prefetcher does not run this script and does
   not POST, so the token survives until a person actually opens the page.
*/
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function note(el, text, kind) {
    if (!el) return;
    el.textContent = text || "";
    el.className = "formnote" + (kind ? " " + kind : "");
  }

  function detailOf(data, fallback) {
    if (data && typeof data.detail === "string") return data.detail;
    if (data && typeof data.message === "string") return data.message;
    return fallback;
  }

  function accepted() {
    var box = $("accept-terms");
    return !box || box.checked;
  }

  function postJson(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    });
  }

  // ---- /login ---------------------------------------------------------------

  /* ---- following a sign-in that happened in another tab ------------------

     An emailed link opens wherever the mail client decides, which is almost
     always a NEW tab. That tab signs in and goes to the dashboard; the tab the
     person actually typed their address into sits on /login forever, showing
     "check your email" for an account that is now signed in. It looks like the
     link did not work.

     The session is a cookie, so this tab already has it -- it just does not
     know. Two ways of finding out, because neither alone is enough:

       storage event   instant, but only fires in tabs of the same origin and
                       only when another tab writes. Free when it works.
       polling         the fallback, and the only thing that catches a sign-in
                       from a different browser profile or a private window.

     Background tabs get their timers throttled to about once a minute, so a
     check on `visibilitychange` is what makes coming back to the tab feel
     immediate rather than up-to-a-minute slow.

     Bounded by the token's own lifetime: after 15 minutes the link is dead and
     there is nothing left to wait for. */

  var SIGNAL = "toscale:signed-in";
  var POLL_MS = 3000;
  var GIVE_UP_MS = 15 * 60 * 1000;

  function watchForSession() {
    var startedAt = Date.now();
    var timer = null;
    var checking = false;
    var done = false;

    function stop() {
      done = true;
      if (timer) window.clearInterval(timer);
      window.removeEventListener("storage", onStorage);
      document.removeEventListener("visibilitychange", onVisible);
    }

    function check() {
      if (done || checking) return;
      if (Date.now() - startedAt > GIVE_UP_MS) { stop(); return; }
      checking = true;
      fetch("/api/auth/me", {
        credentials: "same-origin",
        headers: { "Accept": "application/json" }
      })
        .then(function (res) {
          checking = false;
          // 401 is the normal answer until the link is clicked, not an error.
          if (!res.ok || done) return;
          stop();
          note($("login-note"), "Signed in. Taking you to the dashboard…", "good");
          window.location.replace("/dashboard");
        })
        .catch(function () { checking = false; });
    }

    function onStorage(ev) { if (ev.key === SIGNAL) check(); }
    function onVisible() { if (!document.hidden) check(); }

    timer = window.setInterval(check, POLL_MS);
    window.addEventListener("storage", onStorage);
    document.addEventListener("visibilitychange", onVisible);
    check();
  }

  function wireLogin(form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var email = ($("login-email").value || "").trim();
      if (!email) return;
      var btn = $("login-btn");
      btn.disabled = true;
      note($("login-note"), "Sending…");
      postJson("/api/auth/magic-link", { email: email, accept_terms: accepted() })
        .then(function (r) {
          if (r.status === 503) {
            btn.disabled = false;
            note($("login-note"), detailOf(r.data, "Login is not configured."), "bad");
            return;
          }
          /* The button stays disabled on success. The reply is deliberately the
             same for a registered and an unregistered address, so there is
             nothing further to do here and re-submitting only burns the
             per-address cooldown. */
          note(
            $("login-note"),
            detailOf(r.data, "Check your email.") +
              " The link lasts 15 minutes — check spam if it is not there. " +
              "Leave this tab open: it will follow you to the dashboard once " +
              "you have clicked it.",
            "good"
          );
          watchForSession();
        })
        .catch(function () {
          btn.disabled = false;
          note($("login-note"), "Could not reach the server.", "bad");
        });
    });
  }

  // ---- /auth/verify ---------------------------------------------------------

  function wireVerify(form) {
    var token = form.querySelector('input[name="token"]').value;

    function fail(message) {
      $("verify-title").textContent = "That link did not work";
      $("verify-msg").textContent = message;
      form.innerHTML =
        '<a class="btn" href="/login">Send a new link</a>';
    }

    function go() {
      postJson("/api/auth/verify", { token: token })
        .then(function (r) {
          if (r.ok) {
            $("verify-msg").textContent = "Signed in. Taking you to the dashboard…";
            /* Wake the tab the address was typed into, which is very likely
               still sitting on /login. Best-effort: storage throws in a
               private window, and the poll covers that case anyway. */
            try { window.localStorage.setItem(SIGNAL, String(Date.now())); }
            catch (e) { /* not worth a word to the user */ }
            /* replace(), not assign(): the token is in this page's URL, and a
               back button that returns here would re-POST a spent token and
               show the failure state for no reason. */
            window.location.replace("/dashboard");
            return;
          }
          fail(detailOf(r.data, "Invalid or expired magic link."));
        })
        .catch(function () {
          fail("Could not reach the server. Try the button again.");
        });
    }

    // Submitting the form by hand is the no-JS path; here it is intercepted.
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      go();
    });
    go();
  }

  // ---- login tabs + password sign-in -----------------------------------------

  function showPane(which) {
    var link = which === "link";
    $("pane-link").hidden = !link;
    $("pane-password").hidden = link;
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-logintab]"), function (b) {
        var on = b.getAttribute("data-logintab") === which;
        b.classList.toggle("on", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
  }

  function wirePasswordPane() {
    var form = $("pw-form");
    if (!form) return;

    Array.prototype.forEach.call(
      document.querySelectorAll("[data-logintab]"), function (b) {
        b.addEventListener("click", function () {
          showPane(b.getAttribute("data-logintab"));
        });
      });

    /* Creating an account and signing in are the same two fields, so they are
       the same form with a different endpoint rather than a second page. */
    var mode = "login";
    $("pw-signup").addEventListener("click", function () {
      mode = mode === "login" ? "register" : "login";
      $("pw-btn").textContent = mode === "login" ? "Sign in" : "Create account";
      $("pw-pass").setAttribute(
        "autocomplete", mode === "login" ? "current-password" : "new-password");
      note($("pw-note"),
        mode === "login" ? "" : "Pick a password of at least 8 characters.");
      $("pw-signup").textContent =
        mode === "login" ? "Create one with a password" : "I already have an account";
    });

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var email = ($("pw-email").value || "").trim();
      var pass = $("pw-pass").value || "";
      if (!email || !pass) return;
      var btn = $("pw-btn");
      btn.disabled = true;
      note($("pw-note"), mode === "login" ? "Signing in…" : "Creating your account…");
      var path = mode === "login"
        ? "/api/auth/login" : "/api/auth/register-password";
      postJson(path, {
        email: email, password: pass, accept_terms: accepted()
      })
        .then(function (r) {
          if (r.ok) { window.location.replace("/dashboard"); return; }
          btn.disabled = false;
          note($("pw-note"), detailOf(r.data, "That did not work."), "bad");
        })
        .catch(function () {
          btn.disabled = false;
          note($("pw-note"), "Could not reach the server.", "bad");
        });
    });

    $("forgot-btn").addEventListener("click", function () {
      var email = ($("pw-email").value || "").trim();
      if (!email) {
        note($("pw-note"), "Put your address in first, then press it again.", "bad");
        $("pw-email").focus();
        return;
      }
      note($("pw-note"), "Sending…");
      /* Recovery is a magic link, not a reset token: click it, then set a new
         password from the Account tab. One token system, not two. */
      postJson("/api/auth/forgot-password", { email: email })
        .then(function (r) {
          note($("pw-note"),
            detailOf(r.data, "Check your email.") +
            " Sign in with it, then set a new password on the Account tab.",
            r.status === 503 ? "bad" : "good");
        })
        .catch(function () {
          note($("pw-note"), "Could not reach the server.", "bad");
        });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wirePasswordPane();
    var login = $("login-form");
    if (login) wireLogin(login);
    var verify = $("verify-form");
    if (verify) wireVerify(verify);
  });
})();
