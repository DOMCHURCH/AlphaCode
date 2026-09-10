"""JSON-LD blocks, ready to paste into a `<head>`.

Every page on this site renders as a string of hand-written HTML, and none of
them currently say anything a machine can read about what they are. A crawler
arriving at `/company/JPM` sees a title, a table of dollar amounts, and no
statement anywhere that this is a financial dataset, published by an
organisation, derived from a named regulator's filings. An answer engine
deciding whether to cite the page has to infer all of that from prose. Schema
markup is the one place you can simply *say* it, and it is the single largest
lever on whether an AI engine understands a page well enough to quote it
correctly.

Each function here returns a complete `<script type="application/ld+json">`
element as a string — not a dict, not a fragment. That shape is deliberate.
The page builders in this package concatenate strings; handing them a dict
would mean every call site repeats the serialisation, and the one that gets it
wrong ships a page with a broken `<head>` rather than a page with slightly
worse markup. A function that returns the finished tag can only be used one
way.

**Why `json.dumps` and not an f-string.** A JSON-LD payload built by string
interpolation breaks the moment a company name contains a quote mark, and
about six thousand of the names on this site came out of SEC filings rather
than out of a designer's head. `json.dumps` is the only thing that gets
escaping right for every input, so nothing in this module writes JSON by hand.

**Why `</` becomes `<\\/`.** An HTML parser looking for the end of a `<script>`
element does not parse JSON — it scans for the literal characters `</script`
and stops there, wherever they appear. A description field containing
`</script>` would therefore terminate the block early and spill the remaining
JSON into the document as text, which is both a broken page and, if the field
ever carried user input, an injection. JSON allows `\\/` as an escape for `/`,
so rewriting every `</` to `<\\/` after serialisation makes the sequence
unrepresentable while leaving the parsed value identical. `_script()` does this
once, for everything, and no function here builds a tag any other way.

**Why absolute `https://toscale.pro` URLs, even on a preview deploy.** This is
the deliberate exception to the rule `api_tab.py` states for the API examples,
where baking the production host into a page would print the wrong host for
whoever loaded it. Schema `@id` values are not instructions to the reader's
browser; they are global identifiers for entities, and the entity is the same
entity whichever host served the page. An `@id` that varied by deployment would
tell a knowledge graph that the preview and production sites are two different
organisations. Identifiers are absolute and constant; the API examples stay
host-relative. Both rules point the same way — say the true thing about what
you are describing.

**Rule 16.6 applies to every function in this file.** Google treats marked-up
content that is not visible on the page as spam, and a manual action costs more
than the markup was ever going to earn. So each docstring below names the pages
its block is safe on. `pricing_ld()` belongs only where all four prices render.
`faq_ld()` belongs only beside a visible Q&A. `dataset_ld()` belongs on
`/dataset` and nowhere else. None of these are suggestions.

Nothing here imports from `src.api`, or from anything that imports it, or from
the database. These are pure functions over their arguments and module
constants — importable from a test, a script, or a page builder without
starting the application.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

# The canonical origin. One constant, because the moment two files disagree
# about whether the site is `toscale.pro` or `www.toscale.pro`, the knowledge
# graph gets two organisations and each of them gets half the authority.
SITE = "https://toscale.pro"

# Stable `@id` values so separate blocks on separate pages describe the SAME
# organisation rather than one anonymous organisation per page. The `#org`
# fragment is the convention: a node identifier that is not itself a URL you
# can fetch, hung off a URL you can. Every block below that needs to name the
# publisher points here instead of restating it, which is both smaller and
# impossible to let drift.
ORG_ID = f"{SITE}/#organization"
SITE_ID = f"{SITE}/#website"

ORG_NAME = "To Scale"

# The one-sentence description, in one place. It appears in the Organization
# block, and it is the sentence an assistant is most likely to repeat when
# asked what this product is, so it is written to be repeated: what the data
# is, where it comes from, what state it is in.
ORG_DESCRIPTION = (
    "Reconciled balance sheet data from SEC EDGAR XBRL filings, served as "
    "clean JSON through a REST API and as a bulk CSV download."
)

# The blog does not exist yet. This constant is here so that when it does, the
# route is changed in one place rather than hunted for inside a formatted
# string, and so that `blogposting_ld` can be read today without anyone having
# to guess what shape the URL will take. If the blog ships under a different
# prefix, this is the line to edit.
BLOG_BASE = f"{SITE}/blog"

# The publisher, inlined wherever another block names it.
#
# A bare `{"@id": ORG_ID}` is a *reference* to a node, and a reference is only
# resolvable if the node it points at is somewhere in the same document.
# `organization_ld()` ships on the home page alone, so on /pricing, /dataset,
# /api and a blog post that reference would dangle: a validator reads
# `author` or `publisher` as present-but-empty and reports a missing required
# field. Carrying the name and url alongside the `@id` makes every block
# self-sufficient while keeping the identifier stable — JSON-LD merges nodes
# that share an `@id`, so restating it here is not a second organisation, it
# is the same one described twice.
_ORG_REF = {
    "@type": "Organization",
    "@id": ORG_ID,
    "name": ORG_NAME,
    "url": f"{SITE}/",
}


def _script(payload: Any) -> str:
    """Serialise a payload and wrap it in a script tag that cannot be escaped.

    The single choke point every function in this module goes through. It
    exists so that the `</` rewrite is applied exactly once, in one place,
    rather than being a thing each author has to remember — a safety property
    that depends on remembering is not a safety property.

    `ensure_ascii` is left at its default of True on purpose. Company names
    out of EDGAR carry accents, and non-ASCII characters escaped to `\\uXXXX`
    are both valid JSON and immune to the mojibake that follows a response
    served with the wrong charset. It costs a few bytes and removes an entire
    class of encoding bug.
    """
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    # The only sequence an HTML parser can use to end the script element
    # early. `\/` is a legal JSON escape for `/`, so the parsed string is
    # unchanged — the character just cannot appear adjacent to `<` any more.
    text = text.replace("</", "<\\/")
    return f'<script type="application/ld+json">{text}</script>'


def organization_ld() -> str:
    """Who publishes this site, and how to search it.

    Two nodes in one `@graph` because they answer two different questions and
    an engine may want either: `Organization` is the entity that could appear
    in a knowledge panel and be named as the source of a citation, `WebSite`
    is the container that carries the search action.

    The `SearchAction` target is real — `/search?q=` is a live route that
    resolves a ticker or a company name to a company page. This block is the
    only reason a search engine would ever offer a search box inside this
    site's result listing, and declaring one that did not work would be worse
    than declaring none.

    Belongs on the home page, and only there. It describes the site as a
    whole; repeating it on every page would say nothing new and would put six
    thousand identical Organization declarations into the index.

    `logo` and `sameAs` are deliberately absent rather than invented. There is
    no logo asset in `src/report/static/` to point at, and no company profile
    URLs have been supplied. A `logo` pointing at a 404 is a validation error
    and an empty `sameAs` is noise; both should be added here the moment the
    real values exist.
    """
    return _script(
        {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "Organization",
                    "@id": ORG_ID,
                    "name": ORG_NAME,
                    "url": f"{SITE}/",
                    "description": ORG_DESCRIPTION,
                },
                {
                    "@type": "WebSite",
                    "@id": SITE_ID,
                    "url": f"{SITE}/",
                    "name": ORG_NAME,
                    "publisher": {"@id": ORG_ID},
                    "potentialAction": {
                        "@type": "SearchAction",
                        "target": {
                            "@type": "EntryPoint",
                            "urlTemplate": f"{SITE}/search?q={{search_term_string}}",
                        },
                        # schema.org requires this exact literal string as the
                        # value; it is a placeholder name, not a field the
                        # site fills in.
                        "query-input": "required name=search_term_string",
                    },
                },
            ],
        }
    )


def pricing_ld() -> str:
    """The four ways to pay, as a Product with four Offers.

    **Why `Product` and not `FinancialProduct`.** `FinancialProduct` is
    schema.org's type for the thing a bank sells — a loan, a deposit account,
    an insurance policy — and it carries properties like `interestRate` and
    `annualPercentageRate` that mean nothing here. To Scale sells data about
    finance, which is not a financial product; marking it as one would fail
    validation on the properties that type expects and would tell an engine
    this site is a lender. `Product` with `Offer` children is the type the
    schema cheat sheet gives for a pricing page, and it is what rich results
    actually read.

    Each tier is one `Offer` because they are four purchasable things, not one
    thing with four prices. The two recurring tiers carry a
    `UnitPriceSpecification` with a `referenceQuantity` naming the billing
    period, which is the only way in this vocabulary to say "$49 per month"
    rather than "$49". Without it, an engine comparing this site to a
    competitor reads $490 as more expensive than $49 rather than as the same
    plan bought a year at a time. `MON` and `ANN` are the UN/CEFACT codes
    schema.org expects for `unitCode`.

    The free tier is `price: "0"`, not an omitted offer. An engine asked
    whether this API has a free tier should find the answer marked up rather
    than have to read the page.

    Belongs on `/pricing` only — the page where all four prices are visible.
    Putting it on the home page, where only some tiers appear, is the exact
    hidden-markup case Rule 16.6 forbids.
    """
    return _script(
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "To Scale — reconciled SEC balance sheet data",
            "description": ORG_DESCRIPTION,
            "brand": _ORG_REF,
            "url": f"{SITE}/pricing",
            "offers": [
                {
                    "@type": "Offer",
                    "name": "Free",
                    "price": "0",
                    "priceCurrency": "USD",
                    "url": f"{SITE}/pricing",
                    "availability": "https://schema.org/InStock",
                    "description": (
                        "Registered access to the API with a monthly request "
                        "allowance."
                    ),
                },
                {
                    "@type": "Offer",
                    "name": "Pro API — monthly",
                    "price": "49.00",
                    "priceCurrency": "USD",
                    "url": f"{SITE}/pricing",
                    "availability": "https://schema.org/InStock",
                    "priceSpecification": {
                        "@type": "UnitPriceSpecification",
                        "price": "49.00",
                        "priceCurrency": "USD",
                        "referenceQuantity": {
                            "@type": "QuantitativeValue",
                            "value": 1,
                            "unitCode": "MON",
                        },
                    },
                },
                {
                    "@type": "Offer",
                    "name": "Pro API — annual",
                    "price": "490.00",
                    "priceCurrency": "USD",
                    "url": f"{SITE}/pricing",
                    "availability": "https://schema.org/InStock",
                    "priceSpecification": {
                        "@type": "UnitPriceSpecification",
                        "price": "490.00",
                        "priceCurrency": "USD",
                        "referenceQuantity": {
                            "@type": "QuantitativeValue",
                            "value": 1,
                            "unitCode": "ANN",
                        },
                    },
                },
                {
                    "@type": "Offer",
                    "name": "Full dataset download",
                    "price": "79.99",
                    "priceCurrency": "USD",
                    "url": f"{SITE}/dataset",
                    "availability": "https://schema.org/InStock",
                    "description": (
                        "One-time purchase of the complete reconciled dataset "
                        "as CSV."
                    ),
                },
            ],
        }
    )


def dataset_ld(rows: int, companies: int, as_of: str) -> str:
    """The CSV product, as a schema.org Dataset.

    `Dataset` is the type Google Dataset Search reads, and dataset search is a
    surface almost nobody in this category has bothered to appear in. It is
    also the type that lets the page state its provenance in a field rather
    than in a paragraph: `isBasedOn` naming SEC EDGAR is a machine-readable
    version of the argument the whole product rests on, which is that these
    numbers came from the filings and not from a vendor.

    The three arguments are required rather than defaulted because all three
    are facts about a file that changes. A dataset block whose row count is
    stale is worse than no block — it is a specific wrong number, published in
    the format engines trust most. Pass the values the page itself is
    rendering, from the same source, in the same request.

    `as_of` must already be an ISO 8601 date (`YYYY-MM-DD`) or datetime. It is
    passed straight through and not parsed: a date this module invented a
    format for would be a date nobody could check, and `dateModified` on a
    financial dataset is load-bearing.

    `isAccessibleForFree` is False and stated explicitly. Declaring a paid
    dataset as free is the fastest way to lose the listing entirely, and
    saying nothing leaves an engine to guess.

    `distribution.contentUrl` deliberately points at `/dataset`, the purchase
    page, rather than at the CSV bytes. The file is paywalled, so there is no
    URL that returns it to an anonymous crawler; pointing at one that 402s or
    redirects to a login would describe a download nobody can perform.
    Together with `isAccessibleForFree: false` this reads correctly — the
    distribution exists, and this is where you go to get it.

    Belongs on `/dataset` only.
    """
    return _script(
        {
            "@context": "https://schema.org",
            "@type": "Dataset",
            "name": "To Scale — reconciled US balance sheet dataset",
            "description": (
                "Reconciled balance sheets for "
                f"{companies:,} US-listed companies, {rows:,} data points, "
                "derived from SEC EDGAR XBRL filings. Every line item is "
                "validated against the accounting identity "
                "Assets = Liabilities + Equity, so exactly one figure is "
                "served per concept per period."
            ),
            "url": f"{SITE}/dataset",
            "creator": _ORG_REF,
            "publisher": _ORG_REF,
            "dateModified": as_of,
            "isAccessibleForFree": False,
            "license": f"{SITE}/terms",
            "keywords": [
                "SEC filings",
                "XBRL",
                "balance sheet",
                "financial statements",
                "EDGAR",
                "fundamental data",
            ],
            "measurementTechnique": (
                "Accounting-identity reconciliation: candidate XBRL tags are "
                "validated against Assets = Liabilities + Equity and the set "
                "that reconciles is selected."
            ),
            "isBasedOn": {
                "@type": "Dataset",
                "name": "SEC EDGAR XBRL company facts",
                "url": "https://www.sec.gov/edgar/sec-api-documentation",
            },
            "variableMeasured": [
                "Total assets",
                "Total liabilities",
                "Total equity",
            ],
            "distribution": [
                {
                    "@type": "DataDownload",
                    "encodingFormat": "text/csv",
                    "contentUrl": f"{SITE}/dataset",
                }
            ],
        }
    )


def software_ld() -> str:
    """The API, as something a developer could be recommended.

    Two types on one node. `SoftwareApplication` is the type an engine reaches
    for when a user asks "what should I use to get SEC balance sheet data",
    and it carries `offers`, which is how the answer gets to include a price.
    `WebAPI` is the precise type and the one that says this is an endpoint
    rather than something you install. Declaring both costs nothing and means
    neither retrieval path misses the page — schema.org permits an array of
    types for exactly this.

    `offers` here restates only the entry price, not the whole table.
    `pricing_ld()` owns the four tiers on the page where all four are visible;
    this block is on `/api`, where the point being made is "there is a free
    tier and paid tiers start at $49", and marking up prices the API page does
    not render would be the Rule 16.6 violation again.

    `documentation` points at `/api.json` rather than at `/api`. The reader of
    this field is a machine looking for a machine-readable contract, and
    `/api.json` is the OpenAPI document.

    Belongs on `/api`.
    """
    return _script(
        {
            "@context": "https://schema.org",
            "@type": ["SoftwareApplication", "WebAPI"],
            "name": "To Scale API",
            "description": (
                "REST API returning reconciled balance sheets for US-listed "
                "companies as JSON, derived from SEC EDGAR XBRL filings."
            ),
            "url": f"{SITE}/api",
            "documentation": f"{SITE}/api.json",
            "provider": _ORG_REF,
            "applicationCategory": "DeveloperApplication",
            "applicationSubCategory": "Financial data API",
            # A hosted API runs nowhere in particular from the caller's point
            # of view, and Google's validator wants the field present.
            "operatingSystem": "Any",
            "featureList": [
                "Reconciled balance sheets for 6,201 US-listed companies",
                "Validated against Assets = Liabilities + Equity",
                "As-reported figures, never restated",
                "JSON responses, API key authentication",
                "OpenAPI specification",
            ],
            "offers": [
                {
                    "@type": "Offer",
                    "name": "Free",
                    "price": "0",
                    "priceCurrency": "USD",
                    "url": f"{SITE}/pricing",
                },
                {
                    "@type": "Offer",
                    "name": "Pro API — monthly",
                    "price": "49.00",
                    "priceCurrency": "USD",
                    "url": f"{SITE}/pricing",
                },
            ],
        }
    )


def faq_ld(pairs: Sequence[tuple[str, str]] | Iterable[tuple[str, str]]) -> str:
    """A visible Q&A section, restated for the engine that will quote it.

    `FAQPage` is the highest-yield block on this site that does not exist yet.
    An answer engine asked "how accurate is SEC XBRL tag selection" wants a
    question-shaped chunk with a short answer attached, and this is the schema
    that hands it one. The questions should therefore be phrased the way a
    person would type them, not the way a marketing page would headline them.

    **`pairs` must mirror headings and text that are actually on the page.**
    This is the function in this module most likely to be misused: it accepts
    any string, so it will happily mark up an answer that appears nowhere in
    the HTML. Google issues manual actions for that. The correct pattern is to
    build the visible Q&A list and this block from the same sequence of
    tuples, in the same function, so they cannot drift.

    Answers are wrapped as plain text. HTML is permitted in `acceptedAnswer`
    but nothing here is written to sanitise it, and an answer carrying markup
    would be a second place to get escaping right. Pass prose.

    Returns an empty string for an empty sequence rather than an empty
    `FAQPage` — a block declaring no questions is not smaller markup, it is a
    validation error the page builder cannot see.
    """
    entries = [
        {
            "@type": "Question",
            "name": question,
            "acceptedAnswer": {"@type": "Answer", "text": answer},
        }
        for question, answer in pairs
    ]
    if not entries:
        return ""
    return _script(
        {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": entries,
        }
    )


def blogposting_ld(
    title: str,
    description: str,
    slug: str,
    published: str,
    modified: str,
) -> str:
    """One article, with the two dates that decide whether it gets cited.

    Freshness is a retrieval signal, and an article with no machine-readable
    date is an article an engine cannot tell is current. `datePublished` and
    `dateModified` are therefore both required arguments — not defaulted to
    each other, not defaulted to today. A `dateModified` that silently equals
    the render time is a cosmetic date bump, which is the thing that stops
    working and starts hurting.

    Both dates are passed through untouched and must already be ISO 8601.

    **The author is the organisation, not a person.** `Person` with `sameAs`
    to a real profile is the stronger signal, and it should replace this the
    moment there is a named author with a real profile to point at. Inventing
    a byline to fill the field would be fabricating exactly the kind of
    authorship signal E-E-A-T exists to measure.

    `image` is omitted rather than pointed at a placeholder. There is no
    per-post image asset and no OG image on this site yet; a `BlogPosting`
    whose `image` 404s validates worse than one with no image at all.

    `slug` is the path segment only — `"why-xbrl-tags-disagree"`, not a URL.
    The base lives in `BLOG_BASE` at the top of this module, because the blog
    route does not exist yet and the one thing that should not happen when it
    ships is a URL prefix baked into a formatted string in three files.

    Belongs on an individual post page, once such a page exists.
    """
    url = f"{BLOG_BASE}/{slug}"
    return _script(
        {
            "@context": "https://schema.org",
            "@type": "BlogPosting",
            "headline": title,
            "description": description,
            "url": url,
            "mainEntityOfPage": {"@type": "WebPage", "@id": url},
            "datePublished": published,
            "dateModified": modified,
            "author": _ORG_REF,
            "publisher": _ORG_REF,
        }
    )


def breadcrumb_ld(items: Sequence[tuple[str, str]] | Iterable[tuple[str, str]]) -> str:
    """The path from the home page to here, for the engine that draws it.

    Two things this earns. In a search result, the breadcrumb replaces the raw
    URL under the title, so `To Scale › Companies › JPMorgan Chase` appears
    instead of `toscale.pro/company/JPM` — more legible, and it names the
    brand in a place the brand was not before. In an answer engine, it is a
    statement about where a page sits, which is most of what a flat site of
    six thousand near-identical URLs otherwise fails to communicate.

    That makes this the highest-value block for the company pages
    specifically. Each one is currently a leaf with no declared parent and no
    inbound link from any sibling; a breadcrumb is the cheapest way to say the
    page belongs to something.

    `items` is an ordered sequence of `(name, url)`, root first, the current
    page last. Positions are one-based and generated here — a caller counting
    them by hand is a caller who will eventually start at zero, and a
    `BreadcrumbList` numbered from zero fails validation silently.

    URLs must be absolute. A relative `item` resolves against whatever host
    served the page, which is the drift `SITE` exists to prevent.

    Returns an empty string for an empty sequence, for the same reason
    `faq_ld` does.

    Rule 16.6 applies here too: emit this only where a breadcrumb UI is
    actually rendered. A declared trail the reader cannot see is marked-up
    invisible content.
    """
    elements = [
        {
            "@type": "ListItem",
            "position": position,
            "name": name,
            "item": url,
        }
        for position, (name, url) in enumerate(items, start=1)
    ]
    if not elements:
        return ""
    return _script(
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": elements,
        }
    )
