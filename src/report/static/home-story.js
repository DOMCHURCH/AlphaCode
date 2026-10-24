/* BalanceProof home page: the scroll layer (2026-10-20, motion spec v2).
   ---------------------------------------------------------------------------
   GSAP 3.12.5 + ScrollTrigger + Lenis 1.1.20, vendored under /static/vendor
   and loaded on the home page only. Rules this file keeps
   (tests/test_home_motion.py):

   - It runs only while <html> has `motion` (set before paint by the head line,
     never by the server; on by default, off only when the footer toggle
     says off). When the toggle turns motion off, everything is torn
     down: every ScrollTrigger killed, every tween reverted, Lenis destroyed,
     the ticker callback removed, the inline transform/opacity cleared and the
     layout classes (rail-on, how-on) removed, so the page is its static self.
   - It never writes page text or a figure. It moves and fades real, final
     elements. Bands and bars keep the server's true proportions throughout:
     they slide up inside their column, they are never resized.
   - If GSAP or Lenis fails to load, nothing here runs and the page is the
     static layout (the rail is a row, the method is four steps with their
     pictures, every drawing is finished). Exception: the climb's tall track,
     sticky view and hidden start states are CSS under `.motion` (so nothing
     shifts on load); if the libraries fail on a wide screen with motion on,
     the climb is a held still with the bubble hidden until the toggle.

   What each motion explains:
   - The climb (top of the page): the camera closes in on the brass beam at the
     top of the towers, the bubble says what the check is, and the reader rises through
     the clouds into the cream sky that leads to the hero.
   - Built for: the section holds while vertical scroll slides the three
     cards sideways; the card in front is marked and a line shows how far.
   - How the check works: one pinned picture beside the steps. The filing's
     lines appear; assets and liabilities + equity rise to the same height;
     the flagged filing rises short and its gap fills in with the reason.
   - Real filings, drawn: each drawing fills in as it comes into view
     (CSS scroll-driven where supported, this file elsewhere).
   - Parallax: the hero's ledger and the gradient layers move at different
     depths; the study bar fills to its true width (995 of 38,568).
*/
(function () {
  "use strict";

  var root = document.documentElement;
  var state = null;
  var PROPS = "transform,opacity,visibility,--shade";
  var WIDE = "(min-width: 900px) and (min-height: 700px)";
  var NARROW = "(max-width: 899px), (max-height: 699px)";
  // Portrait phones and tablets play the climb too (home-light.css sizes the
  // frame to the width there); landscape phones keep the still.
  var PHONE = "(max-width: 899px) and (min-height: 560px)";

  function ready() { return !!(window.gsap && window.ScrollTrigger && window.Lenis); }
  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }
  function sda() { return root.classList.contains("sda"); }

  /* One drawing filling in: bands rise inside their column, labels in order,
     then the gap (if any). Used where CSS scroll-driven animation is absent. */
  function fill(tl, panel, at) {
    var gap = $(".gapband", panel);
    $$(".bs-col", panel).forEach(function (col, i) {
      var upr = $(".stack:not(.below) > .rise", col);
      var down = $(".stack.below > .rise", col);
      if (upr) tl.from(upr, { yPercent: 101, duration: 1.2, ease: "power2.out" }, at + i * 0.5);
      if (down) tl.from(down, { yPercent: -101, duration: 0.6 }, at + 1 + i * 0.5);
    });
    var labels = $$(".band .bl", panel).filter(function (l) { return !gap || !gap.contains(l); });
    if (labels.length) tl.from(labels, { opacity: 0, duration: 0.4, stagger: 0.08 }, at + 1.2);
    if (gap) tl.from(gap, { opacity: 0, duration: 0.6 }, at + 2.1);
    var lvl = $(".lvl", panel);
    if (lvl) tl.from(lvl, { scaleX: 0, transformOrigin: "0 50%", duration: 0.6 }, at + 2.1);
  }

  function drawings() {
    if (sda()) return;                     // home-light.css runs it on view()
    var gsap = window.gsap;
    $$(".hero-bs .bs-panel").forEach(function (panel) {
      var tl = gsap.timeline({ scrollTrigger: { trigger: panel, start: "top 85%", end: "center 55%", scrub: 0.15 } });
      fill(tl, panel, 0);
    });
    $$(".hero-bs .nt-col").forEach(function (c, i) {
      gsap.from(c, { scaleY: 0, transformOrigin: "50% 100%",
        scrollTrigger: { trigger: c, start: "top 90%", end: "center 60%", scrub: 0.15 } });
    });
    var bar = $(".pbar .pb-fail");
    if (bar) gsap.from(bar, { scaleX: 0, transformOrigin: "0 50%", ease: "none",
      scrollTrigger: { trigger: bar.parentNode, start: "top 90%", end: "top 55%", scrub: 0.15 } });
  }

  /* The method's pictures: lines, the two totals meeting, the flagged gap. */
  function layer(tl, el, at) {
    var gsap = window.gsap;
    var lines = $$(".hv-lines li", el);
    if (lines.length) tl.from(lines, { opacity: 0, x: -10, duration: 0.5, stagger: 0.12 }, at);
    $$(".hb-bar", el).forEach(function (b, i) {
      tl.from(b, { yPercent: 101, duration: 1, ease: "power2.out" }, at + i * 0.6);
    });
    var caps = $$(".hb-cap, .hb-seg span, .hb-neg", el);
    if (caps.length) tl.from(caps, { opacity: 0, duration: 0.4, stagger: 0.08 }, at + 1.2);
    var gap = $(".hb-seg.gap", el);
    if (gap) tl.from(gap, { opacity: 0, duration: 0.6 }, at + 1.6);
    var why = $(".hb-reason", el);
    if (why) tl.from(why, { opacity: 0, y: 8, duration: 0.5 }, at + 1.9);
  }

  function howPinned() {
    var gsap = window.gsap, ST = window.ScrollTrigger;
    var steps = $$(".hstep"), layers = $$(".hlayer");
    if (!steps.length || !layers.length) return;
    root.classList.add("how-on");
    // The stack: arrived[i] runs 0 -> 1 as step i scrolls up into place; the
    // card's depth is how many cards have landed on top of it since.
    var cards = [], arrived = [];
    steps.forEach(function (s) {
      var l = $('.hlayer[data-layer="' + s.getAttribute("data-step") + '"]');
      if (l) cards.push({ el: l, step: s });
    });
    var land = gsap.parseEase("power3.out");
    function render() {
      var p = -1;
      for (var j = 0; j < arrived.length; j++) p += arrived[j];
      cards.forEach(function (c, i) {
        var d = p - i;
        if (d <= -1) { gsap.set(c.el, { autoAlpha: 0, yPercent: 0, y: 90, scaleX: 1.03, scaleY: 1.03, "--shade": 0 }); return; }
        if (d < 0) {                      // settling onto the front of the stack:
          var t = land(1 + d);            // a short rise and a fade, never a second card below
          gsap.set(c.el, { autoAlpha: t, yPercent: 0, y: 90 * (1 - t),
            scaleX: 1.03 - 0.03 * t, scaleY: 1.03 - 0.03 * t, "--shade": 0 });
          return;
        }
        var s = 1 - 0.055 * d;            // stepping back
        gsap.set(c.el, { autoAlpha: d > 3.5 ? 0 : 1, yPercent: 0, y: -30 * d,
          scaleX: s, scaleY: s, "--shade": Math.min(0.5, 0.16 * d) });
      });
    }
    // Stacking order is CSS (later cards on top). Progress comes straight
    // from the trigger: Lenis already smooths the scroll.
    cards.forEach(function (c, i) {
      arrived.push(i === 0 ? 1 : 0);
      if (i > 0) {
        ST.create({ trigger: c.step, start: "top 85%", end: "top 30%",
          onUpdate: function (self) { arrived[i] = self.progress; render(); } });
      }
      var tl = gsap.timeline({ scrollTrigger: { trigger: c.step, start: "top 45%", end: "top 0%", scrub: 0.15 } });
      layer(tl, c.el, 0);
    });
    render();
  }

  function howStacked() {
    var gsap = window.gsap;
    $$(".hlayer").forEach(function (l) {
      var tl = gsap.timeline({ scrollTrigger: { trigger: l, start: "top 85%", end: "center 55%", scrub: 0.15 } });
      layer(tl, l, 0);
    });
  }

  /* Built for: the held section and the sideways rail. */
  function rail() {
    var gsap = window.gsap;
    var pin = $(".rail-pin"), track = $(".rail-track"), view = $(".rail");
    if (!pin || !track || !view) return;
    var cards = $$(".rcard", track), dots = $$(".rail-dots li"), bar = $(".rail-bar i");
    root.classList.add("rail-on");
    window.ScrollTrigger.refresh();
    var current = -1;
    function mark(p) {
      var k = Math.round(p * (cards.length - 1));
      if (k === current) return;
      current = k;
      cards.forEach(function (c, i) { c.classList.toggle("on", i === k); });
      dots.forEach(function (d, i) { d.classList.toggle("on", i === k); });
    }
    mark(0);
    var tl = gsap.timeline({ scrollTrigger: {
      trigger: pin, start: "top top", end: "bottom bottom", scrub: 0.2, invalidateOnRefresh: true,
      onUpdate: function (self) { mark(self.progress); } } });
    tl.to(track, { x: function () { return -Math.max(0, track.scrollWidth - view.clientWidth); }, ease: "none", duration: 1 }, 0);
    if (bar) tl.fromTo(bar, { scaleX: 0 }, { scaleX: 1, transformOrigin: "0 50%", ease: "none", duration: 1 }, 0);
  }

  /* The climb: one scrubbed timeline over the held scene (the track height
     and the sticky view are CSS under .motion, so nothing shifts). The camera
     closes in on the brass beam (its transform-origin is the beam's
     top-centre, set in CSS, so the beam holds still while everything grows
     around it), the foreground splits outward fastest, the ridges drift
     slowest; the bubble on the beam fades in, then its second line, and is
     gone before anything moves under it. Then the scene drops away, the two
     clouds rise past at their own speeds and the sky slides from blue down to
     the cream the page below starts on. */
  function climb() {
    var gsap = window.gsap;
    var stage = $(".climb-stage"), art = $(".climb-art");
    if (!stage || !art) return;
    // Every layer is lazy in the markup (phones and the still never need
    // them). Here they will be scrubbed into view, the clouds included, so
    // fetch and decode them now rather than mid-scroll, where the decode
    // lands as a dropped frame.
    $$(".climb-scene img", art).forEach(function (img) {
      img.loading = "eager";
      if (img.decode) img.decode().catch(function () { /* shown when ready */ });
    });
    var w = function () { return art.clientWidth; };
    var h = function () { return art.clientHeight; };
    var outL = function () { return -0.42 * w(); };
    var outR = function () { return 0.42 * w(); };
    var drop = function () { return 0.62 * h(); };
    // On a phone the towers already fill most of the width: a closer zoom
    // would push the beam off both edges.
    var zoom = window.matchMedia(PHONE).matches ? 1.45 : 1.85;
    var tl = gsap.timeline({ defaults: { ease: "none" }, scrollTrigger: {
      trigger: stage, start: "top top", end: "bottom bottom", scrub: 0.2, invalidateOnRefresh: true } });
    tl.to($(".climb-cam"), { scaleX: zoom, scaleY: zoom, ease: "power1.inOut", duration: 4 }, 0.3);
    tl.to($(".climb-ridges"), { yPercent: -4, duration: 4 }, 0.3);
    tl.to($(".climb-fg-l"), { x: outL, yPercent: 10, scaleX: 1.3, scaleY: 1.3, ease: "power1.in", duration: 3.4 }, 0.3);
    tl.to($(".climb-fg-r"), { x: outR, yPercent: 10, scaleX: 1.3, scaleY: 1.3, ease: "power1.in", duration: 3.4 }, 0.3);
    tl.to($(".climb-cloud-a"), { yPercent: 14, duration: 4 }, 0.3);
    tl.to($(".climb-sky"), { yPercent: 2, duration: 4 }, 0.3);
    tl.to($(".climb-copy"), { autoAlpha: 0, y: -48, duration: 1.4 }, 0.7);
    tl.fromTo($(".climb-bubble"), { autoAlpha: 0, y: 14 }, { autoAlpha: 1, y: 0, ease: "power2.out", duration: 0.8 }, 3.7);
    tl.fromTo($(".climb-bubble .b2"), { autoAlpha: 0, y: 6 }, { autoAlpha: 1, y: 0, duration: 0.6 }, 4.8);
    tl.to($(".climb-bubble"), { autoAlpha: 0, duration: 0.5 }, 6.0);
    tl.to($(".climb-cam"), { y: drop, ease: "power1.in", duration: 3.6 }, 6.5);
    tl.to($(".climb-cam"), { autoAlpha: 0, duration: 1.1 }, 8.3);
    tl.to($(".climb-sky"), { yPercent: -40, ease: "power1.inOut", duration: 4.1 }, 6.3);
    tl.fromTo($(".climb-cloud-b"), { yPercent: 0 }, { yPercent: -200, duration: 4.4 }, 6.0);
    tl.to($(".climb-cloud-a"), { yPercent: -168, duration: 3.2 }, 6.8);
  }

  /* Depth: gradient layers and the hero's ledger used to move at different
     rates here. Removed 2026-10-23: scrolling a layer that holds a live WebGL
     canvas smeared the hero's text on the owner's two-GPU laptop (copies of the
     headline repeated down the page). The backgrounds now stay put. */
  function parallax() { /* intentionally empty */ }

  function up() {
    if (state || !root.classList.contains("motion") || !ready()) return;
    var gsap = window.gsap, ST = window.ScrollTrigger;
    try {
      gsap.registerPlugin(ST);
      var lenis = new window.Lenis({
        duration: 0.5,
        prevent: function (node) {
          return !!(node.closest && node.closest("pre, .code, .bs-lines, .tablewrap, table"));
        },
      });
      lenis.on("scroll", ST.update);
      var tick = function (time) { lenis.raf(time * 1000); };
      gsap.ticker.add(tick);
      gsap.ticker.lagSmoothing(0);

      var mm = gsap.matchMedia();
      mm.add({ wide: WIDE, narrow: NARROW, phone: PHONE }, function (ctx) {
        if (ctx.conditions.wide) { climb(); rail(); howPinned(); parallax(); }
        else { if (ctx.conditions.phone) climb(); howStacked(); }
        drawings();
        return function () { root.classList.remove("rail-on", "how-on"); };
      });
      state = { lenis: lenis, tick: tick, mm: mm };
      if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(function () { if (state) ST.refresh(); });
      }
    } catch (e) {
      down();
    }
  }

  function down() {
    var gsap = window.gsap, ST = window.ScrollTrigger;
    root.classList.remove("rail-on", "how-on");
    if (!state) return;
    var s = state;
    state = null;
    try { s.mm.revert(); } catch (e) { /* already gone */ }
    try { ST.getAll().forEach(function (t) { t.kill(); }); } catch (e) { /* none */ }
    try { s.lenis.destroy(); } catch (e) { /* none */ }
    gsap.ticker.remove(s.tick);
    gsap.ticker.lagSmoothing(500, 33);
    gsap.set($$(".hero-bs .rise, .hero-bs .bl, .hero-bs .lvl, .hero-bs .gapband, .hero-bs .nt-col, " +
                ".hlayer, .hlayer li, .hb-bar, .hb-cap, .hb-seg, .hb-seg span, .hb-neg, .hb-reason, " +
                ".rail-track, .rail-bar i, .bg, .hledger, .pb-fail, " +
                ".climb-cam, .climb-ridges, .climb-fg, .climb-sky, .climb-copy, .climb-bubble, " +
                ".climb-bubble .b2, .climb-cloud"),
             { clearProps: PROPS });
    $$(".rcard.on, .rail-dots li.on, .hlayer.cur").forEach(function (el) {
      el.classList.remove("on", "cur");
    });
  }

  function start() {
    if (root.classList.contains("motion")) up();
    document.addEventListener("bp-motion", function (ev) {
      if (ev.detail && ev.detail.on) up(); else down();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
