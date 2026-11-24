"""
Add arXiv papers to the website's reading list and publish them with git.

Runs where git can push to GabrieleMonte.github.io with the user's own credentials (Gabriele's
Mac; the cloud sandbox refuses pushes to repos not attached to the session).

Stages:
  1. Sync SITE_DIR to origin/BRANCH. SITE_DIR must be a clone dedicated to this job, marked by
     an empty file SITE_DIR/.git/arxiv-daily-clone, because local changes there are discarded.
  2. Run add_reads.py on the ids (same folder as this script).
  3. If a reads page changed: commit only arxiv_reads/*_reads.html and push. A rejected push
     (someone pushed in between, e.g. the Strava CI) is rebased onto origin and retried once.

Usage:
  python3 save_reads.py SITE_DIR BRANCH ID [ID ...]

Output: add_reads' progress lines, then as the LAST stdout line one JSON object with
add_reads' keys ('added', 'already_listed', 'invalid', 'not_found') plus 'branch', 'commit'
(short hash, '' when nothing was committed) and 'error' ('' on success). Exit status 1 on error.
"""

import json
import subprocess
import sys
from pathlib import Path


# ---- configuration ----------------------------------------------------------------------------
MARKER = 'arxiv-daily-clone'  # file in SITE_DIR/.git that marks a disposable clone
ADD_READS = Path(__file__).resolve().parent/'add_reads.py'
READS_GLOB = 'arxiv_reads/*_reads.html'


# ---- helpers ----------------------------------------------------------------------------------
def _git(site, *args, check=True):
    """Run one git command in the site clone and return its completed process."""
    return subprocess.run(['git', '-C', str(site), *args], capture_output=True, text=True,
                          check=check)


def _finish(result, code):
    print(json.dumps(result))
    sys.exit(code)


# ---- driver -----------------------------------------------------------------------------------
if len(sys.argv) < 4:
    raise SystemExit('usage: python3 save_reads.py SITE_DIR BRANCH ID [ID ...]')
site, branch, ids = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
result = {'added': [], 'already_listed': [], 'invalid': [], 'not_found': [], 'branch': branch,
          'commit': '', 'error': ''}

if not (site/'.git'/MARKER).exists():
    result['error'] = (f'{site} is not a dedicated clone (no .git/{MARKER}); refusing to reset '
                       f'it. Clone the site there and create that empty file.')
    _finish(result, 1)

# --- Stage 1: sync to origin/BRANCH ---
_git(site, 'rebase', '--abort', check=False)  # a previous run may have stopped mid-rebase
try:
    _git(site, 'fetch', '-q', 'origin', branch)
    _git(site, 'checkout', '-q', '-B', branch, f'origin/{branch}')
    _git(site, 'reset', '-q', '--hard', f'origin/{branch}')
except subprocess.CalledProcessError as exc:
    result['error'] = f'sync with origin/{branch} failed: {exc.stderr.strip()[:300]}'
    _finish(result, 1)

# --- Stage 2: add the papers ---
proc = subprocess.run([sys.executable, '-I', str(ADD_READS), str(site), *ids],
                      capture_output=True, text=True)
lines = proc.stdout.strip().splitlines()
print('\n'.join(lines[:-1]))
try:
    result.update(json.loads(lines[-1]))
except (IndexError, json.JSONDecodeError):
    result['error'] = f'add_reads.py failed: {(proc.stderr or proc.stdout).strip()[-300:]}'
    _finish(result, 1)

# --- Stage 3: commit and push the reads pages only ---
changed = _git(site, 'status', '--porcelain', '--', READS_GLOB).stdout.strip()
if not result['added'] or not changed:
    _finish(result, 0)

n = len(result['added'])
message = f'arxiv_reads: add {n} paper{"s" if n > 1 else ""} from email reply'
try:
    _git(site, 'add', '--', READS_GLOB)
    _git(site, 'commit', '-q', '-m', message, '--', READS_GLOB)
    push = _git(site, 'push', '-q', 'origin', f'HEAD:{branch}', check=False)
    if push.returncode != 0:  # remote moved on: replay our commit on top and try once more
        _git(site, 'pull', '-q', '--rebase', 'origin', branch)
        _git(site, 'push', '-q', 'origin', f'HEAD:{branch}')
except subprocess.CalledProcessError as exc:
    result['error'] = f'commit/push failed: {exc.stderr.strip()[:300]}'
    _finish(result, 1)

result['commit'] = _git(site, 'rev-parse', '--short', 'HEAD').stdout.strip()
_finish(result, 0)
