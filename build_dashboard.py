"""
Build a single self-contained dashboard.html from the latest snapshots +
activity log. This file is what gets deployed to GitHub Pages for the public
view, and is equally fine to just open locally for your own use.

Usage:
    python build_dashboard.py
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from fetch_data import CURRENT_CYCLE

ROOT = Path(__file__).parent
ENTITIES_CSV = ROOT / "entities.csv"
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
ACTIVITY_LOG = ROOT / "data" / "activity_log.jsonl"
OUTPUT_HTML = ROOT / "dashboard.html"

RACE_ORDER = ["OH Senate", "OH-01", "OH-07", "OH-09", "OH-10", "OH-13", "OH-15", "Other OH district"]


def load_entities():
    with open(ENTITIES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_snapshot(fec_id):
    path = SNAPSHOT_DIR / f"{fec_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def event_sort_date(e):
    """Sort by the event's own real-world date, not when we happened to log
    it -- 'logged_at' only reflects when a fetch_data.py run noticed the
    record, which clusters everything from one run together instead of
    interleaving chronologically. Falls back to logged_at for the rare event
    with no native date field."""
    return e.get("date") or e.get("receipt_date") or e.get("logged_at", "")


def flag_duplicates(events):
    """Mark events that share a dedup_key (same spender, target, amount,
    date, and payee) with at least one other event. This is a FLAG for
    manual review, not a filter -- see schedule_e_dedup_key in fetch_data.py
    for why we don't auto-drop these: a committee can legitimately place two
    identical-amount buys with the same vendor on the same day."""
    counts = {}
    for e in events:
        key = e.get("dedup_key")
        if key:
            counts[key] = counts.get(key, 0) + 1
    for e in events:
        key = e.get("dedup_key")
        e["possible_duplicate"] = bool(key and counts.get(key, 0) > 1)
    return events


def load_activity(limit=None):
    if not ACTIVITY_LOG.exists():
        return []
    lines = ACTIVITY_LOG.read_text().strip().splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    events.sort(key=event_sort_date, reverse=True)
    flag_duplicates(events)
    if limit:
        events = events[:limit]
    return events


def aggregate_ie_sources(entities, snapshots_by_id, limit=15):
    """
    Rank spending committees by total independent-expenditure dollars across
    all in-cycle candidates' schedule_e_target data. Sourced from the raw
    snapshots (not the activity log), so this reflects everything currently
    known rather than only events logged as 'new' since tracking began.
    """
    totals = {}
    for entity in entities:
        if entity["entity_type"] != "candidate" or entity.get("tracking_tier") != "in_cycle":
            continue
        snapshot = snapshots_by_id.get(entity["fec_id"], {})
        for rec in snapshot.get("schedule_e_target", []):
            committee = rec.get("committee") or {}
            cid = rec.get("committee_id") or committee.get("name") or "unknown"
            name = committee.get("name") or rec.get("committee_id") or "Unknown spender"
            amount = rec.get("expenditure_amount") or 0
            entry = totals.setdefault(cid, {"name": name, "total": 0.0, "count": 0, "support": 0.0, "oppose": 0.0})
            entry["total"] += amount
            entry["count"] += 1
            indicator = rec.get("support_oppose_indicator")
            if indicator == "S":
                entry["support"] += amount
            elif indicator == "O":
                entry["oppose"] += amount
    ranked = sorted(totals.values(), key=lambda x: x["total"], reverse=True)
    return ranked[:limit]


def aggregate_ie_beneficiaries(entities, snapshots_by_id):
    """
    Net IE benefit per in-cycle candidate: $ spent supporting them, PLUS $
    spent opposing their named matchup opponent -- both count as helping
    the same candidate electorally, per how independent expenditures are
    meant to be read (a dollar trashing your opponent has the same effect
    as a dollar boosting you). Pairing only happens within a race_group that
    has exactly two in_cycle candidates (your tracked matchups); anything
    else just shows raw support with no opponent to pair against.
    """
    by_race = {}
    for entity in entities:
        if entity["entity_type"] == "candidate" and entity.get("tracking_tier") == "in_cycle":
            by_race.setdefault(entity["race_group"], []).append(entity)

    raw = {}
    for entity in entities:
        if entity["entity_type"] != "candidate" or entity.get("tracking_tier") != "in_cycle":
            continue
        snapshot = snapshots_by_id.get(entity["fec_id"], {})
        support = oppose = 0.0
        for rec in snapshot.get("schedule_e_target", []):
            amount = rec.get("expenditure_amount") or 0
            indicator = rec.get("support_oppose_indicator")
            if indicator == "S":
                support += amount
            elif indicator == "O":
                oppose += amount
        raw[entity["fec_id"]] = {"support": support, "oppose": oppose}

    results = []
    for race, cands in by_race.items():
        if len(cands) != 2:
            for c in cands:
                r = raw.get(c["fec_id"], {"support": 0, "oppose": 0})
                results.append({
                    "name": c["name"], "race_group": race,
                    "net_benefit": r["support"], "support": r["support"], "oppose_opponent": 0,
                })
            continue
        a, b = cands
        ra = raw.get(a["fec_id"], {"support": 0, "oppose": 0})
        rb = raw.get(b["fec_id"], {"support": 0, "oppose": 0})
        results.append({
            "name": a["name"], "race_group": race,
            "net_benefit": ra["support"] + rb["oppose"], "support": ra["support"], "oppose_opponent": rb["oppose"],
        })
        results.append({
            "name": b["name"], "race_group": race,
            "net_benefit": rb["support"] + ra["oppose"], "support": rb["support"], "oppose_opponent": ra["oppose"],
        })
    results.sort(key=lambda x: x["net_benefit"], reverse=True)
    return results


def top_sources_table(sources):
    if not sources:
        return '<p class="muted">No independent expenditure data yet.</p>'
    rows = "\n".join(f"""
      <tr>
        <td>{s['name']}</td>
        <td>{fmt_money(s['total'])}</td>
        <td>{fmt_money(s['support'])}</td>
        <td>{fmt_money(s['oppose'])}</td>
        <td>{s['count']}</td>
      </tr>""" for s in sources)
    return f"""
    <table>
      <thead><tr><th>Organization</th><th>Total spent</th><th>Supporting</th><th>Opposing</th><th># of expenditures</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def top_beneficiaries_table(beneficiaries):
    if not beneficiaries:
        return '<p class="muted">No independent expenditure data yet.</p>'
    rows = "\n".join(f"""
      <tr>
        <td>{b['name']} <span class="muted">({b['race_group']})</span></td>
        <td>{fmt_money(b['net_benefit'])}</td>
        <td>{fmt_money(b['support'])}</td>
        <td>{fmt_money(b['oppose_opponent'])}</td>
      </tr>""" for b in beneficiaries)
    return f"""
    <table>
      <thead><tr><th>Candidate</th><th>Net IE benefit</th><th>$ supporting them</th><th>$ opposing their opponent</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def fmt_money(val):
    if val is None:
        return "—"
    try:
        return f"${float(val):,.0f}"
    except (TypeError, ValueError):
        return "—"


def latest_total(totals):
    """
    Pick the CURRENT cycle's totals record, not just whatever's first in the
    list. The FEC totals endpoints return one row per election cycle a
    candidate/committee has ever been active in, and the "cycle" field on
    each row is null in practice -- confirmed against real data for Sherrod
    Brown on 2026-07-21, where his 2019-2024 cycle (prior full Senate run,
    $103M/$104M/$394K) was landing here instead of the current 2025-2026
    cycle ($38.6M/$22.3M/$16.2M), because there was nothing reliable to sort
    on. fetch_data.py now asks the API to filter to CURRENT_CYCLE directly,
    but this also re-checks client-side so any ALREADY-fetched snapshot
    self-corrects the next time the dashboard is built, without needing a
    fresh API call.
    """
    if not totals:
        return {}
    for t in totals:
        if t.get("cycle") == CURRENT_CYCLE or t.get("candidate_election_year") == CURRENT_CYCLE:
            return t
    return totals[0]


def candidate_card(entity, snapshot):
    totals = latest_total(snapshot.get("totals", []))
    filings = snapshot.get("filings", [])[:1]
    last_filing = filings[0] if filings else {}
    raised = totals.get("receipts")
    spent = totals.get("disbursements")
    cash = totals.get("cash_on_hand_end_period") or totals.get("last_cash_on_hand_end_period")
    is_in_cycle = entity.get("tracking_tier") == "in_cycle"
    card_class = "card in-cycle" if is_in_cycle else "card"
    badge = '<span class="badge">In-cycle matchup</span>' if is_in_cycle else ""
    return f"""
    <div class="{card_class}">
      <div class="card-name">{entity['name']} {badge}</div>
      <div class="card-id"><a href="https://www.fec.gov/data/candidate/{entity['fec_id']}/" target="_blank">{entity['fec_id']}</a></div>
      <div class="card-stats">
        <div><span class="label">Raised</span><span class="val">{fmt_money(raised)}</span></div>
        <div><span class="label">Spent</span><span class="val">{fmt_money(spent)}</span></div>
        <div><span class="label">Cash on hand</span><span class="val">{fmt_money(cash)}</span></div>
      </div>
      <div class="card-filing">
        {"Last filing: " + str(last_filing.get('form_type', '')) + " on " + str(last_filing.get('receipt_date', ''))[:10] if last_filing else "No filings on record"}
      </div>
    </div>"""


def watch_row(entity, snapshot):
    totals = latest_total(snapshot.get("totals", []))
    cash = totals.get("cash_on_hand_end_period") or totals.get("last_cash_on_hand_end_period")
    return f"""<li><a href="https://www.fec.gov/data/candidate/{entity['fec_id']}/" target="_blank">{entity['name']}</a> <span class="muted">— cash on hand {fmt_money(cash)}</span></li>"""


def committee_card(entity, snapshot):
    """Highlighted card for an in-cycle committee (JFC, principal committee,
    leadership PAC tied to a matchup) -- same visual treatment as an
    in-cycle candidate card, so it doesn't get lost in the full A-Z table."""
    totals = latest_total(snapshot.get("totals", []))
    raised = totals.get("receipts")
    spent = totals.get("disbursements")
    cash = totals.get("cash_on_hand_end_period") or totals.get("last_cash_on_hand_end_period")
    return f"""
    <div class="card in-cycle">
      <div class="card-name">{entity['name']} <span class="badge">Tracked committee</span></div>
      <div class="card-id"><a href="https://www.fec.gov/data/committee/{entity['fec_id']}/" target="_blank">{entity['fec_id']}</a></div>
      <div class="card-stats">
        <div><span class="label">Raised</span><span class="val">{fmt_money(raised)}</span></div>
        <div><span class="label">Spent</span><span class="val">{fmt_money(spent)}</span></div>
        <div><span class="label">Cash on hand</span><span class="val">{fmt_money(cash)}</span></div>
      </div>
    </div>"""


