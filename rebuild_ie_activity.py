"""
One-time fix-up: rebuild every 'outside_spending' / 'independent_expenditure'
entry in data/activity_log.jsonl from the data already sitting in your
snapshots, using the corrected Schedule E field mapping (see fetch_data.py's
schedule_e_event_fields -- the spender's real name lives in a nested
"committee" object, which the original code didn't know to look for).

Why this is needed: fetch_data.py only LOGS a "new" event once, the first
time it sees a given record. Every outside_spending/independent_expenditure
event already sitting in your activity log was written with the OLD, broken
field mapping (spender name silently fell back to the target candidate's own
name). Running fetch_data.py again won't fix those old entries -- they're
already "not new" as far as the diff logic is concerned. This script instead
throws out every old entry of those two types and regenerates them fresh
from your current snapshots (which already hold the raw, complete FEC data),
using each record's own expenditure_date as the event date -- not "when we
happened to run this script" -- so sort order reflects reality too.

Entries of other types (new_filing, large_contribution, new_disbursement)
are left untouched.

Safe to re-run any time; it always rebuilds these two event types from
whatever is in your snapshots right now, so it stays in sync after future
fetch_data.py runs pull in new records.

Usage:
    python3 rebuild_ie_activity.py
"""

import csv
import json
import sys
from pathlib import Path

from fetch_data import schedule_e_event_fields, ROOT, ENTITIES_CSV, SNAPSHOT_DIR, ACTIVITY_LOG

REBUILT_TYPES = {"outside_spending", "independent_expenditure"}


def load_entities():
    with open(ENTITIES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_snapshot(fec_id: str) -> dict:
    path = SNAPSHOT_DIR / f"{fec_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def build_event(entity: dict, event_type: str, rec: dict) -> dict:
    amount = rec.get("expenditure_amount")
    date = rec.get("expenditure_date")
    event = {
        "type": event_type,
        "fec_id": entity["fec_id"],
        "name": entity["name"],
        "race_group": entity.get("race_group") or "",
        "amount": amount,
        "date": date,
        "is_notice": rec.get("is_notice"),
        "form_type": rec.get("form_type"),
    }
    event.update(schedule_e_event_fields(rec, amount, date))
    # Use the real expenditure date as logged_at too, so this doesn't look
    # like it all happened "just now" if you ever inspect logged_at directly.
    event["logged_at"] = (date or "") + "T00:00:00+00:00" if date else ""
    event["rebuilt"] = True
    return event


def main():
    entities = load_entities()
    seen_sub_ids = set()
    new_events = []

    for entity in entities:
        snapshot = load_snapshot(entity["fec_id"])
        if not snapshot:
            continue

        if entity["entity_type"] == "candidate":
            for rec in snapshot.get("schedule_e_target", []):
                sub_id = rec.get("sub_id")
                if sub_id and sub_id in seen_sub_ids:
                    continue
                if sub_id:
                    seen_sub_ids.add(sub_id)
                new_events.append(build_event(entity, "outside_spending", rec))
        else:
            for rec in snapshot.get("schedule_e", []):
                sub_id = rec.get("sub_id")
                if sub_id and sub_id in seen_sub_ids:
                    continue
                if sub_id:
                    seen_sub_ids.add(sub_id)
                new_events.append(build_event(entity, "independent_expenditure", rec))

    # Keep every existing log line that ISN'T one of the two types we're
    # regenerating -- new_filing, large_contribution, new_disbursement, etc.
    kept = []
    removed_count = 0
    if ACTIVITY_LOG.exists():
        for line in ACTIVITY_LOG.read_text().strip().splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") in REBUILT_TYPES:
                removed_count += 1
            else:
                kept.append(ev)

    all_events = kept + new_events
    ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    ACTIVITY_LOG.write_text("\n".join(json.dumps(e, default=str) for e in all_events) + "\n")

    print(f"Removed {removed_count} old outside_spending/independent_expenditure entries.")
    print(f"Wrote {len(new_events)} rebuilt entries (deduplicated by sub_id across candidate/committee sources).")
    print(f"Kept {len(kept)} entries of other types untouched.")
    print(f"Total entries in {ACTIVITY_LOG}: {len(all_events)}")


if __name__ == "__main__":
    sys.exit(main())
