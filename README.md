# Ohio FEC Campaign Finance Tracker

Automatically pulls FEC filing/fundraising/spending data for a tracked list of
Ohio candidates and committees, diffs it against the previous run to surface
what's new, and builds a dashboard you can view privately or publish
publicly via GitHub Pages.

**Tracked races:** OH Senate, OH-01, OH-07, OH-09, OH-10, OH-13, OH-15, plus
~45 PACs/committees (from your existing FEC.gov tracked list).

**In-cycle matchups** (highlighted on the dashboard, everyone else in those
races is still tracked but shown in a collapsed "also tracked" list):
- OH Senate: Jon Husted vs. Sherrod Brown
- OH-01: Eric Conroy vs. Greg Landsman
- OH-07: Max Miller vs. Brian Poindexter
- OH-09: Marcy Kaptur vs. Derek Merrin
- OH-10: Kristina Knickerbocker vs. Mike Turner
- OH-13: Carey Coleman vs. Emilia Sykes
- OH-15: Mike Carey vs. Don Leonard

This is controlled by a `tracking_tier` column in `entities.csv` (`in_cycle`
or `watch`) — edit that column any time to change who gets top billing.
Committees tied to an in-cycle matchup (principal campaign committees, joint
fundraising committees, leadership PACs) are tagged `in_cycle` too, and get
pulled into their own highlighted section on the dashboard instead of
disappearing into the full A-Z committee table.

