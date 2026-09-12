"""No page may publish a numeric accuracy rate.

WHY THIS FILE EXISTS. The site published "99.9% accurate" for a long time, and
the audit in docs/internal/identity-failures.md established that the figure
measured whether a filing balances against ITS OWN stated total -- close to a
self-consistency check -- rather than whether we recovered every component of
it. Those are two different numbers on the same rows (99.8% and 86.84%), and
the flattering one was the one on the homepage under the word "accuracy".

The extraction figure is not published either. It is a temporary engineering
state -- mezzanine tags are mapped but not yet re-ingested -- and printing it
would convert a fixable bug into a permanent marketing claim.

So the rule is: the METHOD is public, the RATE is not. This test is what stops
a percentage drifting back in, which is easy to do by accident because the
figure is still computed and still sitting in `stats.identity()`.

The patterns are matched WITH the percent sign. A bare "99.9" would fire on a
legitimate `$99.9B` in a seeded balance sheet, and a test that cries wolf on
real data gets deleted rather than fixed.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

# The specific figures the audit retired, plus the shape of any new one.
BANNED_EXACT = ("99.9%", "99.8%", "86.84%", "78.6%")
# Any "NN.N% accurate/accuracy" construction, whatever the digits.
BANNED_SHAPE = re.compile(r"\d{1,3}(?:\.\d+)?\s*%\s*(?:accurate|accuracy)", re.I)

PAGES = (
    "/",
    "/pricing",
    "/compare/to-scale-vs-intrinio",
    "/best/sec-filings-api-for-quants",
    # Every page above, as text, in one response. It is the pages, so it
    # cannot publish a rate they do not -- but it is also the file an answer
    # engine reads in preference to them, which makes it the worst place for
    # one to reappear.
    "/llms-full.txt",
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'content.db'}")
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


def _seed_balanced() -> None:
    """One company that balances, so the identity block renders at all."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap, UniverseSnapshot

    q = dt.date(2025, 12, 31)
    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date.today(), ticker="AAA", name="Alpha Inc",
            sector="Financials",
        ))
        s.add(SectorMap(ticker="AAA", sector="Financials", sector_source="sic"))
        for metric, value in (
            ("total_assets", 1_000e9),
            ("total_liabilities", 600e9),
            ("total_equity", 400e9),
        ):
            s.add(Fundamental(
                ticker="AAA", metric=metric, value=value, period_end=q,
                fiscal_period="FY", filing_date=dt.date(2026, 2, 13), source="sec",
            ))


@pytest.mark.parametrize("path", PAGES)
def test_no_page_publishes_a_numeric_accuracy_rate(client, path):
    _seed_balanced()
    r = client.get(path)
    assert r.status_code == 200, f"{path} did not render"
    body = r.text

    for banned in BANNED_EXACT:
        assert banned not in body, f"{path} still publishes {banned}"
    match = BANNED_SHAPE.search(body)
    assert match is None, f"{path} publishes an accuracy rate: {match.group(0)!r}"

    # Any decimal percentage at all, including inside an HTML comment -- a
    # comment ships in the response body, so "the 0.1% is not rounding" was
    # just as public as the headline it sat under. Money and CSS values are
    # integers or carry a unit, so a bare `N.N%` on these pages is a rate.
    stray = re.search(r"\d{1,3}\.\d+\s*%", body)
    assert stray is None, f"{path} carries a percentage: {stray.group(0)!r}"


def test_the_method_is_still_stated_on_the_homepage(client):
    """Removing the number must not remove the claim -- otherwise the page says
    nothing about reconciliation at all, which is a worse outcome than a
    misleading percentage."""
    _seed_balanced()
    body = client.get("/").text
    assert "reconcile" in body.lower()
    assert "assets = liabilities + equity" in body


def test_the_rate_is_still_computed_even_though_it_is_not_shown(client):
    """The figure is not wrong, it is unpublishable. `identity_failures.py` and
    the internal audit both read it, so it must keep working."""
    from src.company.stats import identity

    _seed_balanced()
    ident = identity(max_age_s=0.0)
    assert ident["checkable"] == 1
    assert ident["pass_rate_pct"] == 100.0


