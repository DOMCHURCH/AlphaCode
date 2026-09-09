"""/dataset — the download page, and the honest label on the tin.

The dataset was previously a button on a dashboard tab with a price next to it.
That is enough to sell it and not enough to sell it HONESTLY: the one property
that decides whether it is the right purchase — that it is a snapshot, fixed at
the moment of download and never updated — was nowhere a buyer could read it
before paying.

So this page states, above the button, the three facts somebody wants and could
not previously get:

* **when the data in it was last added to**, which is not the same as today;
* **how many rows and roughly how large the file is**, so a download is a
  decision rather than a surprise;
* **that it is static**, said plainly and not in small print.

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
    filename: str = "to-scale-facts.csv",
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
      as one CSV. Bought once, downloaded as often as you like — and fixed at
      the moment it was generated.</p>
  </header>

  <!-- The disclaimer is the FIRST thing under the heading, not a footnote.
       Somebody who reads only this box has read the thing that decides whether
       this is the right purchase for them. -->
  <section class="sec" id="static-warning">
    <div class="banner warn" role="note">
      <span class="banner-tick" aria-hidden="true">&#9888;</span>
      <span><strong>This is a static snapshot.</strong> It contains the data as
        of {escape(as_of)} and will never update. When new filings land, this
        file does not change — you would buy a fresh copy.
        <a href="/pricing#compare">For live data, use the API</a>, which is
        current every time you call it.</span>
    </div>
  </section>

  <section class="sec" id="whats-in-it">
    <div class="sec-head"><h2>What you get</h2></div>
    <div class="tiles">
      <div class="tile">
        <span class="tlabel">Generated</span>
        <b class="tval sm">{escape(as_of)}</b>
        <span class="tsub">the data does not change after this date</span>
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
        <p class="plan-line">{escape(row_label)} rows, one CSV, no updates.</p>
        <button type="button" class="btn" id="ds-buy-btn">Buy the dataset</button>
      </div>
      <div class="buy">
        <span class="plan-name">Pro</span>
        <p class="plan-price">{escape(pro_price)}<small>/month</small></p>
        <p class="plan-line">Live data instead of a snapshot, queried by API.</p>
        <a class="btn" href="/pricing">Compare them first</a>
      </div>
    </div>
  </section>

  <section class="sec" id="ds-notes">
    <div class="sec-head"><h2>Before you buy</h2></div>
    <ul class="notelist">
      <li><b>It does not update.</b> There is no refresh, no delta and no
        subscription attached to a dataset purchase. A later snapshot is another
        purchase.</li>
      <li><b>It is as-reported, not restated.</b> Each row is the figure as it
        appeared in the filing, which is the point — it is what a backtest needs
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

    return shell("To Scale — The full dataset", body)
