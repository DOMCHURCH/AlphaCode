/* Bake the reeded-glass backdrop to still images.
 * ---------------------------------------------------------------------------
 *   node scripts/render_backdrop.mjs
 *
 * The backdrop used to be a live WebGL canvas on every page. It cost a visibly
 * slow first second on both a phone and a laptop, and three rounds of trying to
 * make its composition sit right on a portrait frame each traded one kind of
 * wrong for another. A backdrop is decoration; it does not get to cost the page
 * its first impression. So the shader runs ONCE, here, offline, and the site
 * loads two pictures.
 *
 * WHAT THIS BUYS, beyond the obvious: a still backdrop makes `backdrop-filter`
 * cheap again. The whole frosted-glass treatment on the panels was costing a
 * full gaussian per element per frame because something was animating behind
 * it. With nothing moving, the browser caches each blur once and the frost is
 * free -- so the site keeps the look and drops the entire frame-rate watchdog,
 * the degrade ladder and the render-scale machinery that existed to protect it.
 *
 * TWO ORIENTATIONS, NOT ONE CROPPED. `object-fit: cover` on a 2400x1350 image
 * in a 390x844 frame scales to HEIGHT and throws away most of the width, which
 * is how the phone ended up looking like a different product. The portrait
 * variant is composed for the frame it will be shown in.
 *
 * THE FRAME IS CHOSEN BY MEASUREMENT. The composition drifts on roughly an
 * 11-second cycle and some moments of it sit low. Picking one by eye is exactly
 * what failed repeatedly, so this renders a sweep of candidate times, measures
 * the vertical brightness centroid of each, and keeps the frame closest to the
 * middle of the frame. `bias` is forced to 0 for the same reason: the standing
 * downward offset on the warm lobe reads as depth on a wide screen and as a
 * fault on a tall one.
 */