def test_the_machine_readable_files_publish_no_rate():
    """llms.txt and financial-data.txt are read by answer engines, which will
    quote a number back with more confidence than the page gave it."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "static_seo"
    for name in ("llms.txt", "financial-data.txt"):
        text = (root / name).read_text(encoding="utf-8")
        for banned in BANNED_EXACT:
            assert banned not in text, f"{name} still publishes {banned}"
        assert "0.999" not in text, f"{name} still publishes 0.999"
        assert "0.786" not in text, f"{name} still publishes 0.786"


# ---------------------------------------------------------------------------
# House style
# ---------------------------------------------------------------------------
# The second half of the same problem. The rule above stops a NUMBER drifting
# back in; this stops the VOICE drifting, which is the thing that makes a post
# read as generated. Every word here is one I do not use and would not notice
# myself typing at the end of a long draft.

FORBIDDEN_WORDS: tuple[str, ...] = (
    "delve", "unlock", "seamless", "robust", "leverage",
    "in today's", "in today\u2019s", "game-changer", "game changer",
)

# Matched with spaces around them so "however" does not fire on "how ever" and
# "us" does not fire inside "thus". The posts are written in the first person
# singular: this is one author, and a corporate "we" here is a fiction.
FORBIDDEN_PRONOUNS: tuple[str, ...] = (" we ", " our ", " ours ", " us ")

# The style rules apply to posts written under them. They are NOT run over the
# back catalogue, for one concrete reason: "why-bank-balance-sheets-are-
# different" uses the word "leverage", correctly, as the noun for a bank's
# equity-to-assets ratio. The rule is aimed at the corporate verb ("leverage
# our platform"), and a test that forced that post to find a synonym for the
# right technical term would be the test making the writing worse.
STYLED_SLUGS: tuple[str, ...] = (
    "build-scalable-sec-edgar-pipeline",
    "sec-xbrl-duplicate-tags",
)


def _post_slugs() -> list[str]:
    from src.report.blog import POSTS

    return [p.slug for p in POSTS]


def _visible_text(html: str) -> str:
    """Rendered prose, with tags and entities out of the way.

    Tag names would otherwise trip the checks all by themselves: every page
    carries a `<header>`, and "leverage" has to be findable in prose without
    `<em>` and `&amp;` getting in the way first.
    """
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&mdash;", "-").replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", text)


@pytest.mark.parametrize("slug", STYLED_SLUGS)
def test_no_post_uses_a_forbidden_word(client, slug):
    r = client.get(f"/blog/{slug}")
    assert r.status_code == 200, f"/blog/{slug} did not render"
    text = _visible_text(r.text).lower()
    for word in FORBIDDEN_WORDS:
        assert word not in text, f"/blog/{slug} uses {word!r}"


@pytest.mark.parametrize("slug", STYLED_SLUGS)
def test_no_post_writes_in_the_first_person_plural(client, slug):
    """One author. "We" is a company voice a one-person project has not
    earned, and it is the tell that a draft was not written by the person
    whose name is on it.

    Case-SENSITIVE, and that is not a detail: lowercasing first makes "a large
    US bank" match the pronoun "us", so the check would fire on the country
    and teach everyone to ignore it.
    """
    from src.report.blog import BY_SLUG

    text = " " + _visible_text(BY_SLUG[slug].body) + " "
    for pronoun in FORBIDDEN_PRONOUNS:
        assert pronoun not in text, f"/blog/{slug} says {pronoun.strip()!r}"
        capitalised = " " + pronoun.strip().capitalize() + " "
        assert capitalised not in text, f"/blog/{slug} says {capitalised.strip()!r}"


@pytest.mark.parametrize("slug", _post_slugs())
def test_no_post_ends_with_a_conclusion_heading(client, slug):
    """A section called "Conclusion" is a section with nothing in it. If the
    last point is worth making it gets a heading that says what it is."""
    from src.report.blog import BY_SLUG

    headings = re.findall(r"<h[23][^>]*>(.*?)</h[23]>", BY_SLUG[slug].body, re.S)
    for h in headings:
        plain = re.sub(r"<[^>]+>", "", h).strip().lower()
        assert plain not in ("conclusion", "in conclusion", "summary"), (
            f"/blog/{slug} has a {plain!r} heading"
        )


@pytest.mark.parametrize("slug", _post_slugs())
def test_every_post_carries_its_schema_and_its_links(client, slug):
    """A post nothing links out of is a dead end, and one without BlogPosting
    is invisible to the thing most likely to quote it."""
    r = client.get(f"/blog/{slug}")
    assert r.status_code == 200
    body = r.text
    assert "BlogPosting" in body, f"/blog/{slug} has no BlogPosting schema"
    assert f'rel="canonical" href="https://toscale.pro/blog/{slug}"' in body
    assert 'property="og:title"' in body
    for target in ('href="/pricing"', 'href="/api"'):
        assert target in body, f"/blog/{slug} does not link {target}"
    assert re.search(r'href="/company/[A-Z.]+"', body), (
        f"/blog/{slug} links no company page"
    )


def test_the_new_posts_carry_no_invented_percentage(client):
    """The posts added for search discuss accuracy, which is exactly where a
    comparative percentage would be tempting to write."""
    for slug in STYLED_SLUGS:
        body = client.get(f"/blog/{slug}").text
        for banned in BANNED_EXACT:
            assert banned not in body, f"/blog/{slug} publishes {banned}"
        match = BANNED_SHAPE.search(body)
        assert match is None, f"/blog/{slug} publishes {match.group(0)!r}"


# ---------------------------------------------------------------------------
# Code samples
# ---------------------------------------------------------------------------
# A different way to be wrong on a page than publishing a rate: a command a
# reader pastes has to be the command that was written. Four of these lost
# their shell line continuations without anybody touching them -- a backslash
# at the end of a line inside a non-raw Python string is a LINE CONTINUATION,
# so Python removed the backslash and the newline before the string existed,
# and the page rendered a folded command that no longer showed its own shape.
#
# Invisible in the source, which is why the assertion reads the RENDER.


def test_a_shell_continuation_survives_into_the_rendered_page(client):
    """Every backslash-newline written in a bash sample must still be one."""
    import html as _html

    from src.report.blog import POSTS

    block = re.compile(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", re.S)
    checked = 0
    for post in POSTS:
        page = client.get(f"/blog/{post.slug}")
        assert page.status_code == 200, post.slug
        for raw in block.findall(page.text):
            lines = _html.unescape(raw).split("\n")
            for i, line in enumerate(lines):
                if not line.rstrip().endswith("\\"):
                    continue
                checked += 1
                # A continuation on the last line continues into nothing.
                assert i < len(lines) - 1, (
                    f"/blog/{post.slug}: a continuation with nothing after it"
                )
    assert checked >= 4, (
        f"expected at least the four known continuations, found {checked} -- "
        "they were eaten by the string literal once already"
    )


def test_no_curl_example_is_folded_onto_one_line_with_its_url(client):
    """The shape the bug left behind: the flags and the URL on one line with
    the backslash gone, which still runs and no longer reads as a wrap."""
    import html as _html

    from src.report.blog import POSTS

    block = re.compile(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", re.S)
    for post in POSTS:
        text = _html.unescape(client.get(f"/blog/{post.slug}").text)
        for code in block.findall(text):
            for line in code.split("\n"):
                if "curl" not in line or "-H " not in line:
                    continue
                if "http" in line and not line.rstrip().endswith("\\"):
                    # One short command on one line is fine; a long one that
                    # was WRITTEN wrapped is what this catches.
                    assert len(line) < 80, (
                        f"/blog/{post.slug}: folded curl: {line!r}"
                    )
