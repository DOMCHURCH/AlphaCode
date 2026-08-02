/* Alpha Funnel dashboard front-end.
 *
 * Drives the one-button research flow: start -> progress -> results. Talks to
 * the FastAPI JSON endpoints (/status, /report/latest, /run, /backfill,
 * /ticker/.../history) and embeds live TradingView charts. Served from /static.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  (s || "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]
  );
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString());
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const RUB = { trend: 25, fundamental: 20, catalyst: 20, news: 15, macro: 10, risk: 10 };
// Score tier -> a coloured square (blue strong / yellow medium / red weak).
// Colour lives in a shape, never low-contrast text, on the paper ground.
const tier = (s) => (s >= 75 ? "hi" : s >= 60 ? "med" : "lo");

let PICKS = {};
let CURRENT = null;
let RESEARCHING = false;
let ABORT = false;

/* ---------- view switching ---------- */
function showView(name) {
  for (const v of ["start", "progress", "results"]) $("view-" + v).hidden = v !== name;
}
function goHome() {
  ABORT = true;
  RESEARCHING = false;
  showView("start");
  refreshKpis();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ---------- TradingView ---------- */
let tvReady = false;
let tvQueue = [];
(function loadTv() {
  const s = document.createElement("script");
  s.src = "https://s3.tradingview.com/tv.js";
  s.onload = () => {
    tvReady = true;
    tvQueue.forEach((fn) => fn());
    tvQueue = [];
  };
  document.head.appendChild(s);
})();
function drawChart(symbol) {
  const run = () => {
    $("tvchart").innerHTML = "";
    new TradingView.widget({
      autosize: true, symbol, interval: "D", timezone: "Etc/UTC",
      theme: "dark", style: "1", locale: "en", toolbar_bg: "#111722",
      enable_publishing: false, allow_symbol_change: true, hide_side_toolbar: false,
      container_id: "tvchart", studies: ["MASimple@tv-basicstudies"],
    });
  };
  tvReady ? run() : tvQueue.push(run);
}
function drawGauge(el, symbol) {
  el.innerHTML =
    '<div class="tradingview-widget-container"><div class="tradingview-widget-container__widget"></div></div>';
  const s = document.createElement("script");
  s.src = "https://s3.tradingview.com/external-embedding/embed-widget-technical-analysis.js";
  s.async = true;
  s.textContent = JSON.stringify({
    interval: "1D", width: "100%", height: 360, symbol,
    showIntervalTabs: true, displayMode: "single", locale: "en", colorTheme: "dark",
  });
  el.querySelector(".tradingview-widget-container").appendChild(s);
}

/* ---------- the one button ---------- */
async function getStatus() {
  try {
    return await (await fetch("/status")).json();
  } catch (e) {
    return {};
  }
}
async function latestReportDate() {
  try {
    const r = await fetch("/report/latest");
    if (r.ok) {
      const j = await r.json();
      return j.names && j.names.length ? j.as_of : null;
    }
  } catch (e) {}
  return null;
}
async function postJSON(path) {
  // No token: the run/backfill endpoints are open by design.
  const r = await fetch(path, { method: "POST" });
  try {
    return { status: r.status, ...(await r.json()) };
  } catch (e) {
    return { status: r.status };
  }
}

async function startResearch(force, skipLlm) {
  if (RESEARCHING) return;
  RESEARCHING = true;
  ABORT = false;
  CURRENT = null;
  showView("progress");
  window.scrollTo({ top: 0, behavior: "smooth" });
  setNote("");
  setActions("");
  renderStepper(null);
  setPbar(2);
  setPhase("Checking the data warehouse…");

  const before = force ? null : await latestReportDate();

  try {
    let st = await getStatus();

    // 1) One-time on a fresh deploy: make sure ~a year of history is loaded.
    if (!st.ready_for_first_run && !st.run_in_progress) {
      st = await loadHistory();
      if (st === null) return; // aborted
    }

    // 2) Kick off the funnel if it isn't already running.
    st = await getStatus();
    if (!st.run_in_progress) {
      setNote("");
      setPhase(skipLlm ? "Starting fast mode (no write-ups)…" : "Starting the funnel…");
      renderStepper(null);
      await postJSON(skipLlm ? "/run?skip_llm=true" : "/run");
    }

    // 3) Stream the run's live stage progress until a fresh report lands.
    let outcome = await pollRun(before);

    // 4) Fast Mode skips the LLM write-ups (stages 4-5), so it can ONLY help a
    //    failure that happened in an LLM stage. A data-quality/gate failure
    //    (stages 0-3) would fail identically -- never retry it in fast mode.
    if (outcome === "failed" && !skipLlm && !ABORT) {
      const lr = (await getStatus()).last_run || {};
      if (isLlmStageFailure(lr)) {
        setPhase("The write-up step failed — retrying without it…");
        setNote(lr.error ? "Reason: " + lr.error : "");
        await postJSON("/run?skip_llm=true");
        outcome = await pollRun(before);
      }
    }

    if (outcome === "failed" && !ABORT) {
      showFailure((await getStatus()).last_run || {});
    }
  } catch (e) {
    setPhase("Something went wrong.", true);
    setNote(String(e));
    setActions("retry");
  } finally {
    RESEARCHING = false;
  }
}

/* First-run history load. Honest about being slow, and safe to walk away from:
   the backfill runs server-side, so closing the tab doesn't stop it. */
async function loadHistory() {
  setNote(
    "First-time setup: loading about a year of market history. This is a one-time " +
      "step and can take a while on a rate-limited data plan — it keeps running on " +
      "the server, so you can leave and come back. Research starts automatically " +
      "once the data is ready."
  );
  if (!(await getStatus()).backfill_running) await postJSON("/backfill?days=600");

  let stalls = 0;
  for (let i = 0; i < 5400; i++) {
    // generous: server-side load survives reloads
    if (ABORT) return null;
    const s = await getStatus();
    const have = s.bar_dates || 0;
    const need = s.history_target || 252;
    const bf = s.backfill || {};
    const src = bf.source ? ` via ${bf.source}` : "";
    setPhase(`Loading market history${src} — ${fmt(have)} / ${fmt(need)} trading days`);
    setPbar(Math.min(100, (have / need) * 100));
    if (s.ready_for_first_run || s.run_in_progress) return s;
    // The backfill reported a hard failure and isn't running: stop spinning and
    // say why, instead of looping on a load that can't progress.
    if (!s.backfill_running && bf.phase === "error" && have < need) {
      setPhase("Couldn't load market history.", true);
      setNote(
        (bf.last_error ? bf.last_error + " " : "") +
          "Check POLYGON_API_KEY (fast primary source) or, for keyless mode, " +
          "SEC_USER_AGENT plus outbound access to stooq.com, then try again."
      );
      return null;
    }
    if (!s.backfill_running && have < need) {
      // Load finished a pass but we're still short. If nothing at all loaded,
      // the data API keys are probably missing — say so instead of looping.
      if (have === 0 && ++stalls >= 3) {
        setPhase("Couldn't load any market data.", true);
        setNote(
          "The backfill ran but fetched 0 bars. Keyless mode needs SEC_USER_AGENT " +
            "set (e.g. \"Your Name you@email.com\") for the universe list, and outbound " +
            "access to stooq.com for the bulk price archive. Check those and try again."
        );
        return null;
      }
      await postJSON("/backfill?days=600"); // nudge the next pass
    }
    await sleep(2500);
  }
  return await getStatus();
}

// Returns "done" (a fresh report landed) or "failed" (the run stopped without
// one). The caller decides whether to fall back to fast mode / show the error.
async function pollRun(before) {
  let idle = 0;
  for (let i = 0; i < 1200; i++) {
    if (ABORT) return "aborted";
    const s = await getStatus();
    const cr = s.current_run;
    if (cr) {
      idle = 0;
      renderStepper(cr);
      setPbar(cr.percent);
      setPhase(`Stage ${cr.active_stage} — ${cr.active_label}`);
    } else if (!s.run_in_progress) {
      const latest = await latestReportDate();
      if (latest && latest !== before) {
        await finishResearch();
        return "done";
      }
      if (++idle >= 4) {
        const latest2 = await latestReportDate();
        if (latest2) {
          await finishResearch();
          return "done";
        }
        return "failed";
      }
    }
    await sleep(2000);
  }
  return "failed";
}

/* Turn a dead-end into an explained, actionable state -- from the CURRENT run's
   actual error, never a guess. */
function showFailure(lr) {
  setPbar(100);
  setPhase("Research couldn't produce a screen.", true);
  // Only surface an error that belongs to the run that just failed. A run that is
  // running/succeeded (or a stale row) must not print an error as if it were live.
  const err = lr && lr.status === "failed" ? lr.error || "" : "";
  if (!err) {
    setNote("The run stopped without producing a screen and reported no error. Try again.");
    setActions("retry");
    return;
  }
  const where = lr.failed_stage != null ? `Stage ${lr.failed_stage}` : "a stage";
  // The exception carries its own specific remedy (e.g. the completeness gate
  // lists the empty factors + the FREE/PAID split + the one action that fixes it).
  // Show it verbatim; never replace it with "this usually means…".
  setNote(`Failed at ${where}.\n\n${err}`);
  // Fast Mode only skips the LLM write-ups, so offer it ONLY on an LLM-stage
  // failure -- on a gate failure it would fail identically.
  setActions(isLlmStageFailure(lr) ? "fail" : "retry");
}

function isLlmStageFailure(lr) {
  return !!lr && lr.status === "failed" && (lr.failed_stage === 4 || lr.failed_stage === 5);
}

async function finishResearch() {
  setNote("");
  setActions("");
  setPbar(100);
  setPhase("Done — here are today's best ideas.");
  await sleep(450);
  showView("results");
  window.scrollTo({ top: 0, behavior: "smooth" });
  await loadPicks(true);
  loadStatusArchive();
}

/* ---------- progress rendering ---------- */
const STAGE_FALLBACK = [
  { stage: 0, label: "Building the universe", target: 6000 },
  { stage: 1, label: "Trend gate — is it going up right now?", target: 1200 },
  { stage: 2, label: "Multi-factor scoring", target: 400 },
  { stage: 3, label: "Catalysts & news", target: 100 },
  { stage: 4, label: "LLM triage", target: 25 },
  { stage: 5, label: "LLM deep dive & scoring", target: 10 },
  { stage: 6, label: "Writing the report", target: 10 },
];
function setPhase(t, err) {
  const p = $("phase");
  p.textContent = t;
  p.style.color = err ? "var(--red)" : "";
}
function setPbar(pct) {
  $("pbarFill").style.width = Math.max(0, Math.min(100, pct)) + "%";
}
function setNote(t) {
  const el = $("progNote");
  if (el) el.textContent = t || "";
}
/* Action buttons under the timeline for failure / retry states. */
function setActions(kind) {
  const el = $("progActions");
  if (!el) return;
  if (kind === "fail") {
    el.innerHTML =
      `<button class="rerun" onclick="startResearch(true,true)">⚡ Fast mode — ranked top-10, no write-ups</button>` +
      `<button class="rerun" onclick="startResearch(true)">↻ Try full run again</button>`;
  } else if (kind === "retry") {
    el.innerHTML = `<button class="rerun" onclick="startResearch(true)">↻ Try again</button>`;
  } else {
    el.innerHTML = "";
  }
}
/* The funnel as a numbered vertical timeline: done nodes filled, the current
   node highlighted, upcoming outlined — each showing its live survivor count. */
function renderStepper(cr) {
  const steps =
    cr && cr.steps
      ? cr.steps
      : STAGE_FALLBACK.map((s, i) => ({ ...s, done: false, active: i === 0 && !!cr, survivors: null }));
  $("stepper").innerHTML =
    "<ol class='timeline'>" +
    steps
      .map((s) => {
        const cls = s.done ? "done" : s.active ? "active" : "";
        const num = String(s.stage).padStart(2, "0");
        const cnt = s.survivors != null ? fmt(s.survivors) : s.done ? "—" : `→ ${fmt(s.target)}`;
        return `<li class="tl-step ${cls}">
        <div class="tl-node"><span class="tl-num">${num}</span></div>
        <div class="tl-body"><div class="tl-label">${esc(s.label)}</div>
          <div class="tl-meta">narrows toward ${fmt(s.target)} names</div></div>
        <div class="tl-count">${cnt}</div></li>`;
      })
      .join("") +
    "</ol>";
}

/* ---------- analyze a symbol ---------- */
async function analyze(symbol) {
  symbol = (symbol || "").toUpperCase().trim();
  if (!symbol) return;
  CURRENT = symbol;
  document.querySelectorAll(".prow").forEach((e) => e.classList.toggle("active", e.dataset.t === symbol));
  $("an-sym").textContent = symbol;
  drawChart(symbol);
  const side = $("an-side");
  side.innerHTML = '<div class="notscreened"><span class="spin"></span>Loading…</div>';
  $("an-tags").innerHTML = "";

  const p = PICKS[symbol];
  if (p) {
    renderPick(side, p);
    return;
  }
  let hist = null;
  try {
    const r = await fetch("/ticker/" + encodeURIComponent(symbol) + "/history");
    if (r.ok) hist = await r.json();
  } catch (e) {}
  const th = hist && hist.theses && hist.theses.length ? hist.theses[hist.theses.length - 1] : null;
  const sc = hist && hist.scores && hist.scores.length ? hist.scores[hist.scores.length - 1] : null;
  let html =
    `<a class="detaillink" href="/stock/${encodeURIComponent(symbol)}">Open full detail page — charts &amp; analysis →</a>` +
    `<div class="notscreened">${symbol} isn't in today's top-10 screen.` +
    (sc ? ` Last scored ${sc.date} (stage ${sc.stage_reached}).` : ` No funnel history yet.`) +
    `</div>`;
  if (th) {
    html += `<div class="case inval" style="margin-top:12px"><b>Last thesis · ${esc(
      th.date
    )}</b>${esc(th.thesis || "")}</div>`;
    if (th.invalidation) html += `<div class="case bear"><b>Invalidation</b>${esc(th.invalidation)}</div>`;
  }
  html += `<div id="tvgauge"></div>`;
  side.innerHTML = html;
  drawGauge($("tvgauge"), symbol);
}

function renderPick(side, n) {
  const subs = n.subscores || {};
  const sub6 = Object.keys(RUB)
    .map((k) => `<div class="c"><b>${subs[k] ?? "—"}</b><span>${k} /${RUB[k]}</span></div>`)
    .join("");
  $("an-tags").innerHTML = `${n.sector ? `<span class="tag">${esc(n.sector)}</span>` : ""}
    <span class="tag ${n.conviction || ""}">${n.conviction || "—"}</span>`;
  const cats = (n.catalysts_ahead || [])
    .map((c) => esc(c.event) + (c.date ? ` (${c.date})` : ""))
    .filter(Boolean);
  side.innerHTML = `
    <a class="detaillink" href="/stock/${encodeURIComponent(n.ticker)}">Open full detail page — charts &amp; analysis →</a>
    <div class="scorebig"><span class="tier ${tier(n.total_score)}" style="width:20px;height:20px"></span><div class="n">${n.total_score}</div><div class="of">/100 · funnel score</div></div>
    <div class="meta"><span class="tag">rank #${n._rank}</span>
      <span class="tag ${n.conviction || ""}">${n.conviction || "—"} conviction</span>
      ${n.time_horizon_days ? `<span class="tag">${n.time_horizon_days}d horizon</span>` : ""}</div>
    <div class="sub6">${sub6}</div>
    <p class="thesis">${esc(n.thesis || "")}</p>
    ${n.bull_case ? `<div class="case bull"><b>Bull</b>${esc(n.bull_case)}</div>` : ""}
    ${n.bear_case ? `<div class="case bear"><b>Bear</b>${esc(n.bear_case)}</div>` : ""}
    ${n.invalidation ? `<div class="case inval"><b>Invalidation</b>${esc(n.invalidation)}</div>` : ""}
    ${(n.key_risks || []).length ? `<div class="risks"><b>Key risks:</b> ${esc((n.key_risks || []).join("; "))}</div>` : ""}
    ${cats.length ? `<div class="risks"><b>Catalysts ahead:</b> ${cats.join("; ")}</div>` : ""}`;
}

function go() {
  const v = $("q").value;
  if (v) analyze(v);
  return false;
}

/* ---------- picks ---------- */
async function loadPicks(auto) {
  let rep = null;
  try {
    const r = await fetch("/report/latest");
    if (r.ok) rep = await r.json();
  } catch (e) {}
  const body = $("picks-body");
  const meta = $("picksMeta");
  if (!rep || !rep.names || !rep.names.length) {
    body.innerHTML = `<div class="empty">No screen yet — press <b>Research today's best stocks</b> to build one.</div>`;
    meta.textContent = "";
    return false;
  }
  const rg = $("regime");
  rg.textContent = rep.regime || "—";
  rg.className = "regime " + (rep.regime || "");
  meta.innerHTML = `<b style="font-family:var(--mono)">${rep.as_of}</b> · regime ${rep.regime || "—"} ·
    <a href="/report/${rep.as_of}/html">full report with charts →</a>`;
  PICKS = {};
  rep.names.forEach((n, i) => {
    n._rank = i + 1;
    PICKS[n.ticker] = n;
  });
  body.innerHTML =
    `<div class="picks">` +
    rep.names
      .map(
        (n, i) => `
    <div class="prow" data-t="${esc(n.ticker)}" onclick="analyze('${esc(n.ticker)}')">
      <span class="rk">#${i + 1}</span>
      <div><div class="tk">${esc(n.ticker)}</div><div class="sec">${esc(n.sector || "")}</div></div>
      <span class="sc"><span class="tier ${tier(n.total_score)}"></span>${n.total_score}</span>
      <a class="prow-detail" href="/stock/${encodeURIComponent(n.ticker)}" onclick="event.stopPropagation()" title="Full detail page">↗</a>
    </div>`
      )
      .join("") +
    `</div>`;
  if (auto && rep.names.length) analyze(rep.names[0].ticker);
  return true;
}

/* ---------- operator controls (no token needed) ---------- */
function flash(t, c) {
  const m = $("msg");
  m.textContent = t;
  m.className = "msg " + (c || "");
}
async function doPost(path, working) {
  const btn = event.target;
  btn.disabled = true;
  flash(working, "");
  try {
    const j = await postJSON(path);
    flash(j.detail || JSON.stringify(j), j.accepted ? "ok" : "err");
  } catch (e) {
    flash("Request failed: " + e, "err");
  } finally {
    btn.disabled = false;
    setTimeout(loadStatusArchive, 1500);
  }
}

/* ---------- status / archive / kpis ---------- */
async function refreshKpis() {
  const s = await getStatus();
  $("kpis").innerHTML = `
    <div class="kpi"><b>${fmt(s.price_bars)}</b><span>price bars</span></div>
    <div class="kpi"><b>${fmt(s.distinct_tickers_with_bars)}</b><span>tickers tracked</span></div>
    <div class="kpi"><b>${fmt(s.runs)}</b><span>runs</span></div>
    <div class="kpi"><b>${s.latest_bar_date || "—"}</b><span>latest data</span></div>`;
  const note = $("goNote");
  if (s.run_in_progress) {
    $("goLbl").textContent = "A run is in progress — watch it";
  } else if (s.backfill_running) {
    note.textContent = `Loading market history — ${fmt(s.bar_dates || 0)} / ${fmt(
      s.history_target || 252
    )} trading days. Click to watch.`;
  } else if (!s.ready_for_first_run) {
    note.textContent = "First click loads about a year of market history (one-time), then screens the market.";
  }
  // If a run is already going when the page loads, jump into the progress view.
  if (s.run_in_progress && !RESEARCHING && $("view-start").hidden === false) startResearch(true);
}
async function loadStatusArchive() {
  try {
    const s = await getStatus();
    $("ops-status").textContent = s.backfill_running
      ? "backfill running…"
      : s.run_in_progress
      ? "run in progress…"
      : s.ready_for_first_run
      ? "ready to run"
      : "needs backfill";
  } catch (e) {}
  try {
    const reps = await (await fetch("/reports?limit=60")).json();
    $("archive-body").innerHTML = reps.length
      ? reps
          .map(
            (r) => `<tr>
      <td class="tkc"><a href="/report/${r.as_of}/html">${r.as_of}</a></td>
      <td>${r.regime || "—"}</td>
      <td>${r.funnel && r.funnel["Stage 5 final"] != null ? r.funnel["Stage 5 final"] : "—"}</td>
      <td class="num">$${(r.cost_usd || 0).toFixed(3)}</td>
      <td><a href="/report/${r.as_of}/html">open</a> · <a href="/report/${r.as_of}/pdf">pdf</a></td>
    </tr>`
          )
          .join("")
      : '<tr><td colspan="5" class="sub">No runs yet.</td></tr>';
  } catch (e) {}
}

/* ---------- boot ---------- */
async function boot() {
  refreshKpis();
  loadStatusArchive();
  // Returning visitor with a screen already built? Show it straight away.
  const has = await loadPicks(true);
  if (has) showView("results");
  setInterval(() => {
    if (!RESEARCHING) loadStatusArchive();
  }, 15000);
}
boot();