def committee_row(entity, snapshot):
    totals = latest_total(snapshot.get("totals", []))
    raised = totals.get("receipts")
    spent = totals.get("disbursements")
    cash = totals.get("cash_on_hand_end_period") or totals.get("last_cash_on_hand_end_period")
    return f"""
    <tr>
      <td><a href="https://www.fec.gov/data/committee/{entity['fec_id']}/" target="_blank">{entity['name']}</a></td>
      <td>{entity['state']}</td>
      <td>{fmt_money(raised)}</td>
      <td>{fmt_money(spent)}</td>
      <td>{fmt_money(cash)}</td>
    </tr>"""


def activity_table_html(events):
    """
    Renders the Recent Activity section as an interactive table: all events
    (no cutoff) embedded as JSON, sorted/filtered/paginated client-side with
    plain JavaScript -- keeps the dashboard a single static file, no backend
    or build step needed for GitHub Pages.

    Sorting defaults to newest-first by the event's own date (see
    event_sort_date) -- this is also computed server-side for the initial
    embed, but the client JS re-sorts on click so it doesn't matter which
    order the JSON happens to be in.
    """
    if not events:
        return '<p class="muted">No activity logged yet — run fetch_data.py at least twice to start seeing diffs.</p>'

    # Keep the embedded payload reasonably lean -- drop server-only bookkeeping
    # fields the client doesn't need.
    slim = []
    for e in events:
        slim.append({k: v for k, v in e.items() if k not in ("logged_at", "fec_id", "rebuilt")})
    payload = json.dumps(slim, default=str).replace("</", "<\\/")

    return f"""
    <div class="activity-controls">
      <input type="text" id="activity-search" placeholder="Search spender, target, payee, description...">
      <select id="activity-type-filter"><option value="">All types</option></select>
      <select id="activity-target-filter"><option value="">All targets</option></select>
      <select id="activity-spender-filter"><option value="">All spenders/contributors</option></select>
      <label class="check-label"><input type="checkbox" id="activity-hide-dupes"> Hide flagged duplicates</label>
    </div>
    <table id="activity-table">
      <thead>
        <tr>
          <th data-sort="date" class="sortable">Date</th>
          <th data-sort="typeLabel" class="sortable">Type</th>
          <th data-sort="target" class="sortable">Target</th>
          <th data-sort="spender" class="sortable">Spender / Contributor</th>
          <th data-sort="amount" class="sortable">Amount</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody id="activity-tbody"></tbody>
    </table>
    <div class="activity-pagination">
      <button id="activity-prev" type="button">&larr; Prev</button>
      <span id="activity-page-info" class="muted"></span>
      <button id="activity-next" type="button">Next &rarr;</button>
    </div>
    <script id="activity-data" type="application/json">{payload}</script>
    <script>
    (function() {{
      var RAW = JSON.parse(document.getElementById('activity-data').textContent);
      var TYPE_LABELS = {{
        new_filing: 'New filing',
        large_contribution: 'Large contribution',
        new_disbursement: 'New disbursement',
        independent_expenditure: 'Independent expenditure (by tracked PAC)',
        outside_spending: 'Outside spending (PAC, for/against)'
      }};

      function esc(s) {{
        return (s === null || s === undefined ? '' : String(s))
          .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      }}
      function fmtMoney(v) {{
        if (v === null || v === undefined || v === '') return '—';
        var n = Number(v);
        if (isNaN(n)) return '—';
        return '$' + n.toLocaleString(undefined, {{maximumFractionDigits: 0}});
      }}

      // Normalize every event type into one common shape. Schedule E events
      // split into two flavors depending on which entity generated them:
      // "outside_spending" is logged against the TARGET candidate (spender
      // lives in spender_name), "independent_expenditure" is logged against
      // the SPENDING committee itself (spender is the entity's own name,
      // target lives in target_name -- added specifically so this event
      // type has a usable target to filter/display, which it didn't before).
      var rows = RAW.map(function(e) {{
        var target = '', spender = '', detail = '';
        var isE = (e.type === 'outside_spending' || e.type === 'independent_expenditure');
        if (e.type === 'outside_spending') {{
          target = e.name || '';
          spender = e.spender_name || '';
        }} else if (e.type === 'independent_expenditure') {{
          target = e.target_name || '(unspecified)';
          spender = e.name || '';
        }} else {{
          target = e.name || '';
          spender = e.contributor_or_payee || '';
        }}
        if (isE) {{
          var so = e.support_oppose === 'S' ? 'Supporting' : (e.support_oppose === 'O' ? 'Opposing' : '');
          var bits = [so];
          if (e.payee_name) bits.push('paid to ' + e.payee_name);
          if (e.purpose) bits.push(e.purpose);
          detail = bits.filter(Boolean).join(' · ');
        }} else {{
          var bits2 = [];
          if (e.form_type) bits2.push('Form ' + e.form_type);
          if (e.detail) bits2.push(e.detail);
          if (e.document_description) bits2.push(e.document_description);
          detail = bits2.filter(Boolean).join(' · ');
        }}
        return {{
          // FEC dates come back as full ISO datetimes (always midnight,
          // e.g. "2026-07-21T00:00:00") even though there's no real
          // time-of-day component -- slice to just the date part.
          date: (e.date || e.receipt_date || '').slice(0, 10),
          type: e.type,
          typeLabel: TYPE_LABELS[e.type] || e.type,
          target: target,
          spender: spender,
          amount: e.amount,
          detail: detail,
          pdfUrl: e.pdf_url || null,
          isNotice: !!e.is_notice,
          possibleDuplicate: !!e.possible_duplicate,
          raceGroup: e.race_group || ''
        }};
      }});

      // Populate filter dropdowns from the full dataset (not the filtered
      // view) so options don't shrink as you narrow things down.
      function uniqueSorted(values) {{
        var seen = {{}}, out = [];
        values.forEach(function(v) {{
          if (v && !seen[v]) {{ seen[v] = true; out.push(v); }}
        }});
        out.sort();
        return out;
      }}
      function fillSelect(id, values) {{
        var sel = document.getElementById(id);
        values.forEach(function(v) {{
          var opt = document.createElement('option');
          opt.value = v; opt.textContent = v;
          sel.appendChild(opt);
        }});
      }}
      fillSelect('activity-type-filter', uniqueSorted(rows.map(function(r) {{ return r.typeLabel; }})));
      fillSelect('activity-target-filter', uniqueSorted(rows.map(function(r) {{ return r.target; }})));
      fillSelect('activity-spender-filter', uniqueSorted(rows.map(function(r) {{ return r.spender; }})));

      var state = {{ sortKey: 'date', sortDir: -1, page: 1, pageSize: 25 }};
      var searchEl = document.getElementById('activity-search');
      var typeEl = document.getElementById('activity-type-filter');
      var targetEl = document.getElementById('activity-target-filter');
      var spenderEl = document.getElementById('activity-spender-filter');
      var hideDupesEl = document.getElementById('activity-hide-dupes');
      var tbody = document.getElementById('activity-tbody');
      var pageInfo = document.getElementById('activity-page-info');

      function filtered() {{
        var q = (searchEl.value || '').toLowerCase();
        var type = typeEl.value, target = targetEl.value, spender = spenderEl.value;
        var hideDupes = hideDupesEl.checked;
        return rows.filter(function(r) {{
          if (type && r.typeLabel !== type) return false;
          if (target && r.target !== target) return false;
          if (spender && r.spender !== spender) return false;
          if (hideDupes && r.possibleDuplicate) return false;
          if (q) {{
            var hay = (r.target + ' ' + r.spender + ' ' + r.detail).toLowerCase();
            if (hay.indexOf(q) === -1) return false;
          }}
          return true;
        }});
      }}

      function sorted(list) {{
        var key = state.sortKey, dir = state.sortDir;
        return list.slice().sort(function(a, b) {{
          var av = a[key], bv = b[key];
          if (key === 'amount') {{
            av = av || 0; bv = bv || 0;
            return dir * (av - bv);
          }}
          av = (av || '').toString().toLowerCase();
          bv = (bv || '').toString().toLowerCase();
          if (av < bv) return -dir;
          if (av > bv) return dir;
          return 0;
        }});
      }}

      function render() {{
        var list = sorted(filtered());
        var totalPages = Math.max(1, Math.ceil(list.length / state.pageSize));
        if (state.page > totalPages) state.page = totalPages;
        if (state.page < 1) state.page = 1;
        var start = (state.page - 1) * state.pageSize;
        var pageRows = list.slice(start, start + state.pageSize);

        tbody.innerHTML = pageRows.map(function(r) {{
          var noticeBadge = r.isNotice ? ' <span class="badge notice">Fast notice</span>' : '';
          var dupBadge = r.possibleDuplicate
            ? ' <span class="badge dup" title="Same spender, target, amount, date, and payee as another row -- likely (not certainly) the same expenditure reported twice. Not auto-removed; verify before treating as separate spending.">Possible duplicate</span>'
            : '';
          var link = r.pdfUrl ? ' <a href="' + esc(r.pdfUrl) + '" target="_blank" rel="noopener">View filing ↗</a>' : '';
          return '<tr' + (r.possibleDuplicate ? ' class="dup-row"' : '') + '>' +
            '<td>' + esc(r.date) + '</td>' +
            '<td><span class="tag">' + esc(r.typeLabel) + '</span>' + noticeBadge + dupBadge + '</td>' +
            '<td>' + esc(r.target) + (r.raceGroup ? ' <span class="muted">(' + esc(r.raceGroup) + ')</span>' : '') + '</td>' +
            '<td>' + esc(r.spender) + '</td>' +
            '<td>' + fmtMoney(r.amount) + '</td>' +
            '<td>' + esc(r.detail) + link + '</td>' +
          '</tr>';
        }}).join('') || '<tr><td colspan="6" class="muted">No activity matches these filters.</td></tr>';

        pageInfo.textContent = 'Page ' + state.page + ' of ' + totalPages + ' (' + list.length + ' rows)';
        document.getElementById('activity-prev').disabled = state.page <= 1;
        document.getElementById('activity-next').disabled = state.page >= totalPages;

        document.querySelectorAll('#activity-table th.sortable').forEach(function(th) {{
          var key = th.getAttribute('data-sort');
          th.textContent = th.textContent.replace(/ [↑↓]$/, '');
          if (key === state.sortKey) th.textContent += state.sortDir === 1 ? ' ↑' : ' ↓';
        }});
      }}

      document.querySelectorAll('#activity-table th.sortable').forEach(function(th) {{
        th.addEventListener('click', function() {{
          var key = th.getAttribute('data-sort');
          if (state.sortKey === key) {{
            state.sortDir = -state.sortDir;
          }} else {{
            state.sortKey = key;
            state.sortDir = (key === 'date' || key === 'amount') ? -1 : 1;
          }}
          state.page = 1;
          render();
        }});
      }});
      [searchEl, typeEl, targetEl, spenderEl, hideDupesEl].forEach(function(el) {{
        el.addEventListener('input', function() {{ state.page = 1; render(); }});
        el.addEventListener('change', function() {{ state.page = 1; render(); }});
      }});
      document.getElementById('activity-prev').addEventListener('click', function() {{ state.page--; render(); }});
      document.getElementById('activity-next').addEventListener('click', function() {{ state.page++; render(); }});

      render();
    }})();
    </script>"""


