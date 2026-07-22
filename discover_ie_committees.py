"""
One-time backfill: find every PAC/committee that has already made an
independent expenditure for or against one of your tracked candidates in the
7 priority races this cycle, and add any you're not already tracking to
entities.csv.

This is a *discovery* pass, separate from fetch_data.py's regular
incremental run. Run it once (or occasionally, e.g. after a primary or a big
news cycle) to backfill committees you didn't know about; fetch_data.py's
schedule_e_by_target check will keep catching brand-new ones going forward
on its own.

Limitation: this only finds PACs spending on candidates already in
entities.csv. It can't discover independent expenditures touching a
candidate you're not tracking at all -- there's no clean "all IE activity
in Ohio" filter in the FEC's schedule_e endpoint, only "by this candidate"
or "by this committee".

Usage:
    FEC_API_KEY=xxxx python discover_ie_committees.py                # writes new rows
    FEC_API_KEY=xxxx python discover_ie_committees.py --dry-run       # preview only
    FEC_API_KEY=xxxx python discover_ie_committees.py --min-date 2025-01-01
"""

import argparse
import csv
import logging
from pathlib import Path

from fec_client import FECClient
from fetch_data import PRIORITY_RACES, redact

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("discover_ie_committees")

ROOT = Path(__file__).parent
ENTITIES_CSV = ROOT / "entities.csv"

DEFAULT_MIN_DATE = "2025-01-01"  # start of the 2025-2026 election cycle


def load_entities():
    with open(ENTITIES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def fmt_money(val):
    try:
        return f"${float(val):,.0f}"
    except (TypeError, ValueError):
        return "—"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be added, don't write entities.csv")
    parser.add_argument("--min-date", default=DEFAULT_MIN_DATE, help="Only count IEs on/after this date (YYYY-MM-DD)")
    args = parser.parse_args()

    fieldnames, entities = load_entities()
    if "source" not in fieldnames:
        fieldnames = fieldnames + ["source"]

    existing_ids = {e["fec_id"] for e in entities}
    priority_candidates = [
        e for e in entities
        if e["entity_type"] == "candidate" and e.get("race_group") in PRIORITY_RACES
    ]
    logger.info("Checking IE activity against %d priority-race candidates since %s...",
                len(priority_candidates), args.min_date)

    client = FECClient()
    discovered = {}  # committee_id -> aggregate info

    for i, cand in enumerate(priority_candidates, 1):
        logger.info("[%d/%d] %s (%s)", i, len(priority_candidates), cand["name"], cand["fec_id"])
        try:
            records = client.schedule_e_by_target(cand["fec_id"], max_records=500, min_date=args.min_date)
        except Exception as exc:  # noqa: BLE001 -- keep going for the rest
            logger.error("Failed on %s: %s", cand["name"], redact(str(exc)))
            continue

        for rec in records:
            committee_id = rec.get("committee_id")
            if not committee_id:
                continue
            entry = discovered.setdefault(committee_id, {
                # The real committee name lives in a nested "committee" object
                # in Schedule E records, not a flat "committee_name" field --
                # same bug (and same fix) as fetch_data.py's schedule_e_event_fields,
                # confirmed against real data on 2026-07-21.
                "name": (rec.get("committee") or {}).get("name") or committee_id,
                "total": 0.0,
                "count": 0,
                "races": set(),
                "support": 0,
                "oppose": 0,
            })
            amount = rec.get("expenditure_amount") or 0
            try:
                entry["total"] += float(amount)
            except (TypeError, ValueError):
                pass
            entry["count"] += 1
            entry["races"].add(cand.get("race_group", ""))
            if rec.get("support_oppose_indicator") == "S":
                entry["support"] += 1
            elif rec.get("support_oppose_indicator") == "O":
                entry["oppose"] += 1

    already_tracked = {cid: info for cid, info in discovered.items() if cid in existing_ids}
    new_committees = {cid: info for cid, info in discovered.items() if cid not in existing_ids}

    print(f"\nFound {len(discovered)} distinct committees making IEs in your priority races since {args.min_date}:")
    print(f"  - {len(already_tracked)} already in entities.csv")
    print(f"  - {len(new_committees)} new\n")

    if new_committees:
        print(f"{'Committee':45} {'ID':12} {'Spent':>12} {'#':>4} {'S/O':>7}  Races")
        for cid, info in sorted(new_committees.items(), key=lambda x: -x[1]["total"]):
            races = ", ".join(sorted(r for r in info["races"] if r))
            print(f"{info['name'][:45]:45} {cid:12} {fmt_money(info['total']):>12} {info['count']:>4} "
                  f"{info['support']}S/{info['oppose']}O  {races}")

    if not new_committees:
        print("Nothing new to add.")
        return

    if args.dry_run:
        print("\n--dry-run set, entities.csv not modified.")
        return

    new_rows = []
    for cid, info in new_committees.items():
        new_rows.append({
            "fec_id": cid,
            "name": info["name"],
            "state": "",
            "district": "",
            "entity_type": "committee",
            "race_group": "",
            "tracking_tier": "watch",
            "source": "ie_discovery",
        })

    with open(ENTITIES_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        for row in new_rows:
            writer.writerow(row)

    print(f"\nAppended {len(new_rows)} new committees to entities.csv (tagged source=ie_discovery, tracking_tier=watch).")
    print("Run fetch_data.py next to pull their totals/filings for the first time.")


if __name__ == "__main__":
    main()
