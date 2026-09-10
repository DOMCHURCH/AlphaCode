/* To Scale — the one animation on the site.
   ---------------------------------------------------------------------------
   The status strip's counts run up to their real value once, on load. That is
   the whole of the motion budget for this page: no fade-slide-up per section,
   no hover glow, no parallax. A data product gets one moment that says "this
   is a live system" and then gets out of the way.

   Three things this does NOT do, each on purpose:

     - It never invents a number. The final value is the one the server already
       rendered into the element, so a blocked script, a thrown error, or a
       reader with reduced motion all leave the correct figure on screen. The
       animation is an effect applied to text that is already right.
     - It never runs for somebody who asked for less motion. The film's
       reduced-motion gate on the old backdrop video was removed with it; this one
       stays, because a number rewriting itself under a reader is exactly the
       kind of motion that setting is asking about, and unlike the film it has
       no atmospheric job that would be lost.
     - It never animates something that is not a number. The element carries the
       target as data-countup, and anything that does not parse is left alone.
*/
(function () {
  "use strict";

  var DURATION_MS = 900;

  function reducedMotion() {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) { return false; }
  }

  /* The server rendered "1.7M" or "6,200". Whatever shape it chose, the
     animation has to end on exactly that string -- so the format is derived
     from the rendered text rather than reimplemented here, and the last frame
     simply restores it. */
  function formatter(finalText, target) {
    var suffix = finalText.match(/[A-Za-z%]+$/);
    var unit = suffix ? suffix[0] : "";
    var head = unit ? finalText.slice(0, -unit.length) : finalText;
    var decimals = (head.split(".")[1] || "").length;
    var grouped = head.indexOf(",") !== -1;
    var scale = target / (parseFloat(head.replace(/,/g, "")) || 1);

    return function (value) {
      var shown = value / scale;
      var text = decimals ? shown.toFixed(decimals) : String(Math.round(shown));
      if (grouped) text = text.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
      return text + unit;
    };
  }

  function run(el) {
    var target = parseFloat(el.getAttribute("data-countup"));
    if (!isFinite(target) || target <= 0) return;

    var finalText = el.textContent;
    var fmt;
    try {
      fmt = formatter(finalText, target);
    } catch (e) { return; }         // an unexpected shape is left as it is

    var started = null;
    function frame(now) {
      if (started === null) started = now;
      var t = Math.min(1, (now - started) / DURATION_MS);
      // Ease out: fast at the start, settling at the end, so the eye reads the
      // final figure rather than watching the last digits crawl.
      var eased = 1 - Math.pow(1 - t, 3);
      if (t >= 1) {
        el.textContent = finalText;   // exactly what the server said
        return;
      }
      el.textContent = fmt(target * eased);
      window.requestAnimationFrame(frame);
    }
    window.requestAnimationFrame(frame);
  }

  function start() {
    if (reducedMotion()) return;
    if (!window.requestAnimationFrame) return;
    var els = document.querySelectorAll("[data-countup]");
    for (var i = 0; i < els.length; i++) run(els[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