import { chromium } from 'playwright';
import { readFileSync, writeFileSync, mkdirSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const STATIC = join(ROOT, 'src', 'report', 'static');
const MEDIA = join(STATIC, 'media');

/* THE SHADER LIVES HERE NOW.
   `src/report/static/reeded.js` is gone -- it was the live canvas, and the
   live canvas is what this script exists to replace. The two GLSL sources sit
   next to it as plain files so they are readable, diffable, and editable by
   anyone who wants to re-cut the picture: change the shader, run this, commit
   the four WebPs. */
function extractShaders() {
  return {
    VERT: readFileSync(join(ROOT, 'scripts', 'reeded.vert.glsl'), 'utf8'),
    FRAG: readFileSync(join(ROOT, 'scripts', 'reeded.frag.glsl'), 'utf8'),
  };
}

const SETTINGS = {
  ribs: 19, jitter: 1.0, refract: 1.0, spec: 0.75, ca: 0.45,
  beam: 0.38, bloom: 0.30, sat: 1.10, sweep: 1.46, shimmer: 0.76,
  breathe: 1.77,
  c0: [0.039, 0.039, 0.039],
  c1: [0.137, 0.251, 0.745],
  c2: [0.882, 0.212, 0.173],
  c3: [0.953, 0.761, 0.094],
};

// Portrait gets the phone grade: held at arm's length in daylight, not on a
// calibrated monitor in a dim room.
const PORTRAIT_GRADE = { sat: 1.42, beam: 0.46, bloom: 0.30 };

const VARIANTS = [
  { name: 'backdrop-2400.webp', w: 2400, h: 1350, q: 0.82, grade: null },
  { name: 'backdrop-1200.webp', w: 1200, h: 675, q: 0.80, grade: null },
  { name: 'backdrop-portrait-1200.webp', w: 1200, h: 2400, q: 0.80, grade: PORTRAIT_GRADE },
  { name: 'backdrop-portrait-800.webp', w: 800, h: 1600, q: 0.78, grade: PORTRAIT_GRADE },
];

const PAGE = (VERT, FRAG) => `<!doctype html><meta charset="utf-8">
<body style="margin:0;background:#000">
<canvas id="c"></canvas>
<script>
const VERT = ${JSON.stringify(VERT)};
const FRAG = ${JSON.stringify(FRAG)};
const SET  = ${JSON.stringify(SETTINGS)};

function draw(w, h, t, grade) {
  const S = Object.assign({}, SET, grade || {});
  const c = document.getElementById('c');
  c.width = w; c.height = h;
  const gl = c.getContext('webgl', { alpha:false, antialias:false, depth:false,
                                     stencil:false, preserveDrawingBuffer:true });
  if (!gl) throw new Error('no webgl');
  const sh = (ty, src) => { const s = gl.createShader(ty); gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s; };
  const pr = gl.createProgram();
  gl.attachShader(pr, sh(gl.VERTEX_SHADER, VERT));
  gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, FRAG));
  gl.linkProgram(pr);
  if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(pr));
  gl.useProgram(pr);
  const b = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,1,1]), gl.STATIC_DRAW);
  const a = gl.getAttribLocation(pr, 'p');
  gl.enableVertexAttribArray(a); gl.vertexAttribPointer(a, 2, gl.FLOAT, false, 0, 0);
  const U = {}; for (const n of ['u_res','u_time','u_bars','u_jitter','u_refract','u_spec',
    'u_ca','u_beam','u_bloom','u_sat','u_sweep','u_shimmer','u_breathe','u_c0','u_c1','u_c2',
    'u_c3','u_c1p','u_c2p','u_beamOff','u_half']) U[n] = gl.getUniformLocation(pr, n);

  gl.viewport(0, 0, w, h);
  gl.uniform2f(U.u_res, w, h);
  gl.uniform1f(U.u_bars, S.ribs);      gl.uniform1f(U.u_jitter, S.jitter);
  gl.uniform1f(U.u_refract, S.refract); gl.uniform1f(U.u_spec, S.spec);
  gl.uniform1f(U.u_ca, S.ca);          gl.uniform1f(U.u_beam, S.beam);
  gl.uniform1f(U.u_bloom, S.bloom);    gl.uniform1f(U.u_sat, S.sat);
  gl.uniform1f(U.u_sweep, S.sweep);    gl.uniform1f(U.u_shimmer, S.shimmer);
  gl.uniform1f(U.u_breathe, S.breathe);
  gl.uniform3fv(U.u_c0, S.c0); gl.uniform3fv(U.u_c1, S.c1);
  gl.uniform3fv(U.u_c2, S.c2); gl.uniform3fv(U.u_c3, S.c3);

  const unit = Math.min(w, h), hx = 0.5*w/unit, hy = 0.5*h/unit, sx = hx/0.80;
  gl.uniform2f(U.u_half, hx, hy);
  // bias 0: the standing downward offset is the thing being removed.
  gl.uniform2f(U.u_c1p, Math.sin(t*0.17)*0.58*sx, Math.cos(t*0.13)*0.34);
  gl.uniform2f(U.u_c2p, (Math.cos(t*0.11)*0.80 + 0.18)*sx, Math.sin(t*0.19)*0.42);
  gl.uniform1f(U.u_beamOff, Math.sin(t*0.09)*0.42);
  gl.uniform1f(U.u_time, t);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  return c;
}

/* THE FRAME IS SCORED THROUGH THE VEIL, NOT BARE.
 *
 * The bare canvas is near-saturated at every moment of its cycle -- that is by
 * design, the veil is what turns it into a backdrop. So scoring the bare canvas
 * rewards whichever frame is MOST blown out, which is how the first pass picked
 * a flat wall of colour with "lit=100%" and called it the winner.
 *
 * This composites the same gradients backdrop.css lays over it, then measures.
 * The image written to disk is still the bare canvas, because the CSS veil is
 * what the page applies -- this only decides WHICH frame to keep. */
const VEILS = {
  landscape: {
    radial: { rx: 1.20, ry: 0.80, cx: 0.5, cy: 0.38,
              stops: [[0, 0.30], [0.55, 0.66], [1, 0.88]] },
    linear: [[0, 0.78], [0.42, 0.42], [0.72, 0.55], [1, 0.90]],
  },
  portrait: {
    radial: { rx: 1.35, ry: 0.58, cx: 0.5, cy: 0.50,
              stops: [[0, 0.44], [0.58, 0.68], [1, 0.84]] },
    linear: [[0, 0.72], [0.26, 0.58], [0.62, 0.56], [1, 0.76]],
  },
};

function veil(cx, w, h, kind) {
  const v = VEILS[kind];
  const lin = cx.createLinearGradient(0, 0, 0, h);
  for (const [at, a] of v.linear) lin.addColorStop(at, 'rgba(10,10,10,' + a + ')');
  cx.fillStyle = lin; cx.fillRect(0, 0, w, h);

  const r = v.radial, rx = r.rx * w, ry = r.ry * h, R = Math.max(rx, ry);
  cx.save();
  cx.translate(r.cx * w, r.cy * h);
  cx.scale(rx / R, ry / R);
  const rad = cx.createRadialGradient(0, 0, 0, 0, 0, R);
  for (const [at, a] of r.stops) rad.addColorStop(at, 'rgba(10,10,10,' + a + ')');
  cx.fillStyle = rad;
  cx.fillRect(-w * 3, -h * 3, w * 6, h * 6);
  cx.restore();
}

window.score = (w, h, t, grade, kind) => {
  const sw = Math.min(w, 480), sh = Math.round(sw * h / w);
  const c = draw(sw, sh, t, grade);
  const o = document.createElement('canvas');
  o.width = sw; o.height = sh;
  const cx = o.getContext('2d');
  cx.drawImage(c, 0, 0);
  veil(cx, sw, sh, kind);
  const d = cx.getImageData(0, 0, sw, sh).data;
  const rows = [];
  let num = 0, den = 0;
  for (let y = 0; y < sh; y++) {
    let s = 0;
    for (let x = 0; x < sw; x += 3) {
      const i = (y * sw + x) * 4;
      s += 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
    }
    s /= Math.ceil(sw / 3);
    rows.push(s);
    num += s * (y + 0.5) / sh; den += s;
  }
  const sorted = [...rows].sort((a, b) => a - b);
  const p10 = sorted[Math.floor(sh * 0.10)], p90 = sorted[Math.floor(sh * 0.90)];
  const half = Math.floor(sh / 2);
  const mean = a => a.reduce((x, y) => x + y, 0) / a.length;
  const top = mean(rows.slice(0, half)), bot = mean(rows.slice(half));
  const all = mean(rows);
  return {
    centroid: num / den,
    /* THE METRIC THAT ACTUALLY MATTERS. A luminance centroid can read a
       perfect 0.500 on an image that is obviously bottom-heavy, because a
       large dim area balances a small bright one. This compares the two
       halves directly, which is what the eye is doing. */
    tilt: all > 0 ? (bot - top) / all : 0,
    // How much top-to-bottom swing there is. Near zero is a flat wall; very
    // high is one bright band with darkness around it. Neither is wanted.
    swing: p90 > 0 ? (p90 - p10) / p90 : 0,
  };
};

/* THE VERTICAL PROFILE IS FLATTENED, NOT HOPED FOR.
 *
 * Every attempt to make the live shader sit evenly on a tall frame failed,
 * and the gradient survived centring the lobes AND switching the beam off --
 * so it is somewhere in the rib optics and no choice of frame removes it.
 *
 * A still does not have to argue with that. Each row is measured and given a
 * gain that pulls its mean toward the image's overall mean. The curve is
 * smoothed over a wide window first, so this corrects the broad top-to-bottom
 * drift and leaves every local feature -- rib edges, the specular line, the
 * beam -- untouched. Strength is short of 1.0 on purpose: a dead-flat image
 * looks like wallpaper, and some falloff is what makes it read as light.
 */
window.encode = (w, h, t, grade, q, strength) => {
  const c = draw(w, h, t, grade);
  const o = document.createElement('canvas');
  o.width = w; o.height = h;
  const cx = o.getContext('2d');
  cx.drawImage(c, 0, 0);
  const img = cx.getImageData(0, 0, w, h), d = img.data;

  const rows = new Float64Array(h);
  for (let y = 0; y < h; y++) {
    let s = 0;
    for (let x = 0; x < w; x += 5) {
      const i = (y * w + x) * 4;
      s += 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
    }
    rows[y] = s / Math.ceil(w / 5);
  }
  // Wide box blur: keep the broad drift, discard everything local.
  const R = Math.max(8, Math.round(h * 0.10));
  const sm = new Float64Array(h);
  for (let y = 0; y < h; y++) {
    let s = 0, n = 0;
    for (let k = -R; k <= R; k++) {
      const j = y + k; if (j < 0 || j >= h) continue;
      s += rows[j]; n++;
    }
    sm[y] = s / n;
  }
  let mean = 0; for (let y = 0; y < h; y++) mean += sm[y]; mean /= h;

  for (let y = 0; y < h; y++) {
    let g = sm[y] > 1 ? mean / sm[y] : 1;
    g = Math.pow(g, strength);
    g = Math.min(2.2, Math.max(0.45, g));
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      d[i]     = Math.min(255, d[i]     * g);
      d[i + 1] = Math.min(255, d[i + 1] * g);
      d[i + 2] = Math.min(255, d[i + 2] * g);
    }
  }
  cx.putImageData(img, 0, 0);
  return o.toDataURL('image/webp', q).split(',')[1];
};
</script>`;

const { VERT, FRAG } = extractShaders();
const browser = await chromium.launch({
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const page = await browser.newPage();
page.on('pageerror', e => { console.error('PAGE ERROR:', e.message); });
await page.setContent(PAGE(VERT, FRAG));

mkdirSync(MEDIA, { recursive: true });

for (const v of VARIANTS) {
  // Sweep a full drift cycle and keep the best-centred, well-spread frame.
  let best = null;
  for (let t = 0; t < 40; t += 0.5) {
    const s = await page.evaluate(
      ([w, h, t, g, k]) => window.score(w, h, t, g, k),
      [v.w, v.h, t, v.grade, v.h > v.w ? 'portrait' : 'landscape']);
    /* Centred first and by a long way, then a MODERATE swing: a flat wall
       and a single bright band are both failures, so the target is a middling
       amount of top-to-bottom variation rather than the most or the least. */
    const cost = Math.abs(s.tilt) * 3
               + Math.abs(s.centroid - 0.5) * 4
               + Math.abs(s.swing - 0.35) * 0.5;
    if (!best || cost < best.cost) best = { t, cost, ...s };
  }
  const b64 = await page.evaluate(
    ([w, h, t, g, q, st]) => window.encode(w, h, t, g, q, st),
    [v.w, v.h, best.t, v.grade, v.q, 0.85]);
  const buf = Buffer.from(b64, 'base64');
  writeFileSync(join(MEDIA, v.name), buf);
  console.log(
    `${v.name.padEnd(30)} ${String(v.w).padStart(4)}x${String(v.h).padEnd(4)} ` +
    `t=${String(best.t).padStart(5)}  centroid=${best.centroid.toFixed(3)} ` +
    `tilt=${best.tilt.toFixed(3)} swing=${best.swing.toFixed(2)}  ${(buf.length/1024).toFixed(1)}KB`);
}

// The 24px placeholder, printed as a data URI for backdrop.css.
const lq = await page.evaluate(() => window.encode(24, 14, 6, null, 0.5, 0.85));
writeFileSync(join(MEDIA, 'backdrop-lqip.webp'), Buffer.from(lq, 'base64'));
console.log('\nLQIP data URI for backdrop.css:\ndata:image/webp;base64,' + lq);

await browser.close();
