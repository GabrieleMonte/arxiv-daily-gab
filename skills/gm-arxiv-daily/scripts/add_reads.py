"""
Add arXiv papers to the reading list on Gabriele's website (arxiv_reads/<year>_reads.html).

Reuses the website's own arxiv_reads/import_tabs.py (already_recorded, fetch_title,
write_papers, year_month), so entries keep exactly the format of the hand-imported ones: each
paper goes under the month of its arXiv id, newest month first, duplicates are skipped.

Usage:
  python3 add_reads.py SITE_DIR ID [ID ...]

  SITE_DIR  a checkout of GabrieleMonte.github.io
  ID        new-style arXiv ids (2611.27986 or 2611.27986v2); anything else is reported invalid

Output: import_tabs' progress lines, then as the LAST stdout line one JSON object {'added': [[id, title], ...], 'already_listed': [...],
'invalid': [...], 'not_found': [...]}. Exit status 0 even when nothing was added.
"""

import json
import re
import sys
from pathlib import Path


# ---- configuration ----------------------------------------------------------------------------
ID_RE = re.compile(r'^(\d{4}\.\d{4,5})(v\d+)?$')
MAX_IDS = 30  # a reply listing more than this is almost certainly a parsing mistake


# ---- driver -----------------------------------------------------------------------------------
if len(sys.argv) < 3:
    raise SystemExit('usage: python3 add_reads.py SITE_DIR ID [ID ...]')

reads_dir = Path(sys.argv[1])/'arxiv_reads'
if not (reads_dir/'import_tabs.py').exists():
    raise FileNotFoundError(f'{reads_dir}/import_tabs.py not found; is {sys.argv[1]} the site?')
sys.path.insert(0, str(reads_dir))
sys.dont_write_bytecode = True  # no __pycache__ inside the website checkout
import import_tabs  # noqa: E402  (the website's own writer)

raw = sys.argv[2:]
if len(raw) > MAX_IDS:
    raise ValueError(f'{len(raw)} ids given; refusing more than {MAX_IDS} in one call')

ids, invalid = [], []
for token in raw:
    m = ID_RE.match(token.strip())
    if m is None:
        invalid.append(token)
    elif m.group(1) not in ids:
        ids.append(m.group(1))

listed = import_tabs.already_recorded(ids)
added, not_found, by_month = [], [], {}
for key in ids:
    if key in listed:
        continue
    title = import_tabs.fetch_title(key)
    if title is None:  # nonexistent id or arXiv unreachable: never write a placeholder title
        not_found.append(key)
        continue
    added.append([key, title])
    by_month.setdefault(import_tabs.year_month(key), []).append((key, title))

if by_month:
    import_tabs.write_papers(by_month)  # prints its own progress lines to stdout
print(json.dumps({'added': added, 'already_listed': sorted(listed), 'invalid': invalid,
                  'not_found': not_found}))
