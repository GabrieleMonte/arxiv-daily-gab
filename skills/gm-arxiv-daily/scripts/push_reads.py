"""
Publish changed arxiv_reads files to GitHub through the REST contents API.

The cloud sandbox's git proxy refuses `git push` to repos not attached to the session, so the
commit is made with the API instead, authenticated by the fine-grained token in $GH_SITE_TOKEN
(Contents read/write on GabrieleMonte.github.io only).

Stages:
  1. List the arxiv_reads/*_reads.html files changed in SITE_DIR's working tree.
  2. Make sure BRANCH exists (created from master if missing; never for master itself).
  3. For each file: abort if the remote blob differs from the one the edit was based on
     (HEAD of the clone), otherwise PUT the new content as one commit.

Usage:
  python3 push_reads.py SITE_DIR BRANCH "commit message"

Output: progress on stderr; as the LAST stdout line one JSON object
{'branch', 'commits': [{'path', 'sha'}], 'error'} ('error' is '' on success). Exit status 1 on
error. The token is never printed.
"""

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


# ---- configuration ----------------------------------------------------------------------------
REPO = 'GabrieleMonte/GabrieleMonte.github.io'
API = f'https://api.github.com/repos/{REPO}'
AUTHOR = {'name': 'arxiv-daily', 'email': 'gabrielemontef@gmail.com'}


# ---- helpers ----------------------------------------------------------------------------------
def _api(method, url, token, payload=None):
    """One GitHub API call; returns (status, parsed JSON or None)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'arxiv-daily'})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b'null')
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors='replace')[:300]
        return exc.code, {'message': body}


def _git(site, *args):
    return subprocess.run(['git', '-C', site, *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _finish(result, code):
    print(json.dumps(result))
    sys.exit(code)


# ---- driver -----------------------------------------------------------------------------------
if len(sys.argv) != 4:
    raise SystemExit('usage: python3 push_reads.py SITE_DIR BRANCH "commit message"')
site, branch, message = sys.argv[1:]
token = os.environ.get('GH_SITE_TOKEN', '')
result = {'branch': branch, 'commits': [], 'error': ''}
if not token:
    result['error'] = 'GH_SITE_TOKEN is not set'
    _finish(result, 1)

porcelain = _git(site, 'status', '--porcelain', '--', 'arxiv_reads').splitlines()
changed = [line[3:] for line in porcelain if line[3:].endswith('_reads.html')]
if not changed:
    _finish(result, 0)

# --- Branch: create from master if missing ---
status, ref = _api('GET', f'{API}/git/ref/heads/{branch}', token)
if status == 404 and branch != 'master':
    status, master = _api('GET', f'{API}/git/ref/heads/master', token)
    if status != 200:
        result['error'] = f'cannot read master ref: HTTP {status} {master}'
        _finish(result, 1)
    status, ref = _api('POST', f'{API}/git/refs', token,
                       {'ref': f'refs/heads/{branch}', 'sha': master['object']['sha']})
    print(f'created branch {branch}', file=sys.stderr)
if status not in (200, 201):
    result['error'] = f'branch {branch}: HTTP {status} {ref}'
    _finish(result, 1)

# --- One commit per changed file, guarded by the base blob ---
for path in changed:
    try:
        base_blob = _git(site, 'rev-parse', f'HEAD:{path}')
    except subprocess.CalledProcessError:  # new file (e.g. a new year's reads page)
        base_blob = None
    status, remote = _api('GET', f'{API}/contents/{path}?ref={branch}', token)
    remote_blob = remote.get('sha') if status == 200 else None
    if remote_blob != base_blob:
        result['error'] = (f'{path} changed on {branch} since the clone (remote {remote_blob}, '
                           f'base {base_blob}); nothing overwritten')
        _finish(result, 1)

    with open(os.path.join(site, path), 'rb') as f:
        content = base64.b64encode(f.read()).decode()
    payload = {'message': message, 'content': content, 'branch': branch,
               'committer': AUTHOR, 'author': AUTHOR}
    if remote_blob:
        payload['sha'] = remote_blob
    status, put = _api('PUT', f'{API}/contents/{path}', token, payload)
    if status not in (200, 201):
        result['error'] = f'PUT {path}: HTTP {status} {put}'
        _finish(result, 1)
    result['commits'].append({'path': path, 'sha': put['commit']['sha']})
    print(f'committed {path} -> {put["commit"]["sha"][:7]}', file=sys.stderr)

_finish(result, 0)
