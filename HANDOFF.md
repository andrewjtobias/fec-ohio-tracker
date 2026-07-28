# FEC Ohio Tracker — Project Handoff

Written 2026-07-23 to brief a new model/session on this project's state, history, and hard-won lessons. This is a working campaign-finance dashboard, not a prototype — treat existing behavior as intentional unless a comment says otherwise.

## What this is

A static dashboard tracking FEC campaign finance data for Ohio's 2026 federal races (Senate + House), built from the OpenFEC API. Python scripts fetch data, diff it against the previous snapshot to log new events, and generate a single static `dashboard.html`. Runs automatically every 6 hours via GitHub Actions and deploys to GitHub Pages at `andrewjtobias.github.io/fec-ohio-tracker`.

Repo (source of truth): `/Users/andrewtobias/Documents/GitHub/fec-ohio-tracker`, cloned via GitHub Desktop, remote at `github.com/andrewjtobias/fec-ohio-tracker`.

There is also a separate folder at `/Users/andrewtobias/Downloads/fec-ohio-tracker` — that was the original working folder before the project was pushed to GitHub. **It is now stale and disconnected from git.** All active work happens in the `Documents/GitHub` copy. Don't edit the Downloads copy by mistake.

## Architecture

- **`entities.csv`** — the master list of everything tracked: candidates and committees, with columns `fec_id, name, state, district, entity_type, race_group, tracking_tier, source, linked_committee`.
  - `tracking_tier`: `in_cycle` (the ~15-20 featured matchups, get full itemized detail every run) vs `watch` (everyone else in a priority race, gets totals/filings only — cheaper).
  - `source`: `manual` (hand-curated) vs `ie_discovery` (auto-found by `discover_ie_committees.py`).
  - `linked_committee`: maps an in-cycle candidate to their principal campaign committee, used for quarterly detail.