Also tracked at the committee level for the Senate race: `HUSTED FOR SENATE`
(principal committee) and `TEAM HUSTED` (joint fundraising committee) for
Husted; `FRIENDS OF SHERROD BROWN` (principal committee) and `DIGNITY OF
WORK PAC` (Brown's long-running leadership PAC) for Brown. I didn't find a
distinct joint fundraising committee registered for Brown in FEC data as of
this writing — if you know of one, send me the name or ID and I'll add it.

## How it works

- `entities.csv` — the list of FEC candidate/committee IDs being tracked, tagged by race and by tracking_tier.
- `fec_client.py` — wrapper around the OpenFEC API (auth, pagination, retries).
- `fetch_data.py` — pulls totals, filings, and (optionally) itemized
  contributions/disbursements/independent expenditures for every entity;
  saves a JSON snapshot per entity in `data/snapshots/`; logs anything new
  since the last run to `data/activity_log.jsonl`.
- `build_dashboard.py` — renders `dashboard.html` from the latest snapshots + activity log.
- `discover_ie_committees.py` — one-time backfill that finds PACs already
  spending in your races this cycle and adds them to `entities.csv` (see below).
- `.github/workflows/update.yml` — runs `fetch_data.py` and `build_dashboard.py`
  every 6 hours on GitHub's servers, commits the updated data, and publishes
  `dashboard.html` to GitHub Pages.

### Independent expenditures — you don't need to know PAC names in advance

For every candidate in the 7 priority races, the fetcher also asks the FEC
"who has spent money for or against this specific person" (via
`/schedules/schedule_e/?candidate_id=...`), rather than only checking the
PACs already in your list. Any committee that runs ads for or against one
of your tracked candidates shows up in the Recent Activity feed the first
time it appears in FEC data — with its own name attached — even if you'd
never heard of it before. That's what the `outside_spending` events in the
activity log are. (Independent expenditures made *by* the PACs you're
already tracking are also still covered separately, labeled `independent_expenditure`.)

Independent expenditures reported via the FEC's 24/48-hour notice system
(the fast-disclosure channel required once a PAC's spending on a race
crosses $10,000, or $1,000 in the final 20 days before an election) are
flagged with an amber "Fast notice" badge in the activity feed — those are
the closest thing to real-time this data gets, since regular periodic
reports can lag months.

### Backfilling PACs that have already spent this cycle

`fetch_data.py` only catches a PAC the first time it appears *after* you
start running it — it won't retroactively surface spending from earlier in
the cycle. For that, run the one-time script:

```bash
FEC_API_KEY=your_key python discover_ie_committees.py --dry-run   # preview first
FEC_API_KEY=your_key python discover_ie_committees.py             # then actually add them
```

It checks every candidate in your 7 priority races for independent
expenditures going back to the start of the 2025-2026 cycle, prints a table
of every PAC that's spent (amount, number of expenditures, support/oppose
split, which races), and appends any you're not already tracking to
`entities.csv` — tagged `source=ie_discovery` and `tracking_tier=watch` so
you can tell them apart from your original curated list and re-tier them if
one of them turns out to matter more than "watch." Run it once now, and
again anytime later if you want a fresh sweep (it skips anything already
tracked, so it's safe to re-run).

One limitation, same as before: it only finds PACs spending on candidates
you're already tracking, not a true "everything in Ohio" sweep — the FEC's
API doesn't offer a clean statewide filter for this. For your 7 priority
races that's not a real gap since you're tracking essentially the full
field already.

### API call budget

A full run makes several hundred calls against the FEC's 1,000-calls/hour
limit, so it's not free to run repeatedly. Two design choices keep it
reasonable:

- **Itemized detail (Schedule A/B/E, outside-spending checks, quarterly/donor
  detail) only runs for `in_cycle`-tagged entities** — the 14 featured
  candidates and their ~20 linked/tagged committees. "Watch" tier
  entities (the other ~80) still get their totals and filings checked every
  run (cheap: 2 calls each), just not the expensive itemized pulls. This was
  the single biggest lever for staying under budget — the alternative was
  doing full itemized pulls for everyone, ~3-4x the calls, most of it spent
  on candidates and PACs you're not actively focused on.
- **`--delay` (default 0.3s between entities)** smooths the request rate out
  instead of bursting and reactively backing off once the API starts
  returning 429s — backoff-and-retry burns *more* calls than pacing does.
- **Closed filing periods' top-donor lists are cached, not re-fetched.** A
  quarter's donor detail can't change once its filing deadline passes, so
  `fetch_quarterly_detail` only queries the API for a period the first time
  it sees it, or for the single most-recent (still "open") period. Every
  other period reuses the donor list already saved in the previous
  snapshot. This means the *first* run for a new in-cycle candidate does a
  full historical pull (one call per past quarter), but every run after
  that only spends one donor call per candidate instead of one per quarter
  — the biggest single reduction in both call volume and request bursting.

Rule of thumb: at most one full run per hour if you're testing manually from
the command line. The scheduled GitHub Actions job already respects this
(every 6 hours). Use `--skip-schedules --skip-quarterly` for a near-free
"just check totals and new filings" run you can repeat as often as you like.

If you want itemized detail restored for everyone (not just in-cycle
entities), that's a config change, not a rewrite -- say the word, just know
it multiplies the call count.

## Setup

There are two separate accounts involved here and it's easy to mix them up:
an **FEC API key** (just a password-like string, no account/login of its
own beyond signing up for it) and a **GitHub account** (a real account you
log into, where the project will live and run). You need both, and you can
get them in either order — the steps below just happen to hit the API key
first.

### 1. Get a free FEC API key

Go to **https://api.open.fec.gov/developers/** and sign up (instant, no
approval wait — this uses the general api.data.gov key system). This gets
you 1,000 requests/hour, which is plenty for this project. Save the key
somewhere safe.

### 2. Test it locally first

Open **Terminal** (Cmd+Space, type "Terminal", Enter). Run each line below
one at a time — press Enter, wait for it to finish, then type the next.
Lines starting with `#` are just notes, not something to type.

```bash
cd ~/Downloads/fec-ohio-tracker
pip3 install -r requirements.txt
export FEC_API_KEY=your_key_here
```

(`export` saves the key for the rest of this Terminal session, so you don't
have to retype it on every command. If `python3`/`pip3` say "command not
found," Python itself isn't installed — macOS usually offers to install it
the first time you type `python3`; click Install if prompted.)

```bash
python3 fetch_data.py --only S6OH00163,H2OH15228
python3 build_dashboard.py
open dashboard.html
```

If that works, run the full list:

```bash
python3 fetch_data.py
python3 build_dashboard.py
```

The first run won't show much in "Recent Activity" — there's nothing to
diff against yet. Run it a second time (or wait for the next scheduled run)
to start seeing new-filing/contribution alerts.

Once this works, it's a good time to also run `discover_ie_committees.py`
(see above) to backfill any PACs already active in your races this cycle,
before you push everything to GitHub.

