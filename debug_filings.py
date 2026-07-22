"""
One-off diagnostic: print the RAW filing records FEC's API returns for a
committee, so we can see exactly which fields are (and aren't) populated --
specifically to chase down why some quarterly periods show blank
raised/spent/cash-on-hand in the dashboard despite the real filing (viewable
directly on docquery.fec.gov) having real numbers.

Defaults to C00916288 (Friends of Sherrod Brown) since that's the committee
in question -- pass --committee-id to check a different one.

Read-only, makes 1 API call, doesn't touch your snapshots or dashboard.

Usage:
    FEC_API_KEY=your_key python3 debug_filings.py
    FEC_API_KEY=your_key python3 debug_filings.py --committee-id C00896019
"""

import argparse
import json
import sys

from fec_client import FECClient

CURRENT_CYCLE = 2026


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--committee-id", default="C00916288",
        help="FEC committee ID to pull filings for (default: C00916288, Friends of Sherrod Brown)",
    )
    args = parser.parse_args()

    client = FECClient()
    filings = client.committee_filings_all(args.committee_id, cycle=CURRENT_CYCLE, max_pages=4)

    if not filings:
        print(f"No filings found for {args.committee_id} in cycle {CURRENT_CYCLE}.")
        sys.exit(1)

    print(f"Got {len(filings)} filing(s) for {args.committee_id}, cycle {CURRENT_CYCLE}. Full raw JSON below:\n")
    for i, rec in enumerate(filings, 1):
        print(f"--- Filing {i} ---")
        print(json.dumps(rec, indent=2, default=str))
        print()

    print(
        "Copy/paste everything above back into the chat -- that tells us which "
        "raw fields actually hold the period totals (or confirms they're genuinely "
        "null/unprocessed on FEC's end right now) instead of guessing at field names again."
    )


if __name__ == "__main__":
    sys.exit(main())
