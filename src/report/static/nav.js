/* To Scale — navigation: the mobile collapse, and why Dashboard is greyed.
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

  document.addEventListener("DOMContentLoaded", function () {
    wireToggle();
    wireDisabledDashboard();
  });
})();
