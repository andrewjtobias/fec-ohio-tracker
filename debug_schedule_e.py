"""
One-off diagnostic: print the RAW fields FEC actually returns for a Schedule E
(independent expenditure) record, so we can fix fetch_data.py's field mapping
against real data instead of guessing field names.

This makes 1 API call and doesn't touch your snapshots, entities.csv, or the
dashboard -- it's read-only and safe to run any time. Delete it once we've
used the output.

Usage:
    FEC_API_KEY=your_key python3 debug_schedule_e.py
    FEC_API_KEY=your_key python3 debug_schedule_e.py --candidate-id S6OH00304
"""

import argparse
import json
import sys

from fec_client import FECClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--candidate-id", default="S6OH00304",
        help="FEC candidate ID to pull outside-spending records for (default: Husted, S6OH00304 -- known to have plenty of records)",
    )
    parser.add_argument("--count", type=int, default=2, help="How many raw records to print (default: 2)")
    args = parser.parse_args()

    client = FECClient()
    records = client.schedule_e_by_target(args.candidate_id, max_records=args.count)

    if not records:
        print(f"No Schedule E records found for {args.candidate_id}. Try a different --candidate-id.")
        sys.exit(1)

    print(f"Got {len(records)} record(s) for {args.candidate_id}. Full raw JSON below:\n")
    for i, rec in enumerate(records, 1):
        print(f"--- Record {i} ---")
        print(json.dumps(rec, indent=2, default=str))
        print()

    print(
        "Copy/paste everything above back into the chat -- that tells us the exact "
        "field names FEC uses for the spending committee's name/ID and any link to "
        "the source filing (pdf_url, image number, etc.), so the fix uses real field "
        "names instead of another guess."
    )


if __name__ == "__main__":
    sys.exit(main())