def donor_list(donors):
    if not donors:
        return '<li class="muted">No itemized contributions on record for this period.</li>'
    items = []
    for d in donors:
        loc = ", ".join(x for x in (d.get("city"), d.get("state")) if x)
        employer = f" — {d['employer']}" if d.get("employer") else ""
        items.append(
            f"<li><strong>{fmt_money(d.get('amount'))}</strong> {d.get('name') or 'Unknown'} "
            f"<span class=\"muted\">{loc}{employer} · {str(d.get('date') or '')[:10]}</span></li>"
        )
    return "\n".join(items)


def quarterly_block(entity, snapshot):
    quarters = snapshot.get("quarterly")
    if not quarters:
        return ""

    rows = "\n".join(f"""
      <tr>
        <td>{q['label']}</td>
        <td>{q['coverage_start']} – {q['coverage_end']}</td>
        <td>{fmt_money(q.get('raised'))}</td>
        <td>{fmt_money(q.get('spent'))}</td>
        <td>{fmt_money(q.get('transfers'))}</td>
        <td>{fmt_money(q.get('cash_on_hand'))}</td>
      </tr>""" for q in quarters)

    donor_sections = "\n".join(f"""
      <details class="donor-list">
        <summary>Top donors — {q['label']} ({len(q.get('top_donors', []))})</summary>
        <ul>{donor_list(q.get('top_donors', []))}</ul>
      </details>""" for q in quarters)

    return f"""
    <div class="quarterly-block">
      <h3>{entity['name']}</h3>
      <table>
        <thead><tr><th>Period</th><th>Coverage</th><th>Raised</th><th>Spent</th><th>Transfers</th><th>Cash on hand</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      {donor_sections}
    </div>"""


