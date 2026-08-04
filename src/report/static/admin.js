/* Admin page. One fetch of /admin.json drives every section and the "Copy
   everything" blob. Auto-refreshes every 10s only while a backfill is active;
   otherwise it's manual (Refresh). Read-only except the action buttons. */
"use strict";

let LATEST = null;      // last payload, for copy + client-side log filtering
let LOG_LEVEL = "";     // "", info, warning, error
let TIMER = null;
let BALANCE = null;     // last /admin/balance-sheet payload, for the copy blob
let VERIFY = null;      // last /admin/verify payload, for the copy blob
let UNIVERSE = null;    // last /admin/universe-check payload, for the copy blob

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function fmtNum(n) {
  return n == null ? "—" : Number(n).toLocaleString("en-US");
}

// Money at balance-sheet scale is unreadable as raw digits on a phone.
function fmtUSD(n) {
  if (n == null) return "—";
  const v = Number(n);
  const a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(2) + "T";
  if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
  return v.toLocaleString("en-US");
}

// ---------------------------------------------------------------- load + render
async function load() {
  if (TIMER) { clearTimeout(TIMER); TIMER = null; }
  try {
    const r = await fetch("/admin.json", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    LATEST = await r.json();
    render(LATEST);
  } catch (e) {
    $("verdict").className = "verdict v-error";
    $("verdict").querySelector(".v-head").textContent = "Admin unreachable";
    $("verdict").querySelector(".v-detail").textContent =
      "Could not load /admin.json (" + e.message + "). The API itself may be down.";
    $("verdict").querySelector(".v-action").textContent = "Retry in a moment.";
    TIMER = setTimeout(load, 10000);
    return;
  }
  // Keep polling while a reload is mid-flight, not just while the lock is held:
  // the fetch and verify phases run outside the backfill loop's own state.
  const busyPhases = ["fetching", "replacing", "verifying", "downloading",
                      "parsing", "loading"];
  const reloading = busyPhases.includes((LATEST.reload || {}).phase);
  const dumping = busyPhases.includes((LATEST.raw_facts || {}).phase);
  if (LATEST.backfill_running || reloading || dumping) TIMER = setTimeout(load, 10000);
}

function render(d) {
  renderVerdict(d.verdict);
  renderHealth(d.data_health || {});
  renderReload(d.reload || {});
  renderSecCache(d.sec_cache || []);
  renderAsk(d.ask || {});
  renderRawFacts(d.raw_facts || {});
  renderExtraction(d.extraction || {});
  renderConfig(d.config || []);
  renderLogs(d.logs || []);
  renderActions(d.actions || {});
  renderBackfillResults((d.data_health || {}).backfill || {});
  const t = d.generated_at ? new Date(d.generated_at).toLocaleString() : "—";
  $("stamp").textContent = "updated " + t +
    (LATEST.backfill_running ? " · backfill active (auto-refresh 10s)" : " · idle");
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

  // Adjustment — split-targeted: the denominator is names that ACTUALLY split.
  const sm = adj.summary || {};
  const src = adj.splits_source ? ` · via ${adj.splits_source}` : "";
  if (adj.status === "unadjusted") {
    out += row("Price adjustment", pill("unadjusted", "bad"),
      "Splits read as crashes and split names look deleted.");
  } else if (adj.status === "adjusted") {
    out += row("Price adjustment", pill("adjusted", "ok"), `${adj.checked || 0} split names tested${src}`);
  } else if (adj.status === "inconclusive") {
    out += row("Price adjustment", pill("inconclusive", "warn"), `${adj.checked || 0} tested — no clean signal${src}`);
  } else {
    out += row("Price adjustment", pill("not checked", "mute"), "tap “Check now”");
  }
  const counts = (x) => `${x.adjusted || 0} adj · ${x.unadjusted || 0} un · ${x.inconclusive || 0} incon · ${x.no_data || 0} n/a`;
  if (adj.checked)
    out += statline(`Split-targeted (${adj.checked})`, counts(sm),
      "names with a known split in the window — this is the real rate");

  // Backfill source.
  const avail = (bf.sources_available || []).join(", ") || "—";
  const phasePill = bf.phase === "error" ? pill("error", "bad")
    : bf.phase === "done" ? pill("done", "ok")
      : bf.phase === "running" ? pill("running", "warn") : pill(bf.phase || "idle", "mute");
  out += row("Backfill source", `${esc(bf.source || "—")} ${phasePill}`, "available: " + avail);
  if (bf.last_error) out += row("Backfill error", `<span style="color:var(--red)">${esc(bf.last_error)}</span>`);

  // Sector map.
  const sm2 = h.sector_map || {};
  if (sm2.error) {
    out += row("Sector map", `<span style="color:var(--red)">${esc(sm2.error)}</span>`);
  } else if ((sm2.total || 0) === 0) {
    out += row("Sector map", pill("empty", "bad"), "SIC backfill has not run");
  } else {
    const ratio = sm2.mapped_ratio != null ? `${(sm2.mapped_ratio * 100).toFixed(0)}%` : "—";
    const kind = sm2.mapped_ratio >= 0.6 ? "ok" : sm2.mapped_ratio > 0 ? "warn" : "bad";
    out += statline(`Sector map (${fmtNum(sm2.total)} cached)`,
      `${fmtNum(sm2.mapped)} mapped · ${fmtNum(sm2.unmapped)} unmapped · ${ratio}`);
  }

  // Fundamentals — is the table empty (backfill not run) or thin?
  const fu = h.fundamentals || {};
  if (fu.error) {
    out += row("Fundamentals", `<span style="color:var(--red)">${esc(fu.error)}</span>`);
  } else if ((fu.rows || 0) === 0) {
    out += row("Fundamentals", pill("empty", "bad"),
      "SEC XBRL backfill has not populated the table");
  } else {
    const uc = fu.universe_coverage != null ? `${(fu.universe_coverage * 100).toFixed(0)}%` : "—";
    out += statline(`Fundamentals (${fmtNum(fu.rows)} rows)`,
      `${fmtNum(fu.distinct_tickers)} tickers · ${fmtNum(fu.universe_with_fundamentals)}/${fmtNum(fu.universe_total)} of universe · ${uc}`,
      `latest filing ${fu.latest_filing_date || "—"}`);
  }

  out += row("Tickers loaded", fmtNum(cov.tickers_loaded));
  if (cov.sec_universe != null)
    out += row("Vs SEC universe", `${fmtNum(cov.joined_with_sec)} / ${fmtNum(cov.sec_universe)}`,
      cov.join_rate != null ? `join rate ${(cov.join_rate * 100).toFixed(1)}%` : "");

  const stale = rec.staleness_days;
  const stalePill = stale == null ? pill("no data", "mute")
    : stale > 5 ? pill(stale + "d old", "bad") : stale > 2 ? pill(stale + "d old", "warn") : pill(stale + "d old", "ok");
  out += row("Latest bar", `${esc(rec.latest_bar_date || "—")} ${stalePill}`);
  out += row("Trading days loaded", fmtNum(hist.loaded));

  for (const [k, v] of Object.entries(h.errors || {}))
    out += row(esc(k) + " unreachable", `<span style="color:var(--red)">${esc(v)}</span>`);

  if (h.reconcile_checked_at)
    out += row("Data checked", esc(new Date(h.reconcile_checked_at).toLocaleString()));

  $("health").innerHTML = out;
}

// Shared by the verify panel and the reload panel's embedded verification.
function verifyHtml(v) {
  let out = "";
  const ok = v.passed;
  out += row("Result", pill(ok ? "PASS" : "FAIL", ok ? "ok" : "bad"), v.summary);

  for (const [ticker, c] of Object.entries(v.companies || {})) {
    if (!c.found) {
      out += row(ticker, pill("no data", "bad"), c.reason || "");
      continue;
    }
    out += row(ticker, pill(c.passed ? "pass" : "fail", c.passed ? "ok" : "bad"),
      `${esc(c.company_name || "")} · ${esc(c.period_end)} · filed ${esc(c.filing_date)}`);

    for (const [metric, m] of Object.entries(c.metrics || {})) {
      const shown = m.actual == null
        ? `<span class="pill mute">missing</span>`
        : `${fmtUSD(m.actual)} <span class="sub">vs ${fmtUSD(m.expected)}` +
          (m.drift_pct != null ? ` · ${m.drift_pct}% off` : "") + `</span>`;
      const tag = m.passed ? pill("✓", "ok")
        : m.confirmed ? pill("✗", "bad") : pill("? ref", "warn");
      out += `<div class="row indent"><div class="k">${esc(metric)}` +
        `<span class="sub">${esc(m.basis)}${m.verdict ? " — " + esc(m.verdict) : ""}</span></div>` +
        `<div class="v">${shown} ${tag}</div></div>`;
    }

    const id = c.identity || {};
    if (id.checkable) {
      out += `<div class="row indent"><div class="k"><strong>A = L + E</strong>` +
        `<span class="sub">${fmtUSD(id.assets)} vs ${fmtUSD(id.liabilities_plus_equity)}</span></div>` +
        `<div class="v">${pill((id.balanced ? "✓ " : "✗ ") + id.drift_pct + "%", id.balanced ? "ok" : "bad")}</div></div>`;
    } else {
      out += `<div class="row indent"><div class="k">A = L + E</div>` +
        `<div class="v"><span class="pill mute">not checkable</span></div></div>`;
    }
    if (c.impossible)
      out += `<div class="stage fail"><div class="st-err">IMPOSSIBLE: ${esc(c.impossible)}</div></div>`;
    for (const issue of c.data_quality_issues || [])
      out += `<div class="stage fail"><div class="st-err">${esc(issue)}</div></div>`;
  }

  const cov = v.coverage || {};
  if (cov.by_concept) {
    out += row("", "<strong>Coverage by concept</strong>");
    out += row("Tickers renderable", fmtNum(cov.tickers_renderable),
      `of ${fmtNum(cov.tickers_with_any_fundamentals)} with any fundamentals`);
    for (const [concept, i] of Object.entries(cov.by_concept)) {
      const kind = i.coverage_pct >= 60 ? "ok" : i.coverage_pct > 0 ? "warn" : "bad";
      out += row(concept, pill(i.coverage_pct + "%", kind),
        `${fmtNum(i.tickers_with_data)} tickers`);
    }
    if ((cov.unmapped_metrics || []).length)
      out += row("Unmapped metrics", esc(cov.unmapped_metrics.join(", ")),
        "present in the table, read by no balance-sheet concept");
  }
  return out;
}

// Live state of the download/replace/verify job. The phase names matter: while
// it says "fetching", nothing has been deleted and an abort costs nothing.
const RELOAD_PHASE_NOTE = {
  fetching: "downloading every quarter — nothing deleted yet",
  replacing: "delete + reload, one transaction",
  verifying: "data is in; checking the five companies",
};

function renderReload(r) {
  const el = $("reload");
  if (!r.phase || r.phase === "idle") {
    el.innerHTML = `<div class="loading">not run this session</div>`;
    return;
  }
  const kind = r.phase === "done" ? "ok" : r.phase === "error" ? "bad" : "warn";
  let out = row("Phase", pill(r.phase, kind),
    (RELOAD_PHASE_NOTE[r.phase] || "") +
    (r.started_at ? " · started " + new Date(r.started_at).toLocaleTimeString() : ""));

  const staged = r.staged || [];
  const want = (r.quarters || []).length;
  const MARK = { cache: "(cached)", network: "↓", unpublished: "not published yet" };
  if (want) {
    out += row("Downloaded", `${staged.length} / ${want}`,
      staged.map((s) => `${s.quarter} ${MARK[s.source] || s.source}`).join(" · ")
        || "—");
  }
  // A quarter SEC hasn't published is not a shortfall, so the denominator for
  // "loaded" is what EXISTS. Six of six is a complete reload even when seven
  // were asked for.
  const have = r.quarters_available;
  if (r.quarters_loaded != null && have != null)
    out += row("Quarters loaded", `${r.quarters_loaded} / ${have}`,
      have < want ? `${want} requested` : "");
  if ((r.unpublished || []).length)
    out += row("Not published yet", pill(r.unpublished.join(", "), "mute"),
      "SEC posts a quarter some weeks after quarter end — expected, not a failure");
  if (r.rows_deleted != null) out += row("Rows deleted", fmtNum(r.rows_deleted));
  if (r.rows_written != null) out += row("Rows written", fmtNum(r.rows_written));
  if (r.phase === "error" && r.data_intact)
    out += row("Existing data", pill("untouched", "ok"),
      "the reload aborted before anything was committed");
  if (r.last_error)
    out += `<div class="err-note">${esc(r.last_error)}</div>`;
  if (r.verification) out += verifyHtml(r.verification);
  el.innerHTML = out;
}

// What is already on disk. A cached quarter is a quarter SEC will not be asked
// for again — the reason the 429s stopped.
function renderSecCache(rows) {
  const el = $("secCache");
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = `<div class="loading">nothing cached — the next load downloads every quarter</div>`;
    return;
  }
  const mb = rows.reduce((a, r) => a + (r.bytes || 0), 0) / 1e6;
  let out = row("Cached quarters", fmtNum(rows.length),
    `${mb.toFixed(0)} MB on disk`);
  for (const r of rows) {
    out += row(r.quarter, `${((r.bytes || 0) / 1e6).toFixed(1)} MB`,
      `fetched ${r.age_hours}h ago`);
  }
  el.innerHTML = out;
}

