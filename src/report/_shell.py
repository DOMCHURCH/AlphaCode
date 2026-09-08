"""The one page shell: the same <head> and the same backdrop on every page.

Four page modules used to write their own `<!DOCTYPE html>`, and they disagreed
about everything a head is for. The home page carried a description; the company
pages carried none. Nothing anywhere carried a canonical tag, an Open Graph
card, or a robots directive -- so the dashboard was as indexable as the front
door, and a link pasted into Slack showed a bare URL. Copies drift; this is the
one definition, and a page says only what makes it different.

Three things it has to get right:

* **Every page gets the backdrop.** It used to be a per-page decision and the
  answer was "the home page", which meant the site looked like two sites. The
  decision that remains is how HARD the backdrop is veiled, because that is a
  readability question and it genuinely differs: a page glanced at can afford a
  sunset behind it, and a page of legal prose cannot. See `FILM_MODES`.
* **Indexing is opt-out per page, not global.** `/dashboard` and `/login` are
  private surfaces; a search result pointing at somebody's account page is a
  bad result for the reader and for us. They pass `robots=NOINDEX`; everything
  public gets the default.
* **Canonical URLs come from SITE_URL, never from the request.** A page served
  from a preview deployment must still name production as its canonical copy,
  or the preview competes with the real site in an index. The request's own
  origin is deliberately not consulted here.

Nothing in the head is required for the page to work. A missing stylesheet, a
missing font, a missing still: the page is still a page. That is the same rule
`backdrop.py` follows and it is why neither can break a reader's visit.
"""

from __future__ import annotations

import json
from html import escape

from src.report.company_page import asset_version

# ---------------------------------------------------------------------------
# The site's own copy, in one place
# ---------------------------------------------------------------------------
# Written here rather than in each page because these strings are the site's
# description of itself, and three slightly different descriptions is how a
# search result ends up quoting the one nobody would have chosen.

SITE_NAME = "To Scale"
DEFAULT_DESCRIPTION = (
    "Clean, real-time balance sheets and financial data for all US public "
    "companies. Free tier available. Pro $49/month, full dataset $29 once."
)
DEFAULT_KEYWORDS = (
    "SEC data, financial API, balance sheets, XBRL, stock research, "
    "fundamental data"
)
OG_TITLE = "To Scale — 99.9% Accurate SEC Data API"
OG_DESCRIPTION = (
    "Clean financial data for all US public companies. See what they actually "
    "own and owe."
)
TWITTER_DESCRIPTION = (
    "Clean financial data for all US public companies. Free tier available."
)

# Paths, not URLs. They are joined to SITE_URL where an absolute address is
# required (social cards, JSON-LD) and used as-is where it is not.
OG_IMAGE_PATH = "/static/og-image.png"
LOGO_PATH = "/static/logo.png"
APPLE_ICON_PATH = "/static/apple-touch-icon.png"
FALLBACK_ICON_PATH = "/static/favicon.ico"

FOUNDER = "Dominique Church"

INDEX = "index, follow"
NOINDEX = "noindex, nofollow"

# How much backdrop a page may spend, and what the veil over it weighs. The
# film is the same file everywhere; only the wash in front of it changes, and
# it changes for exactly one reason -- how long somebody has to read through
# it.
#
#   hero   the film is the point, above the fold        home, login
#   still  a reading page, standard veil                api reference, search
#   calm   figures to read, heavier veil                dashboard, company
#   quiet  the still only, heaviest veil                terms, privacy
#   none   no backdrop at all                           kept for completeness
#
# The veils themselves live in backdrop.css, keyed off `body[data-film]`, so
# the weight of a wash is a CSS question answered in CSS.
FILM_MODES = ("hero", "still", "calm", "quiet", "none")


def site_url() -> str:
    """The public origin, no trailing slash."""
    from src.config.settings import get_settings

    return get_settings().site_url.rstrip("/")


