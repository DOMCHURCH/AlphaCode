/* BalanceProof — the home page's live API demo.
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

  /* What the 200 says about the allowance, in the units the allowance is in.

     The daily counter is the only one with a spent count behind it, so it is
     the only branch that can say "x of y". The hourly window knows its size
     and not how much of it you have used, and saying "used today" about an
     hourly limit would be a smaller lie but still a lie. */
  function limitNote(d) {
    if (!d.calls_limit) return "200 OK";
    if (d.limit_window === "day") {
      return "200 OK — " + d.calls_used_today + " of " + d.calls_limit +
        " demo calls used today.";
    }
    return "200 OK — up to " + d.calls_limit +
      " demo calls an hour from one address.";
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
            note(limitNote(d), "good");
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
        if (r.status === 401 && r.data && r.data.login_url) {
          /* The one redirect that is not a guess: a 401 with `login_url` means
             not signed in and nothing else. */
          window.location.href = r.data.login_url;
          return;
        }
        /* Never a redirect to /login on any OTHER failure -- that is the
           behaviour this replaced. Say what happened and leave the button
           where it is. */
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

/* The search box used to carry `autofocus`, and the browser honours that by
   scrolling the focused element into view. On this page the box sits below the
   stat strip, the headline and the lede, so every visit opened part-way down
   the page with the headline off the top -- which reads as though somebody had
   scrolled for you.
   Focusing from here with preventScroll keeps the cursor in the box AND the
   page at the top. Where preventScroll is unsupported the worst case is the
   old behaviour, so there is nothing to fall back to. Skipped entirely if the
   visitor has already scrolled or the URL carries a hash. */
(function () {
  if (window.scrollY > 0 || window.location.hash) return;
  var box = document.getElementById('q');
  if (!box) return;
  try { box.focus({ preventScroll: true }); } catch (e) { /* leave it be */ }
})();

/* The Pro card's billing toggle. Monthly and annual used to be two cards,
   which asked a reader to compare two near-identical lists to find the one
   line that differed. One card now, and everything the period changes is
   swapped here: price, unit, the saving line, the button's label, and -- the
   one that matters -- `data-plan`, which the checkout handler above reads.
   With no JS the card stays as rendered: the monthly plan, with a working
   button. The fallback is a sale, not a dead card. */
(function () {
  var card = document.querySelector(".pro-plan");
  if (!card) return;
  var cta = card.querySelector(".plan-cta");
  var price = card.querySelector("[data-price]");
  var per = card.querySelector("[data-per]");
  var line = card.querySelector("[data-line]");
  if (!cta || !price || !per || !line) return;

  var monthLine = line.innerHTML;
  var yearLine = cta.getAttribute("data-line-year") || monthLine;

  card.querySelectorAll(".bill-opt").forEach(function (b) {
    b.addEventListener("click", function (ev) {
      /* The options are real links to the billing panel so the annual plan is
         buyable with no JavaScript. With JS we would rather swap in place. */
      ev.preventDefault();
      var year = b.getAttribute("data-period") === "year";
      card.querySelectorAll(".bill-opt").forEach(function (o) {
        var on = o === b;
        o.classList.toggle("on", on);
        o.setAttribute("aria-pressed", on ? "true" : "false");
      });
      price.textContent = cta.getAttribute(year ? "data-price-year" : "data-price-month");
      per.textContent = year ? "/year" : "/month";
      line.innerHTML = year ? yearLine : monthLine;
      cta.textContent = year ? "Go Pro annually" : "Go Pro";
      cta.setAttribute("data-plan", cta.getAttribute(year ? "data-plan-year" : "data-plan-month"));
    });
  });
})();
