"""
Apply approved monthly-feedback proposals to the gm-arxiv-daily references and publish them.

Stages:
  1. Load STATE_DIR/<PERIOD_END>/proposals.json (feedback_report.py) and pick the approved
     numbers; ones already applied are skipped.
  2. Sync CONFIG_DIR (a dedicated clone of claude-config) to origin/main, edit
     references/recent_boost.md and references/interest_profile.md, drop expired boost rows,
     commit and push.
  3. Sync MIRROR_DIR (a dedicated clone of the public mirror) to origin/main, copy the skill
     folder over, commit and push, so the next morning's routine ranks with the change.
  Both clones must carry the marker file .git/arxiv-daily-clone: local changes there are reset.

Usage:
  python3 apply_feedback.py [--branch B] STATE_DIR PERIOD_END CONFIG_DIR MIRROR_DIR all|K [K ...]
  (--branch: publish to branch B of both repos instead of main; for tests)

Output: progress on stderr; as the LAST stdout line one JSON object {'applied': [k],
'already_applied': [k], 'pruned_boosts': [topic], 'commit', 'mirror_commit', 'error'}.
Exit status 1 on error (nothing is applied when any selected proposal no longer fits).
"""

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path


# ---- configuration ----------------------------------------------------------------------------
MARKER = 'arxiv-daily-clone'
SKILL_REL = Path('skills')/'gm-arxiv-daily'
BRANCH = 'main'
EMPTY_ROW = '| (none yet) | | |'


# ---- helpers ----------------------------------------------------------------------------------
def _git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True,
                          check=check)


def _finish(result, code):
    print(json.dumps(result))
    sys.exit(code)


def _sync(repo):
    _git(repo, 'rebase', '--abort', check=False)
    _git(repo, 'fetch', '-q', 'origin')
    base = BRANCH if _git(repo, 'rev-parse', '-q', '--verify', f'origin/{BRANCH}',
                          check=False).returncode == 0 else 'main'
    _git(repo, 'checkout', '-q', '-B', BRANCH, f'origin/{base}')
    _git(repo, 'reset', '-q', '--hard', f'origin/{base}')


def _commit_push(repo, message):
    """Commit everything staged under the skill folder and push; returns the short hash."""
    _git(repo, 'add', '-A', '--', str(SKILL_REL))
    if _git(repo, 'diff', '--cached', '--quiet', check=False).returncode == 0:
        return ''
    _git(repo, 'commit', '-q', '-m', message)
    if _git(repo, 'push', '-q', 'origin', f'HEAD:refs/heads/{BRANCH}',
            check=False).returncode != 0:
        _git(repo, 'pull', '-q', '--rebase', 'origin', BRANCH)
        _git(repo, 'push', '-q', 'origin', f'HEAD:refs/heads/{BRANCH}')
    return _git(repo, 'rev-parse', '--short', 'HEAD').stdout.strip()


def edit_boosts(text, add, remove, today):
    """recent_boost.md with rows added/removed and expired rows dropped; returns (text, pruned)."""
    lines = text.splitlines()
    head = next(i for i, x in enumerate(lines) if x.startswith('| Topic |'))
    end = head + 2
    while end < len(lines) and lines[end].startswith('|'):
        end += 1

    kept, pruned = [], []
    for row in lines[head+2:end]:
        cells = [c.strip() for c in row.strip().strip('|').split('|')]
        if row.strip() == EMPTY_ROW or cells[0] in remove:
            continue
        if cells[2] < today:
            pruned.append(cells[0])
            continue
        kept.append(row)
    kept += add
    lines[head+2:end] = kept or [EMPTY_ROW]
    return '\n'.join(lines) + '\n', pruned


# ---- driver -----------------------------------------------------------------------------------
if len(sys.argv) > 2 and sys.argv[1] == '--branch':  # tests publish to a throwaway branch
    BRANCH = sys.argv[2]
    del sys.argv[1:3]
if len(sys.argv) < 6:
    raise SystemExit('usage: python3 apply_feedback.py [--branch B] STATE_DIR PERIOD_END '
                     'CONFIG_DIR MIRROR_DIR all|K [K ...]')
