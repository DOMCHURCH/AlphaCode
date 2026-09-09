/* To Scale — the home page's live API demo.
   ---------------------------------------------------------------------------
   Calls /api/demo/{ticker}, which is the ordinary company endpoint with the
   demo account's key attached SERVER-SIDE. There is deliberately no key in this
   file: a key shipped to every visitor is a published credential, and the
   five-a-day limit it is supposed to demonstrate would be bypassed by anyone
   who opened the page source and called the real endpoint instead.

   Progressive enhancement, like the rest of the site. The section renders and
   reads correctly with this script blocked; only the output panel needs it.
*/
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function note(text, kind) {
    var el = $("demo-note");
    el.textContent = text || "";
    el.className = "formnote" + (kind ? " " + kind : "");
  }

  function showJson(value) {
    var out = $("demo-out");
    /* textContent, never innerHTML. This is server data rendered into the page
       and the company name in it comes from a filing -- the one place a stray
       angle bracket could become markup. */
    out.firstChild.textContent = JSON.stringify(value, null, 2);
    out.hidden = false;
  }

  function run(ev) {
    ev.preventDefault();
    var raw = ($("demo-ticker").value || "").trim();
    if (!raw) return;
    var btn = $("demo-btn");
    btn.disabled = true;
    note("Calling /api/demo/" + raw.toUpperCase() + "…");

    fetch("/api/demo/" + encodeURIComponent(raw), {
      headers: { "Accept": "application/json" }
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          btn.disabled = false;
          if (res.ok) {
            var d = data.demo || {};
            note(
              d.calls_limit
                ? "200 OK — " + d.calls_used_today + " of " + d.calls_limit +
                  " demo calls used today."
                : "200 OK",
              "good"
            );
            showJson(data);
            return;
          }
          /* The server's `detail` already says what to do next -- it names the
             limit, the reset, and where to get a real key. Replacing it with a
             generic message would throw away the only useful part. */
          note(
            res.status + " — " + (data.detail || "That did not work."),
            "bad"
          );
          $("demo-out").hidden = true;
        });
      })
      .catch(function () {
        btn.disabled = false;
        note("Could not reach the server.", "bad");
        $("demo-out").hidden = true;
      });
  }

  /* The pricing cards. Each paid one carries data-plan; the click is turned
     into a Checkout Session and the browser is sent to Stripe.

     Anonymous on purpose: this runs on the public home page, where most
     clickers have no account yet. With no address to send, Stripe collects one
     on its own page, and the webhook registers that address, grants what was
     bought and mails the key -- so a first purchase needs no signup step
     before it. Nothing is granted here; this only opens the door.

     The anchor's href is left intact as the fallback, so a blocked script
     degrades to a working page rather than a dead button. */
  function planNote(text, kind) {
    var el = $("plan-note");
    if (!el) return;
    el.textContent = text || "";
    el.className = "formnote" + (kind ? " " + kind : "");
  }

  function buy(ev, link) {
    var plan = link.getAttribute("data-plan");
    if (!plan) return;
    ev.preventDefault();
    planNote("Opening a secure checkout on Stripe…");
    fetch("/api/billing/checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan: plan }),
      credentials: "same-origin"
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; })
          .then(function (data) { return { ok: res.ok, status: res.status, data: data }; });
      })
      .then(function (r) {
        if (r.ok && r.data && r.data.url) {
          window.location.href = r.data.url;
          return;
        }
        /* Never a redirect to /login on failure -- that is the behaviour this
           replaced. Say what happened and leave the button where it is. */
        planNote(
          (r.data && r.data.detail) ||
          "Could not open a checkout just now. Please try again.",
          "bad"
        );
      })
      .catch(function () {
        planNote("Could not reach the server. Check your connection.", "bad");
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(
      document.querySelectorAll(".plan-cta[data-plan]"),
      function (link) {
        link.addEventListener("click", function (ev) { buy(ev, link); });
      }
    );
    var form = $("demo-form");
    if (!form) return;               // demo section absent; nothing to wire
    form.addEventListener("submit", run);
  });
})();
