/* BalanceProof home page: drifting brand-colour gradients (2026-10-20).
   ---------------------------------------------------------------------------
   Raw WebGL, no library: one full-screen triangle and a small fragment shader
   that blends four soft shapes in the brand colours (blue, red, yellow,
   off-white) over the band's own background colour. No particles, no grid,
   no 3D objects.

   - Runs only while <html> has `motion`; stops and releases every context
     when the toggle turns motion off (the "bp-motion" event).
   - Draws only the bands that are on screen, and nothing while the tab is
     hidden. Rendered at half resolution: the shapes are soft, so it costs
     little and looks the same.
   - If WebGL is unavailable or fails, the canvas never appears and the band
     keeps its static CSS gradient (home-light.css, `.bg`).
   - It never touches page text or a figure; the shapes sit behind the
     content, at an alpha that keeps the headline contrast high in every frame.
*/
(function () {
  "use strict";

  var root = document.documentElement;
  var VS = "attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}";
  var FS = [
    "precision mediump float;",
    "uniform vec2 r;uniform float t;uniform vec3 base;uniform float a;",
    "const vec3 B=vec3(.137,.251,.745);const vec3 R=vec3(.918,.318,.282);",
    "const vec3 Y=vec3(.969,.847,.282);const vec3 W=vec3(.98,.973,.953);",
    "float blob(vec2 uv,vec2 c,float s){vec2 d=(uv-c)*vec2(r.x/r.y,1.);return exp(-dot(d,d)/s);}",
    "void main(){vec2 uv=gl_FragCoord.xy/r;vec3 col=base;",
    "col=mix(col,B,a*blob(uv,vec2(.22+.12*sin(t*.70),.70+.10*cos(t*.50)),.10));",
    "col=mix(col,R,a*.85*blob(uv,vec2(.80+.10*cos(t*.60),.30+.12*sin(t*.80)),.08));",
    "col=mix(col,Y,a*.9*blob(uv,vec2(.62+.14*sin(t*.45+1.7),.82+.08*cos(t*.65)),.07));",
    "col=mix(col,W,a*.5*blob(uv,vec2(.40+.10*cos(t*.55+.6),.20+.10*sin(t*.40)),.09));",
    "gl_FragColor=vec4(col,1.);}",
  ].join("");

  var items = [];
  var raf = null, io = null, ro = null, t0 = 0;

  function rgb(el) {
    var m = (getComputedStyle(el).backgroundColor || "").match(/\d+(\.\d+)?/g);
    if (!m || m.length < 3) return [0.98, 0.973, 0.953];
    return [m[0] / 255, m[1] / 255, m[2] / 255];
  }

  function make(canvas) {
    var gl = canvas.getContext("webgl", { alpha: false, antialias: false, depth: false,
      stencil: false, premultipliedAlpha: false, powerPreference: "low-power" });
    if (!gl) return null;
    function sh(type, src) {
      var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error("shader");
      return s;
    }
    var prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error("link");
    gl.useProgram(prog);
    var buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    var loc = gl.getAttribLocation(prog, "p");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    var host = canvas.closest("section") || canvas.parentNode;
    var dark = canvas.getAttribute("data-tone") === "dark";
    gl.uniform3fv(gl.getUniformLocation(prog, "base"), rgb(host));
    gl.uniform1f(gl.getUniformLocation(prog, "a"), dark ? 0.24 : 0.20);
    return { canvas: canvas, gl: gl, ur: gl.getUniformLocation(prog, "r"),
      ut: gl.getUniformLocation(prog, "t"), seen: false, host: host };
  }

  function size(it) {
    var w = Math.max(2, Math.round(it.canvas.clientWidth * 0.35));
    var h = Math.max(2, Math.round(it.canvas.clientHeight * 0.35));
    if (it.canvas.width !== w || it.canvas.height !== h) {
      it.canvas.width = w; it.canvas.height = h;
      it.gl.viewport(0, 0, w, h);
    }
  }

  function frame(now) {
    raf = null;
    if (document.hidden || !root.classList.contains("motion")) return;
    var any = false, t = (now - t0) / 1000 * 0.12 + 2.0;
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (!it.seen) continue;
      any = true;
      size(it);
      it.gl.uniform2f(it.ur, it.canvas.width, it.canvas.height);
      it.gl.uniform1f(it.ut, t);
      it.gl.drawArrays(it.gl.TRIANGLES, 0, 3);
      if (!it.canvas.classList.contains("gl-on")) it.canvas.classList.add("gl-on");
    }
    if (any) raf = window.requestAnimationFrame(frame);
  }
  function kick() { if (!raf && items.length) raf = window.requestAnimationFrame(frame); }

  function up() {
    if (items.length || !root.classList.contains("motion")) return;
    try {
      document.querySelectorAll("canvas.bg-gl").forEach(function (c) {
        var it = make(c);
        if (it) items.push(it);
      });
    } catch (e) { down(); return; }
    if (!items.length) return;                    // no WebGL: the static gradient stays
    t0 = performance.now();
    if ("IntersectionObserver" in window) {
      io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          for (var i = 0; i < items.length; i++) if (items[i].canvas === e.target) items[i].seen = e.isIntersecting;
        });
        kick();
      });
      items.forEach(function (it) { io.observe(it.canvas); });
    } else {
      items.forEach(function (it) { it.seen = true; });
    }
    kick();
  }

  function down() {
    if (raf) { window.cancelAnimationFrame(raf); raf = null; }
    if (io) { io.disconnect(); io = null; }
    items.forEach(function (it) {
      it.canvas.classList.remove("gl-on");
    });
    items = [];
  }

  function start() {
    if (root.classList.contains("motion")) up();
    document.addEventListener("visibilitychange", function () { if (!document.hidden) kick(); });
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