// The question box: is it on, what does it cost, how close is it to its caps.
function renderAsk(a) {
  const el = $("ask");
  if (!el) return;
  let out = "";
  if (!a.available) {
    out += row("Status", pill("off", "bad"), esc(a.configured_model || "—"));
    out += `<div class="err-note">${esc(a.error || "not configured")}</div>`;
    el.innerHTML = out;
    return;
  }
  const m = a.model || {};
  out += row("Status", pill("on", "ok"), esc(m.name || m.id || ""));
  out += row("Model", esc(m.id || ""),
    `$${m.prompt_usd_per_mtok}/M in · $${m.completion_usd_per_mtok}/M out`);

  const t = a.today || {}, c = a.caps || {};
  if (t.error) {
    out += `<div class="err-note">${esc(t.error)}</div>`;
    el.innerHTML = out;
    return;
  }
  // Against the cap, not just a raw number: "212" is only meaningful next to
  // the ceiling it is walking toward.
  const reqPct = c.per_day ? Math.round(100 * t.requests / c.per_day) : 0;
  const costPct = c.daily_cost_usd
    ? Math.round(100 * t.cost_usd / c.daily_cost_usd) : 0;
  const kind = (p) => (p >= 100 ? "bad" : p >= 80 ? "warn" : "ok");
  out += row("Questions today", pill(`${fmtNum(t.requests)} / ${fmtNum(c.per_day)}`,
    kind(reqPct)), `${reqPct}% of the daily limit`);
  out += row("Spend today", pill(`$${(t.cost_usd || 0).toFixed(4)} / $${c.daily_cost_usd}`,
    kind(costPct)), `${costPct}% of the daily cap`);
  out += row("Tokens today",
    `${fmtNum(t.prompt_tokens)} in · ${fmtNum(t.completion_tokens)} out`,
    "resets at midnight UTC");
  out += row("Per-IP limit", `${fmtNum(c.per_ip_per_hour)} / hour`);
  el.innerHTML = out;
}

