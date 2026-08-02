/* Diagnostics page. One fetch of /diagnostics.json drives every section and the
   "Copy everything" blob. Auto-refreshes every 10s only while a run/backfill is
   active; otherwise it's manual (Refresh). Read-only except the action buttons. */
"use strict";

let LATEST = null;      // last payload, for copy + client-side log filtering
let LOG_LEVEL = "";     // "", info, warning, error
let TIMER = null;

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function fmtNum(n) {
  return n == null ? "—" : Number(n).toLocaleString("en-US");
}

// ---------------------------------------------------------------- load + render
async function load() {
  if (TIMER) { clearTimeout(TIMER); TIMER = null; }
  try {
    const r = await fetch("/diagnostics.json", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    LATEST = await r.json();
    render(LATEST);
  } catch (e) {
    $("verdict").className = "verdict v-error";
    $("verdict").querySelector(".v-head").textContent = "Diagnostics unreachable";
    $("verdict").querySelector(".v-detail").textContent =
      "Could not load /diagnostics.json (" + e.message + "). The API itself may be down.";
    $("verdict").querySelector(".v-action").textContent = "Retry in a moment.";
    TIMER = setTimeout(load, 10000);
    return;
  }
  // Auto-refresh only while something is running; stop when idle.
  if (LATEST.run_in_progress || LATEST.backfill_running) {
    TIMER = setTimeout(load, 10000);
  }
}

function render(d) {
  renderVerdict(d.verdict);
  renderHealth(d.data_health || {});
  renderLastRun(d.last_run);
  renderConfig(d.config || []);
  renderLogs(d.logs || []);
  renderActions(d.actions || {});
  const t = d.generated_at ? new Date(d.generated_at).toLocaleString() : "—";
  $("stamp").textContent = "updated " + t +
    (LATEST.run_in_progress ? " · run active (auto-refresh 10s)"
      : LATEST.backfill_running ? " · backfill active (auto-refresh 10s)" : " · idle");
}

function renderVerdict(v) {
  v = v || { level: "warn", headline: "No verdict", detail: "", action: "" };
  const lvl = v.level === "error" ? "v-error" : v.level === "warn" ? "v-warn" : "v-ok";
  const s = $("verdict");
  s.className = "verdict " + lvl;
  s.querySelector(".v-head").textContent = v.headline || "—";
  s.querySelector(".v-detail").textContent = v.detail || "";
  const a = s.querySelector(".v-action");
  a.textContent = v.action || "";
  a.style.display = v.action ? "" : "none";
}

function row(k, v, sub) {
  return `<div class="row"><div class="k">${esc(k)}${sub ? `<span class="sub">${esc(sub)}</span>` : ""}</div>` +
    `<div class="v">${v}</div></div>`;
}
function pill(text, kind) { return `<span class="pill ${kind}">${esc(text)}</span>`; }
// A stacked label/value block for values too long to sit beside a label on a
// phone (e.g. the "X adj · Y un · Z incon" count lines).
function statline(k, v, sub) {
  return `<div class="statrow"><div class="k">${esc(k)}</div><div class="vwide">${esc(v)}</div>` +
    (sub ? `<div class="sub">${esc(sub)}</div>` : "") + `</div>`;
}

function renderHealth(h) {
  if (h.error) {
    $("health").innerHTML = `<div class="err-note">Database unreachable: ${esc(h.error)}</div>`;
    return;
  }
  const bf = h.backfill || {}, cov = h.coverage || {}, rec = h.recency || {},
    hist = h.history || {}, adj = h.adjustment || {};
  let out = "";

  // Adjustment — the dangerous one, first and loud. Split-targeted: the
  // denominator is names that ACTUALLY split, not a random draw.
  const sm = adj.summary || {};
  const src = adj.splits_source ? ` · via ${adj.splits_source}` : "";
  if (adj.status === "unadjusted") {
    out += row("Price adjustment", pill("unadjusted", "bad"),
      "Splits read as crashes and split names look deleted — those names' momentum is wrong.");
  } else if (adj.status === "adjusted") {
    out += row("Price adjustment", pill("adjusted", "ok"), `${adj.checked || 0} split names tested${src}`);
  } else if (adj.status === "inconclusive") {
    out += row("Price adjustment", pill("inconclusive", "warn"), `${adj.checked || 0} tested — no clean signal${src}`);
  } else {
    out += row("Price adjustment", pill("not checked", "mute"), "tap “Check now” — samples names that actually split");
  }
  const counts = (x) => `${x.adjusted || 0} adj · ${x.unadjusted || 0} un · ${x.inconclusive || 0} incon · ${x.no_data || 0} n/a`;
  if (adj.checked)
    out += statline(`Split-targeted (${adj.checked})`, counts(sm),
      "names with a known split in the window — this is the real rate");
  const cs = (adj.control && adj.control.summary) || {};
  if (adj.control && adj.control.tested)
    out += statline(`Random control (${adj.control.tested})`, counts(cs),
      "random names rarely split in-window — inconclusive here is expected, not a problem");

  // Backfill source.
  const avail = (bf.sources_available || []).join(", ") || "—";
  const phasePill = bf.phase === "error" ? pill("error", "bad")
    : bf.phase === "done" ? pill("done", "ok")
      : bf.phase === "running" ? pill("running", "warn") : pill(bf.phase || "idle", "mute");
  out += row("Backfill source", `${esc(bf.source || "—")} ${phasePill}`, "available: " + avail);
  if (bf.last_error) out += row("Backfill error", `<span style="color:var(--red)">${esc(bf.last_error)}</span>`);
  out += row("Polygon tier", esc(bf.polygon_tier || "—"));

  // Sector map — the "why is sector coverage 0" answer.
  const sm2 = h.sector_map || {};
  if (sm2.error) {
    out += row("Sector map", `<span style="color:var(--red)">${esc(sm2.error)}</span>`);
  } else if ((sm2.total || 0) === 0) {
    out += row("Sector map", pill("empty", "bad"),
      "SIC backfill has not run — every name is sector-unknown, Stage 2 loses sector-neutrality");
  } else {
    const ratio = sm2.mapped_ratio != null ? `${(sm2.mapped_ratio * 100).toFixed(0)}%` : "—";
    const kind = sm2.mapped_ratio >= 0.6 ? "ok" : sm2.mapped_ratio > 0 ? "warn" : "bad";
    out += statline(`Sector map (${fmtNum(sm2.total)} cached)`,
      `${fmtNum(sm2.mapped)} mapped · ${fmtNum(sm2.unmapped)} unmapped · ${pill(ratio, kind)}`);
    const ex = (sm2.sample_mapped || []).slice(0, 5)
      .map((r) => `${esc(r.ticker)} SIC ${esc(r.sic)}→${esc(r.sector)}`).join(" · ");
    if (ex) out += `<div class="sub" style="padding:0 2px 10px;border-bottom:1px solid rgba(22,19,16,.18)">${ex}</div>`;
  }

  // Fundamentals — is the table empty (backfill not run) or thin?
  const fu = h.fundamentals || {};
  if (fu.error) {
    out += row("Fundamentals", `<span style="color:var(--red)">${esc(fu.error)}</span>`);
  } else if ((fu.rows || 0) === 0) {
    out += row("Fundamentals", pill("empty", "bad"),
      "SEC XBRL backfill has not populated the table — quality/value/pead factors are all 0%");
  } else {
    const uc = fu.universe_coverage != null ? `${(fu.universe_coverage * 100).toFixed(0)}%` : "—";
    const kind = fu.universe_coverage >= 0.6 ? "ok" : fu.universe_coverage > 0 ? "warn" : "bad";
    out += statline(`Fundamentals (${fmtNum(fu.rows)} rows)`,
      `${fmtNum(fu.distinct_tickers)} tickers · ${fmtNum(fu.universe_with_fundamentals)}/${fmtNum(fu.universe_total)} of universe · ${pill(uc, kind)}`,
      `latest filing ${fu.latest_filing_date || "—"}`);
  }

  // Coverage.
  out += row("Tickers loaded", fmtNum(cov.tickers_loaded),
    `min for a valid run: ${fmtNum(cov.min_for_valid_run)}`);
  if (cov.sec_universe != null)
    out += row("Vs SEC universe", `${fmtNum(cov.joined_with_sec)} / ${fmtNum(cov.sec_universe)}`,
      cov.join_rate != null ? `join rate ${(cov.join_rate * 100).toFixed(1)}%` : "");

  // Recency.
  const stale = rec.staleness_days;
  const stalePill = stale == null ? pill("no data", "mute")
    : stale > 5 ? pill(stale + "d old", "bad") : stale > 2 ? pill(stale + "d old", "warn") : pill(stale + "d old", "ok");
  out += row("Latest bar", `${esc(rec.latest_bar_date || "—")} ${stalePill}`);

  // History progress bar.
  const pct = hist.pct || 0;
  const barCls = pct >= 100 ? "full" : "warn";
  out += `<div class="row"><div class="k">Trading days<span class="sub">${fmtNum(hist.loaded)} / ${fmtNum(hist.required)} required</span>
      <div class="bar ${barCls}"><i style="width:${Math.min(100, pct)}%"></i></div></div>
      <div class="v">${pct}%</div></div>`;

  // Unreachable sources.
  const errs = h.errors || {};
  for (const [k, v] of Object.entries(errs))
    out += row(esc(k) + " unreachable", `<span style="color:var(--red)">${esc(v)}</span>`);

  if (h.reconcile_checked_at)
    out += row("Data checked", esc(new Date(h.reconcile_checked_at).toLocaleString()));

  // Per-split detail for the unadjusted names (AERT first) — the numbers to
  // judge whether it's a genuine gap or a reverse-split detector artifact.
  const details = adj.details || {};
  const unadj = adj.unadjusted_names || [];
  let det = "";
  for (const t of unadj) {
    for (const d of details[t] || []) {
      const jump = d.price_before != null
        ? `${d.price_before} → ${d.price_after} = ${d.observed_ratio}× · unadj expects ${d.expected_if_unadjusted}×, adj 1×`
        : "";
      det += `<div class="stage fail"><div class="st-name">${esc(t)} — ${esc(d.kind)} split ${esc(d.ratio)} on ${esc(d.ex_date)}</div>` +
        (jump ? `<div class="st-nums">${esc(jump)}</div>` : "") +
        `<div class="st-err">${esc(d.reasoning)}</div></div>`;
    }
  }
  if (det) out += `<div class="detail-head">Unadjusted — split detail</div>${det}`;

  $("health").innerHTML = out;
}

function renderLastRun(lr) {
  const meta = $("lastrun-meta");
  if (!lr) {
    meta.textContent = "";
    $("lastrun").innerHTML = `<div class="loading">No run recorded yet.</div>`;
    return;
  }
  const statusPill = lr.status === "ok" ? pill("ok", "ok")
    : lr.status === "failed" ? pill("failed", "bad") : pill(lr.status, "warn");
  meta.innerHTML = `${esc(lr.as_of)} · ${lr.regime || "—"}`;
  let out = row("Run", statusPill, lr.run_id);
  if (lr.error) out += `<div class="stage fail"><div class="st-err">${esc(lr.error)}</div></div>`;

  for (const s of lr.stages || []) {
    const nums = [
      s.entry != null ? "in " + fmtNum(s.entry) : null,
      s.exit != null ? "out " + fmtNum(s.exit) : null,
      s.duration_s != null ? s.duration_s + "s" : null,
      s.api_calls ? s.api_calls + " calls" : null,
      s.rss_mb != null ? "rss " + s.rss_mb + "MB" : null,
    ].filter(Boolean).join(" · ");
    out += `<div class="stage ${s.failed ? "fail" : ""}">
      <div class="st-top"><span class="st-name">${s.stage}. ${esc(s.label)}</span>
      <span class="st-nums">${esc(nums) || "—"}</span></div>
      ${s.failed ? `<div class="st-err">died here${lr.error ? ": " + esc(lr.error) : ""}</div>` : ""}</div>`;
  }
  $("lastrun").innerHTML = out;
}

function renderConfig(cfg) {
  const kind = { present: "ok", info: "mute", missing: "bad", invalid: "bad" };
  $("config").innerHTML = cfg.map((c) =>
    row(c.name, pill(c.status, kind[c.status] || "mute"), c.note)).join("");
}

function renderLogs(logs) {
  const filtered = filterLogs(logs, LOG_LEVEL);
  if (!filtered.length) {
    $("logs").innerHTML = `<div class="loading" style="padding:10px 2px">no log lines${LOG_LEVEL ? " at this level" : " captured yet"}.</div>`;
    return;
  }
  $("logs").innerHTML = filtered.map((e) => {
    const lv = (e.level || "info").toLowerCase();
    const fields = e.fields && Object.keys(e.fields).length
      ? " " + Object.entries(e.fields).map(([k, v]) => `${k}=${v}`).join(" ") : "";
    const ts = e.ts ? esc(String(e.ts).slice(11, 19)) : "";
    return `<span class="logline ${lv}"><span class="lv">${esc(lv)}</span> ${ts} ` +
      `<span class="ev">${esc(e.event)}</span><span class="fl">${esc(fields)}</span></span>`;
  }).join("");
}

function filterLogs(logs, level) {
  const order = { debug: 10, info: 20, warning: 30, warn: 30, error: 40, critical: 50 };
  const floor = order[level] || 0;
  return logs.filter((e) => (order[(e.level || "info").toLowerCase()] || 20) >= floor);
}

function renderActions(a) {
  document.querySelectorAll(".act").forEach((btn) => {
    const cfg = a[btn.dataset.action] || { enabled: true };
    const label = btn.dataset.label || (btn.dataset.label = btn.textContent.trim());
    btn.disabled = !cfg.enabled;
    btn.innerHTML = esc(label) + (cfg.reason ? `<span class="why">${esc(cfg.reason)}</span>` : "");
  });
}

// ---------------------------------------------------------------- copy everything
function buildCopyText(d) {
  const L = [];
  const push = (s) => L.push(s == null ? "" : s);
  const v = d.verdict || {};
  push("ALPHA FUNNEL — DIAGNOSTICS");
  push("generated: " + (d.generated_at || "—"));
  push("");
  push(`VERDICT [${(v.level || "?").toUpperCase()}]: ${v.headline || "—"}`);
  if (v.detail) push("  " + v.detail);
  if (v.action) push("  → " + v.action);
  push("");

  const h = d.data_health || {};
  push("DATA HEALTH");
  if (h.error) {
    push("  DATABASE UNREACHABLE: " + h.error);
  } else {
    const bf = h.backfill || {}, cov = h.coverage || {}, rec = h.recency || {},
      hist = h.history || {}, adj = h.adjustment || {}, sm = adj.summary || {};
    push(`  Price adjustment: ${adj.status || "unchecked"} (split-targeted` +
      (adj.splits_source ? `, via ${adj.splits_source}` : "") + ")");
    if (adj.checked)
      push(`    tested ${adj.checked}: adj ${sm.adjusted || 0}, unadj ${sm.unadjusted || 0}, ` +
        `incon ${sm.inconclusive || 0}, no-data ${sm.no_data || 0}`);
    const cs = (adj.control && adj.control.summary) || {};
    if (adj.control && adj.control.tested)
      push(`    control ${adj.control.tested} (random): adj ${cs.adjusted || 0}, unadj ${cs.unadjusted || 0}, ` +
        `incon ${cs.inconclusive || 0}, no-data ${cs.no_data || 0}`);
    const bad = adj.unadjusted_names || [];
    if (bad.length) push("    unadjusted: " + bad.join(", "));
    for (const t of bad) {
      for (const d of (adj.details || {})[t] || []) {
        const jump = d.price_before != null
          ? `${d.price_before}→${d.price_after} = ${d.observed_ratio}x (unadj expects ${d.expected_if_unadjusted}x, adj 1x)` : "";
        push(`      ${t} ${d.kind} ${d.ratio} on ${d.ex_date}: ${jump}`);
        push(`        ${d.reasoning}`);
      }
    }
    push(`  Backfill source: ${bf.source || "—"} [${bf.phase || "idle"}] available: ${(bf.sources_available || []).join(", ") || "—"}`);
    if (bf.last_error) push("    error: " + bf.last_error);
    push(`  Polygon tier: ${bf.polygon_tier || "—"}`);
    push(`  Tickers loaded: ${fmtNum(cov.tickers_loaded)} (min ${fmtNum(cov.min_for_valid_run)})`);
    const sm2 = h.sector_map || {};
    if (sm2.error) push(`  Sector map: ERROR ${sm2.error}`);
    else if ((sm2.total || 0) === 0) push("  Sector map: EMPTY (SIC backfill not run)");
    else {
      push(`  Sector map: ${fmtNum(sm2.mapped)}/${fmtNum(sm2.total)} mapped (${((sm2.mapped_ratio || 0) * 100).toFixed(0)}%)`);
      for (const r of (sm2.sample_mapped || []).slice(0, 5)) push(`    ${r.ticker} SIC ${r.sic} (${r.desc || ""}) -> ${r.sector}`);
    }
    const fu = h.fundamentals || {};
    if (fu.error) push(`  Fundamentals: ERROR ${fu.error}`);
    else if ((fu.rows || 0) === 0) push("  Fundamentals: EMPTY (SEC XBRL backfill not run)");
    else push(`  Fundamentals: ${fmtNum(fu.rows)} rows, ${fmtNum(fu.distinct_tickers)} tickers, ${fmtNum(fu.universe_with_fundamentals)}/${fmtNum(fu.universe_total)} of universe, latest ${fu.latest_filing_date || "—"}`);
    if (cov.sec_universe != null) push(`  Vs SEC: ${fmtNum(cov.joined_with_sec)}/${fmtNum(cov.sec_universe)} (${cov.join_rate != null ? (cov.join_rate * 100).toFixed(1) + "%" : "—"})`);
    push(`  Latest bar: ${rec.latest_bar_date || "—"} (${rec.staleness_days == null ? "?" : rec.staleness_days + "d"} stale)`);
    push(`  Trading days: ${fmtNum(hist.loaded)}/${fmtNum(hist.required)} (${hist.pct || 0}%)`);
    for (const [k, val] of Object.entries(h.errors || {})) push(`  ${k} unreachable: ${val}`);
  }
  push("");

  const lr = d.last_run;
  push("LAST RUN");
  if (!lr) { push("  none recorded"); }
  else {
    push(`  ${lr.as_of} [${lr.status}] regime ${lr.regime || "—"} run_id ${lr.run_id}`);
    if (lr.error) push("  ERROR: " + lr.error);
    for (const s of lr.stages || []) {
      const nums = [
        s.entry != null ? "in " + fmtNum(s.entry) : "",
        s.exit != null ? "out " + fmtNum(s.exit) : "",
        s.duration_s != null ? s.duration_s + "s" : "",
        s.api_calls ? s.api_calls + "calls" : "",
        s.rss_mb != null ? "rss " + s.rss_mb + "MB" : "",
      ].filter(Boolean).join(" ");
      push(`  ${s.failed ? "✗" : " "} ${s.stage}. ${s.label}  ${nums}`);
    }
  }
  push("");

  push("CONFIG");
  for (const c of d.config || []) push(`  ${c.name}: ${c.status}${c.note ? " — " + c.note : ""}`);
  push("");

  const logs = filterLogs(d.logs || [], LOG_LEVEL);
  push(`RECENT LOGS (${logs.length}${LOG_LEVEL ? ", " + LOG_LEVEL + "+" : ""})`);
  for (const e of logs) {
    const fields = e.fields && Object.keys(e.fields).length
      ? " " + Object.entries(e.fields).map(([k, val]) => `${k}=${val}`).join(" ") : "";
    push(`  [${(e.level || "info").toUpperCase()}] ${e.ts || ""} ${e.event}${fields}`);
  }
  return L.join("\n");
}

async function copyEverything() {
  if (!LATEST) return;
  const text = buildCopyText(LATEST);
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (e) {
    // Fallback for browsers without the async clipboard API.
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.focus(); ta.select();
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    document.body.removeChild(ta);
  }
  const btn = $("copyBtn"), msg = $("copyMsg");
  btn.classList.toggle("done", ok);
  btn.textContent = ok ? "✓ Copied" : "📋 Copy everything";
  msg.hidden = false;
  msg.textContent = ok ? "Full diagnostics copied — paste into chat." :
    "Couldn't access the clipboard. Long-press to select the page instead.";
  setTimeout(() => { btn.classList.remove("done"); btn.textContent = "📋 Copy everything"; msg.hidden = true; }, 4000);
}

// ---------------------------------------------------------------- actions
async function postAction(action) {
  const map = {
    run: "/run", run_fast: "/run?skip_llm=true", backfill: "/backfill?days=600&sectors=true",
  };
  const url = map[action];
  const msg = $("actionMsg");
  msg.textContent = "sending…";
  try {
    const r = await fetch(url, { method: "POST" });
    const body = await r.json().catch(() => ({}));
    if (r.status === 429) {
      msg.textContent = "Rate limited — " + (body.detail || "try again later.");
    } else {
      msg.textContent = body.detail || (body.accepted ? "Accepted." : "Done.");
    }
  } catch (e) {
    msg.textContent = "Failed: " + e.message;
  }
  setTimeout(load, 800);
}

async function runCheck(kind) {
  const btn = kind === "reconcile" ? $("reconcileBtn") : $("llmBtn");
  const url = kind === "reconcile" ? "/reconcile?sample=15" : "/llm-check";
  const label = btn.textContent;
  btn.disabled = true; btn.classList.add("busy"); btn.textContent = "checking…";
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (r.status === 429) {
      const b = await r.json().catch(() => ({}));
      $("actionMsg").textContent = "Rate limited — " + (b.detail || "try again later.");
    }
  } catch (e) {
    $("actionMsg").textContent = "Check failed: " + e.message;
  }
  btn.classList.remove("busy"); btn.textContent = label; btn.disabled = false;
  await load();
}

// ---------------------------------------------------------------- wire up
function init() {
  $("copyBtn").addEventListener("click", copyEverything);
  $("refreshBtn").addEventListener("click", load);
  $("reconcileBtn").addEventListener("click", () => runCheck("reconcile"));
  $("llmBtn").addEventListener("click", () => runCheck("llm"));
  document.querySelectorAll(".act").forEach((b) =>
    b.addEventListener("click", () => { if (!b.disabled) postAction(b.dataset.action); }));
  $("logfilters").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    LOG_LEVEL = chip.dataset.level;
    document.querySelectorAll("#logfilters .chip").forEach((c) => c.classList.toggle("on", c === chip));
    if (LATEST) renderLogs(LATEST.logs || []);
  });
  load();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
