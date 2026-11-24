"""
Validate the monthly feedback proposals and render the report email.

Usage:
  python3 feedback_report.py DATA.json PROPOSALS.json SKILL_DIR STATE_DIR
      Check PROPOSALS.json against DATA.json (feedback_data.py output) and the current
      references, then write STATE_DIR/<period end>/{proposals.json, report.html, report.txt}
      and print the email subject. Raises ValueError naming the first invalid proposal.
  python3 feedback_report.py --sent STATE_DIR PERIOD_END
      Record that the report for PERIOD_END was emailed: the next period starts after it.

PROPOSALS.json (written by Claude, references/feedback.md):
  {'summary': '2-4 sentences on the period',
   'proposals': [
     {'kind': 'boost_add', 'topic': '...', 'weeks': 6, 'why': '...', 'evidence': [id, ...]},
     {'kind': 'boost_remove', 'topic': '<exact topic of an existing row>', 'why': '...'},
     {'kind': 'profile_edit', 'old': '<text found exactly once in interest_profile.md>',
      'new': '...', 'why': '...', 'evidence': [id, ...]}]}
"""

import html
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path


# ---- configuration ----------------------------------------------------------------------------
MAX_PROPOSALS = 8
MAX_MISSED_SHOWN = 15
KINDS = ('boost_add', 'boost_remove', 'profile_edit')
REPLY_HINT = ('Reply "apply 1 3", "apply all" or "none" (processed the next morning). Nothing '
              'changes until you reply.')
STYLE = {
    'body': 'font-family:Georgia,serif;color:#1d1d1b;max-width:680px;margin:0 auto;'
            'padding:12px;line-height:1.45;',
    'h2': 'font-size:16px;margin:22px 0 6px;border-bottom:1px solid #ddd;padding-bottom:3px;',
    'meta': 'color:#6b6a65;font-size:12px;',
    'p': 'font-size:14px;margin:6px 0;',
    'old': 'font-size:13px;background:#fbeaea;padding:4px 6px;white-space:pre-wrap;',
    'new': 'font-size:13px;background:#e9f5ea;padding:4px 6px;white-space:pre-wrap;',
    'foot': 'color:#9a9893;font-size:11px;margin-top:18px;border-top:1px solid #eee;'
            'padding-top:6px;',
}