// The raw num.txt dump: the evidence a disputed figure gets settled against.
function renderRawFacts(rf) {
  const el = $("rawFacts");
  if (!rf.phase || rf.phase === "idle") {
    el.innerHTML = `<div class="loading">not run this session</div>`;
    return;
  }
  const kind = rf.phase === "done" ? "ok" : rf.phase === "error" ? "bad" : "warn";
  const req = rf.request || {};
  let out = row("Phase", pill(rf.phase, kind),
    `${esc(req.ticker || "")} ${esc((req.tags || []).join(", "))} · ${esc(req.dataset || "")}` +
    (req.ddate ? ` · ddate ${esc(req.ddate)}` : ""));
  if (rf.last_error) out += `<div class="err-note">${esc(rf.last_error)}</div>`;

  const res = rf.result;
  if (res) {
    if (res.error) {
      out += `<div class="err-note">${esc(res.error)}</div>`;
    } else {
      out += row("Submissions", fmtNum((res.submissions || []).length),
        (res.submissions || []).map((x) => `${x.form} ${x.period}`).join(" · "));
      for (const [tag, t] of Object.entries(res.by_tag || {})) {
        out += row(tag, `${fmtNum(t.row_count)} rows`,
          t.note || `${(t.consolidated_instant || []).length} consolidated instant`);
        for (const c of t.consolidated_instant || []) {
          out += `<div class="row indent"><div class="k">consolidated` +
            `<span class="sub">ddate ${esc(c.ddate)} · qtrs ${esc(c.qtrs)} · ${esc(c.uom)}</span></div>` +
            `<div class="v"><strong>${fmtUSD(Number(c.value))}</strong>` +
            `<span class="sub">${esc(c.value)}</span></div></div>`;
        }
        if (t.row_count && !t.filter_selects_exactly_one) {
          out += `<div class="stage fail"><div class="st-err">Filter selected ` +
            `${(t.consolidated_instant || []).length} rows, expected exactly 1 ` +
            `per (ddate, uom). Narrow with a ddate, or the filter is wrong.</div></div>`;
        }
        const vary = t.varying_columns || {};
        if (Object.keys(vary).length) {
          out += statline("  columns that vary",
            Object.keys(vary).join(", "),
            "this is what separates consolidated from dimensional");
          for (const dc of t.dimension_columns_present || []) {
            if (vary[dc])
              out += statline(`  ${dc}`, vary[dc].map((v) => v || "(empty)").join(" | "));
          }
        }
      }
    }
  }
  el.innerHTML = out;
}

