/* BalanceProof — the reeded-glass backdrop.
   ---------------------------------------------------------------------------
   Vertical ribs of glass with light moving behind them, drawn into the
   backdrop layer on every page.

   WHAT IT IS PHYSICALLY, because that is what makes it read as glass rather
   than as stripes: reeded (fluted) glass is a sheet of parallel half-round
   ribs. Each rib is a small CYLINDRICAL LENS, so it does not dim what is
   behind it -- it DISPLACES it sideways, by an amount set by where across the
   rib you are looking. Dead centre of a rib you see straight through; near
   its edge you see a slice from further along the glow. That one idea is the
   whole shader: the sample coordinate is pushed sideways by the rib's local
   slope, and the specular line down each rib, the dark seams and the colour
   fringing all fall out of the same number.

   WHERE IT SITS. Inside `.backdrop`, at z-index 1 -- between the still image
   (z 0) and the veil (z 2). That position is deliberate and load-bearing:

     * the still WebP stays underneath as the fallback, so a browser with no
       WebGL, a failed compile, or a device this switches itself off on gets
       the photograph rather than a black rectangle. Nothing to sequence.
     * the veil stays ON TOP. The veil is what makes copy readable over the
       backdrop, and putting the canvas under it means that contract keeps
       working unchanged rather than having to be re-earned here.

   COST. There is no fbm and no noise octaves: the light behind is four
   smoothsteps and six sines, total, because a large soft glow has no
   high-frequency detail to lose and it gets sliced into ribs afterwards
   regardless. It renders at HALF resolution by default (see SETTINGS.scale)
   and one fullscreen pass, no textures, no framebuffers, no post.

   AND IT GIVES UP IF IT IS SLOW. `watchFrames` samples the real frame rate
   and steps the render scale down, then switches the whole thing off and
   reveals the still image. A backdrop is decoration; it does not get to cost
   the page its smoothness. See DEGRADE.
*/

