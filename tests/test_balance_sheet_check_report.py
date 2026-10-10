"""The 2026-10 report on filings that fail the balance sheet check.

WHAT IS BEING DEFENDED. The report is a normal blog post, so the generic post
tests already cover its title budget, schema, sitemap and llms-full entry, and
`test_no_page_publishes_a_numeric_accuracy_rate` covers `/llms-full.txt` (which
includes every post's text, and is why the prose here uses counts and no decimal
percentage). What those do not cover is specific to this post:

  * the dash rule. `NO_EM_DASH_SLUGS` in test_content_accuracy.py also drives a
    test that forbids any percent sign at all, which a report whose charts are
    about rates cannot meet, so the dash rule is checked here instead.
  * words the owner ruled out for these filings.
  * the files the page points at: four charts (SVG and PNG), the CSV and its
    README. A page whose images 404 reads as a finished report in a crawl and as a
    broken one to a person.
  * that the CSV and the page say the same thing.
"""

from __future__ import annotations

import csv
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

SLUG = "which-sec-balance-sheets-fail-the-identity-check"
FOLDER = (pathlib.Path(__file__).resolve().parent.parent
          / "src" / "report" / "static" / "research" / "balance-sheet-check-2026-10")
CSV_PATH = FOLDER / "flagged-filings-2026-10-09.csv"
README_PATH = FOLDER / "README-flagged-filings-2026-10-09.md"
CHARTS = (
    "failure-rate-by-year-2026-10-09",
    "failure-rate-by-sector-2026-10-09",
    "failure-rate-by-size-2026-10-09",
    "gap-size-distribution-2026-10-09",
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'report.db'}")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()

    from src.api import app

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def _post():
    from src.report.blog import BY_SLUG

    return BY_SLUG[SLUG]


def _visible(html: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text)


def test_the_meta_description_fits_a_result_list():
    post = _post()
    assert len(post.seo_title) <= 60
    assert len(post.description) <= 155


def test_the_post_uses_no_dash_and_none_of_the_words_ruled_out():
    """No em or en dash. And the vocabulary the owner ruled out: a filing that
    failed OUR check is not a wrong filing, the report does not use the "one in
    five" framing the data does not support, and it speaks as the company."""
    post = _post()
    text = _visible(post.body + " " + post.title + " " + post.description + " " + post.summary)
    assert "—" not in text and "–" not in text
    low = " " + text.lower() + " "
    for banned in (" wrong", " error", " fraud", "red flag", "1 in 5", "one in five",
                   "restate", " 8-k", " solo ", "17-year", " forensic"):
        assert banned not in low, f"the report uses {banned.strip()!r}"


def test_the_rendered_page_has_no_dash_in_its_prose(client):
    r = client.get(f"/blog/{SLUG}")
    assert r.status_code == 200
    m = re.search(r'<div class="prose">(.*?)</div>\s*</article>', r.text, re.S)
    assert m, "could not find the post body"
    prose = _visible(m.group(1))
    assert "—" not in prose and "–" not in prose


def test_every_chart_is_on_the_page_and_on_disk(client):
    body = client.get(f"/blog/{SLUG}").text
    for chart in CHARTS:
        svg = f"/static/research/balance-sheet-check-2026-10/{chart}.svg"
        png = f"/static/research/balance-sheet-check-2026-10/{chart}.png"
        assert svg in body and png in body, chart
        assert (FOLDER / f"{chart}.svg").is_file() and (FOLDER / f"{chart}.png").is_file()
        assert client.get(svg).status_code == 200
        assert client.get(png).status_code == 200
    imgs = re.findall(r"<img\b[^>]*>", body)
    report_imgs = [i for i in imgs if "balance-sheet-check-2026-10" in i]
    assert len(report_imgs) == len(CHARTS)
    for tag in report_imgs:
        alt = re.search(r'alt="([^"]+)"', tag)
        assert alt and len(alt.group(1)) > 40, f"image without real alt text: {tag}"
        assert 'width="' in tag and 'height="' in tag


def test_the_data_files_the_page_links_are_served(client):
    body = client.get(f"/blog/{SLUG}").text
    for name in ("flagged-filings-2026-10-09.csv", "README-flagged-filings-2026-10-09.md"):
        href = f"/static/research/balance-sheet-check-2026-10/{name}"
        assert href in body
        assert client.get(href).status_code == 200


def test_the_internal_links_in_the_report_resolve(client):
    body = client.get(f"/blog/{SLUG}").text
    for href in ("/methodology", "/xbrl-balance-sheet-api"):
        assert f'href="{href}"' in body
        assert client.get(href).status_code == 200


def _rows() -> list[dict]:
    with CSV_PATH.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_the_csv_is_the_995_failed_filings_once_each_in_order():
    rows = _rows()
    assert len(rows) == 995
    keys = [(r["ticker"], r["period_end"]) for r in rows]
    assert len(set(keys)) == len(keys)
    assert keys == sorted(keys)
    assert len({r["ticker"] for r in rows}) == 286


def test_the_csv_agrees_with_the_numbers_the_page_states():
    rows = _rows()
    count = {}
    for r in rows:
        count[r["check_category"]] = count.get(r["check_category"], 0) + 1
    assert count == {"missing_tag": 971, "rounding": 14, "unexplained": 10}
    assert sum(r["sector"] == "Unclassified" for r in rows) == 434
    status = {}
    for r in rows:
        status[r["link_status"]] = status.get(r["link_status"], 0) + 1
    assert status == {
        "exact": 985,
        "not_10k_10q_foreign_filer": 6,
        "near_match_period_date_differs": 3,
        "no_cik": 1,
    }
    assert sum(1 for r in rows if r["liabilities_and_equity_stated"] == "") == 10

    body = _post().body
    for claim in ("995 of 38,568", "971 of the 995", "434 of the 995"):
        assert claim in body, claim


def test_the_csv_arithmetic_ties_out():
    for r in _rows():
        a = float(r["total_assets"])
        gap = a - (float(r["total_liabilities"]) + float(r["total_equity"]))
        assert abs(gap - float(r["gap_vs_components_usd"])) <= 0.5, r["ticker"]
        assert abs(abs(gap) / a * 100 - float(r["gap_pct_of_assets"])) < 1e-5, r["ticker"]
        if r["liabilities_and_equity_stated"]:
            stated_gap = a - float(r["liabilities_and_equity_stated"])
            assert abs(stated_gap - float(r["gap_vs_stated_usd"])) <= 0.5, r["ticker"]


def test_the_readme_does_not_use_the_words_ruled_out():
    text = README_PATH.read_text(encoding="utf-8")
    assert "—" not in text and "–" not in text
    low = text.lower()
    for banned in ("restate", "8-k", "solo", "wrong"):
        assert banned not in low, banned
    assert "not a statement that a filer's filing is incorrect" in low
