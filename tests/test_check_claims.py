"""Nothing the site, the API docs or the MCP server says claims every filing is checked.

A filing that does not report the lines the identity check needs is marked not
testable instead of passing, so "every filing is checked / verified /
reconciled" is false about the product. The one sentence used for the
balance-sheet check is below; where it is quoted it is quoted whole.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_check_not_testable import client  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]

CANONICAL = (
    "Each filing is checked against Assets = Liabilities + Equity when it "
    "reports the lines needed; where it does not, it is marked not testable "
    "instead of passing."
)

BANNED = (
    "every valid filing",
    "every filing is reconciled",
    "every filing is checked",
    "every filing is tested",
    "every filing tested",
    "every filing checked",
    "every sec filing is verified",
    "every balance sheet is checked",
    "every balance sheet is tested",
    "every balance sheet reconciled",
    "checks every filing",
    "checks every balance sheet",
    "check every sec balance sheet",
    "tests every filing",
    "verify every number",
    "verified against a = l + e",
    "every figure is reconciled",
    "every figure is checked against",
    "every figure reconciles",
)


def _texts():
    roots = [ROOT / "src" / "report", ROOT / "static_seo", ROOT / "clients" / "python" / "README.md",
             ROOT / "README.md", ROOT / "src" / "mcp_server.py"]
    for base in roots:
        files = base.rglob("*") if base.is_dir() else [base]
        for p in files:
            if p.suffix in {".py", ".txt", ".md", ".html"} and "research" not in p.parts:
                yield p, " ".join(p.read_text(encoding="utf-8", errors="ignore").lower().split())


def test_no_page_or_doc_claims_every_filing_is_checked_or_verified():
    hits = [(p.relative_to(ROOT).as_posix(), w) for p, text in _texts() for w in BANNED if w in text]
    assert hits == []


def test_the_methodology_page_and_the_mcp_server_state_the_canonical_sentence(client):
    from src.mcp_server import _INSTRUCTIONS

    assert CANONICAL in _INSTRUCTIONS
    assert CANONICAL in " ".join(client.get("/methodology").text.split())
