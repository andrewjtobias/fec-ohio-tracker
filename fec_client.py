"""
Thin wrapper around the OpenFEC API (https://api.open.fec.gov/developers/).

Handles: API key injection, pagination (both page-based and the seek-based
"last_index" style required by the /schedules/ endpoints), rate-limit backoff,
and basic retry on transient errors.

Get a free API key at https://api.open.fec.gov/developers/ (instant, no
approval wait) and set it as the FEC_API_KEY environment variable.
"""

import os
import time
import logging
from typing import Any, Optional

import requests

logger = logging.getLogger("fec_client")

BASE_URL = "https://api.open.fec.gov/v1"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 4


class FECClient:
    def __init__(self, api_key: Optional[str] = None, per_page: int = 100):
        self.api_key = api_key or os.environ.get("FEC_API_KEY") or "DEMO_KEY"
        if self.api_key == "DEMO_KEY":
            logger.warning(
                "Using DEMO_KEY, which is heavily rate-limited. "
                "Set FEC_API_KEY for real use."
            )
        self.per_page = per_page
        self.session = requests.Session()

    def _get(self, path: str, params: dict) -> dict:
        params = dict(params)
        params["api_key"] = self.api_key
        url = f"{BASE_URL}{path}"

        for attempt in range(1, MAX_RETRIES + 1):
            resp = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = min(2 ** attempt, 60)
                logger.warning("Rate limited (429). Waiting %ss...", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = min(2 ** attempt, 30)
                logger.warning(
                    "Server error %s on %s. Retrying in %ss...",
                    resp.status_code, path, wait,
                )
                time.sleep(wait)
                continue
            # 4xx other than 429: don't retry, surface the error body for debugging
            resp.raise_for_status()

        resp.raise_for_status()
        return {}

    def get(self, path: str, **params) -> dict:
        """Single-page GET. Returns the full JSON response (with 'results' and 'pagination')."""
        return self._get(path, params)

    def get_all_pages(self, path: str, max_pages: int = 5, **params) -> list[dict]:
        """
        Page-based pagination for endpoints like /candidate/{id}/filings/.
        Stops after max_pages to keep runs bounded; most of our lookups only
        need the first page (most-recent-first) anyway.
        """
        params.setdefault("per_page", self.per_page)
        params.setdefault("page", 1)
        results: list[dict] = []
        for page in range(1, max_pages + 1):
            params["page"] = page
            data = self._get(path, params)
            page_results = data.get("results", [])
            results.extend(page_results)
            pagination = data.get("pagination", {})
            total_pages = pagination.get("pages", 1)
            if page >= total_pages or not page_results:
                break
        return results

    def get_schedule_pages(
        self, path: str, max_records: int = 200, sort: str = "-contribution_receipt_date", **params
    ) -> list[dict]:
        """
        Seek-based pagination for /schedules/schedule_a/, /schedules/schedule_b/,
        /schedules/schedule_e/. These endpoints reject deep page-number offsets,
        so the API requires walking forward using the "indexes" object returned
        in each response's pagination block.

        That object (e.g. {"last_index": 123, "last_expenditure_date": "..."})
        needs to be unpacked into individual top-level query params on the next
        request -- NOT sent as a single "last_indexes" param. requests silently
        mangles a dict passed as a param value (it iterates over the dict's
        *keys*), producing a well-formed-looking but garbage request that the
        API rejects with 422. Bug history: an earlier version of this function
        did exactly that.
        """
        params.setdefault("per_page", min(self.per_page, 100))
        params["sort"] = sort
        results: list[dict] = []
        next_indexes: dict | None = None

        while len(results) < max_records:
            call_params = dict(params)
            if next_indexes:
                call_params.update(next_indexes)  # flatten: last_index=..., last_expenditure_date=..., etc.

            data = self._get(path, call_params)
            page_results = data.get("results", [])
            if not page_results:
                break
            results.extend(page_results)

            # A page shorter than what we asked for means there's nothing
            # left -- stop here instead of firing an extra (wasted) request
            # that would just come back empty. Saves real API budget on the
            # common case of a committee with fewer records than max_records.
            if len(page_results) < call_params["per_page"]:
                break

            pagination = data.get("pagination", {})
            next_indexes = pagination.get("last_indexes")
            if not next_indexes:
                break

        return results[:max_records]

    # --- Convenience wrappers -------------------------------------------------

    def candidate_totals(self, candidate_id: str, cycle: Optional[int] = None) -> list[dict]:
        """
        cycle: filter to a specific 2-year election cycle (e.g. 2026). Without
        it, the endpoint returns one row per cycle the candidate has EVER run
        in, and sort=-cycle is unreliable here -- confirmed against real data
        on 2026-07-21 that the "cycle" field on each returned row is null, so
        sorting by it does nothing, and a veteran candidate's oldest/largest
        prior cycle can come back first instead of the current one. Always
        pass cycle explicitly when you want "this election's" numbers.
        """
        params: dict = {"per_page": 10, "sort": "-cycle"}
        if cycle:
            params["cycle"] = cycle
        return self.get(f"/candidate/{candidate_id}/totals/", **params).get("results", [])

    def candidate_filings(self, candidate_id: str, per_page: int = 5) -> list[dict]:
        return self.get(
            f"/candidate/{candidate_id}/filings/", per_page=per_page, sort="-receipt_date"
        ).get("results", [])

    def committee_totals(self, committee_id: str, cycle: Optional[int] = None) -> list[dict]:
        """See candidate_totals -- same cycle-filtering caveat applies."""
        params: dict = {"per_page": 10, "sort": "-cycle"}
        if cycle:
            params["cycle"] = cycle
        return self.get(f"/committee/{committee_id}/totals/", **params).get("results", [])

    def committee_filings(self, committee_id: str, per_page: int = 5) -> list[dict]:
        return self.get(
            f"/committee/{committee_id}/filings/", per_page=per_page, sort="-receipt_date"
        ).get("results", [])

    def committee_filings_all(self, committee_id: str, cycle: Optional[int] = None, max_pages: int = 4) -> list[dict]:
        """
        All of a committee's filings for a cycle, sorted most-recent-period-first.
        Used to build a quarter-by-quarter breakdown -- each periodic filing
        (F3/F3X/F3P) already carries that period's raised/spent/cash-on-hand,
        so we don't need to compute anything, just read each filing's own numbers.
        """
        params: dict[str, Any] = {"sort": "-coverage_end_date"}
        if cycle:
            params["cycle"] = cycle
        return self.get_all_pages(f"/committee/{committee_id}/filings/", max_pages=max_pages, per_page=20, **params)

    def schedule_a_recent(self, committee_id: str, max_records: int = 50, min_amount: float = 0) -> list[dict]:
        """Recent itemized contributions received by a committee."""
        params: dict[str, Any] = {"committee_id": committee_id}
        if min_amount:
            params["min_amount"] = min_amount
        return self.get_schedule_pages(
            "/schedules/schedule_a/", max_records=max_records,
            sort="-contribution_receipt_date", **params
        )

    def schedule_a_top_donors(self, committee_id: str, min_date: str, max_date: str, limit: int = 20) -> list[dict]:
        """
        Largest individual itemized contributions to a committee within a
        date range (e.g. one reporting quarter), sorted by transaction size.
        Note: this ranks single contributions, not each donor's total given
        across multiple checks in the period -- someone who gave $1,000 four
        times shows as four separate $1,000 entries, not one $4,000 entry.
        A single page covers it since the API sorts server-side and we only
        want the top `limit`.
        """
        return self.get(
            "/schedules/schedule_a/", committee_id=committee_id,
            min_date=min_date, max_date=max_date,
            sort="-contribution_receipt_amount", per_page=limit,
        ).get("results", [])

    def schedule_b_recent(self, committee_id: str, max_records: int = 50) -> list[dict]:
        """Recent itemized disbursements made by a committee."""
        return self.get_schedule_pages(
            "/schedules/schedule_b/", max_records=max_records,
            sort="-disbursement_date", committee_id=committee_id
        )

    def schedule_e_recent(self, committee_id: str, max_records: int = 50) -> list[dict]:
        """Recent independent expenditures made by a committee (for/against a candidate)."""
        return self.get_schedule_pages(
            "/schedules/schedule_e/", max_records=max_records,
            sort="-expenditure_date", committee_id=committee_id
        )

    def schedule_e_by_target(self, candidate_id: str, max_records: int = 100, min_date: Optional[str] = None) -> list[dict]:
        """
        Independent expenditures made FOR OR AGAINST a given candidate, by
        ANY committee/PAC -- this is what lets you discover outside spending
        without already knowing which PAC is behind it. Each result includes
        the spending committee's own name/ID, support_oppose_indicator, and
        the amount, so a brand-new PAC that starts running ads shows up here
        automatically the first time it files.

        min_date (YYYY-MM-DD) scopes the pull to a date range -- e.g. the
        start of the current election cycle, for a one-time historical
        backfill rather than just catching new activity going forward.
        """
        params = {"candidate_id": candidate_id}
        if min_date:
            params["min_date"] = min_date
        return self.get_schedule_pages(
            "/schedules/schedule_e/", max_records=max_records,
            sort="-expenditure_date", **params
        )