**Note on speed:** by default the script also pulls itemized Schedule
A/B/E data (donor-level contributions, spending, independent expenditures)
for every committee, which is a lot of API calls across ~100 entities. If
you just want fundraising totals and new filings fast, add
`--skip-schedules`.

### 3. Create a GitHub account (if you don't have one) and push the project

Do this **after** step 2 works locally, so you know the project itself is
good before adding automation on top of it. No command line needed —
everything below is clicking around on a website, same as any other signup.

1. Go to **github.com** → **Sign up** (top right). Email, password, username
   — same as signing up for any site. Free.
2. Once logged in, click the **+** in the top right → **New repository**.
   Name it `fec-ohio-tracker`, leave it **Public** (or **Private** if you'd
   rather — Private just means the code and dashboard aren't visible to
   strangers, but then you can't use the free public GitHub Pages hosting
   for the dashboard). Click **Create repository**.
3. On the new repo's page, click **Add file → Upload files**, then drag the
   entire contents of the `fec-ohio-tracker` folder (all the files, plus
   the `.github` and `data` folders) into the browser window. Scroll down
   and click **Commit changes**.

   *(If you're comfortable with the command line instead: `git init && git add . && git commit -m "Initial commit" && gh repo create fec-ohio-tracker --public --source=. --push`)*

Then, still on github.com, in your new repo:

1. **Settings tab → Secrets and variables → Actions → New repository secret.**
   Name: `FEC_API_KEY`, value: your key from step 1. This is what keeps the
   key out of the public files while still letting the automation use it.
2. **Settings tab → Pages → Build and deployment → Source:** GitHub Actions.
3. **Actions tab →** click into "Update FEC Tracker" → **Run workflow**
   (this is the manual trigger, `workflow_dispatch`) → confirm it finishes
   green, not red. If it's red, click into it to see the error and send it
   to me.

After that first successful run, it repeats automatically every 6 hours —
you never have to open GitHub again unless you want to change something.
(To change how often, edit the cron line in `.github/workflows/update.yml`
— e.g. `0 */2 * * *` for every 2 hours.) Your public dashboard will be live
at `https://<your-github-username>.github.io/fec-ohio-tracker/`.

### 4. Using it just for yourself (no public page)

Skip the Pages setup. Either:
- Run `python3 fetch_data.py && python3 build_dashboard.py` locally whenever you want an update, or
- Keep the GitHub Actions workflow but remove the `deploy` job — it'll still
  commit fresh data/dashboard.html to the repo on schedule, which you can
  pull down (`git pull`) or just view on github.com.

## Data notes

- **"Real-time" caveat:** FEC data itself isn't instantaneous — committees
  file periodically (quarterly/monthly, more often close to an election),
  and processed/coded data can lag a few days behind raw e-filings. 24/48-hour
  notices (flagged in the activity feed, see above) are the fast path;
  regular periodic filings are the slow path.
- **Large contribution threshold:** defaults to $1,000 for the "large
  contribution" activity feed (`--big-donor-threshold` to change).
- **Committee-to-race mapping:** I only tagged a committee with a specific
  race when it's a candidate's own principal committee (e.g. "CAREY FOR
  CONGRESS" → OH-15). Generic PACs are listed under "Committees & PACs"
  rather than guessed into a race, to avoid misattributing them.
- **Adding/removing tracked entities:** just edit `entities.csv`. Find IDs
  at https://www.fec.gov/data/ (search a candidate or committee, the ID is
  in the URL). New row needs `entity_type` (candidate/committee) and, for
  candidates, `race_group` if you want it grouped under one of the priority
  races. Set `tracking_tier` to `in_cycle` to feature it prominently.
- **`source` column:** `manual` for everything you originally curated,
  `ie_discovery` for PACs added automatically by `discover_ie_committees.py`.
  Purely informational — doesn't affect how anything is fetched or displayed,
  just lets you tell at a glance how a row got there.

## Next ideas (not built yet, say the word if you want these)

- Email/Slack digest of new activity instead of (or in addition to) the dashboard
- Historical trend charts (raised/spent over time) rather than point-in-time totals
- Faster-than-6-hours updates using the `/schedules/*/efile` endpoints (raw, unprocessed data as little as 24-48 hours old)