def absolute(path: str) -> str:
    """A site-relative path as an absolute URL. Anything already absolute is
    returned untouched, so a caller can pass a CDN address without this having
    to know about one."""
    if path.startswith(("http://", "https://")):
        return path
    return f"{site_url()}/{path.lstrip('/')}"


def canonical_url(path: str) -> str:
    """The canonical address of a page.

    The home page is "/" with the slash kept, because `https://toscale.pro` and
    `https://toscale.pro/` are the same page to a reader and two URLs to a
    crawler, and the one with the slash is the one everything else links to.
    """
    path = path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return f"{site_url()}{path}"


def json_ld(*blocks: dict) -> str:
    """One <script type="application/ld+json"> per block, or "" for none.

    `</script>` inside a JSON string would close this element early and turn
    the rest of the document into text, so "<" is escaped to its unicode form.
    That is legal JSON, structured-data parsers read it identically, and it is
    the only escaping this needs -- the payload is built here from our own
    settings, never from anything a reader typed.
    """
    out = []
    for block in blocks:
        if not block:
            continue
        payload = json.dumps(block, ensure_ascii=False, indent=2).replace("<", "\\u003c")
        out.append(f'<script type="application/ld+json">\n{payload}\n</script>')
    return "\n".join(out)


def _meta(name: str, content: str) -> str:
    return f'<meta name="{name}" content="{escape(content, quote=True)}">'


def _prop(prop: str, content: str) -> str:
    return f'<meta property="{prop}" content="{escape(content, quote=True)}">'


def render_head(
    *,
    title: str,
    path: str = "/",
    description: str = DEFAULT_DESCRIPTION,
    keywords: str = DEFAULT_KEYWORDS,
    robots: str = INDEX,
    og_title: str = OG_TITLE,
    og_description: str = OG_DESCRIPTION,
    og_type: str = "website",
    og_image: str = OG_IMAGE_PATH,
    twitter_title: str = OG_TITLE,
    twitter_description: str = TWITTER_DESCRIPTION,
    structured_data: tuple[dict, ...] = (),
    head_extra: str = "",
) -> str:
    """Everything between <head> and </head>, for any page on the site."""
    v = asset_version()
    image = absolute(og_image)
    url = canonical_url(path)
    ld = json_ld(*structured_data)
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
{_meta("description", description)}
{_meta("keywords", keywords)}
{_meta("robots", robots)}
{_meta("theme-color", "#0a0a0a")}
{_meta("color-scheme", "dark")}
<link rel="canonical" href="{escape(url, quote=True)}">

<!-- The mark is an inline SVG served by /favicon.ico, which is what a modern
     browser will take and what stays crisp on a retina tab. The .ico is the
     fallback for the browsers that will not read an SVG icon, and it is listed
     second so nothing that understands the first ever fetches it. -->
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<link rel="alternate icon" href="{FALLBACK_ICON_PATH}?v={v}" sizes="any">
<link rel="apple-touch-icon" href="{APPLE_ICON_PATH}?v={v}">

<!-- Social cards. og:image is absolute because a relative one is simply
     dropped by every scraper that reads it. -->
{_prop("og:site_name", SITE_NAME)}
{_prop("og:title", og_title)}
{_prop("og:description", og_description)}
{_prop("og:type", og_type)}
{_prop("og:url", url)}
{_prop("og:image", image)}
{_prop("og:image:alt", "To Scale — a balance sheet drawn at true proportion.")}
{_prop("og:locale", "en_US")}
{_meta("twitter:card", "summary_large_image")}
{_meta("twitter:title", twitter_title)}
{_meta("twitter:description", twitter_description)}
{_meta("twitter:image", image)}

<!-- Both hosts, because a Google font costs two round trips: the stylesheet
     from one and the font file from the other. Stripe is here because the
     checkout button on this page opens a session against it, and the
     handshake is the slowest part of that click. -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preconnect" href="https://api.stripe.com" crossorigin>
<link rel="dns-prefetch" href="https://checkout.stripe.com">
<link href="https://fonts.googleapis.com/css2?family=Jost:wght@400;600;700;800&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">

