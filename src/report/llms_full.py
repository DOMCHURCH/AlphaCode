"""`/llms-full.txt` -- every page of prose on this site, as text, in one file.

`llms.txt` is a map. It says which of six thousand URLs answers which
question, and a model that follows it faithfully then makes another ten
requests to find out what those pages say. This is the other half of the
convention: the text itself, in one response, so that what a model says about
this product is what the product's own pages say.

Two decisions worth stating, because both could reasonably have gone the
other way.

IT IS GENERATED, NOT WRITTEN. `llms.txt` and `financial-data.txt` are files on
disk because they are prose about the product that nobody else writes. This
one is prose SOMEBODY ELSE ALREADY WROTE -- it is the pages -- and a second
copy of a page is a copy that drifts. The coverage count, the prices and the
data freshness on those pages all move; the last time a number was pasted into
a flat file here it said "6,201 companies" for months while `/dataset` counted
the same table live on every request.

IT IS BUILT FROM WHAT THE ROUTES RETURN. Not from a second rendering assembled
with its own arguments, which is the same drift one layer down: the argument
that gets forgotten is the one that was added last.

What is dropped is navigation, footers, scripts and the visually-hidden
captions that exist for screen readers -- chrome that costs a reader context
and tells it nothing about the product.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Nothing inside these is prose. `nav` and `footer` are the same links on
# every page; repeating them thirteen times is most of the file and none of
# the content.
_DROP = frozenset({
    "script", "style", "noscript", "template", "svg", "canvas",
    "nav", "footer", "head", "button", "select", "option", "iframe",
    "form",
})
# Never start a skip on a tag that has no closing tag, or the skip never ends.
_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})
# Block-level: each one starts a new line.
_BLOCK = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "details", "div",
    "dl", "dt", "figcaption", "figure", "form", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "main", "ol", "p", "pre", "section",
    "summary", "table", "tbody", "tfoot", "thead", "tr", "ul",
})
# Block-level AND a paragraph in its own right: gets a blank line after it.
# `li` and `tr` are deliberately absent -- a list with a blank line between
# every item reads as a list of one-line paragraphs. So is `div`, for the same
# reason: the status strip is seven of them in a row, and a paragraph break
# between "Facts: 1.8M" and "Companies: 6,184" says they are unrelated.
_PARAGRAPH = frozenset({
    "blockquote", "dd", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "pre", "section", "table",
})
_HEADINGS = {"h1": 2, "h2": 3, "h3": 4, "h4": 5, "h5": 6, "h6": 6}
# A class the site uses for a label whose value is the element right after it.
# The status strip is `<span class="sk">Companies</span><span class="sv">6,184
# </span>` -- laid out as a pair, written with nothing between them, and as
# text it wants a colon rather than a space. If the class is ever renamed this
# degrades to the space: flatter to read, still correct.
_LABEL_CLASSES = frozenset({"sk"})
# A class the site uses for a chip sitting BESIDE the text before it, written
# with nothing between the two: `Pro annual<span class="badge">Save $98</span>`
# ran together as "Pro annualSave $98". Named rather than handled by a rule
# that puts a space before every element, because there is one four lines
# below it -- `$490<small>/year</small>` -- that such a rule would break.
_CHIP_CLASSES = frozenset({"badge"})
# Classes holding a DRAWING rather than prose. The home page leads with one
# real balance sheet and five thumbnails, and as text those are a company's
# line items and percentages -- six companies' worth of figures in a document
# about what the product is. The figures belong in the API; the prose around
# the drawing stays, because it is the part that explains it.
_DROP_CLASSES = frozenset({"bs", "cards"})

_BLANK_RUN = re.compile(r"\n{3,}")
# Any run of whitespace INCLUDING newlines: a paragraph wrapped across six
# source lines is one sentence, and `&nbsp;` is a space to a reader.
_SPACES = re.compile(r"\s+")


class _ToText(HTMLParser):
    """HTML in, readable text out.

    A stream parser rather than a tree, because the input is this site's own
    markup and the thing being defended against is a long page, not malformed
    tags from somewhere else.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._pending = ""      # a bullet or a heading marker for the next line
        self._skip_tag = ""     # the tag whose subtree is being dropped
        self._skip_depth = 0
        self._pre = 0
        self._closed_inline = ""      # `</span><span>` needs something between
        self._inline_labels: list[bool] = []

    # -- assembling lines ---------------------------------------------------
    def _flush(self, blank_after: bool = False) -> None:
        line = "".join(self._buf)
        self._buf.clear()
        if not self._pre:
            line = _SPACES.sub(" ", line).strip()
        else:
            line = line.rstrip()
        if line:
            self.lines.append(self._pending + line)
            if blank_after:
                self.lines.append("")
        elif blank_after and self.lines and self.lines[-1] != "":
            # </table> carries no text of its own, and the heading after it
            # still needs the blank line that ends the table.
            self.lines.append("")
        self._pending = ""

    # -- parser callbacks ---------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag:
            if tag == self._skip_tag and tag not in _VOID:
                self._skip_depth += 1
            return

        classes = ""
        for key, value in attrs:
            if key == "class":
                classes = value or ""
        # `vh` is the site's visually-hidden class: table captions and form
        # labels written for a screen reader. A reader that is not one gets
        # the same sentence twice.
        names = set(classes.split())
        hidden = "vh" in names or any(k == "hidden" for k, _ in attrs)
        if (tag in _DROP or hidden or _DROP_CLASSES & names) and tag not in _VOID:
            self._skip_tag, self._skip_depth = tag, 1
            self._flush()
            return

        if self._closed_inline and self._buf and not self._buf[-1][-1:].isspace():
            self._buf.append(self._closed_inline)
        self._closed_inline = ""

        if (_CHIP_CLASSES & names
                and self._buf and not self._buf[-1][-1:].isspace()):
            self._buf.append(" ")

        # Void tags never close, and a cell is closed by the branch above, so
        # neither may push anything the matching endtag will not pop.
        if tag not in _BLOCK and tag not in _VOID and tag not in ("td", "th"):
            self._inline_labels.append(bool(_LABEL_CLASSES & names))

        if tag in _BLOCK:
            self._flush(blank_after=tag in _PARAGRAPH)
        if tag == "pre":
            self._pre += 1
        if tag == "li":
            self._pending = "- "
        elif tag == "tr":
            self._pending = "| "
        elif tag in _HEADINGS:
            self._pending = "#" * _HEADINGS[tag] + " "

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag, self._skip_depth = "", 0
            return
        if tag in ("td", "th"):
            self._buf.append(" | ")
            return
        if tag in _BLOCK:
            self._flush(blank_after=tag in _PARAGRAPH)
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
        if tag not in _BLOCK:
            was_label = self._inline_labels.pop() if self._inline_labels else False
            self._closed_inline = ": " if was_label else " "

    def handle_data(self, data: str) -> None:
        if self._skip_tag:
            return
        if data.strip():
            self._closed_inline = ""
        self._buf.append(data)

    # -- result -------------------------------------------------------------
    def text(self) -> str:
        self._flush()
        return _BLANK_RUN.sub("\n\n", "\n".join(self.lines)).strip()


