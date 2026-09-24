/* The plan features on the dashboard: Data and Alerts tabs.

   dashboard.js owns sign-in and status; it fires `bp:status` with the status
   payload whenever it renders one. This file reads the tier from that event,
   and against the plan matrix the server embedded (window.BP_PLANS) either
   enables each section or replaces it with a note naming the plan that
   unlocks it. The server enforces the same rules on every call, so this is
   presentation, not protection.

   Every call sends the stored key if there is one, and always the
   X-BP-Dashboard header, which is what lets a signed-in session (no key in
   this browser) use these endpoints. DOM is built with textContent only. */
(function () {
  "use strict";

  var KEY_STORE = "toscale.api_key";
  var PLANS = window.BP_PLANS || { matrix: {}, names: {}, order: [] };
  var tier = "free";

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function note(id, text, kind) {
    var n = $(id); if (!n) return;
    n.textContent = text || "";
    n.className = "formnote" + (kind ? " " + kind : "");
  }
  function key() { try { return localStorage.getItem(KEY_STORE) || ""; } catch (e) { return ""; } }

  function api(path, opts) {
    opts = opts || {};
    var headers = { "X-BP-Dashboard": "1" };
    var k = key();
    if (k) headers["X-API-Key"] = k;
    if (opts.body) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: opts.method || "GET", headers: headers, credentials: "same-origin",
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    });
  }

  function why(r, fallback) {
    var d = r && r.data && r.data.detail;
    if (!d) return fallback;
    if (typeof d === "string") return d;
    if (d.error === "plan_required") return "This needs the " + d.required_plan + " plan.";
    if (d.error === "watchlist_full") return "Your plan follows up to " + d.limit + " companies.";
    if (d.error === "webhook_limit") return "Your plan allows " + d.limit + " webhooks.";
    if (Array.isArray(d) && d[0] && d[0].msg) return d[0].msg;
    return fallback;
  }

  // ---- plan gates -------------------------------------------------------------

  function allowance(feature) {
    var row = PLANS.matrix[feature] || {};
    return tier in row ? row[tier] : row.free;
  }
  function allowed(feature) { var a = allowance(feature); return a === null || !!a; }
  function unlockingPlan(feature) {
    var row = PLANS.matrix[feature] || {};
    for (var i = 0; i < PLANS.order.length; i++) {
      var t = PLANS.order[i];
      if (row[t] === null || row[t]) return PLANS.names[t] || t;
    }
    return "a paid";
  }

  function applyLocks() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-lock]"), function (n) {
      var f = n.getAttribute("data-lock");
      var ok = allowed(f);
      n.hidden = ok;
      if (!ok) {
        n.textContent = "";
        n.appendChild(document.createTextNode("Included from the " + unlockingPlan(f) + " plan. "));
        var a = el("a", "See plans"); a.href = "/pricing"; n.appendChild(a);
      }
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-feature]"), function (n) {
      n.hidden = !allowed(n.getAttribute("data-feature"));
    });
    // A paid account changes plan in Stripe's portal, not with a second checkout.
    var paid = tier !== "free";
    Array.prototype.forEach.call(document.querySelectorAll("[data-buy-plan]"), function (b) {
      b.disabled = paid;
    });
    var years = allowance("history_years");
    var hb = $("hist-btn");
    if (hb) hb.title = years === null ? "All the history we hold" : ("Up to " + years + " year" + (years === 1 ? "" : "s") + " on your plan");
  }

  // ---- formatting ---------------------------------------------------------------

  function money(v) {
    if (v == null || isNaN(v)) return "—";
    var a = Math.abs(v), s = v < 0 ? "-" : "";
    if (a >= 1e12) return s + "$" + (a / 1e12).toFixed(2) + "T";
    if (a >= 1e9) return s + "$" + (a / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return s + "$" + (a / 1e6).toFixed(1) + "M";
    return s + "$" + a.toLocaleString();
  }
  function pick(sheet, section, keyName) {
    var v = sheet[section] && sheet[section][keyName];
    return v ? v.value : null;
  }
  function table(headers, rows) {
    var wrap = el("div", null, "tablewrap");
    var t = el("table", null, "compare");
    var thead = el("thead"), tr = el("tr");
    headers.forEach(function (h) { tr.appendChild(el("th", h)); });
    thead.appendChild(tr); t.appendChild(thead);
    var tb = el("tbody");
    rows.forEach(function (r) {
      var row = el("tr");
      r.forEach(function (c) {
        var td = el("td");
        if (c && c.nodeType) td.appendChild(c); else td.textContent = c;
        row.appendChild(td);
      });
      tb.appendChild(row);
    });
    t.appendChild(tb); wrap.appendChild(t);
    return wrap;
  }

  // ---- Data tab -----------------------------------------------------------------

  function showHistory(ev) {
    ev.preventDefault();
    var t = ($("hist-q").value || "").trim().toUpperCase();
    if (!t) return;
    note("hist-note", "Loading…");
    $("hist-out").textContent = ""; $("chg-out").textContent = "";
    api("/api/company/" + encodeURIComponent(t) + "/history").then(function (r) {
      if (!r.ok) { note("hist-note", why(r, "Could not load that company."), "bad"); return; }
      var d = r.data;
      var reach = d.years_allowed == null ? "all the history we hold" : ("the last " + d.years_allowed + " year" + (d.years_allowed === 1 ? "" : "s"));
      note("hist-note", d.periods + " balance sheet" + (d.periods === 1 ? "" : "s") + " for " + d.ticker +
        " (" + reach + " on your plan; data goes back to " + d.available_from + ").", "good");
      var rows = d.balance_sheets.map(function (b) {
        var cells = [b.period_end, money(pick(b, "assets", "total_assets")),
          money(pick(b, "liabilities", "total_liabilities")), money(pick(b, "equity", "shareholders_equity"))];
        if (b.provenance) {
          if (b.provenance.sec_url) {
            var a = el("a", b.provenance.form || "Filing"); a.href = b.provenance.sec_url; a.rel = "noopener"; a.target = "_blank";
            cells.push(a);
          } else { cells.push("—"); }
        }
        return cells;
      });
      var heads = ["Period", "Assets", "Liabilities", "Equity"];
      if (d.balance_sheets[0] && d.balance_sheets[0].provenance) heads.push("SEC filing");
      $("hist-out").appendChild(table(heads, rows));
      if (allowed("changes")) showChanges(t);
    }).catch(function () { note("hist-note", "Could not reach the server.", "bad"); });
  }

  var LABELS = { total_assets: "Total assets", total_liabilities: "Total liabilities",
    shareholders_equity: "Equity", total_equity_incl_nci: "Equity incl. minority interest",
    cash: "Cash", current_assets: "Current assets", current_liabilities: "Current liabilities",
    long_term_debt: "Long-term debt" };

  function showChanges(t) {
    var out = $("chg-out");
    api("/api/company/" + encodeURIComponent(t) + "/changes").then(function (r) {
      out.textContent = "";
      if (!r.ok) { out.appendChild(el("p", why(r, "Could not load what changed."), "formnote bad")); return; }
      var d = r.data;
      out.appendChild(el("p", d.previous_period_end
        ? "Period ending " + d.period_end + " against " + d.previous_period_end + "."
        : "Only one period is loaded, so there is nothing to compare yet.", "plan-note"));
      var rows = Object.keys(d.since_previous_period || {}).map(function (k) {
        var c = d.since_previous_period[k];
        var pct = c.change_pct == null ? "—" : (c.change_pct > 0 ? "+" : "") + c.change_pct.toFixed(1) + "%";
        return [LABELS[k] || k, money(c.previous), money(c.current), pct];
      });
      if (rows.length) out.appendChild(table(["Figure", "Before", "Now", "Change"], rows));
      var rs = d.restatements || [];
      out.appendChild(el("p", rs.length
        ? rs.length + " figure" + (rs.length === 1 ? " was" : "s were") + " restated by a later filing:"
        : "Nothing restated in the last three years.", "plan-note"));
      if (rs.length) {
        out.appendChild(table(["Figure", "Period", "Originally", "Revised to", "Revised in filing of"],
          rs.slice(0, 25).map(function (x) {
            return [LABELS[x.metric] || x.metric, x.period_end, money(x.previous), money(x.current), x.revised_in_filing];
          })));
      }
    });
  }

  function verify(ev) {
    ev.preventDefault();
    var list = ($("verify-q").value || "").split(/[\s,;]+/).map(function (s) { return s.trim().toUpperCase(); })
      .filter(Boolean);
    if (!list.length) return;
    note("verify-note", "Checking " + list.length + "…");
    $("verify-out").textContent = "";
    api("/api/verify", { method: "POST", body: { tickers: list } }).then(function (r) {
      if (!r.ok) { note("verify-note", why(r, "Could not check them."), "bad"); return; }
      var d = r.data;
      note("verify-note", d.checked + " checked: " + d.reconcile + " reconcile, " + d.do_not_reconcile +
        " do not" + (d.not_found ? ", " + d.not_found + " not found" : "") + ".", "good");
      $("verify-out").appendChild(table(["Ticker", "Period", "Reconciles", "Gap"], d.results.map(function (x) {
        if (!x.found) return [x.ticker, "—", "not found", "—"];
        return [x.ticker, x.period_end || "—", x.reconciles ? "Yes" : "No",
          x.imbalance_pct == null ? "—" : x.imbalance_pct.toFixed(2) + "%"];
      })));
    }).catch(function () { note("verify-note", "Could not reach the server.", "bad"); });
  }

  // ---- Alerts tab ---------------------------------------------------------------

  function loadWatch() {
    if (!allowed("watchlist")) return;
    api("/api/watchlist").then(function (r) {
      if (!r.ok) { note("watch-note", why(r, "Could not load your watchlist."), "bad"); return; }
      var list = $("watch-list"); list.textContent = "";
      var lim = r.data.limit;
      $("watch-count").textContent = r.data.items.length + (lim == null ? "" : " of " + lim);
      if (!r.data.items.length) list.appendChild(el("li", "Not following any companies yet."));
      r.data.items.forEach(function (it) {
        var li = el("li");
        var a = el("a", it.ticker); a.href = "/company/" + encodeURIComponent(it.ticker);
        li.appendChild(a);
        li.appendChild(document.createTextNode(it.last_alert_at
          ? " · last alert " + it.last_alert_at.slice(0, 10) : " · no new filing yet"));
        var b = el("button", "Stop following", "linkish"); b.type = "button";
        b.addEventListener("click", function () {
          api("/api/watchlist/" + encodeURIComponent(it.ticker), { method: "DELETE" }).then(loadWatch);
        });
        li.appendChild(document.createTextNode(" "));
        li.appendChild(b);
        list.appendChild(li);
      });
    });
  }

  function addWatch(ev) {
    ev.preventDefault();
    var t = ($("watch-q").value || "").trim().toUpperCase();
    if (!t) return;
    api("/api/watchlist", { method: "POST", body: { ticker: t } }).then(function (r) {
      if (!r.ok) { note("watch-note", why(r, "Could not follow that company."), "bad"); return; }
      note("watch-note", r.data.added ? "Following " + r.data.ticker + ". You will get an email when it files."
        : "Already following " + r.data.ticker + ".", "good");
      $("watch-q").value = "";
      loadWatch();
    });
  }

  function loadHooks() {
    if (!allowed("webhooks")) return;
    api("/api/webhooks").then(function (r) {
      if (!r.ok) { note("hook-note", why(r, "Could not load your webhooks."), "bad"); return; }
      var list = $("hook-list"); list.textContent = "";
      $("hook-count").textContent = r.data.endpoints.length + " of " + r.data.limit;
      if (!r.data.endpoints.length) list.appendChild(el("li", "No webhooks yet."));
      r.data.endpoints.forEach(function (h) {
        var li = el("li");
        li.appendChild(el("code", h.url));
        li.appendChild(document.createTextNode(h.active ? " · active" : " · switched off"));
        if (h.last_error) li.appendChild(document.createTextNode(" · last error: " + h.last_error));
        var test = el("button", "Send test", "linkish"); test.type = "button";
        test.addEventListener("click", function () {
          api("/api/webhooks/" + h.id + "/test", { method: "POST" }).then(function (x) {
            note("hook-note", x.ok && x.data.delivered ? "Test delivered." : "Test did not get a 2xx back.", x.ok && x.data.delivered ? "good" : "bad");
            loadHooks();
          });
        });
        var del = el("button", "Remove", "linkish"); del.type = "button";
        del.addEventListener("click", function () {
          api("/api/webhooks/" + h.id, { method: "DELETE" }).then(loadHooks);
        });
        li.appendChild(document.createTextNode(" "));
        li.appendChild(test);
        li.appendChild(document.createTextNode(" "));
        li.appendChild(del);
        list.appendChild(li);
      });
    });
  }

  function addHook(ev) {
    ev.preventDefault();
    var url = ($("hook-q").value || "").trim();
    if (!url) return;
    api("/api/webhooks", { method: "POST", body: { url: url } }).then(function (r) {
      if (!r.ok) { note("hook-note", why(r, "Could not add that webhook."), "bad"); return; }
      $("hook-secret-text").textContent = r.data.secret;
      $("hook-secret").hidden = false;
      note("hook-note", "Added.", "good");
      $("hook-q").value = "";
      loadHooks();
    });
  }

  // ---- wiring -------------------------------------------------------------------

  document.addEventListener("bp:status", function (ev) {
    tier = (ev.detail && ev.detail.tier) || "free";
    applyLocks();
    loadWatch();
    loadHooks();
  });

  document.addEventListener("DOMContentLoaded", function () {
    applyLocks();
    var f;
    if ((f = $("hist-form"))) f.addEventListener("submit", showHistory);
    if ((f = $("verify-form"))) f.addEventListener("submit", verify);
    if ((f = $("watch-form"))) f.addEventListener("submit", addWatch);
    if ((f = $("hook-form"))) f.addEventListener("submit", addHook);
    Array.prototype.forEach.call(document.querySelectorAll("[data-buy-plan]"), function (b) {
      b.addEventListener("click", function () {
        if (window.BP_startCheckout) window.BP_startCheckout(b.getAttribute("data-buy-plan"), b.getAttribute("data-buy-name"));
      });
    });
  });
})();