# ---- helpers ----------------------------------------------------------------------------------
def _span(start, end):
    """'Nov 25 – Dec 31 2026' from two ISO dates."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return f'{a:%b} {a.day} – {b:%b} {b.day} {b.year}'


def _boost_topics(skill_dir):
    rows = []
    for line in (skill_dir/'references'/'recent_boost.md').read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) == 3 and cells[2][:2] == '20':
            rows.append(cells[0])
    return rows


def validate(props, data, skill_dir):
    """Normalised, numbered proposals; ValueError on the first invalid one."""
    known = ({s['id'] for s in data['saved']} | {u['id'] for u in data['unsaved_top']}
             | {u['id'] for u in data['unsaved_also']})
    profile = (skill_dir/'references'/'interest_profile.md').read_text()
    topics = _boost_topics(skill_dir)
    items = props.get('proposals', [])
    if not isinstance(props.get('summary'), str) or not props['summary'].strip():
        raise ValueError('PROPOSALS.json needs a non-empty "summary"')
    if len(items) > MAX_PROPOSALS:
        raise ValueError(f'{len(items)} proposals; at most {MAX_PROPOSALS}, keep the strongest')

    out = []
    for k, p in enumerate(items, start=1):
        kind, why = p.get('kind'), (p.get('why') or '').strip()
        if kind not in KINDS:
            raise ValueError(f'proposal {k}: kind {kind!r}; choose one of {KINDS}')
        if not why:
            raise ValueError(f'proposal {k}: "why" is empty')
        bad = [e for e in p.get('evidence', []) if e not in known]
        if bad:
            raise ValueError(f'proposal {k}: evidence ids not in this period\'s data: {bad}')
        row = {'k': k, 'kind': kind, 'why': why, 'evidence': p.get('evidence', [])}

        if kind == 'boost_add':
            topic, weeks = (p.get('topic') or '').strip(), int(p.get('weeks', 6))
            if not topic or len(topic) > 100 or '|' in topic:
                raise ValueError(f'proposal {k}: topic must be 1-100 chars without "|"')
            if not 2 <= weeks <= 12:
                raise ValueError(f'proposal {k}: weeks = {weeks}; choose 2-12')
            row.update(topic=topic, weeks=weeks)
        elif kind == 'boost_remove':
            if p.get('topic') not in topics:
                raise ValueError(f'proposal {k}: no boost row with topic {p.get("topic")!r}; '
                                 f'rows are {topics}')
            row['topic'] = p['topic']
        else:
            old, new = p.get('old', ''), p.get('new', '')
            if profile.count(old) != 1 or not old.strip():
                raise ValueError(f'proposal {k}: "old" must occur exactly once in '
                                 f'interest_profile.md (found {profile.count(old)} times)')
            if not new.strip() or new == old or len(new) > 2000:
                raise ValueError(f'proposal {k}: "new" must be non-empty, differ from "old" '
                                 f'and be under 2000 chars')
            row.update(old=old, new=new)
        out.append(row)
    return out


def _proposal_text(p):
    if p['kind'] == 'boost_add':
        return f'Boost for {p["weeks"]} weeks: {p["topic"]}'
    if p['kind'] == 'boost_remove':
        return f'Remove boost: {p["topic"]}'
    return 'Edit interest_profile.md'


# ---- driver -----------------------------------------------------------------------------------
if len(sys.argv) == 4 and sys.argv[1] == '--sent':
    state_dir, period_end = Path(sys.argv[2]), sys.argv[3]
    if not (state_dir/period_end/'proposals.json').exists():
        raise FileNotFoundError(f'no report for {period_end} in {state_dir}')
    sent_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    (state_dir/'last_report.json').write_text(json.dumps({'period_end': period_end,
                                                          'sent_at': sent_at}))
    print(f'recorded report {period_end} as sent at {sent_at}')
    sys.exit(0)
if len(sys.argv) != 5:
    raise SystemExit('usage: python3 feedback_report.py DATA.json PROPOSALS.json SKILL_DIR '
                     'STATE_DIR\n       python3 feedback_report.py --sent STATE_DIR PERIOD_END')

data = json.loads(Path(sys.argv[1]).read_text())
props = json.loads(Path(sys.argv[2]).read_text())
skill_dir, state_dir = Path(sys.argv[3]), Path(sys.argv[4])
if data.get('skip'):
    raise ValueError(f'DATA.json says skip: {data["reason"]}')

items = validate(props, data, skill_dir)
period, m = data['period'], data['metrics']
out_dir = state_dir/period['end']
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir/'proposals.json').write_text(json.dumps(
    {'period': period, 'summary': props['summary'].strip(), 'proposals': items, 'applied': {}},
    indent=1))

# --- Numbers ---
span = _span(period['start'], period['end'])
subject = f'arXiv feedback for Gab [{span}]'
lines_numbers = [
    f'{period["digests"]} digests ({span}). You saved {m["saves"]} papers '
    f'({m["saves_reply"]} by email reply, {m["saves_manual"]} added by hand).',
    f'Of the {m["saves_scored"]} saved papers the digests could have caught: {m["saved_in_top"]} '
    f'in a top list, {m["saved_in_also"]} in an also list, {m["missed"]} missed '
    f'(recall {m["recall_top"]:.0%} top, {m["recall_top_also"]:.0%} top + also).',
    f'Top picks saved: {m["top_picks_saved"]}/{m["top_picks"]}; also picks saved: '
    f'{m["also_picks_saved"]}/{m["also_picks"]}. By tier: '
    + ', '.join(f'{t} {v["saved"]}/{v["picks"]}' for t, v in sorted(data['top_by_tier'].items()))
    + '.',
]
missed = [s for s in data['saved'] if s['status'] == 'missed']
expired = [b['topic'] for b in data['boosts'] if b['expired']]

# --- Plain text ---
text = [subject, '', props['summary'].strip(), '', 'NUMBERS', ''] + lines_numbers + ['']
if missed:
    text += [f'MISSED ({len(missed)})', '']
    text += [f'- {s["title"]} ({", ".join(s["categories"][:2])}) https://arxiv.org/abs/{s["id"]}'
             for s in missed[:MAX_MISSED_SHOWN]] + ['']
text += ['PROPOSED CHANGES', '']
if not items:
    text += ['None this period.', '']
for p in items:
    text += [f'{p["k"]}. {_proposal_text(p)}', f'   Why: {p["why"]}']
    if p['evidence']:
        text.append(f'   Evidence: {", ".join(p["evidence"])}')
    if p['kind'] == 'profile_edit':
        text += ['   Replace:', *['     ' + x for x in p['old'].splitlines()],
                 '   With:', *['     ' + x for x in p['new'].splitlines()]]
    text.append('')
if expired:
    text += [f'Expired boosts, dropped at the next apply: {"; ".join(expired)}', '']
text += ['--', REPLY_HINT, f'arxiv-feedback-report: {period["end"]}', '']
(out_dir/'report.txt').write_text('\n'.join(text))

# --- HTML ---
esc = html.escape
parts = [f'<div style="{STYLE["body"]}">',
         f'<div style="{STYLE["meta"]}">gm-arxiv-daily monthly feedback &middot; {esc(span)}</div>',
         f'<p style="{STYLE["p"]}">{esc(props["summary"].strip())}</p>',
         f'<h2 style="{STYLE["h2"]}">Numbers</h2>']
parts += [f'<p style="{STYLE["p"]}">{esc(x)}</p>' for x in lines_numbers]
if missed:
    parts.append(f'<h2 style="{STYLE["h2"]}">Missed ({len(missed)})</h2>')
    parts += [f'<p style="{STYLE["p"]}"><a href="https://arxiv.org/abs/{s["id"]}">'
              f'{esc(s["title"])}</a> <span style="{STYLE["meta"]}">'
              f'{esc(", ".join(s["categories"][:2]))}</span></p>'
              for s in missed[:MAX_MISSED_SHOWN]]
parts.append(f'<h2 style="{STYLE["h2"]}">Proposed changes</h2>')
if not items:
    parts.append(f'<p style="{STYLE["p"]}">None this period.</p>')
for p in items:
    parts.append(f'<p style="{STYLE["p"]}"><b>{p["k"]}. {esc(_proposal_text(p))}</b><br>'
                 f'{esc(p["why"])}' + (f'<br><span style="{STYLE["meta"]}">Evidence: '
                                       f'{esc(", ".join(p["evidence"]))}</span>'
                                       if p['evidence'] else '') + '</p>')
    if p['kind'] == 'profile_edit':
        parts += [f'<div style="{STYLE["old"]}">{esc(p["old"])}</div>',
                  f'<div style="{STYLE["new"]}">{esc(p["new"])}</div>']
if expired:
    parts.append(f'<p style="{STYLE["meta"]}">Expired boosts, dropped at the next apply: '
                 f'{esc("; ".join(expired))}</p>')
parts.append(f'<div style="{STYLE["foot"]}">{esc(REPLY_HINT, quote=False)}<br>'
             f'arxiv-feedback-report: {period["end"]}</div></div>')
(out_dir/'report.html').write_text('\n'.join(parts))
print(subject)
