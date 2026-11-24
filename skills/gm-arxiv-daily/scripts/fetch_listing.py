"""
Fetch the latest arXiv mailing for astro-ph.CO + hep-ph (new submissions and cross-lists).

Stages:
  1. Read arXiv's RSS feed for the two categories; it holds exactly the most recent mailing.
  2. Keep announce types 'new' and 'cross' (replacements are dropped) and de-duplicate.
  3. Enrich every entry from the arXiv API (full author list, primary category, comments).

Usage:
  python3 fetch_listing.py [OUT.json]       default OUT: ./arxiv_listing_<mailing date>.json

Output: JSON {'mailing': 'YYYY-MM-DD', 'fetched': ISO time, 'n': int, 'papers': [...]} with
papers in feed order, each {'n', 'id', 'title', 'authors', 'primary', 'categories',
'announce_type', 'abstract', 'comment'}. 'n' is the 1-based position used in rankings.

Standard library only, so it runs in a bare cloud environment.
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


# ---- configuration ----------------------------------------------------------------------------
CATEGORIES = ['astro-ph.CO', 'hep-ph']
KEEP_TYPES = ('new', 'cross')
RSS_URL = 'https://rss.arxiv.org/rss/' + '+'.join(CATEGORIES)
API_URL = 'https://export.arxiv.org/api/query'
USER_AGENT = 'arxiv_daily/0.2 (personal reading-list tool)'
MAX_TRIES = 5
MIN_PAPERS = 5  # fewer than this in a weekday mailing means the feed is broken

ATOM = '{http://www.w3.org/2005/Atom}'
ARX = '{http://arxiv.org/schemas/atom}'
NS = {'arxiv': 'http://arxiv.org/schemas/atom', 'dc': 'http://purl.org/dc/elements/1.1/'}


# ---- helpers ----------------------------------------------------------------------------------
def _clean(s):
    """Collapse whitespace; None -> ''."""
    return re.sub(r'\s+', ' ', (s or '').strip())


def _get(url):
    """GET with back-off for arXiv's transient 503 / 429 responses; returns bytes."""
    for attempt in range(MAX_TRIES):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception as exc:  # network errors of any kind are retried
            print(f'  request failed (try {attempt+1}/{MAX_TRIES}): {exc}', file=sys.stderr)
            time.sleep(10*(attempt+1))
    raise RuntimeError(f'{url} failed {MAX_TRIES} times')


def read_feed():
    """Mailing date and the new + cross entries of the latest RSS mailing."""
    channel = ET.fromstring(_get(RSS_URL)).find('channel')
    mailing = parsedate_to_datetime(channel.findtext('pubDate')).date().isoformat()

    papers, seen = [], set()
    for item in channel.findall('item'):
        kind = item.findtext('arxiv:announce_type', namespaces=NS)
        key = re.sub(r'v\d+$', '', item.findtext('link').rsplit('/', 1)[-1])
        if kind not in KEEP_TYPES or key in seen:
            continue
        seen.add(key)
        desc = item.findtext('description') or ''
        papers.append({
            'id': key,
            'title': _clean(item.findtext('title')),
            'authors': [a.strip() for a in
                        _clean(item.findtext('dc:creator', namespaces=NS)).split(',') if a],
            'primary': '',
            'categories': [c.text for c in item.findall('category')],
            'announce_type': kind,
            'abstract': _clean(desc.split('Abstract:', 1)[-1]),
            'comment': '',
        })
    return mailing, papers


def enrich(papers, chunk=100):
    """Fill authors, primary category and comments from the arXiv API (in place)."""
    by_id = {p['id']: p for p in papers}
    ids = list(by_id)
    for start in range(0, len(ids), chunk):
        query = urllib.parse.urlencode({'id_list': ','.join(ids[start:start+chunk]),
                                        'max_results': chunk})
        root = ET.fromstring(_get(f'{API_URL}?{query}'))
        for entry in root.findall(ATOM + 'entry'):
            key = re.sub(r'v\d+$', '', _clean(entry.findtext(ATOM + 'id')).rsplit('/', 1)[-1])
            if key not in by_id:
                continue
            p = by_id[key]
            primary = entry.find(ARX + 'primary_category')
            p['primary'] = primary.get('term', '') if primary is not None else p['categories'][0]
            p['authors'] = [_clean(a.findtext(ATOM + 'name'))
                            for a in entry.findall(ATOM + 'author')] or p['authors']
            p['comment'] = _clean(entry.findtext(ARX + 'comment'))
        time.sleep(3)  # arXiv API policy: <= 1 request / 3 s

    for p in papers:
        if not p['primary']:  # API missed it: the feed lists the primary first
            p['primary'] = p['categories'][0] if p['categories'] else ''


# ---- driver -----------------------------------------------------------------------------------
mailing, papers = read_feed()
if len(papers) < MIN_PAPERS:
    raise RuntimeError(f'only {len(papers)} new/cross papers in the feed for {mailing}; '
                       f'check {RSS_URL}')
enrich(papers)
for i in range(len(papers)):
    papers[i] = {'n': i+1, **papers[i]}

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(f'arxiv_listing_{mailing}.json')
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({'mailing': mailing,
                           'fetched': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                           'n': len(papers), 'papers': papers}, indent=1))
n_new = sum(p['announce_type'] == 'new' for p in papers)
print(f'mailing {mailing}: {len(papers)} papers ({n_new} new, {len(papers)-n_new} cross) -> {out}')
