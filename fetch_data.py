"""
Pull the latest FEC data for every tracked entity in entities.csv, save a
dated snapshot, and log anything new (filings, big contributions, independent
expenditures) since the last run to data/activity_log.jsonl.

Usage:
    FEC_API_KEY=xxxx python fetch_data.py
    FEC_API_KEY=xxxx python fetch_data.py --skip-schedules   # totals/filings only, faster
    FEC_API_KEY=xxxx python fetch_data.py --big-donor-threshold 2000

Designed to be run on a schedule (see .github/workflows/update.yml). Every
run is incremental: it only *reports* new records compared to the previous
snapshot. That diffing itself is free, but don't mistake that for the whole
run being cheap -- a full run against all ~100 tracked entities makes several
hundred API calls (schedules + quarterly detail add up fast) and can eat a
meaningful chunk of the 1,000-calls/hour budget. Rule of thumb: at most one
full run per hour if you're testing manually; the scheduled GitHub Actions
job already respects this (every 6 hours). Use --skip-schedules and
--skip-quarterly for a cheap "just check totals and new filings" run you can
repeat more often.

For candidates in the priority races (see PRIORITY_RACES below), this also
pulls independent expenditures made FOR OR AGAINST them by ANY committee --
so a new PAC running ads for/against one of your tracked candidates shows up
in the activity log automatically, with its own name attached, the first
time it appears in FEC data. You don't need to know a PAC's name in advance.
"""

import argparse
import csv
import json
import logging
import re
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fec_client import FECClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fetch_data")

ROOT = Path(__file__).parent
ENTITIES_CSV = ROOT / "entities.csv"
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
ACTIVITY_LOG = DATA_DIR / "activity_log.jsonl"

PRIORITY_RACES = {"OH Senate", "OH-01", "OH-07", "OH-09", "OH-10", "OH-13", "OH-15"}
CURRENT_CYCLE = 2026  # FEC's even-year label for the 2025-2026 cycle
CYCLE_START_DATE = "2025-01-01"  # first day of the 2025-2026 cycle


def is_current_cycle_ohio_ie(rec: dict) -> bool:
    """
    Keep only Schedule E records that belong to THIS cycle and an OHIO race.
    Two distinct kinds of junk get through an unfiltered schedule_e_by_target
    pull, both confirmed against real snapshot data on 2026-07-28:

    1. Prior-cycle records. The endpoint returns a candidate's ENTIRE IE
       history sorted newest-first, so a candidate with little 2026 activity
       gets their 100-record window filled by old cycles (Turner's went back
       to 2002; Carey's and Max Miller's were entirely 2021-2024). 734 of
       995 outside_spending events ($16.2M of $28.6M) in the log were
       pre-2025. Worse, FEC reprocessing gives old records fresh sub_ids,
       so the diff kept logging 2022/2024 expenditures as "new" activity.

    2. FEC mis-attributions. A record can carry a tracked candidate's ID
       but actually belong to a different race -- e.g. two HOUSE FREEDOM
       FUND records with Max Miller's ID (H2OH16051) but candidate_name
       "MILLER, MARY", candidate_office_state IL, district 15 (that's Mary
       Miller of Illinois). The record's own candidate_office_state is the
       reliable tell: every legitimate record in our data says "OH".

    Records missing either field are dropped too -- as of 2026-07-28 no
    legitimate record lacks them, and a record we can't place in a cycle or
    a state can't be trusted in Ohio-race totals. If a real Ohio record
    ever shows up without them, loosen this deliberately, don't guess.
    """
    date = (rec.get("expenditure_date") or "")[:10]
    return date >= CYCLE_START_DATE and rec.get("candidate_office_state") == "OH"


