/* BalanceProof -- the sign-up prompt on company pages and blog posts.

   A small card that slides in at the bottom right (a short bottom sheet on a
   phone) once the reader has reached the end of the company's facts or of the
   post: the marker `[data-nudge-end]` scrolling into view. Never on load, never
   on a timer, never on exit intent. No overlay, no dimmed page, no focus trap,
   and nothing is focused when it appears.

   Once per visitor: "Close", Esc or a successful sign-up writes `bp.nudge` to
   localStorage and it never shows again. Storage can be blocked (a private
   window, a browser set to refuse site data); every access is wrapped, and
   then it simply shows at most once per page view.

   The form inside is the ordinary free-key form; keyform.js handles it and
   shows the key exactly as the home page does.
*/
(function () {
  "use strict";

  var STORE = "bp.nudge";

  function remembered() {
    try { return !!window.localStorage.getItem(STORE); } catch (e) { return false; }
  }
  function remember(why) {
    try { window.localStorage.setItem(STORE, why); } catch (e) { /* storage blocked */ }
  }

  function init() {
    var card = document.getElementById("signup-nudge");
    var end = document.querySelector("[data-nudge-end]");
    if (!card || !end || remembered()) return;

    var shown = false;
    var closed = false;

    function sheetOpen() {
      /* The site-wide sign-in sheet (nav.js) is its own offer; two at once is
         one too many, so this waits until that one is out of the way. */
      var sheet = document.getElementById("signin-prompt");
      return !!(sheet && !sheet.hidden);
    }

    function show() {
      if (shown || closed || remembered() || sheetOpen()) return;
      shown = true;
      card.hidden = false;
      /* Room below the content for the card, measured, so the end of the page
         can always be scrolled clear of it. */
      document.body.style.paddingBottom = (card.offsetHeight + 32) + "px";
      /* next frame, so the slide-in transition has a start state */
      window.requestAnimationFrame(function () { card.classList.add("in"); });
      document.addEventListener("keydown", onKey);
    }

    function close(why) {
      closed = true;
      remember(why || "closed");
      card.classList.remove("in");
      card.hidden = true;
      document.body.style.paddingBottom = "";
      document.removeEventListener("keydown", onKey);
    }

    function onKey(ev) {
      if (ev.key === "Escape" || ev.key === "Esc") close("closed");
    }

    card.querySelector("[data-nudge-close]").addEventListener("click", function () { close("closed"); });

    /* Checked on scroll only, never on load: the marker has reached the top
       45% of the screen (or gone past it), so the card lands below what was just
       read rather than on top of it -- or, on a page too short to scroll the
       marker that high, the reader has reached the bottom with the marker on
       screen. A scroll listener rather than an IntersectionObserver because the
       marker has no height, and a zero-height target sitting exactly on an
       edge, or skipped past in one fling, is not reliably reported. */
    function onScroll() {
      if (shown || closed) { window.removeEventListener("scroll", onScroll); return; }
      var top = end.getBoundingClientRect().top;
      var doc = document.documentElement;
      var bottom = window.innerHeight + window.scrollY >= doc.scrollHeight - 4;
      if (top <= window.innerHeight * 0.45 || (bottom && top < window.innerHeight)) {
        show();
        if (shown) window.removeEventListener("scroll", onScroll);
      }
    }
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
