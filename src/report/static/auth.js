/* To Scale — sign-in and the verify hand-off.
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

  function wireLogin(form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var email = ($("login-email").value || "").trim();
      if (!email) return;
      var btn = $("login-btn");
      btn.disabled = true;
      note($("login-note"), "Sending…");
      postJson("/api/auth/magic-link", { email: email })
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
              " The link lasts 15 minutes — check spam if it is not there.",
            "good"
          );
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

  document.addEventListener("DOMContentLoaded", function () {
    var login = $("login-form");
    if (login) wireLogin(login);
    var verify = $("verify-form");
    if (verify) wireVerify(verify);
  });
})();
