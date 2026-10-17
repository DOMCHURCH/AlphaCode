"""/dataset — the download page, and the honest label on the tin.

The dataset was previously a button on a dashboard tab with a price next to it.
That is enough to sell it and not enough to sell it HONESTLY: the one property
that decides whether it is the right purchase — that the file is built from the
database when it is requested, and a copy you save then stops changing — was
nowhere a buyer could read it before paying.

So this page states, above the button, the three facts somebody wants and could
not previously get:

* **how current the data in it is**: it is built fresh on each download, and
  holds what was loaded through the date shown, which is not the same as today;
* **how many rows and roughly how large the file is**, so a download is a
  decision rather than a surprise;
* **how it works**, said plainly and not in small print: built per request, a
  saved copy never changes, and no refresh is pushed to you.

The size is an estimate and is labelled as one. `dataset.iter_csv` streams the
file and never assembles it, so there is nothing on disk with a size to read —
the figure is a real sample of the real encoding, extrapolated, and a tilde in
front of it is more truthful than a precise number that was also a guess.

Public on purpose. Everything here is a reason to buy or a reason not to, and
putting it behind the purchase is how somebody buys the wrong product.
"""

from __future__ import annotations

from html import escape

from src.report.company_page import asset_version
from src.report.home_page import shell


def _js(value: str) -> str:
    """A string safe to drop into a <script> literal. Same rule as the
    dashboard's: `json.dumps` alone leaves `</script>` intact, which closes the
    block early and turns the rest of the page into markup."""
    import json

    return json.dumps(value).replace("</", "<\\/")