(function () {
  'use strict';

  /* The settings, as tuned. Every one of these maps to a uniform below.
     Palette is read off the site's own tokens rather than invented:
       #0A0A0A  terminal.css --bg          the ground
       #16204F  a darkened --a0            low
       #3B56D6  dark.css --a0              assets indigo, mid
       #22D3EE  terminal.css --acc         the accent, HOT TIP ONLY
     Red (--l*) and lemon (--yellow) are deliberately absent: those mean
     liabilities and equity, and this is scenery. The accent is the hot tip and
     nothing wider, because the site spends that colour exactly three times
     and the scarcity is the point of it. */
  var SETTINGS = {
    ribs: 19,
    jitter: 1.00,
    refract: 1.00,
    spec: 0.75,
    ca: 0.45,
    speed: 3.00,
    beam: 0.38,
    bloom: 0.30,
    gamma: 1.85,
    sweep: 1.46,
    shimmer: 0.76,
    breathe: 1.77,
    scale: 0.50,
    c0: [0.039, 0.039, 0.039],
    // c1 lifted and c2 held at --a0 exactly. The indigo was there before but
    // barely surfaced: the low stop was almost black, so most of the frame
    // read as ground and the eye only caught the cyan tip.
    c1: [0.129, 0.184, 0.470],
    c2: [0.231, 0.337, 0.839],
    c3: [0.133, 0.827, 0.933]
  };

  /* GIVE-UP LADDER, cheapest loss first. Each step is tried for CHECK_MS
     before the next one is taken.

       frost   drop the frosted glass on the overlays
       0.35    then start cutting render scale
       0.25
       0       then stop drawing and remove the canvas

     Frost goes FIRST on purpose. `backdrop-filter` cost lands on the
     compositor rather than the main thread, so it is invisible to a
     requestAnimationFrame measurement and cannot be measured honestly from a
     headless software rasteriser -- which means the only trustworthy
     benchmark is the reader's own machine, at runtime. Shedding it first
     costs a visual effect; shedding scale first costs the whole look. */
  var DEGRADE = ['frost', 0.35, 0.25, 0];
  // Measured against the 36fps draw cap below, not against vsync.
  var MIN_FPS = 22;
  var CHECK_MS = 1600;

  /* NARROW SCREENS DRAW IT TOO. This used to bail under 720px and hand a
     phone the still image, which made the site look like two different
     products depending on what you opened it on.

     What replaces the cut-off is a cheaper starting point rather than an
     exception: a phone begins at a lower render scale, and the same watchdog
     that protects a desktop protects it. If the device cannot hold the frame
     rate it still ends up on the still image -- it just has to demonstrate
     that rather than be assumed. */
  var NARROW = 820;

  var canvas = document.getElementById('backdrop-gl');
  if (!canvas) { return; }

  function bail() {
    if (canvas && canvas.parentNode) { canvas.parentNode.removeChild(canvas); }
    canvas = null;
    // Give the blurs back: nothing is animating behind them any more, so they
    // are a one-off cost again rather than a per-frame one.
    document.documentElement.classList.remove('reeded-on');
    document.documentElement.classList.remove('reeded-frost');
  }


  var gl = null;
  try {
    gl = canvas.getContext('webgl', {
      alpha: false, antialias: false, depth: false, stencil: false,
      powerPreference: 'low-power', preserveDrawingBuffer: false
    });
  } catch (e) { gl = null; }
  if (!gl) { bail(); return; }

  var VERT = 'attribute vec2 p; void main(){ gl_Position = vec4(p,0.,1.); }';

  var FRAG = [
    'precision mediump float;',
    'uniform vec2  u_res;',
    'uniform float u_time;',
    'uniform float u_bars;',
    'uniform float u_jitter;',
    'uniform float u_refract;',
    'uniform float u_spec;',
    'uniform float u_ca;',
    'uniform float u_beam;',
    'uniform float u_bloom;',
    'uniform float u_gamma;',
    'uniform float u_sweep;',
    'uniform float u_shimmer;',
    'uniform float u_breathe;',
    'uniform vec3  u_c0;',
    'uniform vec3  u_c1;',
    'uniform vec3  u_c2;',
    'uniform vec3  u_c3;',
    '',
    'float hash(float n){ return fract(sin(n * 127.1) * 43758.5453123); }',
    '',
    // The light behind the glass: two drifting lobes plus one diagonal beam.
    // Anisotropic scaling keeps the lobes from reading as circles.
    // The two lobe centres and the beam offset depend only on TIME, not on the
    // pixel -- so they are computed once on the CPU and arrive as uniforms
    // rather than being recomputed, identically, for every pixel on screen.
    // That removes 5 of the 6 sines this function used to run per sample, and
    // it ran three times per pixel.
    'uniform vec2  u_c1p;',
    'uniform vec2  u_c2p;',
    'uniform float u_beamOff;',
    // Half-extents of the frame in p units. The composition is laid out
    // against these rather than against constants, so it fills a
    // portrait frame instead of being a landscape one cropped.
    'uniform vec2  u_half;',
    '',
    'float lightField(vec2 q, float t){',
    // smoothstep on SQUARED distance instead of length(): the falloff is
    // arbitrary anyway, so squaring the bounds gives the same curve shape
    // without the sqrt. Three sqrt per sample x three samples per pixel was
    // real money for a difference nobody can see.
    // Lobe size tracks the frame too: fixed radii on a tall screen are two
    // small blobs adrift in a lot of black.
    '  float grow = max(1.0, u_half.y / 0.5);',
    '  vec2 d1 = (q - u_c1p) * vec2(0.80, 1.35 / grow);',
    '  float g1 = 1.0 - smoothstep(0.0, 1.00 * grow, dot(d1, d1));',
    '  vec2 d2 = (q - u_c2p) * vec2(1.15, 0.95 / grow);',
    '  float g2 = 1.0 - smoothstep(0.0, 0.61 * grow, dot(d2, d2));',
    // normalize() of a literal is a sqrt and a divide per sample, for a
    // constant. Baked.
    '  float across = dot(q, vec2(0.6172, -0.7868)) + u_beamOff;',
    '  float beam = 1.0 - smoothstep(0.0, 0.52 * max(u_half.y, 0.5) * 2.0,',
    '                              abs(across));',
    '  return max(g1 * 0.95 + g2 * 0.72 + beam * u_beam, 0.0);',
    '}',
    '',
    'void main(){',
    '  vec2 uv = gl_FragCoord.xy / u_res;',
    // NORMALISED TO THE SHORT EDGE, not to height.
    //
    // Dividing by height makes rib width a fraction of the HEIGHT, so a
    // portrait phone got the same 19 ribs spread over a p.x range of
    // only +/-0.23 -- about nine fat slabs instead of thirty fine ones.
    // The short edge is the one the ribs run across, so that is what
    // their width should be a fraction of. On a landscape screen the
    // short edge IS the height, so desktop is unchanged to the pixel.
    '  float unit = min(u_res.x, u_res.y);',
    '  vec2 p  = (gl_FragCoord.xy - 0.5 * u_res) / unit;',
    '  float t = u_time;',
    '',
    // Rib widths vary, and the warp PHASE drifts, so the wall slowly pans.
    // Two frequencies: one alone gives a regular comb with a wobble.
    '  float xw = p.x',
    '           + u_jitter * 0.055 * sin(p.x * 3.10 + 1.7 + t * 0.09 * u_breathe)',
    '           + u_jitter * 0.022 * sin(p.x * 8.70 - 0.6 - t * 0.14 * u_breathe);',
    '  float s = xw * u_bars;',
    '  float i = floor(s);',
    '  float f = fract(s);',
    '',
    // -1 at the rib's left edge, 0 at its crown, +1 at its right edge. This IS
    // the surface slope and every optical term below is a function of it.
    '  float lens = (f - 0.5) * 2.0;',
    '  float ribVar = 0.80 + 0.40 * hash(i * 1.37);',
    '',
    // Cubed, not raw: zero at the crown and steepest at the edges, which is
    // how a round surface behaves. A linear ramp looks like a shear.
    '  float bend = lens * lens * lens;',
    '  float disp = bend * 0.085 * u_refract * ribVar;',
    '  float ca = u_ca * 0.012 * abs(bend);',
    '',
    '  vec2 q = p + vec2(disp, 0.0);',
    '  float lg = lightField(q, t);',
    '  float lr = lg;',
    '  float lb = lg;',
    // THREE SAMPLES ONLY WHERE THERE IS A FRINGE. `ca` is proportional to
    // |bend|, which is zero along the crown of every rib, so most of the
    // screen was paying for two extra field evaluations that returned the
    // same number. Threshold rather than always-on.
    '  if (ca > 0.0008) {',
    '    lr = lightField(q + vec2(ca, 0.0), t);',
    '    lb = lightField(q - vec2(ca, 0.0), t);',
    '  }',
    '',
    '  float body = 0.42 + 0.58 * cos(lens * 1.45);',
    '  float seam = 1.0 - 0.75 * smoothstep(0.80, 1.0, abs(lens));',
    '',
    // Specular from an upper-left light: a narrow vertical line slightly off
    // the crown. Modulated by the glow so ribs stay invisible in the dark.
    '  float ndl = max(1.0 - abs(lens + 0.38), 0.0);',
    '  float n2 = ndl * ndl; float n4 = n2 * n2;',
    '  float spec = n4 * n2 * ndl * 0.55 * u_spec * ribVar;',
    '  float gl1 = max(1.0 - abs(lens + 0.62), 0.0);',
    '  float g2a = gl1 * gl1; float g4 = g2a * g2a; float g8 = g4 * g4;',
    '  float glint = g8 * g8 * g8 * g2a * 0.5 * u_spec;',
    '',
    // Per-rib speed AND per-rib frequency. One shared speed made the whole
    // wall pulse in unison, which reads as a single blinking object.
    '  float rs = 0.22 + hash(i * 5.13) * 1.05;',
    '  float travel  = 0.5 + 0.5 * sin(p.y * (1.2 + hash(i * 2.7) * 1.7)',
    '                                  + t * rs + i * 0.22);',
    '  float travel2 = 0.5 + 0.5 * sin(p.y * 3.7 - t * rs * 1.9 + i * 1.13);',
    '  spec *= 0.40 + u_shimmer * (0.70 * travel + 0.36 * travel2);',
    '',
    '  vec3 lit = vec3(lr, lg, lb) * (body * seam);',
    '  lit += (spec + glint) * (0.25 + 0.95 * lg);',
    '',
    // A bright band crossing on a long cycle -- the one EVENT. Everything
    // else here drifts, and drift alone reads as static after a few seconds.
    // Travels the width of THIS frame. Hard-coded to +/-1.6 it spent most
    // of its cycle off the side of a phone, so the one event on the page
    // was invisible there.
    '  float halfW = 0.5 * u_res.x / unit;',
    '  float sweepX = (fract(t * 0.055) * 2.0 - 1.0) * (halfW + 0.45);',
    '  float sx = (p.x - sweepX) * 1.9;',
    '  float sweep = exp(-sx * sx);',
    '  sweep *= 0.55 + 0.45 * sin(p.y * 2.1 + t * 0.8);',
    '  lit += sweep * u_sweep * (0.16 + 0.55 * lg) * (body * seam);',
    '',
    // Intensity through a four-stop ramp: the geometry never touches hue.
    '  float m = clamp(lit.g, 0.0, 1.6);',
    // Indigo owns the middle now: it arrives earlier and cyan is pushed to
    // the top of the ramp, so the accent stays a tip rather than a wash.
    '  vec3 col = mix(u_c0, u_c1, smoothstep(0.02, 0.30, m));',
    '  col = mix(col, u_c2, smoothstep(0.26, 0.70, m));',
    '  col = mix(col, u_c3, smoothstep(0.97, 1.40, m));',
    '',
    // Dispersion applied as a ratio against green, so it tints the ramped
    // colour rather than overwriting it and losing the palette.
    '  if (u_ca > 0.0) {',
    '    float g = max(lit.g, 1e-3);',
    '    col *= clamp(vec3(lit.r / g, 1.0, lit.b / g), 0.55, 1.85);',
    '  }',
    '',
    // Bloom in u_c2, not u_c3. Glowing in the accent was quietly washing
    // cyan across everything bright enough to bloom, which is most of
    // the lit side.
    '  col += u_bloom * 0.34 * smoothstep(0.50, 1.20, m) * u_c2;',
    '  float vig = smoothstep(1.45, 0.30, length(uv - vec2(0.5, 0.52)));',
    '  col *= mix(0.55, 1.05, vig);',
    '  col = clamp(col, 0.0, 1.0);',
    '  col = col * (col * 0.86 + 0.14);',
    '  col += (hash(dot(gl_FragCoord.xy, vec2(0.7, 3.1)) + t) - 0.5) / 210.0;',
    '  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);',
    '}'
  ].join('\n');

  function compile(type, src) {
    var sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) { return null; }
    return sh;
  }

  var vs = compile(gl.VERTEX_SHADER, VERT);
  var fs = compile(gl.FRAGMENT_SHADER, FRAG);
  if (!vs || !fs) { bail(); return; }

  var prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { bail(); return; }
  gl.useProgram(prog);

  var buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER,
    new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  var aloc = gl.getAttribLocation(prog, 'p');
  gl.enableVertexAttribArray(aloc);
  gl.vertexAttribPointer(aloc, 2, gl.FLOAT, false, 0, 0);

  var U = {};
  ['u_res', 'u_time', 'u_bars', 'u_jitter', 'u_refract', 'u_spec', 'u_ca',
   'u_beam', 'u_bloom', 'u_gamma', 'u_sweep', 'u_shimmer', 'u_breathe',
   'u_c0', 'u_c1', 'u_c2', 'u_c3',
   'u_c1p', 'u_c2p', 'u_beamOff', 'u_half'].forEach(function (n) {
    U[n] = gl.getUniformLocation(prog, n);
  });

  // Uniforms that never change after boot are set once, not every frame.
  gl.uniform1f(U.u_bars, SETTINGS.ribs);
  gl.uniform1f(U.u_jitter, SETTINGS.jitter);
  gl.uniform1f(U.u_refract, SETTINGS.refract);
  gl.uniform1f(U.u_spec, SETTINGS.spec);
  gl.uniform1f(U.u_ca, SETTINGS.ca);
  gl.uniform1f(U.u_beam, SETTINGS.beam);
  gl.uniform1f(U.u_bloom, SETTINGS.bloom);
  gl.uniform1f(U.u_gamma, SETTINGS.gamma);
  gl.uniform1f(U.u_sweep, SETTINGS.sweep);
  gl.uniform1f(U.u_shimmer, SETTINGS.shimmer);
  gl.uniform1f(U.u_breathe, SETTINGS.breathe);
  gl.uniform3fv(U.u_c0, SETTINGS.c0);
  gl.uniform3fv(U.u_c1, SETTINGS.c1);
  gl.uniform3fv(U.u_c2, SETTINGS.c2);
  gl.uniform3fv(U.u_c3, SETTINGS.c3);

  var step = 0;
  /* A PHONE GETS A HIGHER SCALE, NOT A LOWER ONE, AND THAT IS NOT A TYPO.

     Cost is the pixel COUNT, and a phone has far fewer pixels to begin with.
     390x844 at 0.75 is 185k pixels; 1440x900 at 0.50 is 324k. So the phone is
     still doing barely half the desktop's work at more than twice the scale
     factor -- the first cut used 0.34 on the reasoning that phones are weak,
     and bought a saving that was never needed by paying for it in the one
     thing this image cannot afford to lose.

     Ribs are thin vertical edges. A soft glow upscales invisibly; an edge
     upscaled 3x on a dpr-3 screen turns to mush, which is most of why the
     phone render looked bad. */
  var scale = window.innerWidth < NARROW ? 0.75 : SETTINGS.scale;

  /* A BACKDROP DOES NOT NEED 60fps. Capped at ~36, which halves the GPU work
     against a vsync-paced loop and is indistinguishable on a drifting glow --
     there is nothing in this image with an edge sharp enough for the
     difference to show. The cap is on DRAWING only: the rAF loop still runs
     every frame, so the clock stays smooth and the page keeps its own
     scheduling. */
  var FRAME_MS = 1000 / 36;

  /* Viewport size is CACHED, not read per frame. `window.innerWidth` is a
     layout read, and doing one inside requestAnimationFrame is a classic way
     to force synchronous layout on every frame of an animation -- the page
     pays for it, not the canvas. Updated on resize, which is when it can
     actually change. */
  var vw = window.innerWidth;
  var vh = window.innerHeight;

  function resize() {
    var w = Math.max(1, Math.round(vw * scale));
    var h = Math.max(1, Math.round(vh * scale));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
      gl.uniform2f(U.u_res, w, h);
    }
  }
  window.addEventListener('resize', function () {
    vw = window.innerWidth;
    vh = window.innerHeight;
    resize();
  }, { passive: true });
  resize();

  /* The blurs come off while this is drawing -- see the `.reeded-on` block in
     backdrop.css. This is the single biggest win available, and it is not in
     the shader: `backdrop-filter` caches while the backdrop holds still, so
     animating behind `nav` turned a cached blur into a per-frame one on every
     page. Removed again by bail(). */
  document.documentElement.classList.add('reeded-on');
  // Frost is opt-IN and separately revocable: see the ladder above.
  document.documentElement.classList.add('reeded-frost');

  /* Reduced motion is honoured as a SLOWDOWN, not a freeze. A still frame of
     this is a perfectly good backdrop, and what triggers motion sickness is
     sudden large movement rather than a crawl.

     It is deliberately not a switch-off. On Windows, the "Show animations"
     system setting being off makes Chrome report
     `prefers-reduced-motion: reduce` permanently, for every site -- so
     freezing on that alone would leave most Windows readers looking at a
     still frame and calling it broken. */
  var rmScale = 1;
  try {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      rmScale = 0.4;
    }
  } catch (e) { /* matchMedia is not worth a broken backdrop */ }

  var clock = 0;
  var last = 0;
  var running = true;
  var frames = 0;
  var lastDraw = 0;
  var windowStart = 0;
  var lowWindows = 0;

  /* THE GIVE-UP LADDER.
     Measured over CHECK_MS windows rather than per frame: a single slow frame
     is a hiccup, two slow seconds is a slow machine. Two consecutive low
     windows step the render scale down, and the last step removes the canvas
     entirely, revealing the still image underneath.

     This exists because the observed frame rate on the author's own machine
     was 10fps -- and a backdrop is scenery. It is not allowed to cost the
     page its smoothness, so it measures itself and leaves. */
  function watchFrames(now) {
    if (!windowStart) { windowStart = now; frames = 0; return; }
    frames++;
    if (now - windowStart < CHECK_MS) { return; }
    var fps = frames * 1000 / (now - windowStart);
    windowStart = now;
    frames = 0;
    if (fps >= MIN_FPS) { lowWindows = 0; return; }
    lowWindows++;
    if (lowWindows < 2) { return; }
    lowWindows = 0;
    var next = DEGRADE[step];
    step++;
    if (next === 'frost') {
      document.documentElement.classList.remove('reeded-frost');
      return;
    }
    if (next === 0 || step > DEGRADE.length) {
      running = false;
      bail();
      return;
    }
    scale = next;
    resize();
  }

  function frame(now) {
    if (!running || !canvas) { return; }
    window.requestAnimationFrame(frame);

    if (!last) { last = now; }
    // Clamped: coming back to a backgrounded tab must not fast-forward the
    // animation by however long the reader was away.
    var dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    clock += dt * SETTINGS.speed * rmScale;

    if (now - lastDraw < FRAME_MS) { return; }
    lastDraw = now;

    var t = clock;

    /* THE COMPOSITION IS LAID OUT AGAINST THE FRAME IT IS IN.
       `hx`/`hy` are the half-extents in shader units: 0.5 on the short edge
       and more on the long one. On a 1440x900 desktop that is (0.80, 0.50) --
       the numbers this was originally tuned against, so nothing moves there.
       On a 390x844 phone it is (0.50, 1.08), and the lobes spread down the
       tall axis instead of bunching in a landscape band across the middle. */
    var unit = Math.min(canvas.width, canvas.height);
    var hx = 0.5 * canvas.width / unit;
    var hy = 0.5 * canvas.height / unit;
    gl.uniform2f(U.u_half, hx, hy);

    // Time-only terms, computed once here rather than identically for every
    // pixel -- five sines per sample, and the shader samples three times.
    // Scaled into the frame: the x terms by hx/0.8 and y by hy/0.5, both of
    // which are 1.0 at the aspect this was tuned at.
    var sx = hx / 0.80;
    var sy = hy / 0.50;
    /* On a tall frame, lift the whole composition toward the top.
       Spreading the lobes down a portrait screen put the light below the
       fold: a phone opened on an almost black first screen, which is the
       only screen most phone readers see. Zero on a landscape frame, where
       sy is 1. */
    var lift = 0.42 * (sy - 1);
    gl.uniform2f(U.u_c1p,
      Math.sin(t * 0.17) * 0.58 * sx,
      (Math.cos(t * 0.13) * 0.34 - 0.10) * sy + lift);
    gl.uniform2f(U.u_c2p,
      (Math.cos(t * 0.11) * 0.80 + 0.18) * sx,
      (Math.sin(t * 0.19) * 0.42 + 0.26) * sy + lift);
    gl.uniform1f(U.u_beamOff, Math.sin(t * 0.09) * 0.42 * sy - lift * 0.6);
    gl.uniform1f(U.u_time, t);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

    watchFrames(now);
  }

  // A backdrop animating behind a hidden tab is pure waste.
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      running = false;
    } else if (canvas) {
      running = true;
      last = 0;
      windowStart = 0;
      window.requestAnimationFrame(frame);
    }
  });

  window.requestAnimationFrame(frame);
})();
