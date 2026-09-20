precision mediump float;
uniform vec2  u_res;
uniform float u_time;
uniform float u_bars;
uniform float u_jitter;
uniform float u_refract;
uniform float u_spec;
uniform float u_ca;
uniform float u_beam;
uniform float u_bloom;
uniform float u_sat;
uniform float u_sweep;
uniform float u_shimmer;
uniform float u_breathe;
uniform vec3  u_c0;
uniform vec3  u_c1;
uniform vec3  u_c2;
uniform vec3  u_c3;

float hash(float n){ return fract(sin(n * 127.1) * 43758.5453123); }

uniform vec2  u_c1p;
uniform vec2  u_c2p;
uniform float u_beamOff;
uniform vec2  u_half;

float lightField(vec2 q, float t){
  vec2 d1 = (q - u_c1p) * vec2(0.80, 1.35);
  float g1 = 1.0 - smoothstep(0.0, 1.00, dot(d1, d1));
  vec2 d2 = (q - u_c2p) * vec2(1.15, 0.95);
  float g2 = 1.0 - smoothstep(0.0, 0.61, dot(d2, d2));
  float across = dot(q, vec2(0.6172, -0.7868)) + u_beamOff;
  float beam = 1.0 - smoothstep(0.0, 0.52, abs(across));
  return max(g1 * 0.95 + g2 * 0.72 + beam * u_beam, 0.0);
}

void main(){
  vec2 uv = gl_FragCoord.xy / u_res;
  float unit = min(u_res.x, u_res.y);
  vec2 p  = (gl_FragCoord.xy - 0.5 * u_res) / unit;
  float ny = p.y * (0.5 / u_half.y);
  float t = u_time;

  float xw = p.x
           + u_jitter * 0.055 * sin(p.x * 3.10 + 1.7 + t * 0.09 * u_breathe)
           + u_jitter * 0.022 * sin(p.x * 8.70 - 0.6 - t * 0.14 * u_breathe);
  float s = xw * u_bars;
  float i = floor(s);
  float f = fract(s);

  float lens = (f - 0.5) * 2.0;
  float ribVar = 0.80 + 0.40 * hash(i * 1.37);

  float bend = lens * lens * lens;
  float disp = bend * 0.085 * u_refract * ribVar;
  float ca = u_ca * 0.012 * abs(bend);

  vec2 q = p + vec2(disp, 0.0);
  q.y = ny + (q.y - p.y);
  float lg = lightField(q, t);
  float lr = lg;
  float lb = lg;
  if (ca > 0.0008) {
    lr = lightField(q + vec2(ca, 0.0), t);
    lb = lightField(q - vec2(ca, 0.0), t);
  }

  float body = 0.42 + 0.58 * cos(lens * 1.45);
  float seam = 1.0 - 0.75 * smoothstep(0.80, 1.0, abs(lens));

  float ndl = max(1.0 - abs(lens + 0.38), 0.0);
  float n2 = ndl * ndl; float n4 = n2 * n2;
  float spec = n4 * n2 * ndl * 0.55 * u_spec * ribVar;
  float gl1 = max(1.0 - abs(lens + 0.62), 0.0);
  float g2a = gl1 * gl1; float g4 = g2a * g2a; float g8 = g4 * g4;
  float glint = g8 * g8 * g8 * g2a * 0.5 * u_spec;

  float rs = 0.22 + hash(i * 5.13) * 1.05;
  float travel  = 0.5 + 0.5 * sin(ny * (1.2 + hash(i * 2.7) * 1.7)
                                  + t * rs + i * 0.22);
  float travel2 = 0.5 + 0.5 * sin(ny * 3.7 - t * rs * 1.9 + i * 1.13);
  spec *= 0.40 + u_shimmer * (0.70 * travel + 0.36 * travel2);

  vec3 lit = vec3(lr, lg, lb) * (body * seam);
  lit += (spec + glint) * (0.25 + 0.95 * lg);

  float halfW = 0.5 * u_res.x / unit;
  float sweepX = (fract(t * 0.055) * 2.0 - 1.0) * (halfW + 0.45);
  float sx = (p.x - sweepX) * 1.9;
  float sweep = exp(-sx * sx);
  sweep *= 0.55 + 0.45 * sin(ny * 2.1 + t * 0.8);
  lit += sweep * u_sweep * (0.16 + 0.55 * lg) * (body * seam);

  float m = clamp(lit.g, 0.0, 1.6);
  float hue = 0.5 + 0.5 * sin(p.x * 3.30 - t * 0.26);
  vec3 tone = mix(u_c1, u_c2, smoothstep(0.26, 0.48, hue));
  tone = mix(tone, u_c3, smoothstep(0.58, 0.80, hue));
  float amb = m + 0.13;
  vec3 col = mix(u_c0, tone, smoothstep(0.02, 0.42, amb));
  col += tone * smoothstep(0.62, 1.20, m) * 0.55;

  if (u_ca > 0.0) {
    float g = max(lit.g, 1e-3);
    col *= clamp(vec3(lit.r / g, 1.0, lit.b / g), 0.55, 1.85);
  }

  col += u_bloom * 0.34 * smoothstep(0.50, 1.20, m) * u_c2;
  float vig = smoothstep(1.45, 0.30, length(uv - vec2(0.5, 0.52)));
  col *= mix(0.55, 1.05, vig);
  col = clamp(col, 0.0, 1.0);
  col = col * (col * 0.86 + 0.14);
  float luma = dot(col, vec3(0.299, 0.587, 0.114));
  col = clamp(mix(vec3(luma), col, u_sat), 0.0, 1.0);
  col += (hash(dot(gl_FragCoord.xy, vec2(0.7, 3.1)) + t) - 0.5) / 210.0;
  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