def render_dataset(
    *,
    nav: str = "",
    admin_email: str = "",
    dataset_price: str = "$79.99",
    pro_price: str = "$49",
    rows: int = 0,
    row_label: str = "—",
    size_label: str = "—",
    as_of: str = "the last quarterly load",
    newest_filing: str = "",
    columns: tuple[str, ...] = (),
    filename: str = "balanceproof-facts.csv",
) -> str:
    from src.report.nav import render_footer

    contact = (
        f'<a class="mail" href="mailto:{escape(admin_email)}">{escape(admin_email)}</a>'
        if admin_email
        else "the site owner"
    )
    cols = "".join(
        f'<li><code>{escape(c)}</code></li>' for c in columns
    ) or "<li>ticker, metric, value, period, filing date, source</li>"
    filed_line = (
        f" The newest filing in it is dated {escape(newest_filing)}."
        if newest_filing
        else ""
    )

    body = f"""{nav}
<main class="wrap" id="main">
  <header class="hero">
    <h1 class="htitle">The full dataset</h1>
    <p class="hlede">Every as-reported fact behind every drawing on this site,
      as one CSV. Bought once, downloaded as often as you like, and built from
      the database each time you ask for it.</p>
  </header>

  <!-- The disclaimer is the FIRST thing under the heading, not a footnote.
       Somebody who reads only this box has read the thing that decides whether
       this is the right purchase for them. -->
  <section class="sec" id="static-warning">
    <div class="banner warn" role="note">
      <span class="banner-tick" aria-hidden="true">&#9888;</span>
      <span><strong>Built when you download it.</strong> Each download is
        generated from our database at that moment, so it holds everything
        loaded through {escape(as_of)}. The copy you save does not change
        afterwards, and nothing is sent to you when new filings land: download
        again for a file that includes them.
        <a href="/pricing#compare">For the latest filing the day it lands, use
        the API</a>, which answers from the current database every time you
        call it.</span>
    </div>
  </section>

  <section class="sec" id="whats-in-it">
    <div class="sec-head"><h2>What you get</h2></div>
    <div class="tiles">
      <div class="tile">
        <span class="tlabel">Loaded through</span>
        <b class="tval sm">{escape(as_of)}</b>
        <span class="tsub">built fresh on every download</span>
      </div>
      <div class="tile">
        <span class="tlabel">Rows</span>
        <b class="tval">{escape(row_label)}</b>
        <span class="tsub">one per company, metric and period</span>
      </div>
      <div class="tile">
        <span class="tlabel">File size</span>
        <b class="tval">~{escape(size_label)}</b>
        <span class="tsub">estimated; the file is streamed, not stored</span>
      </div>
    </div>
    <p class="sec-sub">One UTF-8 CSV named
      <code>{escape(filename)}</code>, sorted by ticker, metric and period so two
      downloads taken months apart can be diffed against each other.{filed_line}</p>
    <p class="tryline">Columns</p>
    <ul class="collist">{cols}</ul>
  </section>

  <section class="sec" id="download">
    <div class="sec-head"><h2>Download</h2></div>
    <p class="sec-sub">The download needs the API key the purchase was made
      against. Signed in on this browser, it is filled in for you; otherwise
      paste it below.</p>
    <div class="sfield" id="dl-key-field">
      <input id="dl-key" type="text" placeholder="Your API key"
        autocomplete="off" autocapitalize="none" spellcheck="false"
        maxlength="128">
    </div>
    <button type="button" class="btn wide" id="ds-btn">
      Download the CSV (~{escape(size_label)})</button>
    <p class="formnote" id="ds-note" role="status" aria-live="polite"></p>

    <div class="buyrow" id="ds-buy" hidden>
      <div class="buy">
        <span class="plan-name">Full dataset</span>
        <p class="plan-price">{escape(dataset_price)}<small> once</small></p>
        <p class="plan-line">{escape(row_label)} rows, one CSV, repeat downloads included.</p>
        <button type="button" class="btn" id="ds-buy-btn">Buy the dataset</button>
      </div>
      <div class="buy">
        <span class="plan-name">Pro</span>
        <p class="plan-price">{escape(pro_price)}<small>/month</small></p>
        <p class="plan-line">The latest filing the day it lands, queried by API.</p>
        <a class="btn" href="/pricing">Compare them first</a>
      </div>
    </div>
  </section>

  <section class="sec" id="ds-notes">
    <div class="sec-head"><h2>Before you buy</h2></div>
    <ul class="notelist">
      <li><b>A copy you save does not change.</b> Nothing is pushed to you: no
        refresh, no delta and no subscription comes with a dataset purchase. The
        download is built from the database each time you request it, so
        downloading again later includes any filings loaded since, at no extra
        charge. Data is added when new filings are loaded, not on a schedule you
        can rely on.</li>
      <li><b>Older filings can carry SEC&rsquo;s month-end date.</b> Where
        SEC&rsquo;s bulk data sets rounded a period end to the month end and the
        filing also reports the real date, the real date is used. Filings loaded
        only from the bulk data sets still carry SEC&rsquo;s month-end date,
        which can be up to 15 days from the actual period end.</li>
      <li><b>Where two copies of a figure disagree, both are kept.</b> The last
        column, <code>period_note</code>, is blank except on the two rows of a
        figure whose value differs between a month-end date and the real date;
        each names the other&rsquo;s date. Neither value is guessed at.</li>
      <li><b>It is as-reported, not restated.</b> Each row is the figure as it
        appeared in the filing, which is the point: it is what a backtest needs
        and what a restated feed cannot give you.</li>
      <li><b>It is a large file.</b> Leave the tab open while it streams; it is
        generated row by row rather than served from disk.</li>
      <li><b>Questions before paying?</b> Email {contact}. The
        <a href="/pricing#faq">FAQ</a> answers the four asked most often.</li>
    </ul>
  </section>

{render_footer(f"Questions about the data: {contact}.")}
</main>

<div class="modal" id="pay-modal" hidden>
  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="pay-title">
    <h2 id="pay-title">This one is paid</h2>
    <p id="pay-body"></p>
    <button type="button" class="btn wide" id="pay-close">Close</button>
  </div>
</div>

<script>
  window.TO_SCALE = {{
    adminEmail: {_js(admin_email)},
    datasetPrice: {_js(dataset_price)},
    rows: {int(rows)}
  }};
</script>
<script src="/static/nav.js?v={asset_version()}" defer></script>
<script src="/static/dataset.js?v={asset_version()}" defer></script>"""

    from src.report.home_page import companies_count, companies_label
    from src.report.schema import breadcrumb_ld, dataset_ld

    return shell(
        "SEC XBRL Balance Sheet Dataset (CSV) — BalanceProof",
        body,
        description=(
            f"Every as-reported SEC balance sheet figure as one CSV: "
            f"{row_label} rows, {companies_label()} companies, each reconciled "
            f"with A = L + E. One-time {dataset_price}, no subscription."
        ),
        canonical="/dataset",
        # `companies=6201` was a literal ARGUMENT here -- the hardest
        # kind of stale figure to find, because it is not a string and
        # no grep of the copy turns it up, and it lands in structured
        # data that engines quote back verbatim.
        ld=dataset_ld(rows=rows, companies=companies_count(), as_of=as_of)
        + breadcrumb_ld([("Home", "/"), ("Dataset", "/dataset")]),
    )
