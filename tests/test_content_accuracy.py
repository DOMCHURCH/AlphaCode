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
    "/compare/balanceproof-vs-intrinio",
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
    "in conclusion", "it's important to note", "it\u2019s important to note",
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
    "what-i-got-wrong-about-sec-filings",
)

# Posts written under the no-em-dash rule. Scoped rather than site-wide on
# purpose: the back catalogue uses em dashes throughout and correctly, and a
# test that forced those posts to be rewritten would be the test making the
# writing worse. Same reasoning as STYLED_SLUGS itself.
NO_EM_DASH_SLUGS: tuple[str, ...] = (
    "what-i-got-wrong-about-sec-filings",
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
    assert f'rel="canonical" href="https://balanceproof.dev/blog/{slug}"' in body
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


# ---------------------------------------------------------------------------
# Structured data: the rebrand, and the fields Google requires
# ---------------------------------------------------------------------------

def _ld_blocks(html: str) -> list[dict]:
    """Every JSON-LD payload on a page, parsed."""
    import json

    out = []
    for m in re.finditer(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    ):
        out.append(json.loads(m.group(1)))
    return out


# Pages that carry a Product block, or would if one drifted onto them.
PRODUCT_PAGES = ("/pricing", "/", "/dataset", "/api")


@pytest.mark.parametrize("path", PRODUCT_PAGES)
def test_product_schema_has_no_old_domain(client, path):
    """No JSON-LD may still name the pre-rebrand host.

    Asserted over the SERIALISED block rather than a URL field by field: the
    thing that breaks is a hostname anywhere in the payload, including inside
    an `@id` or a nested brand reference, and naming the fields to check is how
    the next one gets missed.
    """
    import json

    r = client.get(path)
    assert r.status_code == 200, path
    for block in _ld_blocks(r.text):
        raw = json.dumps(block)
        assert "toscale.pro" not in raw, f"{path}: {block.get('@type')} names the old host"
        assert "toscale" not in raw.lower(), f"{path}: {block.get('@type')} names the old brand"


def test_product_schema_has_every_required_field(client):
    """`image` is required, and its absence is what Search Console reported.

    Merchant listings and Product snippets both read this block. Every other
    required property was already present, which is why the page looked fine
    and the report did not.
    """
    blocks = [b for b in _ld_blocks(client.get("/pricing").text)
              if b.get("@type") == "Product"]
    assert len(blocks) == 1, "expected exactly one Product block on /pricing"
    product = blocks[0]

    for field in ("name", "description", "url", "image", "offers", "brand"):
        assert product.get(field), f"Product is missing {field}"
    assert product["image"].startswith("https://balanceproof.dev/")

    offers = product["offers"]
    assert isinstance(offers, list) and offers, "offers must be a non-empty list"
    for offer in offers:
        assert offer["@type"] == "Offer"
        for field in ("name", "price", "priceCurrency", "availability", "url"):
            assert offer.get(field) not in (None, ""), f"Offer {offer.get('name')} missing {field}"
        assert offer["priceCurrency"] == "USD"
        # A digital good has no shipping and no returns. Asserting either would
        # be marking up something untrue to satisfy a checklist.
        assert "shippingDetails" not in offer
        assert "hasMerchantReturnPolicy" not in offer


BREADCRUMB_PAGES = ("/pricing", "/api", "/methodology", "/dataset", "/blog", "/terms")


@pytest.mark.parametrize("path", BREADCRUMB_PAGES)
def test_breadcrumbs_are_absolute_and_well_formed(client, path):
    """Every ListItem: position from 1 in order, a name, an absolute item.

    The `item` values were site-relative paths. A relative one does not
    resolve to anything a crawler can index, which is what Search Console
    reported, and it is invisible on the page because the breadcrumb UI is
    built from the same paths and renders correctly either way.
    """
    import json

    r = client.get(path)
    assert r.status_code == 200, path
    crumbs = [b for b in _ld_blocks(r.text) if b.get("@type") == "BreadcrumbList"]
    assert crumbs, f"{path} renders a breadcrumb trail but no BreadcrumbList"

    for block in crumbs:
        raw = json.dumps(block)
        assert "toscale" not in raw.lower(), f"{path}: breadcrumb names the old brand"

        items = block["itemListElement"]
        assert items, f"{path}: empty breadcrumb"
        for expected, item in enumerate(items, start=1):
            assert item["@type"] == "ListItem"
            assert item["position"] == expected, f"{path}: positions out of order"
            assert item["name"], f"{path}: ListItem {expected} has no name"
            assert item["item"].startswith("https://balanceproof.dev"), (
                f"{path}: ListItem {expected} item is not absolute: {item['item']!r}"
            )
        # The trail ends on the page serving it.
        assert items[-1]["item"].rstrip("/").endswith(path.rstrip("/")), (
            f"{path}: last crumb is {items[-1]['item']!r}"
        )


AUDITED_PAGES = (
    "/", "/pricing", "/api", "/dataset", "/methodology", "/blog", "/terms",
    "/privacy", "/compare/balanceproof-vs-intrinio",
    "/best/sec-filings-api-for-quants",
)


@pytest.mark.parametrize("path", AUDITED_PAGES)
def test_every_jsonld_url_is_absolute_and_ours(client, path):
    """Walks every node, including @graph children and nested offers.

    The two exceptions are named rather than pattern-matched: `isBasedOn`
    points at SEC because that is what the data is based on, and schema.org
    URLs are vocabulary rather than links to this site.
    """
    import json

    r = client.get(path)
    assert r.status_code == 200, path

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("url", "item", "@id", "image", "contentUrl"):
                    if isinstance(value, str):
                        assert not value.startswith("/"), f"{path}: relative {key}={value!r}"
                        assert "toscale" not in value.lower(), f"{path}: old brand in {key}"
                if key == "isBasedOn":
                    continue  # upstream source, correctly off-site
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for block in _ld_blocks(r.text):
        walk(block)
        assert "toscale" not in json.dumps(block).lower(), path


@pytest.mark.parametrize("path", ("/", "/api", "/pricing"))
def test_every_offer_says_whether_it_is_available(client, path):
    """An Offer with a price and no `availability` prices a thing without
    saying it is sold. Both blocks that carry offers now agree."""
    def offers(node):
        if isinstance(node, dict):
            if node.get("@type") == "Offer":
                yield node
            for value in node.values():
                yield from offers(value)
        elif isinstance(node, list):
            for item in node:
                yield from offers(item)

    found = 0
    for block in _ld_blocks(client.get(path).text):
        for offer in offers(block):
            found += 1
            assert offer.get("availability"), f"{path}: {offer.get('name')} has no availability"
            assert offer.get("price") is not None
            assert offer.get("priceCurrency")
    assert found, f"{path} carries no Offer to check"


def _post_prose(html: str) -> str:
    """Just the post's own body.

    Scoped to `div.prose` rather than the whole page on purpose. The rendered
    page also carries the nav, the footer, and a list of every other post's
    summary, and the back catalogue uses em dashes correctly and throughout.
    A page-wide assertion would be testing those posts, which are not written
    under this rule and are not being rewritten to satisfy it.
    """
    m = re.search(r'<div class="prose">(.*?)</div>\s*</article>', html, re.S)
    if m is None:
        m = re.search(r'<div class="prose">(.*)', html, re.S)
    assert m, "could not find the post body"
    return _visible_text(m.group(1))


@pytest.mark.parametrize("slug", NO_EM_DASH_SLUGS)
def test_no_post_uses_an_em_dash(client, slug):
    """Periods and commas do the same work without the typographic tell."""
    r = client.get(f"/blog/{slug}")
    assert r.status_code == 200, f"/blog/{slug} did not render"
    prose = _post_prose(r.text)
    assert "—" not in prose, f"/blog/{slug} uses an em dash"
    assert "–" not in prose, f"/blog/{slug} uses an en dash"


@pytest.mark.parametrize("slug", NO_EM_DASH_SLUGS)
def test_no_post_publishes_a_percentage(client, slug):
    """Counts, not rates. The whole reason test_content_accuracy.py exists."""
    r = client.get(f"/blog/{slug}")
    assert "%" not in _post_prose(r.text), f"/blog/{slug} publishes a percentage"


# ---------------------------------------------------------------------------
# Title tags have a budget
# ---------------------------------------------------------------------------

TITLE_MAX = 60


def test_no_post_title_tag_truncates_in_a_result_list():
    """`seo_title` is what a search result shows, and it has a width.

    Past 60 characters the tail is replaced with an ellipsis, so whatever was
    put at the end is the part nobody reads. Checked against `seo_title` and
    not `title`: the H1 is read on the page, where there is room for it, and
    the two are separate fields precisely so they can be different lengths.
    """
    from src.report.blog import POSTS

    over = [(p.slug, len(p.seo_title), p.seo_title)
            for p in POSTS if len(p.seo_title) > TITLE_MAX]
    assert not over, f"title tags over {TITLE_MAX} chars: {over!r}"


def test_every_post_has_a_distinct_title_tag():
    """Two posts sharing a title tag compete with each other for the same
    result slot, which is a worse outcome than either ranking alone."""
    from src.report.blog import POSTS

    titles = [p.seo_title for p in POSTS]
    assert len(titles) == len(set(titles)), "duplicate title tags"
