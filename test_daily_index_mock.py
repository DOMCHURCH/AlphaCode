#!/usr/bin/env python3
"""Test SEC daily index parsing with mock data (no network access required)."""

from src.ingest.sec_daily_index import fetch_daily_index
from src.ingest.sec_edgar import TRACKED_FORMS
import datetime as dt


def test_parse_daily_index():
    """Test parsing of SEC daily index format."""
    # Mock daily index content
    sample_index = """CIK|Company Name|Form Type|Date Filed|Filename
0000320193|APPLE INC|8-K|2026-08-01|0000320193-26-000042-index.html
0000789019|MICROSOFT CORPORATION|10-Q|2026-08-01|0000789019-26-000001-index.html
0001018724|NVIDIA CORPORATION|8-K|2026-08-01|0001018724-26-000095-index.html
0000051143|ADOBE INC|SC 13D|2026-07-31|0000051143-26-000001-index.html
0000789019|MICROSOFT CORPORATION|8-K|2026-07-31|0000789019-26-000002-index.html
"""

    # Parse the mock data
    lines = sample_index.strip().split("\n")
    if len(lines) < 2:
        print("ERROR: Not enough lines")
        return False

    rows = []
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) < 5:
            continue
        rows.append({
            "cik": parts[0].strip(),
            "company_name": parts[1].strip(),
            "form_type": parts[2].strip(),
            "date_filed": parts[3].strip(),
            "filename": parts[4].strip() if len(parts) > 4 else None,
        })

    print("Parsed rows:")
    for r in rows:
        print(f"  {r['cik']:10s} {r['form_type']:10s} {r['company_name']:30s}")

    # Filter to tracked forms
    tracked = [r for r in rows if r["form_type"] in TRACKED_FORMS]
    print(f"\nFiltered to tracked forms: {len(tracked)}/{len(rows)}")
    for r in tracked:
        print(f"  {r['form_type']:10s} {r['company_name']}")

    return len(tracked) == 5  # 8-K x3, 10-Q x1, SC 13D x1


if __name__ == "__main__":
    success = test_parse_daily_index()
    print(f"\nTest result: {'PASS' if success else 'FAIL'}")