def build():
    entities = load_entities()
    activity = load_activity()

    by_race = {race: [] for race in RACE_ORDER}
    committees = []
    snapshots_by_id = {}

    for entity in entities:
        snapshot = load_snapshot(entity["fec_id"])
        snapshots_by_id[entity["fec_id"]] = snapshot
        if entity["entity_type"] == "candidate":
            race = entity["race_group"] or "Other OH district"
            by_race.setdefault(race, []).append((entity, snapshot))
        else:
            committees.append((entity, snapshot))

    race_sections = ""
    quarterly_candidates = []  # in-cycle candidates with quarterly data, collected for the section below
    for race in RACE_ORDER:
        entries = by_race.get(race, [])
        if not entries:
            continue
        in_cycle = [(e, s) for e, s in entries if e.get("tracking_tier") == "in_cycle"]
        watch = [(e, s) for e, s in entries if e.get("tracking_tier") != "in_cycle"]
        quarterly_candidates.extend((e, s) for e, s in in_cycle if s.get("quarterly"))

        in_cycle_html = "\n".join(candidate_card(e, s) for e, s in in_cycle) if in_cycle else "\n".join(candidate_card(e, s) for e, s in entries)
        watch_html = ""
        if in_cycle and watch:
            watch_items = "\n".join(watch_row(e, s) for e, s in sorted(watch, key=lambda x: x[0]["name"]))
            watch_html = f"""
          <details class="watch-list">
            <summary>Also tracked in {race} ({len(watch)})</summary>
            <ul>{watch_items}</ul>
          </details>"""

        race_sections += f"""
        <section>
          <h2>{race}</h2>
          <div class="card-grid">{in_cycle_html}</div>
          {watch_html}
        </section>"""

    # Split committees so in-cycle ones (JFCs, leadership PACs, principal
    # committees tied to a current matchup) get their own highlighted row of
    # cards instead of disappearing into the alphabetical table of ~50 PACs.
    in_cycle_committees = [(e, s) for e, s in committees if e.get("tracking_tier") == "in_cycle"]
    watch_committees = [(e, s) for e, s in committees if e.get("tracking_tier") != "in_cycle"]

    in_cycle_committee_html = ""
    if in_cycle_committees:
        cards = "\n".join(committee_card(e, s) for e, s in sorted(in_cycle_committees, key=lambda x: x[0]["name"]))
        in_cycle_committee_html = f"""
    <section>
      <h2>Committees tied to your in-cycle races</h2>
      <div class="card-grid">{cards}</div>
    </section>"""

    committee_rows = "\n".join(committee_row(e, s) for e, s in sorted(watch_committees, key=lambda x: x[0]["name"]))

    quarterly_html = ""
    if quarterly_candidates:
        blocks = "\n".join(quarterly_block(e, s) for e, s in sorted(quarterly_candidates, key=lambda x: x[0]["name"]))
        quarterly_html = f"""
    <section>
      <h2>Quarterly Detail — In-Cycle Candidates</h2>
      <p class="muted">Each row is one filed report period (as disclosed, not forced into calendar quarters). Top donors are the largest individual contributions in that period, capped per quarter — not each donor's running total.</p>
      {blocks}
    </section>"""

    ie_sources = aggregate_ie_sources(entities, snapshots_by_id)
    ie_beneficiaries = aggregate_ie_beneficiaries(entities, snapshots_by_id)
    ie_overview_html = ""
    if ie_sources or ie_beneficiaries:
        ie_overview_html = f"""
    <section>
      <h2>Independent Expenditure Overview</h2>
      <p class="muted">Built from all currently-known Schedule E data for your in-cycle candidates (not just recently-logged activity). "Net IE benefit" combines $ spent supporting a candidate with $ spent opposing their matchup opponent, since both help the same candidate electorally.</p>
      <div class="ie-overview-grid">
        <div>
          <h3>Top sources of independent expenditures</h3>
          {top_sources_table(ie_sources)}
        </div>
        <div>
          <h3>Top beneficiaries (net IE support)</h3>
          {top_beneficiaries_table(ie_beneficiaries)}
        </div>
      </div>
    </section>"""

    activity_html = activity_table_html(activity)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ohio FEC Campaign Finance Tracker</title>