def is_national_pac(entity: dict) -> bool:
    """
    True for a tracked committee whose OWN FEC filings cover its activity
    nationwide and therefore can't be assumed relevant to an Ohio race.

    A watch-tier committee that isn't an Ohio-registered entity is
    presumptively a national PAC we track only because it made one
    Ohio-relevant expenditure. Its filings (F24 24-hour IE reports, F3X
    periodic reports, etc.) report its spending everywhere, and the filing
    record carries no candidate/race field to filter on -- so a filing about
    a Missouri or Nevada race looks identical to an Ohio one. Two ways a
    committee lands here:
      - source == "ie_discovery": found by discover_ie_committees.py, which
        never populates `state` for those rows.
      - state != "OH": added manually but registered elsewhere, e.g. DEFEND
        AMERICAN JOBS (VA), AMERICAN MISSION (NV), AMERICAN CONSERVATIVE
        FUND (VA).
    in_cycle committees are exempt: those are candidates' own principal or
    joint fundraising committees (or a deliberately promoted exception), so
    their filings are inherently relevant whatever their registered state.

    This lives in ONE place and is imported by build_dashboard.py rather
    than being reimplemented there. Duplicated extraction logic is how the
    nested-committee-name bug survived in two files at once; don't repeat
    that pattern.
    """
    return entity.get("tracking_tier") != "in_cycle" and (
        entity.get("source") == "ie_discovery" or entity.get("state") != "OH"
    )


_API_KEY_RE = re.compile(r"api_key=[^&\s'\")]+")


def redact(text: str) -> str:
    """Strip api_key values out of error strings before they get logged or
    written to a snapshot file that may end up committed to a repo."""
    return _API_KEY_RE.sub("api_key=REDACTED", text)