// What the consolidated filter did, per quarter. Structural drops are supposed
// to be large; validation rejections are the ones that matter.
function renderExtraction(ex) {
  const quarters = Object.keys(ex).sort().reverse();
  if (!quarters.length) {
    $("extraction").innerHTML =
      `<div class="loading">no fundamentals load this session</div>`;
    return;
  }
  let out = "";
  for (const q of quarters) {
    const r = ex[q] || {};
    out += row(q, `${fmtNum(r.kept)} kept`, `of ${fmtNum(r.tag_matched)} tag matches`);
    out += statline("  dropped",
      `${fmtNum(r.dropped_dimensional)} dimensional · ${fmtNum(r.dropped_wrong_qtrs)} wrong qtrs ` +
      `(${fmtNum(r.dropped_ytd_cumulative)} YTD cumulative) · ` +
      `${fmtNum(r.dropped_non_usd)} non-USD · ${fmtNum(r.dropped_alias_duplicate)} alias dupes`,
      "structural — expected to be large");
    const hist = r.duration_qtrs_seen || {};
    if (Object.keys(hist).length) {
      out += statline("  duration qtrs seen",
        Object.entries(hist).map(([k, v]) => `qtrs=${k}: ${fmtNum(v)}`).join(" · "),
        "what filers actually report; we keep 1 and 4 only");
    }
    const rate = r.reject_rate != null ? (r.reject_rate * 100).toFixed(2) + "%" : "—";
    const kind = (r.reject_rate || 0) > 0.05 ? "bad" : (r.reject_rate || 0) > 0 ? "warn" : "ok";
    out += row("  validation", pill(rate, kind),
      `${fmtNum(r.rejected_periods)} of ${fmtNum(r.validated_periods)} company-periods rejected`);
    for (const rej of (r.rejections || []).slice(0, 8)) {
      out += `<div class="stage fail"><div class="st-name">${esc(rej.ticker)} ${esc(rej.metric)}</div>` +
        `<div class="st-err">${esc(rej.rule)} — value ${esc(fmtUSD(rej.value))} @ ${esc(rej.period_end)}</div></div>`;
    }
    const un = r.top_unmapped_tags || [];
    if (un.length) {
      out += statline("  top unmapped tags",
        un.slice(0, 12).map((u) => `${u.tag} (${fmtNum(u.count)})`).join(" · "),
        "consolidated tags no concept reads — a synonym here explains thin coverage");
    }
    for (const fl of (r.flags || []).slice(0, 8)) {
      out += `<div class="stage"><div class="st-name">${esc(fl.ticker)} ${esc(fl.rule)}</div>` +
        `<div class="st-nums">${esc(JSON.stringify(fl))}</div></div>`;
    }
  }
  $("extraction").innerHTML = out;
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

function renderBackfillResults(bf) {
  const results = bf.results || {};
  document.querySelectorAll(".bf-result").forEach((el) => {
    const kind = el.dataset.kind;
    const r = results[kind];
    if (!r) {
      el.textContent = "not run this session";
      el.className = "bf-result mute";
      return;
    }
    const when = r.at ? new Date(r.at).toLocaleTimeString() : "";
    if (r.error) {
      el.innerHTML = `<span class="bf-bad">failed</span> ${esc(r.error)}` +
        (when ? `<span class="bf-when">${esc(when)}</span>` : "");
      el.className = "bf-result bad";
    } else {
      el.innerHTML = `<span class="bf-ok">${fmtNum(r.rows)} rows</span>` +
        (when ? `<span class="bf-when">${esc(when)}</span>` : "");
      el.className = "bf-result ok";
    }
  });
}

function renderActions(a) {
  document.querySelectorAll(".act[data-action]").forEach((btn) => {
    const cfg = a[btn.dataset.action] || { enabled: true };
    const label = btn.dataset.label || (btn.dataset.label = btn.textContent.trim());
    btn.disabled = !cfg.enabled;
    btn.innerHTML = esc(label) + (cfg.reason ? `<span class="why">${esc(cfg.reason)}</span>` : "");
  });
}

// ---------------------------------------------------------------- balance sheet
async function testBalanceSheet() {
  const btn = $("balanceSheetBtn"), div = $("balanceSheet");
  const label = btn.textContent;
  btn.disabled = true; btn.classList.add("busy"); btn.textContent = "checking…";
  div.innerHTML = `<div class="loading">reading…</div>`;
  try {
    const r = await fetch("/admin/balance-sheet?tickers=JPM,AAL,MSFT,WMT,FCX",
      { cache: "no-store" });
    const data = await r.json();
    BALANCE = data;
    if (data.error) {
      div.innerHTML = `<div class="err-note">${esc(data.error)}</div>` +
        (data.traceback ? `<pre class="tb">${esc(data.traceback)}</pre>` : "");
      return;
    }

    let html = "";
    const cov = data.coverage || {};
    html += row("Tickers renderable", fmtNum(cov.tickers_renderable),
      `of ${fmtNum(cov.tickers_with_any_fundamentals)} with any fundamentals`);

    html += row("", "<strong>Coverage by concept</strong>");
    for (const [concept, info] of Object.entries(cov.by_concept || {})) {
      const kind = info.coverage_pct >= 60 ? "ok" : info.coverage_pct > 0 ? "warn" : "bad";
      html += row(concept, pill(info.coverage_pct + "%", kind),
        `${fmtNum(info.tickers_with_data)} tickers`);
    }

    html += row("", "<strong>Verification companies</strong>");
    for (const [ticker, s] of Object.entries(data.company_sheets || {})) {
      if (!s.found) { html += row(ticker, pill("no data", "bad")); continue; }
      html += row(ticker, esc(s.company_name || "—"),
        `${esc(s.period_end)} · filed ${esc(s.filing_date)}`);
      const groups = [["assets", s.assets], ["liabilities", s.liabilities], ["equity", s.equity]];
      for (const [gname, g] of groups) {
        for (const [name, v] of Object.entries(g || {})) {
          const shown = v.missing
            ? `<span class="pill mute">missing</span>`
            : fmtUSD(v.value) + (v.restated ? ' <span class="pill warn">restated</span>' : "");
          html += `<div class="row indent"><div class="k">${esc(name)}</div><div class="v">${shown}</div></div>`;
        }
      }
      const bc = s.balance_check || {};
      if (bc.error) {
        html += `<div class="stage fail"><div class="st-err">${esc(bc.error)}</div></div>`;
      } else {
        const ok = bc.balanced;
        html += `<div class="row indent"><div class="k"><strong>A = L + E</strong>` +
          `<span class="sub">${fmtUSD(bc.total_assets)} vs ${fmtUSD(bc.liabilities_plus_equity)}</span></div>` +
          `<div class="v">${pill((ok ? "✓ " : "✗ ") + bc.diff_pct + "%", ok ? "ok" : "bad")}</div></div>`;
      }
      for (const issue of s.data_quality_issues || []) {
        html += `<div class="stage fail"><div class="st-err">${esc(issue)}</div></div>`;
      }
    }
    div.innerHTML = html;
  } catch (e) {
    div.innerHTML = `<div class="err-note">Failed: ${esc(e.message)}</div>`;
  }
  btn.classList.remove("busy"); btn.textContent = label; btn.disabled = false;
}

async function runUniverseCheck() {
  const btn = $("universeBtn"), div = $("universe");
  const label = btn.textContent;
  btn.disabled = true; btn.classList.add("busy"); btn.textContent = "running…";
  div.innerHTML = `<div class="loading">checking every ticker…</div>`;
  try {
    const r = await fetch("/admin/universe-check", { cache: "no-store" });
    const d = await r.json();
    UNIVERSE = d;
    if (d.error) {
      div.innerHTML = `<div class="err-note">${esc(d.error)}</div>` +
        (d.traceback ? `<pre class="tb">${esc(d.traceback)}</pre>` : "");
      btn.classList.remove("busy"); btn.textContent = label; btn.disabled = false;
      return;
    }

    const id = d.identity || {}, b = id.buckets || {};
    let out = "";
    const rate = id.pass_rate_pct;
    const kind = rate >= 95 ? "ok" : rate >= 80 ? "warn" : "bad";
    out += row("Identity pass rate", pill(rate + "%", kind),
      `${fmtNum(b.within_1pct)} of ${fmtNum(id.checkable)} checkable within 1%`);
    out += row("within 1%", fmtNum(b.within_1pct));
    out += row("1–5%", fmtNum(b["1_to_5pct"]));
    out += row("5–10%", fmtNum(b["5_to_10pct"]));
    out += row("over 10%", fmtNum(b.over_10pct));
    out += row("Not checkable", fmtNum(id.not_checkable),
      "no reported total liabilities or equity for the latest period");
    out += row("Balanced vs equity incl. NCI", fmtNum(id.equity_basis_incl_nci),
      "filers reporting the NCI-inclusive total");
    if (id.explained_by_nci)
      out += row("Drift explained by NCI", fmtNum(id.explained_by_nci),
        "closes to within 1% once noncontrolling interests are added");

    const st = d.stated_total || {};
    if (st.used != null) {
      out += row("Filer's own stated total", fmtNum(st.used),
        "used LiabilitiesAndStockholdersEquity instead of reconstructing L + E");
      if (st.comparable) {
        const nb = st.buckets_if_reconstructed || {};
        out += row("  moved into 1% by it", fmtNum(st.moved_into_1pct),
          `of ${fmtNum(st.comparable)} reporting both`);
        out += statline("  if reconstructed instead",
          `within 1%: ${fmtNum(nb.within_1pct)} · 1–5%: ${fmtNum(nb["1_to_5pct"])} · ` +
          `5–10%: ${fmtNum(nb["5_to_10pct"])} · >10%: ${fmtNum(nb.over_10pct)}`,
          "what L + E would have scored on the same companies");
      }
    }

    const sz = d.over_10pct_by_size || {};
    if (sz.buckets && b.over_10pct) {
      out += statline("Over-10% failures by size",
        `<$1M: ${fmtNum(sz.buckets.under_1m)} · $1–10M: ${fmtNum(sz.buckets["1m_to_10m"])} · ` +
        `$10–100M: ${fmtNum(sz.buckets["10m_to_100m"])} · >$100M: ${fmtNum(sz.buckets.over_100m)}`,
        `${sz.under_10m_pct}% of them are under $10M in assets`);
      const microKind = sz.under_10m_pct >= 80 ? "ok" : sz.under_10m_pct >= 50 ? "warn" : "bad";
      out += row("  verdict", pill(sz.under_10m_pct + "% microcap", microKind),
        sz.under_10m_pct >= 80
          ? "mostly shells — arithmetic on noise, not a parser problem"
          : "a real share of these are substantial companies — worth reading");
    }

    const sec = d.by_sector || [];
    if (sec.length) {
      out += row("", "<strong>By sector</strong> — worst first");
      for (const s2 of sec) {
        const sk = s2.pass_rate_pct == null ? "mute"
          : s2.pass_rate_pct >= 95 ? "ok" : s2.pass_rate_pct >= 80 ? "warn" : "bad";
        out += row(s2.sector,
          pill(s2.pass_rate_pct == null ? "n/a" : s2.pass_rate_pct + "%", sk),
          `${fmtNum(s2.checkable)} checkable · ${esc(s2.verdict)}`);
      }
    }

    if ((d.scale_jumps || []).length) {
      out += row("", `<strong>Scale jumps</strong> — ${fmtNum(d.scale_jumps_total)} total`);
      for (const j of d.scale_jumps) {
        out += `<div class="row indent"><div class="k">${esc(j.ticker)}` +
          `<span class="sub">${esc(j.from_period)} → ${esc(j.to_period)}</span></div>` +
          `<div class="v">${fmtUSD(j.from_assets)} → ${fmtUSD(j.to_assets)} ` +
          pill(j.ratio + "×", "bad") + `</div></div>`;
      }
    }

    if ((d.worst || []).length) {
      out += row("", `<strong>Worst by drift</strong> — ${fmtNum(d.worst_total)} over 1%`);
      for (const w of d.worst) {
        const note = w.explained_by
          ? ` · NCI closes it to ${w.drift_pct_with_nci}%` : "";
        out += `<div class="row indent"><div class="k">${esc(w.ticker)}` +
          `<span class="sub">${esc(w.sector)} · ${esc(w.period_end)} · ` +
          `${fmtUSD(w.total_assets)} vs ${fmtUSD(w.liabilities_plus_equity)}` +
          `${esc(note)}</span></div>` +
          `<div class="v">${pill(w.drift_pct + "%", w.explained_by ? "warn" : "bad")}</div></div>`;
      }
    }
    div.innerHTML = out;
  } catch (e) {
    div.innerHTML = `<div class="err-note">Failed: ${esc(e.message)}</div>`;
  }
  btn.classList.remove("busy"); btn.textContent = label; btn.disabled = false;
}

// ---------------------------------------------------------------- verify + reload
async function runVerify() {
  const btn = $("verifyBtn"), div = $("verify");
  const label = btn.textContent;
  btn.disabled = true; btn.classList.add("busy"); btn.textContent = "checking…";
  div.innerHTML = `<div class="loading">reading…</div>`;
  try {
    const r = await fetch("/admin/verify", { cache: "no-store" });
    const data = await r.json();
    VERIFY = data;
    div.innerHTML = data.error
      ? `<div class="err-note">${esc(data.error)}</div>` +
        (data.traceback ? `<pre class="tb">${esc(data.traceback)}</pre>` : "")
      : verifyHtml(data);
  } catch (e) {
    div.innerHTML = `<div class="err-note">Failed: ${esc(e.message)}</div>`;
  }
  btn.classList.remove("busy"); btn.textContent = label; btn.disabled = false;
}

async function startReload() {
  // This deletes every fundamentals row and cannot be undone, so it asks first
  // and the endpoint independently requires confirm=true.
  const ok = window.confirm(
    "Replace EVERY fundamentals row from 7 quarters of SEC data?\n\n" +
    "Nothing is deleted until all 7 quarters have downloaded, and the swap " +
    "is one transaction — if it fails, the current data stays.\n\n" +
    "Takes several minutes.");
  if (!ok) return;

  const btn = $("reloadBtn"), msg = $("actionMsg");
  btn.disabled = true;
  msg.textContent = "starting reload…";
  try {
    const r = await fetch("/admin/reload-fundamentals?confirm=true&quarters=7",
      { method: "POST" });
    const body = await r.json().catch(() => ({}));
    msg.textContent = r.status === 429
      ? "Rate limited — " + (body.detail || "try again later.")
      : (body.detail || (body.accepted ? "Accepted." : "Done."));
  } catch (e) {
    msg.textContent = "Failed: " + e.message;
  }
  btn.disabled = false;
  setTimeout(load, 800);
}

// Takes its parameters explicitly. A preset passes its own; the form passes the
// inputs. Nothing reads shared state at submit time, so a preset cannot fire
// with whatever happened to be left in the form.
async function submitRawFacts(params, btn) {
  const msg = $("actionMsg");
  const q = new URLSearchParams(params);
  btn.disabled = true;
  msg.textContent = "starting dump… (downloads ~100MB, takes a minute)";
  try {
    const r = await fetch("/admin/raw-facts?" + q.toString(), { method: "POST" });
    const body = await r.json().catch(() => ({}));
    msg.textContent = r.status === 429
      ? "Rate limited — " + (body.detail || "try again later.")
      : (body.detail || (body.accepted ? "Accepted." : "Done."));
  } catch (e) {
    msg.textContent = "Failed: " + e.message;
  }
  btn.disabled = false;
  setTimeout(load, 800);
}

function startRawFacts() {
  return submitRawFacts({
    ticker: $("rawTicker").value.trim() || "MSFT",
    tags: $("rawTags").value.trim() || "Assets,StockholdersEquity",
    year: $("rawYear").value || "2026",
    quarter: $("rawQuarter").value || "1",
    ddate: $("rawDdate").value.trim(),
  }, $("rawFactsBtn"));
}

// ---------------------------------------------------------------- copy everything
function buildCopyText(d) {
  const L = [];
  const push = (s) => L.push(s == null ? "" : s);
  const v = d.verdict || {};
  push("COMPANY DATA — ADMIN");
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
    push(`  Price adjustment: ${adj.status || "unchecked"}`);
    if (adj.checked)
      push(`    tested ${adj.checked}: adj ${sm.adjusted || 0}, unadj ${sm.unadjusted || 0}, ` +
        `incon ${sm.inconclusive || 0}, no-data ${sm.no_data || 0}`);
    push(`  Backfill source: ${bf.source || "—"} [${bf.phase || "idle"}]`);
    if (bf.last_error) push("    error: " + bf.last_error);
    const fu = h.fundamentals || {};
    if (fu.error) push(`  Fundamentals: ERROR ${fu.error}`);
    else if ((fu.rows || 0) === 0) push("  Fundamentals: EMPTY");
    else push(`  Fundamentals: ${fmtNum(fu.rows)} rows, ${fmtNum(fu.distinct_tickers)} tickers, latest ${fu.latest_filing_date || "—"}`);
    push(`  Tickers loaded: ${fmtNum(cov.tickers_loaded)}`);
    push(`  Latest bar: ${rec.latest_bar_date || "—"} (${rec.staleness_days == null ? "?" : rec.staleness_days + "d"} stale)`);
    push(`  Trading days: ${fmtNum(hist.loaded)}`);
    for (const [k, val] of Object.entries(h.errors || {})) push(`  ${k} unreachable: ${val}`);
  }
  push("");

  push("XBRL EXTRACTION");
  const ex = d.extraction || {};
  const qs = Object.keys(ex).sort().reverse();
  if (!qs.length) push("  no fundamentals load this session");
  for (const q of qs) {
    const r = ex[q] || {};
    push(`  ${q}: kept ${fmtNum(r.kept)} of ${fmtNum(r.tag_matched)} tag matches`);
    push(`    dropped: ${fmtNum(r.dropped_dimensional)} dimensional, ${fmtNum(r.dropped_wrong_qtrs)} wrong-qtrs ` +
      `(${fmtNum(r.dropped_ytd_cumulative)} YTD-cumulative), ` +
      `${fmtNum(r.dropped_non_usd)} non-USD, ${fmtNum(r.dropped_alias_duplicate)} alias-dupes`);
    const hist = r.duration_qtrs_seen || {};
    if (Object.keys(hist).length)
      push(`    duration qtrs seen: ${Object.entries(hist).map(([k, v]) => `${k}=${v}`).join(", ")}`);
    push(`    validation: ${fmtNum(r.rejected_periods)}/${fmtNum(r.validated_periods)} periods rejected (${((r.reject_rate || 0) * 100).toFixed(2)}%)`);
    for (const rej of (r.rejections || []).slice(0, 20))
      push(`      REJECT ${rej.ticker} ${rej.metric} ${rej.rule} value=${rej.value} @${rej.period_end}`);
    for (const fl of (r.flags || []).slice(0, 20))
      push(`      FLAG ${JSON.stringify(fl)}`);
    for (const u of (r.top_unmapped_tags || []).slice(0, 40))
      push(`      UNMAPPED ${u.tag} ${u.count}`);
  }
  push("");

  if (BALANCE && !BALANCE.error) {
    push("BALANCE SHEET CHECK");
    const cov = BALANCE.coverage || {};
    push(`  renderable: ${fmtNum(cov.tickers_renderable)} of ${fmtNum(cov.tickers_with_any_fundamentals)}`);
    for (const [c, i] of Object.entries(cov.by_concept || {}))
      push(`    ${c}: ${fmtNum(i.tickers_with_data)} (${i.coverage_pct}%)`);
    for (const [t, s] of Object.entries(BALANCE.company_sheets || {})) {
      if (!s.found) { push(`  ${t}: NO DATA`); continue; }
      push(`  ${t} ${s.company_name || ""} — ${s.period_end} filed ${s.filing_date}`);
      for (const g of ["assets", "liabilities", "equity"])
        for (const [n, val] of Object.entries(s[g] || {}))
          push(`    ${n}: ${val.missing ? "MISSING" : val.value}`);
      const bc = s.balance_check || {};
      push(`    A=L+E: ${bc.error ? "ERROR " + bc.error : (bc.balanced ? "OK" : "OFF") + " " + bc.diff_pct + "%"}`);
      for (const issue of s.data_quality_issues || []) push(`    ISSUE: ${issue}`);
    }
    push("");
  }

  const rl = d.reload || {};
  if (rl.phase && rl.phase !== "idle") {
    push("RELOAD");
    push(`  phase: ${rl.phase}`);
    if ((rl.staged || []).length)
      push(`  downloaded: ${rl.staged.length}/${(rl.quarters || []).length} — ` +
           rl.staged.map((s) => `${s.quarter}:${s.source}`).join(" "));
    if (rl.quarters_loaded != null)
      push(`  quarters loaded: ${rl.quarters_loaded}/${rl.quarters_available} ` +
           `(${(rl.quarters || []).length} requested)`);
    if ((rl.unpublished || []).length)
      push(`  not published yet: ${rl.unpublished.join(", ")}`);
    if (rl.phase === "error" && rl.data_intact) push(`  existing data: UNTOUCHED`);
    if (rl.rows_deleted != null) push(`  rows deleted: ${fmtNum(rl.rows_deleted)}`);
    if (rl.rows_written != null) push(`  rows written: ${fmtNum(rl.rows_written)}`);
    if (rl.last_error) push(`  ERROR: ${rl.last_error}`);
    push("");
  }

  const V = VERIFY || rl.verification;
  if (V && !V.error) {
    push("VERIFICATION");
    push(`  ${V.summary}`);
    for (const [t, c] of Object.entries(V.companies || {})) {
      if (!c.found) { push(`  ${t}: NO DATA (${c.reason || ""})`); continue; }
      push(`  ${t} [${c.passed ? "PASS" : "FAIL"}] ${c.period_end} filed ${c.filing_date}`);
      for (const [m, x] of Object.entries(c.metrics || {}))
        push(`    ${m}: actual=${x.actual} expected=${x.expected} drift=${x.drift_pct}% ${x.passed ? "OK" : "OFF"}`);
      const id = c.identity || {};
      push(`    A=L+E: ${id.checkable ? (id.balanced ? "OK " : "OFF ") + id.drift_pct + "%" : "not checkable"}`);
      if (c.impossible) push(`    IMPOSSIBLE: ${c.impossible}`);
    }
    const cov = V.coverage || {};
    if (cov.by_concept) {
      push(`  renderable: ${fmtNum(cov.tickers_renderable)} of ${fmtNum(cov.tickers_with_any_fundamentals)}`);
      for (const [c, i] of Object.entries(cov.by_concept))
        push(`    ${c}: ${fmtNum(i.tickers_with_data)} (${i.coverage_pct}%)`);
      if ((cov.unmapped_metrics || []).length)
        push(`    unmapped metrics: ${cov.unmapped_metrics.join(", ")}`);
    }
    push("");
  }

  const rf = d.raw_facts || {};
  if (rf.result && !rf.result.error) {
    const res = rf.result;
    push("RAW num.txt FACTS");
    push(`  ${res.ticker} CIK ${res.cik} ${res.dataset}` +
      (res.ddate_filter ? ` ddate=${res.ddate_filter}` : ""));
    push(`  columns: ${(res.num_columns || []).join(", ")}`);
    for (const s2 of res.submissions || [])
      push(`  submission: ${s2.form} period=${s2.period} filed=${s2.filed} adsh=${s2.adsh}`);
    for (const [tag, t] of Object.entries(res.by_tag || {})) {
      push(`  ${tag}: ${t.row_count} rows`);
      for (const c of t.consolidated_instant || [])
        push(`    CONSOLIDATED ddate=${c.ddate} qtrs=${c.qtrs} ${c.uom} value=${c.value}`);
      for (const [col, vals] of Object.entries(t.varying_columns || {}))
        push(`    varies: ${col} -> ${vals.map((v) => v || "(empty)").join(" | ")}`);
    }
    push("");
  } else if (rf.phase && rf.phase !== "idle") {
    push(`RAW num.txt FACTS: ${rf.phase}${rf.last_error ? " — " + rf.last_error : ""}`);
    push("");
  }

  if (UNIVERSE && !UNIVERSE.error) {
    const u = UNIVERSE, id = u.identity || {}, b = id.buckets || {};
    push("WHOLE-UNIVERSE CHECK");
    push(`  tickers in table: ${fmtNum(u.tickers_in_table)}`);
    push(`  checkable: ${fmtNum(id.checkable)} · not checkable: ${fmtNum(id.not_checkable)}`);
    push(`  within 1%: ${fmtNum(b.within_1pct)} (${id.pass_rate_pct}%)`);
    push(`  1-5%: ${fmtNum(b["1_to_5pct"])} · 5-10%: ${fmtNum(b["5_to_10pct"])} · >10%: ${fmtNum(b.over_10pct)}`);
    push(`  balanced vs equity incl NCI: ${fmtNum(id.equity_basis_incl_nci)}`);
    push(`  drift explained by NCI: ${fmtNum(id.explained_by_nci)}`);
    const st = u.stated_total || {};
    push(`  stated total used: ${fmtNum(st.used)} of ${fmtNum(id.checkable)} checkable`);
    if (st.comparable) {
      const nb = st.buckets_if_reconstructed || {};
      push(`    moved into 1%: ${fmtNum(st.moved_into_1pct)} of ${fmtNum(st.comparable)} reporting both`);
      push(`    if reconstructed: within1=${fmtNum(nb.within_1pct)} 1-5=${fmtNum(nb["1_to_5pct"])} 5-10=${fmtNum(nb["5_to_10pct"])} >10=${fmtNum(nb.over_10pct)}`);
    }
    const sz = u.over_10pct_by_size || {};
    if (sz.buckets)
      push(`  over-10% by size: <1M=${fmtNum(sz.buckets.under_1m)} 1-10M=${fmtNum(sz.buckets["1m_to_10m"])} ` +
        `10-100M=${fmtNum(sz.buckets["10m_to_100m"])} >100M=${fmtNum(sz.buckets.over_100m)} ` +
        `(${sz.under_10m_pct}% under $10M)`);
    push("  BY SECTOR (worst first):");
    for (const s2 of u.by_sector || [])
      push(`    ${s2.sector}: ${s2.pass_rate_pct}% of ${fmtNum(s2.checkable)} — ${s2.verdict}`);
    push(`  SCALE JUMPS (${fmtNum(u.scale_jumps_total)}):`);
    for (const j of u.scale_jumps || [])
      push(`    ${j.ticker} ${j.from_period}->${j.to_period} ${j.from_assets} -> ${j.to_assets} (${j.ratio}x)`);
    push(`  WORST BY DRIFT (${fmtNum(u.worst_total)} over 1%):`);
    for (const w of u.worst || [])
      push(`    ${w.ticker} [${w.sector}] ${w.drift_pct}% A=${w.total_assets} L+E=${w.liabilities_plus_equity} basis=${w.equity_basis}` +
        (w.explained_by ? ` NCI->${w.drift_pct_with_nci}%` : ""));
    push("");
  }

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
  msg.textContent = ok ? "Full admin state copied — paste into chat." :
    "Couldn't access the clipboard. Long-press to select the page instead.";
  setTimeout(() => { btn.classList.remove("done"); btn.textContent = "📋 Copy everything"; msg.hidden = true; }, 4000);
}