- **`fec_client.py`** — thin OpenFEC API wrapper. Handles pagination (page-based and the schedules' seek-based `last_indexes` style), 429/5xx retry with backoff, and API key injection. Has docstring-documented gotchas (see "Hard-won API lessons" below).
- **`fetch_data.py`** — the main fetch script. For each entity in `entities.csv`, pulls totals/filings (everyone) plus itemized Schedule A/B/E and quarterly detail (in_cycle only), diffs against the last snapshot, and logs new events to `data/activity_log.jsonl`. Saves a fresh snapshot per entity to `data/snapshots/{fec_id}.json`.
- **`build_dashboard.py`** — reads all snapshots + the activity log, generates `dashboard.html` (single self-contained file, dark theme, vanilla JS for the interactive activity table — no build step, no dependencies).
- **`discover_ie_committees.py`** — one-time/occasional backfill: finds PACs making independent expenditures in tracked races that aren't in `entities.csv` yet, adds them as `watch` tier / `source=ie_discovery`.
- **`rebuild_ie_activity.py`** — one-time script (already run) that regenerated all IE-related activity log entries from snapshots after a field-mapping bug was fixed. Probably not needed again unless another historical rebuild is required.
- **`debug_filings.py`, `debug_schedule_e.py`** — diagnostic one-offs used to inspect raw API responses while chasing bugs. Safe to delete, not required.
- **`.github/workflows/update.yml`** — runs `fetch_data.py` + `build_dashboard.py` every 6 hours (`cron: "0 */6 * * *"`) and on manual trigger, commits changes back to `main`, deploys `dashboard.html` to Pages. `FEC_API_KEY` is read from an encrypted repo Secret (Settings → Secrets and variables → Actions) — never appears in any committed file.
- **`.gitignore`** — excludes `__pycache__/`, `.DS_Store`, `.env`.

## Hard-won API lessons (don't rediscover these)

1. **Schedule E spender name is nested.** The real spending committee's name is `rec["committee"]["name"]`, not a flat `committee_name` field. Getting this wrong silently falls back to the candidate's own name — this bug existed in *two* places independently (`fetch_data.py` and `discover_ie_committees.py`) because each had its own copy of the extraction logic. If you add a third place that touches Schedule E records, check for this.
2. **`candidate_totals`/`committee_totals` cycle field is unreliable.** The `cycle` field on returned rows is null in practice, so `sort=-cycle` does nothing — a veteran candidate's oldest/largest historical cycle can rank first. Always pass `cycle=` as an explicit filter param (both `fec_client.py` methods do this), and `build_dashboard.py`'s `latest_total()` has a client-side fallback that self-corrects even already-fetched data.
3. **Form 3 vs Form 3L.** A committee can file two documents covering the identical period: Form 3 (real financial totals) and Form 3L (bundled-lobbyist-contribution disclosure — totals always null by design). When picking "the" filing for a period, prefer `form_type == "F3"` or you'll get blank totals for periods that actually have data.
4. **Seek-based pagination for `/schedules/*` endpoints.** These reject deep page-number offsets. The `last_indexes` object from the response's pagination block must be unpacked into individual top-level params on the next request (`last_index=...`, not a dict passed as one param — `requests` will silently mangle a dict param by iterating its keys, producing a garbage 422).
5. **Duplicate-looking Schedule E records aren't necessarily duplicates.** Two records can have identical committee/candidate/amount/date/payee but distinct `sub_id`/`image_number` — confirmed against real AFP Action data. The dashboard flags likely duplicates (`dedup_key` = committee+candidate+amount+date+payee) for manual review via a badge; it never silently drops them.
6. **`docquery.fec.gov` is reachable from sandboxes that can't reach `api.open.fec.gov`.** Useful for verifying real filing documents directly if the API itself is unreachable in a given environment.

## The recurring bug this session: non-Ohio IE noise (now fixed, twice)

**Root cause:** any committee tracked at `watch` tier gets its generic FEC filings checked every run (`committee_filings()` → `diff_filings()` → a `new_filing` event). For a candidate's own campaign committee, that's always relevant. But for a third-party PAC — especially ones only added because they made *one* Ohio-relevant expenditure — their filings (F24 24-hour IE reports, etc.) cover their spending **nationwide**, and the filing record itself carries no candidate/race info to filter on. Result: PACs like WIN IT BACK PAC (spent on Husted in OH, but also nationally) leaked out-of-state filings (a Missouri Taylor Burks race) into "Recent Activity."

**Fix, in two rounds** (in `fetch_data.py`, committee branch):
- Round 1 caught `source == "ie_discovery"` committees only.
- Round 2 widened it after discovering manually-added national PACs (e.g. DEFEND AMERICAN JOBS, registered in VA) had the identical problem. Final rule:
  ```python
  is_national_pac = entity.get("tracking_tier") != "in_cycle" and (
      entity.get("source") == "ie_discovery" or entity.get("state") != "OH"
  )
  if has_prev and not is_national_pac:
      diff_filings(entity, prev.get("filings", []), filings)
  ```
- Also added a parallel guard on the committee-side `schedule_e_recent()` path (independent of the above), filtering results to `tracked_candidate_ids` before diffing — this doesn't currently do anything (no watch-tier IE committee is `in_cycle` yet) but closes the same hole if one gets promoted later.
- Historical bad entries were purged from `data/activity_log.jsonl` by filtering out `new_filing` events whose `fec_id` matches the national-PAC criteria above.

**If more non-Ohio noise shows up**, the pattern to check: is there a new committee type or event path that surfaces a committee's own general activity (not scoped to a specific target candidate)? Anything derived from `schedule_e_by_target(candidate_id)` is safe (queried by *our* candidate, always scoped correctly). Anything derived from a committee's own general filings/schedules is not automatically scoped and needs the same treatment.

## Other fixes made this session

- Quarterly donor lookups now cache closed (historical) periods instead of re-fetching every run — only new/most-recent periods get fetched.
- Quarterly period labels use `report_type_full` (human-readable) instead of raw codes like "Q2S"/"12P".
- Card layout widened and the raised/spent/cash-on-hand row switched from a cramped flexbox to a 3-column grid — numbers over $10M were visually overlapping.
- Activity table dates were showing full ISO timestamps (`2026-07-21T00:00:00`) — truncated to just the date, since FEC always returns midnight for date-only fields.
- Recent Activity rebuilt as a full interactive vanilla-JS table: sortable columns, search, filters (type/target/spender), pagination, duplicate-flagging badges, direct links to source FEC filings (`pdf_url`).
- Added "Top Sources of IE Spending" and "Top Beneficiaries" (net support — combines pro-X spending with anti-opponent spending, per the user's explicit framing that these are equivalent) aggregate tables above the activity list.

## Current state (as of this handoff)

- 130 tracked entities in `entities.csv` (104 original manual + 26 from an `ie_discovery` backfill run).
- ~1,260 events in `data/activity_log.jsonl`.
- 133 snapshot files in `data/snapshots/`.
- Repo pushed to GitHub, Actions workflow running successfully on schedule, Pages deploying successfully.
- `.gitignore` added; `__pycache__` and `.DS_Store` cleaned out of tracking.

## Known gaps / not yet done

- **"transfers" field in quarterly detail** hasn't been verified against a real case with nonzero transfers — worth checking before trusting that number if it ever looks suspicious.
- **Sherrod Brown joint fundraising committee**: none found. Only Husted's "Team Husted (Joint Fundraising)" is confirmed tracked.
- **Promoting heavy IE spenders**: AFP Action, SLF PAC, Ohio Flyer PAC, and Red Bridge Leadership PAC are currently `watch` tier / `ie_discovery` sourced. The user floated promoting them to `in_cycle` for fuller itemized tracking — not yet decided or done. (If done, remember the `schedule_e_recent` candidate-filter guard above will keep their spending scoped to tracked candidates automatically.)
- **GitHub Actions Node.js 20 deprecation warning** — cosmetic for now (GitHub auto-substitutes Node 24), but `actions/checkout@v4`, `actions/setup-python@v5`, `actions/upload-artifact@v4` may eventually need version bumps in `update.yml` if GitHub stops the automatic substitution.
- **debug_filings.py / debug_schedule_e.py** — diagnostic scripts, safe to delete, never cleaned up.
- **Flourish integration** (discussed, not built): user wants to eventually visualize this data in Flourish instead of the hand-rolled dashboard. Flourish's auto-refresh from a live URL is a Publisher/Enterprise-only feature; free tier requires manual refresh. If pursued, the natural next step is having `build_dashboard.py` also emit a clean summary CSV to a stable public URL (already have GitHub Pages hosting) for Flourish to point at.

## Operating notes for whoever picks this up

- **Rate limit: 1,000 calls/hour on the FEC API.** `fetch_data.py` has a `--delay` throttle (default 0.3s between entities) and `--only fec_id1,fec_id2` for scoping a run to specific entities instead of a full run — use `--only` liberally when testing something narrow.
- **The user (Andrew) explicitly does not want features implemented preemptively** without discussing them first — he's said this more than once. Propose, don't just build, for anything beyond the immediate ask.
- **He values being challenged on practical issues** — he's said this explicitly too. If a direction has a real downside (cost, rate limits, data integrity), say so before building it.
- **Verify against real data, not assumptions, for anything data-shape-related.** Nearly every subtle bug this session (nested committee name, null cycle field, Form3/3L, this session's IE noise) was caught by the user cross-referencing an actual FEC filing or dashboard screenshot, not by guessing. When something in the data looks surprising, the answer is a debug script against real API output, not a theory.
- **Git operations belong to GitHub Desktop, not to bash in a connected sandbox.** Running `git pull`/`merge` from a sandboxed shell against a mounted folder on the user's actual Mac caused repeated stale-lock-file problems (`.git/index.lock`, `.git/objects/maintenance.lock`) that blocked GitHub Desktop and required manual Finder cleanup. The working pattern: edit files directly (Read/Write/Edit tools work fine on the mount), let the user do all actual commits/pushes/pulls/merges through GitHub Desktop's UI.
- **`dashboard.html` and `data/activity_log.jsonl` are both machine-generated and get rewritten by two independent processes** — the scheduled GitHub Action (every 6 hours) and manual local edits. This causes recurring merge conflicts when both touch the files before a push happens. The safe resolution pattern (used successfully twice this session): never hand-edit conflict markers in these files — instead resolve `activity_log.jsonl` by keeping the union of both sides' lines (it's append-only), and fully regenerate `dashboard.html` via `python3 build_dashboard.py` rather than merging it. Then tell the user to click "Continue Merge" in GitHub Desktop.
- **GitHub Pages does not redeploy on every push** — it only redeploys when the Actions workflow's `deploy` job runs (on schedule or manual "Run workflow" trigger). Pushing a commit updates the repo but not the live site until the workflow runs.
