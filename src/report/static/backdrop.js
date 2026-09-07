/* To Scale — upgrading the backdrop from a still to a film.
   ---------------------------------------------------------------------------
   The still is already on screen before this file runs. Everything here is an
   upgrade that has to earn its place, and the default answer is no.

   It is a no unless ALL of these hold:

     - the viewport is desktop-width (a phone gets the still, full stop)
     - the page has finished loading (the film never competes with the content)
     - reduced motion has not been asked for
     - the connection is not metered, saving data, or 2g/3g
     - the tab is actually visible

   And it is undone the moment any of that stops being true: the film is paused
   off-screen, and torn down entirely when the page is left. That last part is
   the one people forget -- a <video> left decoding through a navigation keeps a
   phone's radio and GPU busy for a page nobody is looking at any more.
*/
(function () {
  "use strict";

  var WEBM = "/static/media/backdrop.webm";
  var MP4 = "/static/media/backdrop.mp4";
  var MIN_WIDTH = 901;          // matches the CSS breakpoint
  var film = null;

  function reducedMotion() {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) { return false; }
  }

  /* Connection hints are advisory and not everywhere, so an absent API means
     "no reason to refuse" rather than "refuse". Present and saying the
     connection is poor is taken at its word. */
  function connectionIsCheap() {
    var c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (!c) return true;
    if (c.saveData) return false;
    if (/(^|-)(2g|3g)$/.test(c.effectiveType || "")) return false;
    return true;
  }

  function eligible() {
    return window.innerWidth >= MIN_WIDTH &&
      !reducedMotion() &&
      connectionIsCheap();
  }

  function build() {
    var host = document.getElementById("backdrop");
    if (!host || film) return;

    var v = document.createElement("video");
    v.className = "backdrop-film";
    // Every one of these is required for autoplay to be allowed at all: a
    // browser will refuse to start a video that has sound or wants fullscreen.
    v.muted = true;
    v.defaultMuted = true;
    v.autoplay = true;
    v.loop = true;
    v.playsInline = true;
    v.setAttribute("playsinline", "");
    v.setAttribute("aria-hidden", "true");
    v.preload = "auto";
    v.disablePictureInPicture = true;
    v.tabIndex = -1;

    // WebM first: it is ~40% smaller here, and any browser that understands it
    // is the one that should be getting it.
    [[WEBM, "video/webm"], [MP4, "video/mp4"]].forEach(function (pair) {
      var s = document.createElement("source");
      s.src = pair[0];
      s.type = pair[1];
      v.appendChild(s);
    });

    // Only fade it in once there are actually frames to show, so the crossfade
    // never reveals a black box mid-download.
    v.addEventListener("canplay", function () { v.classList.add("on"); });
    /* A failed load is not an error worth surfacing. The still is already
       there and is a complete answer; the film simply does not happen. */
    v.addEventListener("error", teardown);

    host.appendChild(v);
    film = v;
    play();
  }

  function play() {
    if (!film) return;
    var p = film.play();
    // Some browsers reject the promise rather than throwing. Either way the
    // still stays up and nothing is broken.
    if (p && typeof p.catch === "function") p.catch(function () {});
  }

  function teardown() {
    if (!film) return;
    var v = film;
    film = null;
    v.classList.remove("on");
    try {
      v.pause();
      // Emptying the sources and calling load() is what actually makes the
      // browser release the buffered data and stop the fetch. Removing the
      // element alone can leave a download running to completion.
      while (v.firstChild) v.removeChild(v.firstChild);
      v.removeAttribute("src");
      v.load();
    } catch (e) { /* tearing down must not throw */ }
    if (v.parentNode) v.parentNode.removeChild(v);
  }

  function sync() {
    if (!eligible()) { teardown(); return; }
    if (!film) { build(); return; }
    // Visible tab: play. Hidden: pause, but keep what is buffered, because the
    // person is coming back to this page and re-fetching would be worse.
    if (document.hidden) { film.pause(); } else { play(); }
  }

  var resizeTimer = null;
  function onResize() {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(sync, 250);
  }

  function start() {
    if (!eligible()) return;          // never even look at the network
    sync();
    window.addEventListener("resize", onResize);
    document.addEventListener("visibilitychange", sync);

    /* Offload on leaving the page. `pagehide` rather than `unload`, because
       unload is not fired on iOS and blocks the back/forward cache everywhere
       else. On a restore from that cache, put it back. */
    window.addEventListener("pagehide", teardown);
    window.addEventListener("pageshow", function (ev) {
      if (ev.persisted) sync();
    });
  }

  /* After load, not after DOMContentLoaded: the film must never compete with
     the stylesheet, the fonts, or the drawings for bandwidth. A short idle
     window on top of that keeps it out of the way of first interaction. */
  function whenIdle() {
    if (window.requestIdleCallback) {
      window.requestIdleCallback(start, { timeout: 2500 });
    } else {
      window.setTimeout(start, 900);
    }
  }

  if (document.readyState === "complete") {
    whenIdle();
  } else {
    window.addEventListener("load", whenIdle);
  }
})();