state_dir, period_end = Path(sys.argv[1]), sys.argv[2]
config_dir, mirror_dir = Path(sys.argv[3]), Path(sys.argv[4])
result = {'applied': [], 'already_applied': [], 'pruned_boosts': [], 'commit': '',
          'mirror_commit': '', 'error': ''}

for repo in (config_dir, mirror_dir):
    if not (repo/'.git'/MARKER).exists():
        result['error'] = f'{repo} is not a dedicated clone (no .git/{MARKER}); refusing'
        _finish(result, 1)

# --- Stage 1: proposals ---
prop_path = state_dir/period_end/'proposals.json'
if not prop_path.exists():
    result['error'] = f'no proposals for report {period_end} ({prop_path})'
    _finish(result, 1)
record = json.loads(prop_path.read_text())
by_k = {p['k']: p for p in record['proposals']}
if sys.argv[5:] == ['all']:
    wanted = sorted(by_k)
else:
    try:
        wanted = sorted({int(x) for x in sys.argv[5:]})
    except ValueError:
        result['error'] = f'proposal numbers must be integers or "all": {sys.argv[5:]}'
        _finish(result, 1)
unknown = [k for k in wanted if k not in by_k]
if unknown:
    result['error'] = f'report {period_end} has no proposal(s) {unknown} (1..{len(by_k)})'
    _finish(result, 1)
result['already_applied'] = [k for k in wanted if str(k) in record['applied']]
todo = [by_k[k] for k in wanted if str(k) not in record['applied']]
if not todo:
    _finish(result, 0)

# --- Stage 2: edit claude-config ---
try:
    _sync(config_dir)
except subprocess.CalledProcessError as exc:
    result['error'] = f'syncing {config_dir} failed: {exc.stderr.strip()[:300]}'
    _finish(result, 1)
refs = config_dir/SKILL_REL/'references'
profile = (refs/'interest_profile.md').read_text()
today = date.today()
add, remove = [], set()
for p in todo:
    if p['kind'] == 'profile_edit':
        if profile.count(p['old']) != 1:
            result['error'] = (f'proposal {p["k"]}: the profile text it replaces changed since '
                               f'the report; nothing applied')
            _finish(result, 1)
        profile = profile.replace(p['old'], p['new'])
    elif p['kind'] == 'boost_add':
        expires = (today + timedelta(weeks=p['weeks'])).isoformat()
        evidence = ', '.join(p['evidence'][:6]) or 'monthly feedback'
        add.append(f'| {p["topic"]} | {evidence} | {expires} |')
        remove.add(p['topic'])  # re-adding a topic refreshes its row instead of duplicating it
    else:
        remove.add(p['topic'])
boosts, result['pruned_boosts'] = edit_boosts((refs/'recent_boost.md').read_text(), add, remove,
                                              today.isoformat())
(refs/'interest_profile.md').write_text(profile)
(refs/'recent_boost.md').write_text(boosts)

ks = ', '.join(str(p['k']) for p in todo)
try:
    result['commit'] = _commit_push(config_dir, f'gm-arxiv-daily: monthly feedback {period_end}, '
                                                f'proposal(s) {ks} approved by email')
except subprocess.CalledProcessError as exc:
    result['error'] = f'claude-config commit/push failed: {exc.stderr.strip()[:300]}'
    _finish(result, 1)
for p in todo:
    record['applied'][str(p['k'])] = result['commit']
    result['applied'].append(p['k'])
prop_path.write_text(json.dumps(record, indent=1))

# --- Stage 3: publish to the mirror ---
try:
    _sync(mirror_dir)
    subprocess.run(['rsync', '-a', '--delete', '--exclude', '__pycache__', '--exclude',
                    '.DS_Store', f'{config_dir/SKILL_REL}/', f'{mirror_dir/SKILL_REL}/'],
                   check=True, capture_output=True, text=True)
    result['mirror_commit'] = _commit_push(
        mirror_dir, f'Sync gm-arxiv-daily from claude-config {result["commit"]} (monthly feedback)')
except subprocess.CalledProcessError as exc:
    result['error'] = (f'claude-config updated ({result["commit"]}) but the mirror sync failed: '
                       f'{(exc.stderr or "").strip()[:300]}; run sync_mirror.sh')
    _finish(result, 1)
_finish(result, 0)