<link rel="stylesheet" href="/static/company.css?v={v}">
<!-- Second sheet rather than more of the first: the accuracy banner and the
     dashboard's controls are the only things that use it, and keeping them out
     of company.css keeps the drawing's stylesheet about the drawing. -->
<link rel="stylesheet" href="/static/dashboard.css?v={v}">
<!-- Last, and only token overrides: this is what turns the whole site dark,
     drawings included, without a rule being rewritten. -->
<link rel="stylesheet" href="/static/backdrop.css?v={v}">
<link rel="stylesheet" href="/static/dark.css?v={v}">
<!-- Last of all, and token overrides again: this is what turns the flat
     surfaces into glass and the corners soft. Swap this one line back to
     terminal.css to return to the ruled treatment; nothing else changes. -->
<link rel="stylesheet" href="/static/glass.css?v={v}">
{ld}
{head_extra}"""


def render_page(
    *,
    title: str,
    body: str,
    film: str = "still",
    path: str = "/",
    description: str = DEFAULT_DESCRIPTION,
    keywords: str = DEFAULT_KEYWORDS,
    robots: str = INDEX,
    og_title: str = OG_TITLE,
    og_description: str = OG_DESCRIPTION,
    og_type: str = "website",
    og_image: str = OG_IMAGE_PATH,
    twitter_title: str = OG_TITLE,
    twitter_description: str = TWITTER_DESCRIPTION,
    structured_data: tuple[dict, ...] = (),
    head_extra: str = "",
) -> str:
    """A complete document: head, backdrop, skip link, body.

    `body` is everything from the nav down, written by the page. This function
    owns the parts every page has to agree about and nothing else.
    """
    from src.report.backdrop import render_backdrop

    if film not in FILM_MODES:
        film = "still"
    backdrop = "" if film == "none" else render_backdrop()
    head = render_head(
        title=title,
        path=path,
        description=description,
        keywords=keywords,
        robots=robots,
        og_title=og_title,
        og_description=og_description,
        og_type=og_type,
        og_image=og_image,
        twitter_title=twitter_title,
        twitter_description=twitter_description,
        structured_data=structured_data,
        head_extra=head_extra,
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{head}
</head>
<body data-film="{film}">
{backdrop}
<a class="skip" href="#main">Skip to content</a>
{body}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Structured data
# ---------------------------------------------------------------------------
# Read off the same settings the endpoints enforce, so the price in a rich
# result cannot drift from the price a buyer is charged. A schema quoting $39
# while Stripe charges $49 is a claim Google will happily show for months.

def web_application_schema() -> dict:
    from src.config.settings import get_settings

    s = get_settings()
    return {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": SITE_NAME,
        "description": (
            "Clean, 99.9% accurate SEC data API for US public companies."
        ),
        "url": f"{site_url()}/",
        "applicationCategory": "FinancialApplication",
        "operatingSystem": "All",
        "offers": {
            "@type": "Offer",
            "price": "0",
            "priceCurrency": "USD",
            "description": (
                f"Free tier with {s.free_tier_monthly_calls} API calls/month"
            ),
        },
    }


def organization_schema() -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": SITE_NAME,
        "url": f"{site_url()}/",
        "logo": absolute(LOGO_PATH),
        "description": "Clean SEC data API for US public companies.",
        "founder": {"@type": "Person", "name": FOUNDER},
    }


def product_schema() -> dict:
    from src.config.settings import get_settings

    s = get_settings()
    return {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": f"{SITE_NAME} Pro",
        "description": (
            f"{s.pro_tier_monthly_calls:,} API calls per month to clean SEC data"
        ),
        "offers": {
            "@type": "Offer",
            "price": str(s.pro_price_usd),
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
            "url": f"{site_url()}/#pricing",
        },
    }


def home_structured_data() -> tuple[dict, ...]:
    """The three blocks the home page carries."""
    return (web_application_schema(), organization_schema(), product_schema())