def load_entities():
    with open(ENTITIES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_prev_snapshot(fec_id: str) -> dict:
    path = SNAPSHOT_DIR / f"{fec_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            logger.warning("Corrupt snapshot for %s, ignoring", fec_id)
    return {}


def save_snapshot(fec_id: str, snapshot: dict):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{fec_id}.json"
    path.write_text(json.dumps(snapshot, indent=2, default=str))


def log_event(event: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    event["logged_at"] = datetime.now(timezone.utc).isoformat()
    with open(ACTIVITY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def diff_filings(entity, prev_filings, new_filings):
    prev_ids = {f.get("beginning_image_number") or f.get("document_description") for f in prev_filings}
    for filing in new_filings:
        key = filing.get("beginning_image_number") or filing.get("document_description")
        if key and key not in prev_ids:
            log_event({
                "type": "new_filing",
                "fec_id": entity["fec_id"],
                "name": entity["name"],
                "race_group": entity["race_group"],
                "form_type": filing.get("form_type"),
                "receipt_date": filing.get("receipt_date"),
                "document_description": filing.get("document_description"),
                "pdf_url": filing.get("pdf_url"),
            })


def schedule_e_dedup_key(rec: dict, amount, date) -> str:
    """
    A best-effort fingerprint for 'this looks like the same real-world
    expenditure reported more than once' -- same spending committee, same
    target candidate, same dollar amount, same date, same vendor paid.

    This is NOT certain proof of a duplicate: a committee genuinely can
    place two different $2,500 buys with the same vendor on the same day
    (confirmed against real data -- see debug_schedule_e.py output from
    2026-07-21, two AFP Action records with identical amount/date/payee but
    distinct sub_id/transaction_id/image_number). So this key is used only
    to *flag* likely duplicates for manual review, never to silently drop
    a record -- dropping real spending because of a coincidental match
    would misrepresent actual money in the race.
    """
    return "|".join(str(x) for x in (
        rec.get("committee_id"), rec.get("candidate_id"), amount, date, rec.get("payee_name"),
    ))


def dedupe_ie_for_totals(records: list[dict]) -> tuple[list[dict], int, float]:
    """
    Collapse duplicate REPORTS of the same real-world expenditure before
    summing dollars. Returns (kept_records, n_excluded, dollars_excluded).

    Verified against real snapshot data on 2026-07-28: the dominant source
    of double-counting is NOT amendments but the FEC's normal two-step
    disclosure flow -- a committee files a 24/48-hour notice (is_notice=True)
    when it spends, then re-reports the identical expenditure in its periodic
    report (is_notice=False). The API returns BOTH. In our data the pairs
    almost always share a transaction_id (e.g. WIN IT BACK PAC's $250,000
    anti-Husted buy: txn SE24.2961 appears once as a notice, once as a
    report). Left alone, this inflated IE totals by ~$2.8M of $14.2M (20%).

    Rules, applied within each dedup_key group (same committee, candidate,
    amount, date, payee):
      1. If any periodic-report (non-notice) version exists, drop the
         notice versions -- the report is the authoritative restatement.
         (Rare edge: two genuinely separate identical buys where only one
         has hit a report yet would briefly count as one. Accepted cost;
         self-corrects when the next periodic report arrives.)
      2. Among what's left, records sharing a transaction_id are the same
         expenditure re-filed (amendment chains, e.g. AMERICANS UNITED FOR
         VALUES txn SE.4561 filed N then A/1) -- keep only the highest
         file_number (latest version).
      3. Distinct transaction_ids that survive are treated as genuinely
         separate buys and ALL kept -- confirmed real: AFP Action placed
         three separate $2,500 same-day/same-payee buys (txns SE24.38302/
         38304/38305 in one filing), and that's real spending.

    This is used for AGGREGATE TOTALS only. The activity feed still shows
    every record with a "possible duplicate" flag -- never silently drops.
    """
    groups: dict[str, list[dict]] = {}
    for rec in records:
        key = schedule_e_dedup_key(rec, rec.get("expenditure_amount"), rec.get("expenditure_date"))
        groups.setdefault(key, []).append(rec)

    kept: list[dict] = []
    n_excluded = 0
    dollars_excluded = 0.0
    for recs in groups.values():
        if len(recs) == 1:
            kept.extend(recs)
            continue
        chosen = recs
        non_notice = [r for r in chosen if not r.get("is_notice")]
        if non_notice:
            chosen = non_notice  # rule 1
        by_txn: dict[str, dict] = {}
        no_txn: list[dict] = []
        for r in chosen:
            txn = r.get("transaction_id")
            if not txn:
                no_txn.append(r)  # can't chain-match without a txn id; keep
                continue
            cur = by_txn.get(txn)
            if cur is None or (r.get("file_number") or 0) > (cur.get("file_number") or 0):
                by_txn[txn] = r  # rule 2
        final = list(by_txn.values()) + no_txn
        kept.extend(final)
        n_excluded += len(recs) - len(final)
        dollars_excluded += sum(
            (r.get("expenditure_amount") or 0) for r in recs if not any(r is f for f in final)
        )
    return kept, n_excluded, dollars_excluded


def schedule_e_event_fields(rec: dict, amount, date) -> dict:
    """
    Field extraction specific to Schedule E (independent expenditure)
    records -- these have a different shape than Schedule A/B records, most
    importantly: the spending committee's NAME lives in a nested "committee"
    object (rec["committee"]["name"]), not a flat "committee_name" field.
    Confirmed against a real raw API record via debug_schedule_e.py on
    2026-07-21 -- the flat field genuinely doesn't exist, which is why the
    old code silently fell through to the candidate's own name instead.
    """
    committee = rec.get("committee") or {}
    return {
        "spender_name": committee.get("name") or rec.get("committee_id") or "Unknown spender",
        "spender_committee_id": rec.get("committee_id"),
        # The record's own candidate_name/candidate_id is the actual target
        # of the expenditure. For "outside_spending" events this duplicates
        # the entity we already logged against (a candidate). For
        # "independent_expenditure" events (logged against the SPENDING
        # committee, not a candidate) this is the only place the target
        # shows up -- without it, a PAC's own spending history has no record
        # of who it targeted.
        "target_name": rec.get("candidate_name"),
        "target_id": rec.get("candidate_id"),
        "payee_name": rec.get("payee_name"),
        "purpose": rec.get("expenditure_description"),
        "pdf_url": rec.get("pdf_url"),
        "support_oppose": rec.get("support_oppose_indicator"),
        "dedup_key": schedule_e_dedup_key(rec, amount, date),
    }


def diff_schedule(entity, event_type, prev_records, new_records, id_field, amount_field=None, threshold=0,
                   record_kind=None):
    prev_ids = {r.get(id_field) for r in prev_records}
    for rec in new_records:
        rec_id = rec.get(id_field)
        if rec_id and rec_id not in prev_ids:
            amount = rec.get(amount_field) if amount_field else None
            if amount_field and threshold and (amount is None or amount < threshold):
                continue
            date = rec.get("contribution_receipt_date") or rec.get("disbursement_date") or rec.get("expenditure_date")
            event = {
                "type": event_type,
                "fec_id": entity["fec_id"],
                "name": entity["name"],
                "race_group": entity["race_group"],
                "amount": amount,
                "date": date,
                # Only meaningful for schedule_e records: whether this came in
                # via a fast 24/48-hour notice (near-real-time) vs a regular
                # periodic report (can lag months). None for other schedules.
                "is_notice": rec.get("is_notice"),
                "form_type": rec.get("form_type"),
            }
            if record_kind == "schedule_e":
                event.update(schedule_e_event_fields(rec, amount, date))
            else:
                event["contributor_or_payee"] = (
                    rec.get("contributor_name") or rec.get("recipient_name")
                    or rec.get("committee_name") or rec.get("candidate_name")
                )
                event["detail"] = (
                    rec.get("recipient_committee_name") or rec.get("support_oppose_indicator")
                    or rec.get("disbursement_description") or rec.get("purpose")
                )
            log_event(event)


def fetch_quarterly_detail(client: FECClient, committee_id: str, prev_quarterly: Optional[list[dict]] = None,
                            donor_cap: int = 20) -> list[dict]:
    """
    Quarter-by-quarter (really: filing-period-by-filing-period) breakdown for
    one committee this cycle, each with its top donors by contribution size.

    Rather than bucketing into calendar quarters ourselves, this reads each
    periodic filing's own reported period (coverage_start_date/coverage_end_date)
    and numbers -- that matches what the campaign actually disclosed, and
    handles both quarterly and monthly filers correctly. Donor lookups use
    that exact same date range so the two line up.

    A CLOSED period's top-donor list can't change -- once a quarter's filing
    deadline passes, the FEC data for it is final. So donor lookups are only
    made for a period the first time we see it, or for the single
    most-recent period (which can still gain amendments/late-reported
    contributions before the *next* filing closes it out). Every other
    period reuses the top-donor list from the previous snapshot instead of
    re-querying the API for the same historical data every run. Financial
    totals (raised/spent/cash-on-hand) are still rebuilt fresh every run from
    the filing data itself, which costs nothing extra -- only the itemized
    per-quarter donor calls are skipped for settled periods. This is what
    keeps a steady-state run to ~1 donor call per in-cycle candidate instead
    of one per historical quarter.

    NOTE: the "transfers" field name (transfers_from_affiliated_committee) is
    my best-documented guess for that line item -- if it comes back empty/None
    for a committee that you know received transfers, send me a sample filing
    (the raw JSON) and I'll fix the field mapping.
    """
    prev_by_period = {
        (q["coverage_start"], q["coverage_end"]): q
        for q in (prev_quarterly or [])
        if q.get("coverage_start") and q.get("coverage_end")
    }

    filings = client.committee_filings_all(committee_id, cycle=CURRENT_CYCLE, max_pages=4)

    # A committee can file TWO separate documents covering the identical
    # period: Form 3 (the actual periodic financial report -- real
    # raised/spent/cash totals) and Form 3L (bundled-lobbyist-contribution
    # disclosure, a different required form that never carries those totals
    # at all -- they're genuinely null on that form, not unprocessed).
    # Confirmed against real data for C00916288 on 2026-07-21: every "blank"
    # quarter in the dashboard was a Form 3L that won the same-period dedup
    # race purely by chance of API return order, hiding the real Form 3.
    # Sort so Form 3 always wins ties, while preserving descending-by-date
    # order overall so index 0 below still lands on the true most-recent
    # period.
    filings = sorted(
        filings,
        key=lambda f: (f.get("coverage_end_date") or "", f.get("form_type") == "F3"),
        reverse=True,
    )

    periods = []
    seen = set()
    for f in filings:
        start, end = f.get("coverage_start_date"), f.get("coverage_end_date")
        if not start or not end:
            continue  # skip filings without a clear period (e.g. standalone 24/48hr notices)
        key = (start[:10], end[:10])
        if key in seen:
            continue  # keep only the first (Form 3 preferred, most-recent) filing per period
        seen.add(key)
        periods.append(f)

    quarterly = []
    for i, f in enumerate(periods):
        # periods is sorted most-recent-period-first (matches committee_filings_all's
        # -coverage_end_date sort), so index 0 is the only one still "open."
        is_most_recent = (i == 0)
        start, end = f["coverage_start_date"][:10], f["coverage_end_date"][:10]
        cached = prev_by_period.get((start, end))

        # FEC filings carry both a terse code ("report_type", e.g. "12P",
        # "YE", "Q2S") and a plain-English version ("report_type_full", e.g.
        # "12-DAY PRE-PRIMARY", "YEAR-END") -- prefer the readable one and
        # append the year so multi-year history doesn't collapse into
        # ambiguous repeats (e.g. two different "Year-End" rows).
        report_type_full = f.get("report_type_full")
        report_type = f.get("report_type")
        year = end[:4] if end else ""
        if report_type_full:
            label = f"{report_type_full.title()} {year}".strip()
        elif report_type:
            label = f"{report_type} {year}".strip()
        else:
            label = f"{start} to {end}"

        entry = {
            "label": label,
            "coverage_start": start,
            "coverage_end": end,
            "raised": f.get("total_receipts_period") or f.get("total_receipts"),
            "spent": f.get("total_disbursements_period") or f.get("total_disbursements"),
            "transfers": f.get("transfers_from_affiliated_committee") or f.get("other_receipts_period"),
            "cash_on_hand": f.get("cash_on_hand_end_period"),
        }

        if cached and cached.get("top_donors") and not is_most_recent:
            entry["top_donors"] = cached["top_donors"]
        else:
            try:
                donors = client.schedule_a_top_donors(committee_id, min_date=start, max_date=end, limit=donor_cap)
                entry["top_donors"] = [
                    {
                        "name": d.get("contributor_name"),
                        "amount": d.get("contribution_receipt_amount"),
                        "date": d.get("contribution_receipt_date"),
                        "employer": d.get("contributor_employer"),
                        "city": d.get("contributor_city"),
                        "state": d.get("contributor_state"),
                    }
                    for d in donors
                ]
            except Exception as exc:  # noqa: BLE001 -- one bad quarter shouldn't kill the rest
                logger.warning("Top donors lookup failed for %s (%s): %s", committee_id, entry["label"], redact(str(exc)))
                # Fall back to whatever was cached for this period rather than
                # losing it entirely because of a transient timeout/429.
                entry["top_donors"] = cached.get("top_donors", []) if cached else []

        quarterly.append(entry)

    return quarterly


def fetch_entity(client: FECClient, entity: dict, skip_schedules: bool, big_donor_threshold: float,
                  skip_quarterly: bool = False, donor_cap: int = 20, tracked_candidate_ids: set | None = None):
    fec_id = entity["fec_id"]
    entity_type = entity["entity_type"]
    prev = load_prev_snapshot(fec_id)
    has_prev = bool(prev)  # False on the very first run -- nothing to diff against yet
    snapshot = {"fec_id": fec_id, "name": entity["name"], "fetched_at": datetime.now(timezone.utc).isoformat()}

    errors = []

    def safe(step_name, fn):
        """Run one sub-fetch in isolation -- a failure here (e.g. a rate
        limit or a bad page) shouldn't prevent the other sub-fetches for this
        same entity from running. Each failure is recorded, not fatal."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            safe_msg = redact(str(exc))
            logger.error("  [%s] %s (%s): %s", step_name, entity["name"], fec_id, safe_msg)
            errors.append({"step": step_name, "error": safe_msg})
            return None

    if entity_type == "candidate":
        totals = safe("totals", lambda: client.candidate_totals(fec_id, cycle=CURRENT_CYCLE))
        if totals is not None:
            snapshot["totals"] = totals
        filings = safe("filings", lambda: client.candidate_filings(fec_id))
        if filings is not None:
            snapshot["filings"] = filings
            if has_prev:
                diff_filings(entity, prev.get("filings", []), filings)

        # Outside spending FOR/AGAINST this candidate, from any PAC -- this is
        # what lets us catch a new PAC's activity without already knowing its
        # name. Restricted to in_cycle candidates: this single check is the
        # single biggest driver of API call volume (it ran for every
        # candidate in a priority race, ~40+ people, regardless of whether
        # they're the featured matchup). "Watch" tier candidates still get
        # their totals/filings checked every run -- they just don't get the
        # expensive per-candidate outside-spending sweep. Run
        # discover_ie_committees.py periodically to backfill anything that
        # slips through for a watch-tier candidate.
        if not skip_schedules and entity.get("tracking_tier") == "in_cycle":
            # min_date scopes the pull to this cycle server-side, which also
            # frees the record window for current spending instead of letting
            # a candidate's decades of IE history crowd it out.
            # max_records=500: at 100, a hot race's window overflows during
            # peak season (Husted already had 79 current-cycle records in
            # July 2026) and older CURRENT-cycle records silently slide out
            # of the snapshot -- which would quietly shrink the dashboard's
            # aggregate IE totals right when they matter most. 500 costs at
            # most 4 extra API calls per in-cycle candidate per run.
            schedule_e_target = safe(
                "schedule_e_target",
                lambda: client.schedule_e_by_target(fec_id, max_records=500, min_date=CYCLE_START_DATE),
            )
            if schedule_e_target is not None:
                # Re-check client-side too: min_date doesn't catch FEC
                # mis-attributions (records carrying our candidate's ID but
                # belonging to another state's race -- see
                # is_current_cycle_ohio_ie for the Mary Miller case).
                schedule_e_target = [r for r in schedule_e_target if is_current_cycle_ohio_ie(r)]
                snapshot["schedule_e_target"] = schedule_e_target
                if has_prev:
                    diff_schedule(
                        entity, "outside_spending", prev.get("schedule_e_target", []), schedule_e_target,
                        id_field="sub_id", amount_field="expenditure_amount", record_kind="schedule_e",
                    )

        # Quarter-by-quarter raised/spent/transfers/cash-on-hand + top donors
        # per quarter, for in-cycle candidates with a known principal
        # committee. Skipped for "watch" tier candidates to keep run size
        # reasonable -- see entities.csv's linked_committee column.
        linked_committee = entity.get("linked_committee")
        if not skip_quarterly and entity.get("tracking_tier") == "in_cycle" and linked_committee:
            prev_quarterly = prev.get("quarterly", [])
            quarterly = safe(
                "quarterly",
                lambda: fetch_quarterly_detail(
                    client, linked_committee, prev_quarterly=prev_quarterly, donor_cap=donor_cap
                ),
            )
            if quarterly is not None:
                snapshot["quarterly"] = quarterly
    else:  # committee
        totals = safe("totals", lambda: client.committee_totals(fec_id, cycle=CURRENT_CYCLE))
        if totals is not None:
            snapshot["totals"] = totals
        filings = safe("filings", lambda: client.committee_filings(fec_id))
        if filings is not None:
            snapshot["filings"] = filings
            # Skip generic filing diffs for national PACs -- see
            # is_national_pac() for why their filings can't be assumed
            # Ohio-relevant. build_dashboard.py applies the SAME rule at
            # render time, so events logged before this guard existed stop
            # appearing without needing the log rewritten.
            if has_prev and not is_national_pac(entity):
                diff_filings(entity, prev.get("filings", []), filings)

        # Itemized Schedule A/B/E pulls (donor-level detail) are the other
        # big cost driver -- restricted to in_cycle committees for the same
        # reason as above. Watch-tier committees (the ~45 other PACs on your
        # list) still get totals/filings checked every run.
        if not skip_schedules and entity.get("tracking_tier") == "in_cycle":
            schedule_a = safe("schedule_a", lambda: client.schedule_a_recent(fec_id, max_records=50))
            if schedule_a is not None:
                snapshot["schedule_a"] = schedule_a
                if has_prev:
                    diff_schedule(
                        entity, "large_contribution", prev.get("schedule_a", []), schedule_a,
                        id_field="sub_id", amount_field="contribution_receipt_amount",
                        threshold=big_donor_threshold,
                    )

            schedule_b = safe("schedule_b", lambda: client.schedule_b_recent(fec_id, max_records=50))
            if schedule_b is not None:
                snapshot["schedule_b"] = schedule_b
                if has_prev:
                    diff_schedule(
                        entity, "new_disbursement", prev.get("schedule_b", []), schedule_b,
                        id_field="sub_id",
                    )

            schedule_e = safe("schedule_e", lambda: client.schedule_e_recent(fec_id, max_records=50))
            if schedule_e is not None:
                # A committee's OWN recent Schedule E filings cover spending
                # for/against ANY candidate nationwide -- unlike
                # schedule_e_by_target (queried from the candidate side),
                # this isn't scoped to our tracked races at all. Filter to
                # only the candidates we're actually tracking (i.e. Ohio
                # races) so a national PAC's out-of-state spending doesn't
                # show up here. Only matters if/when a watch-tier IE
                # committee gets promoted to in_cycle -- currently none are.
                if tracked_candidate_ids is not None:
                    schedule_e = [r for r in schedule_e if r.get("candidate_id") in tracked_candidate_ids]
                # Same cycle + Ohio-race guard as the candidate-side pull: a
                # record can carry a tracked candidate's ID and still be a
                # prior-cycle expenditure (or an FEC mis-attribution to
                # another state's race). See is_current_cycle_ohio_ie.
                schedule_e = [r for r in schedule_e if is_current_cycle_ohio_ie(r)]
                snapshot["schedule_e"] = schedule_e
                if has_prev:
                    diff_schedule(
                        entity, "independent_expenditure", prev.get("schedule_e", []), schedule_e,
                        id_field="sub_id", amount_field="expenditure_amount", record_kind="schedule_e",
                    )

    if errors:
        snapshot["errors"] = errors

    save_snapshot(fec_id, snapshot)
    return snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-schedules", action="store_true",
                         help="Skip Schedule A/B/E pulls (faster, totals + filings only)")
    parser.add_argument("--big-donor-threshold", type=float, default=1000,
                         help="Only log new contributions at/above this amount (default: $1000)")
    parser.add_argument("--only", type=str, default=None,
                         help="Comma-separated fec_ids to run, for testing a subset")
    parser.add_argument("--skip-quarterly", action="store_true",
                         help="Skip the per-quarter raised/spent/transfers/cash-on-hand + top-donor pull for in-cycle candidates")
    parser.add_argument("--donor-cap", type=int, default=20,
                         help="Max top donors to pull per quarter per in-cycle candidate (default: 20)")
    parser.add_argument("--delay", type=float, default=0.3,
                         help="Seconds to pause between entities (default: 0.3) -- a courtesy throttle "
                              "so we don't burst into the API's rate limiter and trigger reactive "
                              "backoff-and-retry, which wastes more calls than it saves")
    args = parser.parse_args()

    entities = load_entities()
    tracked_candidate_ids = {e["fec_id"] for e in entities if e["entity_type"] == "candidate"}
    if args.only:
        wanted = set(args.only.split(","))
        entities = [e for e in entities if e["fec_id"] in wanted]

    logger.info("Fetching %d entities (~%.0fs minimum runtime from throttling alone)...",
                len(entities), len(entities) * args.delay)
    client = FECClient()

    error_count = 0
    for i, entity in enumerate(entities, 1):
        logger.info("[%d/%d] %s (%s)", i, len(entities), entity["name"], entity["fec_id"])
        snapshot = fetch_entity(client, entity, args.skip_schedules, args.big_donor_threshold,
                                 skip_quarterly=args.skip_quarterly, donor_cap=args.donor_cap,
                                 tracked_candidate_ids=tracked_candidate_ids)
        if snapshot.get("errors"):
            error_count += 1
        if args.delay:
            time.sleep(args.delay)

    logger.info("Done. %d/%d entities had at least one failed sub-fetch (see per-entity 'errors' in "
                "their snapshot JSON for detail). Snapshots in %s, activity log at %s",
                error_count, len(entities), SNAPSHOT_DIR, ACTIVITY_LOG)


if __name__ == "__main__":
    sys.exit(main())