def to_text(html: str) -> str:
    """The readable text of one page, without its chrome."""
    parser = _ToText()
    parser.feed(html)
    parser.close()
    return parser.text()


_MAIN = re.compile(r"<main\b[^>]*>(.*?)</main>", re.I | re.S)
_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.I | re.S)


def page_text(html: str) -> tuple[str, str]:
    """(heading, body) for one rendered page.

    Only what is inside `<main>` is read, which is how the nav, the head and
    the skip link are dropped without knowing anything about them. A page with
    no `<main>` falls back to the whole document rather than to nothing -- an
    empty section in this file is worse than a noisy one, because a model
    reading it cannot tell an empty section from a product that has nothing to
    say on the subject.
    """
    found = _MAIN.search(html)
    inner = found.group(1) if found else html

    heading = ""
    first = _H1.search(inner)
    if first:
        heading = to_text(first.group(1))
        # Dropped from the body because it becomes the section header.
        inner = inner[: first.start()] + inner[first.end():]
    return heading, to_text(inner)


_PREAMBLE = """# BalanceProof — full text

Every page of prose on toscale.pro, in one document: what the product is, how
the figures are selected, what it costs, what the API returns, and how it
compares with the alternatives.

This is generated from the live pages on each request, so the counts, prices
and dates below are the ones those pages are showing right now. The map of
which URL answers which question is at {origin}/llms.txt. Per-company balance
sheets are not here -- there are thousands of them, one per ticker, and they
are enumerated in {origin}/sitemap.xml.
"""


def render(sections: list[tuple[str, str]], *, origin: str) -> str:
    """The whole file, from `(path, rendered HTML)` pairs in reading order."""
    parts = [_PREAMBLE.format(origin=origin)]
    for path, html in sections:
        heading, body = page_text(html)
        parts.append(
            # "URL:" and not "Source:", which is what the home page's own
            # status strip calls the thing it reads: "Source: SEC EDGAR". Two
            # different claims must not be written the same way in a file
            # whose whole purpose is to be quoted.
            f"## {heading or path}\n"
            f"URL: {origin}{path}\n\n"
            f"{body}\n"
        )
    return _BLANK_RUN.sub("\n\n", "\n\n".join(parts)).strip() + "\n"
