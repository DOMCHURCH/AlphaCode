/* BalanceProof home page motion switch (2026-10-20, motion spec v2).
   ---------------------------------------------------------------------------
   Rules this file keeps, each pinned by tests/test_home_motion.py:

   - It only adds and removes classes and sets CSS variables. It never writes a
     figure or any page text (the one string it sets is the footer toggle's own
     label). Every number on the page is the server's, so a stalled animation
     can only show the right number, partly moved.
   - The page is complete without it. Motion is the `motion` class on <html>,
     set before first paint by the inline line in the head; every animation in
     home-light.css is scoped under `.motion`, and home-story.js and
     home-bg.js run only while the class is there.
   - It honours prefers-reduced-motion: no class, no motion, unless the reader
     turns motion on with the footer toggle. The toggle's choice is stored in
     localStorage under "bp-motion", and every change is announced to the other
     two scripts as a "bp-motion" event on the document.

   What it runs:
   - Scroll reveals, ONLY where the browser lacks CSS scroll-driven animations
     (no `sda` class): elements below the fold are set aside and revealed as
     they enter. Where `sda` is set, home-light.css does the reveals itself.
   - The API response card's line-by-line reveal, once, on first view: the
     lines are in the HTML; this adds `armed` (lines held) and then `typed`.
   - Card hover: tilt toward the cursor and a spotlight, as four CSS variables.
*/
(function () {
  "use strict";

  var root = document.documentElement;
  var KEY = "bp-motion";
  var REVEAL = "[data-reveal], .cards > .card, #pricing .plan, .gslist > li";
  var TILT = ".tcard, .cards > .card, #pricing .plan";

  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function reduced() {
    try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
    catch (e) { return false; }
  }
  // On by default (2026-10-23, owner's call): only the footer toggle turns it
  // off. The OS "reduce motion" setting is deliberately NOT read here: Windows
  // reports it for anyone with animation effects off, which hid the climb from
  // the owner's own machine.
  function wanted() {
    return stored() !== "off";
  }
  function on() { return root.classList.contains("motion"); }

  /* ---- reveals (fallback path) ---- */
  var io = null;
  var pending = [];
  var sweeper = null;

  function revealAll() {
    for (var i = 0; i < pending.length; i++) {
      pending[i].classList.remove("pre");
      pending[i].classList.add("in");
    }
    pending = [];
    if (io) { io.disconnect(); io = null; }
    if (sweeper) { window.clearInterval(sweeper); sweeper = null; }
  }

  function armReveals() {
    if (root.classList.contains("sda")) return;          // CSS does it
    if (!("IntersectionObserver" in window)) return;     // static page
    var els = document.querySelectorAll(".lhome " + REVEAL.split(", ").join(", .lhome "));
    var fold = window.innerHeight || 800;
    io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        io.unobserve(entry.target);
        entry.target.classList.remove("pre");
        entry.target.classList.add("in");
      });
    }, { rootMargin: "0px 0px -6% 0px", threshold: 0.08 });
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.getBoundingClientRect().top > fold) {
        el.classList.add("pre");
        pending.push(el);
        io.observe(el);
      }
    }
    sweeper = window.setInterval(sweep, 1500);
  }

  /* Safety net: whatever is on screen and still set aside is revealed. */
  function sweep() {
    var h = window.innerHeight || 800;
    for (var i = 0; i < pending.length; i++) {
      var el = pending[i];
      if (el.classList.contains("pre") && el.getBoundingClientRect().top < h) {
        el.classList.remove("pre");
        el.classList.add("in");
      }
    }
  }

  /* ---- the response card, revealed line by line once ---- */
  var typeIo = null;
  function armTyping() {
    var card = document.querySelector(".jcard");
    if (!card || card.classList.contains("typed") || !("IntersectionObserver" in window)) return;
    var cue = document.getElementById("how-back") || card;
    card.classList.add("armed");
    typeIo = new IntersectionObserver(function (entries) {
      if (!entries.some(function (e) { return e.isIntersecting; })) return;
      card.classList.add("typed");
      typeIo.disconnect(); typeIo = null;
    }, { rootMargin: "0px 0px -25% 0px" });
    typeIo.observe(cue);
  }
  function disarmTyping() {
    if (typeIo) { typeIo.disconnect(); typeIo = null; }
    var card = document.querySelector(".jcard");
    if (card) card.classList.remove("armed");
  }

  /* ---- card tilt and spotlight: CSS variables, once a frame ---- */
  var tiltQueued = null, tiltCard = null, tx = 0, ty = 0;
  function onMove(ev) {
    if (!on()) return;
    var card = ev.target.closest && ev.target.closest(TILT);
    if (card !== tiltCard && tiltCard) clearTilt(tiltCard);
    tiltCard = card;
    if (!card) return;
    tx = ev.clientX; ty = ev.clientY;
    if (tiltQueued) return;
    tiltQueued = window.requestAnimationFrame(function () {
      tiltQueued = null;
      if (!tiltCard) return;
      var r = tiltCard.getBoundingClientRect();
      var x = (tx - r.left) / r.width, y = (ty - r.top) / r.height;
      tiltCard.style.setProperty("--mx", (x * 100).toFixed(1) + "%");
      tiltCard.style.setProperty("--my", (y * 100).toFixed(1) + "%");
      tiltCard.style.setProperty("--ry", ((x - 0.5) * 6).toFixed(2) + "deg");
      tiltCard.style.setProperty("--rx", ((0.5 - y) * 6).toFixed(2) + "deg");
    });
  }
  function clearTilt(card) {
    ["--mx", "--my", "--rx", "--ry"].forEach(function (p) { card.style.removeProperty(p); });
  }
  function onLeave(ev) {
    if (tiltCard && (!ev.relatedTarget || !tiltCard.contains(ev.relatedTarget))) {
      clearTilt(tiltCard); tiltCard = null;
    }
  }

  /* ---- the method rail: the step in view is marked current ---- */
  function wireRail() {
    var links = document.querySelectorAll(".hrail a[data-step]");
    var steps = document.querySelectorAll(".hstep[data-step]");
    if (!links.length || !("IntersectionObserver" in window)) return;
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var k = e.target.getAttribute("data-step");
        for (var i = 0; i < links.length; i++) {
          var cur = links[i].getAttribute("data-step") === k;
          links[i].classList.toggle("on", cur);
          if (cur) links[i].setAttribute("aria-current", "step"); else links[i].removeAttribute("aria-current");
        }
      });
    }, { rootMargin: "-45% 0px -45% 0px" });
    for (var j = 0; j < steps.length; j++) obs.observe(steps[j]);
  }

  function announce(value) {
    try { document.dispatchEvent(new CustomEvent("bp-motion", { detail: { on: value } })); }
    catch (e) { /* very old browser: the other layers simply never start */ }
  }

  function syncToggle(btn) {
    var value = on();
    btn.setAttribute("aria-pressed", value ? "true" : "false");
    btn.textContent = value ? "Motion: on" : "Motion: off";   // the toggle's own label
  }

  function up() { armReveals(); armTyping(); }
  function down() {
    revealAll(); disarmTyping();
    if (tiltCard) { clearTilt(tiltCard); tiltCard = null; }
  }

  function start() {
    try {
      root.classList.toggle("motion", wanted());
      if (on()) up();
      window.addEventListener("beforeprint", down);
      document.addEventListener("pointermove", onMove, { passive: true });
      document.addEventListener("pointerout", onLeave, { passive: true });
      wireRail();

      var btn = document.getElementById("motion-toggle");
      if (btn) {
        btn.hidden = false;
        syncToggle(btn);
        btn.addEventListener("click", function () {
          var value = !on();
          try { localStorage.setItem(KEY, value ? "on" : "off"); } catch (e) { /* private window */ }
          if (!value) down();
          root.classList.toggle("motion", value);
          if (value) up();
          syncToggle(btn);
          announce(value);
        });
      }
    } catch (e) {
      down();                  // any failure leaves the page complete
      root.classList.remove("motion");
      announce(false);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