// ---------------------------------------------------------------- actions
async function postAction(action) {
  const map = {
    backfill_bars: "/backfill?kind=bars&days=600",
    backfill_sectors: "/backfill?kind=sectors",
    backfill_fundamentals: "/backfill?kind=fundamentals",
    backfill_earnings: "/backfill?kind=earnings",
  };
  const url = map[action];
  const msg = $("actionMsg");
  if (!url) { msg.textContent = "Unknown action: " + action; return; }
  msg.textContent = "sending…";
  try {
    const r = await fetch(url, { method: "POST" });
    const body = await r.json().catch(() => ({}));
    msg.textContent = r.status === 429
      ? "Rate limited — " + (body.detail || "try again later.")
      : body.detail || (body.accepted ? "Accepted." : "Done.");
  } catch (e) {
    msg.textContent = "Failed: " + e.message;
  }
  setTimeout(load, 800);
}

async function runReconcile() {
  const btn = $("reconcileBtn");
  const label = btn.textContent;
  btn.disabled = true; btn.classList.add("busy"); btn.textContent = "checking…";
  try {
    const r = await fetch("/reconcile?sample=15", { cache: "no-store" });
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
// ---------------------------------------------------------------- code gate
const GATE_CODE = "473";

// Unlocking also starts the page. Without this the poll would run behind the
// gate, hitting /admin.json every 10s for a page nobody is looking at.
function unlock() {
  try { sessionStorage.setItem("admin-gate", "ok"); } catch (e) { /* private mode */ }
  document.documentElement.classList.remove("locked");
  boot();
}

function initGate() {
  const input = $("gateInput"), msg = $("gateMsg");
  if (!input) return;
  input.addEventListener("input", () => {
    // Digits only, however they arrive — typed, pasted or autofilled.
    input.value = input.value.replace(/\D/g, "").slice(0, 3);
    input.classList.remove("wrong");
    msg.textContent = "";
    if (input.value.length < 3) return;
    if (input.value === GATE_CODE) { unlock(); return; }
    input.classList.add("wrong");
    msg.textContent = "Not that one.";
    // Clear so the next attempt starts from empty rather than needing a
    // backspace on a phone keypad.
    setTimeout(() => { input.value = ""; input.focus(); }, 550);
  });
  input.focus();
}

function init() {
  initGate();
  if (document.documentElement.classList.contains("locked")) return;
  boot();
}

function boot() {
  $("copyBtn").addEventListener("click", copyEverything);
  $("refreshBtn").addEventListener("click", load);
  $("reconcileBtn").addEventListener("click", runReconcile);
  $("balanceSheetBtn").addEventListener("click", testBalanceSheet);
  $("verifyBtn").addEventListener("click", runVerify);
  $("universeBtn").addEventListener("click", runUniverseCheck);
  $("reloadBtn").addEventListener("click", startReload);
  $("rawFactsBtn").addEventListener("click", startRawFacts);
  // One tap, straight to the dump with the preset's own parameters.
  $("rawPresets").addEventListener("click", (e) => {
    const b = e.target.closest(".chipbtn");
    if (!b) return;
    submitRawFacts({
      ticker: b.dataset.ticker,
      tags: b.dataset.tags,
      year: b.dataset.year || "2026",
      quarter: b.dataset.quarter || "1",
      ddate: b.dataset.ddate || "",
    }, b);
  });
  document.querySelectorAll(".act[data-action]").forEach((b) =>
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
