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
    c1: [0.086, 0.125, 0.310],
    c2: [0.231, 0.337, 0.839],
    c3: [0.133, 0.827, 0.933]
  };

  /* Give-up ladder. Each step is tried for CHECK_MS before the next.
     The last entry is 0, which means stop drawing and remove the canvas. */
  var DEGRADE = [0.50, 0.35, 0.25, 0];
  var MIN_FPS = 24;
  var CHECK_MS = 1600;

  /* Below this viewport width the backdrop is not drawn at all. A phone gets
     the still image: the animation would cost battery for scenery nobody is
     looking at, on the device least able to afford it. */
  var MIN_WIDTH = 720;

  var canvas = document.getElementById('backdrop-gl');
  if (!canvas) { return; }

  function bail() {
    if (canvas && canvas.parentNode) { canvas.parentNode.removeChild(canvas); }
    canvas = null;
  }

  if (window.innerWidth < MIN_WIDTH) { bail(); return; }

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
    'float lightField(vec2 q, float t){',
    '  vec2 c1 = vec2(sin(t * 0.17) * 0.58, cos(t * 0.13) * 0.34 - 0.10);',
    '  float g1 = 1.0 - smoothstep(0.0, 1.00, length((q - c1) * vec2(0.80, 1.35)));',
    '  vec2 c2 = vec2(cos(t * 0.11) * 0.80 + 0.18, sin(t * 0.19) * 0.42 + 0.26);',
    '  float g2 = 1.0 - smoothstep(0.0, 0.78, length((q - c2) * vec2(1.15, 0.95)));',
    '  vec2 bdir = normalize(vec2(0.62, -0.79));',
    '  float across = dot(q, bdir) + sin(t * 0.09) * 0.42;',
    '  float beam = 1.0 - smoothstep(0.0, 0.52, abs(across));',
    '  return max(g1 * 0.95 + g2 * 0.72 + beam * u_beam, 0.0);',
    '}',
    '',
    'void main(){',
    '  vec2 uv = gl_FragCoord.xy / u_res;',
    '  vec2 p  = (gl_FragCoord.xy - 0.5 * u_res) / u_res.y;',
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
    '  float lr = lightField(q + vec2(ca, 0.0), t);',
    '  float lg = lightField(q, t);',
    '  float lb = lightField(q - vec2(ca, 0.0), t);',
    '',
    '  float body = 0.42 + 0.58 * cos(lens * 1.45);',
    '  float seam = 1.0 - 0.75 * smoothstep(0.80, 1.0, abs(lens));',
    '',
    // Specular from an upper-left light: a narrow vertical line slightly off
    // the crown. Modulated by the glow so ribs stay invisible in the dark.
    '  float ndl  = 1.0 - abs(lens + 0.38);',
    '  float spec = pow(max(ndl, 0.0), 7.0) * 0.55 * u_spec * ribVar;',
    '  float glint = pow(max(1.0 - abs(lens + 0.62), 0.0), 26.0) * 0.5 * u_spec;',
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
    '  float sweepX = fract(t * 0.055) * 3.2 - 1.6;',
    '  float sweep = exp(-pow((p.x - sweepX) * 1.9, 2.0));',
    '  sweep *= 0.55 + 0.45 * sin(p.y * 2.1 + t * 0.8);',
    '  lit += sweep * u_sweep * (0.16 + 0.55 * lg) * (body * seam);',
    '',
    // Intensity through a four-stop ramp: the geometry never touches hue.
    '  float m = clamp(lit.g, 0.0, 1.6);',
    '  vec3 col = mix(u_c0, u_c1, smoothstep(0.02, 0.38, m));',
    '  col = mix(col, u_c2, smoothstep(0.34, 0.78, m));',
    '  col = mix(col, u_c3, smoothstep(0.72, 1.18, m));',
    '',
    // Dispersion applied as a ratio against green, so it tints the ramped
    // colour rather than overwriting it and losing the palette.
    '  if (u_ca > 0.0) {',
    '    float g = max(lit.g, 1e-3);',
    '    col *= clamp(vec3(lit.r / g, 1.0, lit.b / g), 0.55, 1.85);',
    '  }',
    '',
    '  col += u_bloom * 0.30 * smoothstep(0.55, 1.25, m) * u_c3;',
    '  float vig = smoothstep(1.45, 0.30, length(uv - vec2(0.5, 0.52)));',
    '  col *= mix(0.55, 1.05, vig);',
    '  col = pow(clamp(col, 0.0, 1.0), vec3(u_gamma));',
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
   'u_c0', 'u_c1', 'u_c2', 'u_c3'].forEach(function (n) {
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
  var scale = DEGRADE[0];

  function resize() {
    var w = Math.max(1, Math.round(window.innerWidth * scale));
    var h = Math.max(1, Math.round(window.innerHeight * scale));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
    }
  }
  window.addEventListener('resize', resize, { passive: true });
  resize();

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
    step++;
    if (step >= DEGRADE.length || DEGRADE[step] === 0) {
      running = false;
      bail();
      return;
    }
    scale = DEGRADE[step];
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

    resize();
    gl.uniform2f(U.u_res, canvas.width, canvas.height);
    gl.uniform1f(U.u_time, clock);
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
