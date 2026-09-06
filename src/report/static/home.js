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

  document.addEventListener("DOMContentLoaded", function () {
    var form = $("demo-form");
    if (!form) return;               // demo section absent; nothing to wire
    form.addEventListener("submit", run);
  });
})();
