"""
Render a ranking JSON as the daily email (HTML + plain text).

Usage:
  python3 render_email.py RANKING.json LISTING.json

Input:
  RANKING.json  {'mailing', 'top': [{'n', 'reason', 'tier'}], 'also': [{'n', 'reason', 'tier',
                'wildcard'}], 'note': optional one-line comment on the day}
  LISTING.json  output of fetch_listing.py (titles, authors, categories come from here, so the
                ranking cannot misquote them)

Output: RANKING.html and RANKING.txt next to RANKING.json, and the subject line on stdout.
"""

import html
import json
import sys
from datetime import date
from pathlib import Path


# ---- configuration ----------------------------------------------------------------------------
MAX_AUTHORS = 4
TIER_LABEL = {'1a': 'your research', '1b': 'core', 'release': 'data release', '2': 'interest',
              '3': 'broad'}
STYLE = {
    'body': 'font-family:Georgia,serif;color:#1d1d1b;max-width:680px;margin:0 auto;'
            'padding:12px;line-height:1.45;',
    'h2': 'font-size:16px;margin:22px 0 6px;border-bottom:1px solid #ddd;padding-bottom:3px;',
    'item': 'margin:0 0 12px;',
    'title': 'color:#2f5d8a;text-decoration:none;font-weight:bold;font-size:15px;',
    'meta': 'color:#6b6a65;font-size:12px;',
    'reason': 'font-size:14px;margin-top:2px;',
    'tag': 'font-size:11px;color:#6b6a65;border:1px solid #ccc;border-radius:3px;'
           'padding:0 4px;margin-left:4px;',
}


# ---- helpers ----------------------------------------------------------------------------------
def _authors(names):
    """'A, B, C, D et al.' with at most MAX_AUTHORS names."""
    head = ', '.join(names[:MAX_AUTHORS])
    return head + (' et al.' if len(names) > MAX_AUTHORS else '')


def _entries(picks, papers, kind):
    """Join ranking picks with listing metadata; fail loudly on a bad index."""
    rows = []
    for pick in picks:
        n = pick['n']
        if not 1 <= n <= len(papers) or papers[n-1]['n'] != n:
            raise ValueError(f'{kind} pick n={n} is not in the listing (1..{len(papers)})')
        rows.append({**papers[n-1], **pick})
    return rows


def _html_item(r):
    tag = 'wildcard' if r.get('wildcard') else TIER_LABEL.get(str(r.get('tier')), '')
    cross = ' &middot; cross-list' if r['announce_type'] == 'cross' else ''
    return (f'<div style="{STYLE["item"]}"><a style="{STYLE["title"]}" '
            f'href="https://arxiv.org/abs/{r["id"]}">{html.escape(r["title"])}</a>'
            f'<span style="{STYLE["tag"]}">{tag}</span>'
            f'<div style="{STYLE["meta"]}">{html.escape(_authors(r["authors"]))} &middot; '
            f'{r["primary"]}{cross} &middot; <a href="https://arxiv.org/pdf/{r["id"]}">pdf</a>'
            f'</div><div style="{STYLE["reason"]}">{html.escape(r["reason"])}</div></div>')


def _text_item(i, r):
    tag = ' [wildcard]' if r.get('wildcard') else ''
    return (f'{i}. {r["title"]}{tag}\n   {_authors(r["authors"])} ({r["primary"]})\n'
            f'   https://arxiv.org/abs/{r["id"]}\n   {r["reason"]}\n')


# ---- driver -----------------------------------------------------------------------------------
if len(sys.argv) != 3:
    raise SystemExit('usage: python3 render_email.py RANKING.json LISTING.json')
rank_path = Path(sys.argv[1])
ranking = json.loads(rank_path.read_text())
listing = json.loads(Path(sys.argv[2]).read_text())
if ranking['mailing'] != listing['mailing']:
    raise ValueError(f'ranking is for {ranking["mailing"]}, listing for {listing["mailing"]}')

papers = listing['papers']
top = _entries(ranking['top'], papers, 'top')
also = _entries(ranking.get('also', []), papers, 'also')
if len(top) > 5:
    raise ValueError(f'top list has {len(top)} papers; the profile allows at most 5')

mail_day = date.fromisoformat(listing['mailing'])
subject = f'Daily Arxiv Picks for Gab [{mail_day:%a}, {mail_day:%b} {mail_day.day} {mail_day.year}]'
counts = f'{len(top)} top + {len(also)} also from {listing["n"]} new and cross-listed papers'

note = ranking.get('note', '')
parts = [f'<div style="{STYLE["body"]}">',
         f'<div style="{STYLE["meta"]}">astro-ph.CO + hep-ph &middot; mailing '
         f'{listing["mailing"]} &middot; {counts}</div>']
if note:
    parts.append(f'<p style="{STYLE["reason"]}">{html.escape(note)}</p>')
parts.append(f'<h2 style="{STYLE["h2"]}">Top {len(top)}</h2>')
parts += [_html_item(r) for r in top]
if also:
    parts.append(f'<h2 style="{STYLE["h2"]}">Also interesting</h2>')
    parts += [_html_item(r) for r in also]
parts.append('</div>')
rank_path.with_suffix('.html').write_text('\n'.join(parts))

text = [subject, counts, '', note, '', f'TOP {len(top)}', '']
text += [_text_item(i+1, top[i]) for i in range(len(top))]
if also:
    text += ['ALSO INTERESTING', '']
    text += [_text_item(i+1, also[i]) for i in range(len(also))]
rank_path.with_suffix('.txt').write_text('\n'.join(text))
print(subject)