<style>
  :root {{ --bg:#0f1115; --card:#171a21; --text:#e8eaed; --muted:#8b92a1; --accent:#4f8cff; --border:#262a34; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background:var(--bg); color:var(--text); }}
  header {{ padding: 24px 32px; border-bottom: 1px solid var(--border); }}
  header h1 {{ margin:0; font-size: 22px; }}
  header p {{ margin: 6px 0 0; color: var(--muted); font-size: 13px; }}
  main {{ padding: 24px 32px 64px; max-width: 1200px; margin: 0 auto; }}
  section {{ margin-bottom: 36px; }}
  h2 {{ font-size: 16px; border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-bottom: 16px; }}
  .card-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }}
  .card.in-cycle {{ border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }}
  .badge {{ background: var(--accent); color: #fff; font-size: 9px; font-weight: 600; text-transform: uppercase; padding: 2px 6px; border-radius: 4px; vertical-align: middle; }}
  .badge.notice {{ background: #e8a33d; color: #1a1200; }}
  .watch-list {{ margin-top: 12px; font-size: 12px; }}
  .watch-list summary {{ cursor: pointer; color: var(--muted); }}
  .watch-list ul {{ list-style: none; padding: 8px 0 0 4px; margin: 0; }}
  .watch-list li {{ padding: 4px 0; border-bottom: 1px solid var(--border); }}
  .card-name {{ font-weight: 600; font-size: 14px; margin-bottom: 2px; }}
  .card-id a {{ color: var(--muted); font-size: 11px; text-decoration: none; }}
  .card-id a:hover {{ color: var(--accent); }}
  .card-stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 14px; }}
  .card-stats .label {{ display:block; font-size: 10px; color: var(--muted); text-transform: uppercase; white-space: nowrap; }}
  .card-stats .val {{ display:block; font-size: 16px; font-weight: 600; margin-top: 3px; white-space: nowrap; }}
  .card-filing {{ margin-top: 10px; font-size: 11px; color: var(--muted); }}
  .quarterly-block {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; }}
  .quarterly-block h3 {{ margin: 0 0 12px; font-size: 14px; }}
  .quarterly-block table {{ margin-bottom: 10px; }}
  .donor-list {{ margin-top: 6px; font-size: 12px; }}
  .donor-list summary {{ cursor: pointer; color: var(--accent); padding: 4px 0; }}
  .donor-list ul {{ list-style: none; padding: 6px 0 6px 4px; margin: 0; }}
  .donor-list li {{ padding: 5px 0; border-bottom: 1px solid var(--border); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; }}
  a {{ color: var(--accent); }}
  .muted {{ color: var(--muted); font-size: 12px; }}
  .tag {{ background: rgba(79,140,255,0.15); color: var(--accent); padding: 2px 8px; border-radius: 999px; font-size: 11px; }}
  footer {{ text-align:center; color: var(--muted); font-size: 11px; padding: 20px; }}
  .ie-overview-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 20px; }}
  .ie-overview-grid h3 {{ font-size: 13px; margin: 0 0 8px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.02em; }}
  .activity-controls {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; align-items: center; }}
  .activity-controls input[type="text"], .activity-controls select {{
    background: var(--card); color: var(--text); border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 10px; font-size: 12px;
  }}
  .activity-controls input[type="text"] {{ flex: 1 1 220px; min-width: 180px; }}
  .activity-controls select {{ max-width: 220px; }}
  .check-label {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); white-space: nowrap; }}
  #activity-table th.sortable {{ cursor: pointer; user-select: none; }}
  #activity-table th.sortable:hover {{ color: var(--accent); }}
  tr.dup-row {{ background: rgba(232, 163, 61, 0.06); }}
  .badge.dup {{ background: transparent; color: #e8a33d; border: 1px solid #e8a33d; cursor: help; }}
  .activity-pagination {{ display: flex; align-items: center; gap: 12px; margin-top: 12px; }}
  .activity-pagination button {{
    background: var(--card); color: var(--text); border: 1px solid var(--border); border-radius: 6px;
    padding: 5px 12px; font-size: 12px; cursor: pointer;
  }}
  .activity-pagination button:disabled {{ opacity: 0.4; cursor: default; }}
  .activity-pagination button:not(:disabled):hover {{ border-color: var(--accent); color: var(--accent); }}
</style>
</head>
<body>
<header>
  <h1>Ohio FEC Campaign Finance Tracker</h1>
  <p>Data from the FEC's OpenFEC API · Last updated {generated_at} · Tracking {len(entities)} candidates &amp; committees</p>
</header>
<main>
  {race_sections}
  {in_cycle_committee_html}
  {quarterly_html}
  <section>
    <h2>Committees &amp; PACs (tracked)</h2>
    <p class="muted">These are committee-level entities from your tracked list not tied to a specific in-cycle matchup (PACs, joint fundraising committees, leadership PACs, generic/former committees, etc.)</p>
    <table>
      <thead><tr><th>Name</th><th>State</th><th>Raised</th><th>Spent</th><th>Cash on hand</th></tr></thead>
      <tbody>{committee_rows}</tbody>
    </table>
  </section>
  {ie_overview_html}
  <section>
    <h2>Recent Activity</h2>
    <p class="muted">All logged filings, contributions, and independent expenditures. Click a column header to sort; use the filters to narrow the list. "Possible duplicate" rows share the same spender, target, amount, date, and payee as another row — likely (not certainly) the same expenditure reported twice; nothing is auto-removed, review and judge for yourself.</p>
    {activity_html}
  </section>
</main>
<footer>Source: api.open.fec.gov &middot; Not affiliated with the FEC &middot; Generated automatically</footer>
</body>
</html>"""

    OUTPUT_HTML.write_text(html)
    print(f"Wrote {OUTPUT_HTML}")


if __name__ == "__main__":
    build()
