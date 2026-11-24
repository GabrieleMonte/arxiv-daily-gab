"""Enumerate open Google Chrome tabs and resolve them to papers."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional

# Field / record separators chosen so they can never appear in a page title.
FS = "\x01"
RS = "\x02"

_APPLESCRIPT = f'''
tell application "Google Chrome"
    set out to ""
    set widx to 0
    repeat with w in windows
        set widx to widx + 1
        set tidx to 0
        repeat with t in tabs of w
            set tidx to tidx + 1
            set out to out & widx & "{FS}" & tidx & "{FS}" & (title of t) & "{FS}" & (URL of t) & "{RS}"
        end repeat
    end repeat
    return out
end tell
'''

# 2608.05032, 2608.05032v2  (new style, Apr 2007 onwards)
_NEW_ID = r"\d{4}\.\d{4,5}(?:v\d+)?"
# hep-th/9711200, astro-ph.CO/0601001v3  (old style)
_OLD_ID = r"[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"

# Hosts that mirror arXiv content under a recognisable path.
_ARXIV_URL = re.compile(
    r"(?:arxiv\.org|ar5iv\.(?:labs\.)?arxiv\.org|alphaxiv\.org|browse\.arxiv\.org)"
    r"/(?:abs|pdf|html|format|ps)/(" + _NEW_ID + r"|" + _OLD_ID + r")",
    re.IGNORECASE,
)
_HF_URL = re.compile(r"huggingface\.co/papers/(" + _NEW_ID + r")", re.IGNORECASE)
_BARE_ID = re.compile(r"^(?:arxiv:)?(" + _NEW_ID + r"|" + _OLD_ID + r")$", re.IGNORECASE)

# Listing / search / profile pages carry no single paper.
_NOT_A_PAPER = re.compile(r"arxiv\.org/(?:list|find|search|archive|category_taxonomy)", re.I)


@dataclass
class Tab:
    window: int
    index: int
    title: str
    url: str


@dataclass
class Target:
    """A tab resolved to something we can actually read."""

    kind: str  # "arxiv" or "pdf"
    key: str  # arXiv id (version stripped) or the raw URL for plain PDFs
    url: str  # canonical source URL
    tab: Optional[Tab] = None

    @property
    def base_id(self) -> str:
        return re.sub(r"v\d+$", "", self.key)


def _run_applescript(script: str) -> str:
    proc = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "Application isn't running" in err or "-600" in err:
            raise RuntimeError("Google Chrome does not appear to be running.")
        raise RuntimeError(f"osascript failed: {err}")
    return proc.stdout


def list_tabs(window: Optional[int] = None) -> List[Tab]:
    """Every open tab in every Chrome window, in on-screen order."""
    raw = _run_applescript(_APPLESCRIPT)
    tabs: List[Tab] = []
    for record in raw.split(RS):
        record = record.strip("\n\r")
        if not record:
            continue
        parts = record.split(FS)
        if len(parts) != 4:
            continue
        w, i, title, url = parts
        try:
            tab = Tab(window=int(w), index=int(i), title=title.strip(), url=url.strip())
        except ValueError:
            continue
        if window is not None and tab.window != window:
            continue
        tabs.append(tab)
    return tabs


def arxiv_id_from_url(url: str) -> Optional[str]:
    """Pull an arXiv identifier out of a URL, or None if there isn't one."""
    if _NOT_A_PAPER.search(url):
        return None
    m = _ARXIV_URL.search(url) or _HF_URL.search(url)
    if not m:
        return None
    ident = m.group(1)
    # /pdf/2608.05032.pdf leaves a trailing extension on the match in some forms
    return ident[:-4] if ident.lower().endswith(".pdf") else ident


def resolve(tabs: List[Tab], include_pdf_urls: bool = False) -> tuple:
    """Split tabs into (targets, skipped). Duplicate papers collapse to one target."""
    targets: List[Target] = []
    skipped: List[Tab] = []
    seen = set()

    for tab in tabs:
        ident = arxiv_id_from_url(tab.url)
        if ident:
            base = re.sub(r"v\d+$", "", ident)
            if base in seen:
                continue
            seen.add(base)
            targets.append(
                Target(kind="arxiv", key=ident, url=f"https://arxiv.org/abs/{ident}", tab=tab)
            )
        elif include_pdf_urls and tab.url.lower().split("?")[0].endswith(".pdf"):
            if tab.url in seen:
                continue
            seen.add(tab.url)
            targets.append(Target(kind="pdf", key=tab.url, url=tab.url, tab=tab))
        else:
            skipped.append(tab)

    return targets, skipped


def targets_from_ids(raw_ids: List[str]) -> List[Target]:
    """Build targets from explicit ids/URLs, bypassing Chrome entirely."""
    targets: List[Target] = []
    seen = set()
    for item in raw_ids:
        item = item.strip()
        if not item:
            continue
        m = _BARE_ID.match(item)
        ident = m.group(1) if m else arxiv_id_from_url(item)
        if ident:
            base = re.sub(r"v\d+$", "", ident)
            if base in seen:
                continue
            seen.add(base)
            targets.append(
                Target(kind="arxiv", key=ident, url=f"https://arxiv.org/abs/{ident}")
            )
        elif item.lower().startswith("http"):
            if item in seen:
                continue
            seen.add(item)
            targets.append(Target(kind="pdf", key=item, url=item))
    return targets
