"""arXiv metadata lookup, PDF retrieval and text extraction."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from . import extract

try:  # PyMuPDF renamed its import in 1.24
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

ATOM = "{http://www.w3.org/2005/Atom}"
ARX = "{http://arxiv.org/schemas/atom}"

API = "https://export.arxiv.org/api/query"
# arXiv asks that automated clients identify themselves and stay under ~1 req / 3 s.
UA = "arxiv_skimmer/1.0 (personal reading tool; mailto:montefalcone@wisc.edu)"
MIN_INTERVAL = 3.0

_throttle_lock = threading.Lock()
_last_request = [0.0]


def _throttle() -> None:
    """Serialise arXiv hits so we never exceed their published rate."""
    with _throttle_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()


@dataclass
class Paper:
    key: str
    url: str
    title: str = ""
    authors: List[str] = field(default_factory=list)
    abstract: str = ""
    published: str = ""
    updated: str = ""
    primary_category: str = ""
    categories: List[str] = field(default_factory=list)
    comment: str = ""
    journal_ref: str = ""
    doi: str = ""
    version: str = ""
    # filled in later
    text: str = ""
    source: str = ""
    n_pages: int = 0
    truncated: bool = False

    @property
    def abs_url(self) -> str:
        return self.url

    @property
    def display_title(self) -> str:
        return self.title or (self.key if self.key.startswith("http") else f"arXiv:{self.key}")


def _clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def fetch_metadata(ids: List[str], chunk: int = 50, log=print) -> Dict[str, Paper]:
    """Batch-query the arXiv API. Returns {base_id: Paper} for whatever resolved."""
    out: Dict[str, Paper] = {}
    for start in range(0, len(ids), chunk):
        batch = ids[start : start + chunk]
        root = None
        for attempt in range(4):
            _throttle()
            try:
                resp = requests.get(
                    API,
                    params={"id_list": ",".join(batch), "max_results": len(batch)},
                    headers={"User-Agent": UA},
                    timeout=60,
                )
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
                break
            except Exception as exc:  # noqa: BLE001 - metadata is best-effort
                # The API 503s under load and expects the client to back off.
                if attempt == 3:
                    log(f"  ! metadata lookup failed for {len(batch)} ids: {exc}")
                else:
                    time.sleep(2 ** attempt * 3)
        if root is None:
            continue

        for entry in root.findall(ATOM + "entry"):
            raw_id = _clean(entry.findtext(ATOM + "id"))
            m = re.search(r"abs/(.+?)$", raw_id)
            if not m:
                continue
            versioned = m.group(1)
            base = re.sub(r"v\d+$", "", versioned)
            vm = re.search(r"(v\d+)$", versioned)

            paper = Paper(key=base, url=f"https://arxiv.org/abs/{versioned}")
            paper.title = _clean(entry.findtext(ATOM + "title"))
            paper.abstract = _clean(entry.findtext(ATOM + "summary"))
            paper.published = _clean(entry.findtext(ATOM + "published"))[:10]
            paper.updated = _clean(entry.findtext(ATOM + "updated"))[:10]
            paper.version = vm.group(1) if vm else ""
            paper.authors = [
                _clean(a.findtext(ATOM + "name")) for a in entry.findall(ATOM + "author")
            ]
            pc = entry.find(ARX + "primary_category")
            paper.primary_category = pc.get("term", "") if pc is not None else ""
            paper.categories = [c.get("term", "") for c in entry.findall(ATOM + "category")]
            paper.comment = _clean(entry.findtext(ARX + "comment"))
            paper.journal_ref = _clean(entry.findtext(ARX + "journal_ref"))
            paper.doi = _clean(entry.findtext(ARX + "doi"))
            out[base] = paper

        if start + chunk < len(ids):
            log(f"  metadata: {len(out)}/{len(ids)}")
    return out


def _cache_name(key: str) -> str:
    if key.startswith("http"):
        return "url-" + hashlib.sha1(key.encode()).hexdigest()[:16]
    return key.replace("/", "_")


def download_pdf(
    key: str, url: str, cache_dir: str, refresh: bool = False, offline: bool = False
) -> str:
    """Fetch the PDF into the cache and return its path. offline=True: cache or raise."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, _cache_name(key) + ".pdf")
    if os.path.exists(path) and os.path.getsize(path) > 1024 and not refresh:
        return path
    if offline:
        raise RuntimeError("PDF is not in the local cache")

    src = f"https://arxiv.org/pdf/{key}" if not key.startswith("http") else url
    if "arxiv.org" in src:
        _throttle()
    resp = requests.get(src, headers={"User-Agent": UA}, timeout=180, allow_redirects=True)
    resp.raise_for_status()
    body = resp.content
    if not body.startswith(b"%PDF"):
        raise RuntimeError(
            f"server did not return a PDF (content-type {resp.headers.get('content-type')!r})"
        )
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def extract_text(pdf_path: str, max_chars: int = 600_000) -> tuple:
    """Return (text, n_pages, truncated). Long papers keep head and tail."""
    with fitz.open(pdf_path) as doc:
        n_pages = doc.page_count
        chunks = []
        for page in doc:
            chunks.append(page.get_text())
    text = "\n".join(chunks)
    # Collapse the ragged whitespace PDF extraction leaves behind.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    truncated = False
    if len(text) > max_chars:
        truncated = True
        head = int(max_chars * 0.75)
        tail = max_chars - head
        text = (
            text[:head]
            + "\n\n[... middle of the paper elided to fit the context budget ...]\n\n"
            + text[-tail:]
        )
    return text, n_pages, truncated


# --------------------------------------------------------------------------
# Source selection: arXiv's LaTeXML HTML when it exists, the PDF otherwise.
# HTML is strictly better input -- the math comes back as the author's own
# LaTeX instead of PDF-mangled glyphs, and tables survive as tables.
# --------------------------------------------------------------------------

def fetch_html(
    key: str, cache_dir: str, refresh: bool = False, offline: bool = False
) -> Optional[str]:
    """Download the arXiv HTML rendering. Returns raw HTML, or None if there isn't one.

    offline=True only reads the cache (None when nothing is cached).
    """
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, _cache_name(key) + ".html")
    if os.path.exists(path) and os.path.getsize(path) > 2048 and not refresh:
        return open(path, encoding="utf-8", errors="replace").read()
    if offline:
        return None

    _throttle()
    resp = requests.get(
        f"https://arxiv.org/html/{key}",
        headers={"User-Agent": UA},
        timeout=120,
        allow_redirects=True,
    )
    if resp.status_code != 200:
        return None
    html = resp.text
    # Papers before Dec 2023, and ones where LaTeXML choked, serve a stub page.
    if len(html) < 8000 and "html" in html[:2000].lower():
        return None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return html


def get_text(
    paper: Paper, cache_dir: str, max_chars: int, refresh: bool = False, offline: bool = False
) -> None:
    """Populate paper.text / .source / .n_pages / .truncated. Raises on total failure.

    offline=True rebuilds the text from the local HTML/PDF cache without any network access
    (used to re-check cached summaries against the text their agent was given).
    """
    text: Optional[str] = None
    source = ""
    n_pages = 0
    html_error = ""

    if not paper.key.startswith("http"):
        try:
            html = fetch_html(paper.key, os.path.join(cache_dir, "html"), refresh, offline)
            if html:
                candidate = extract.html_to_text(html)
                if extract.looks_like_article(candidate):
                    text, source = candidate, "html"
        except Exception as exc:  # noqa: BLE001 - HTML is the preferred path, not the only one
            html_error = str(exc)

    if text is None:
        try:
            pdf = download_pdf(
                paper.key, paper.url, os.path.join(cache_dir, "pdf"), refresh, offline
            )
            text, n_pages, _ = extract_text(pdf, max_chars=10**9)
            source = "pdf"
        except Exception as exc:  # noqa: BLE001
            detail = f"; HTML also failed ({html_error})" if html_error else ""
            raise RuntimeError(f"could not retrieve the paper: {exc}{detail}") from exc

    if len(text) < 2000:
        raise RuntimeError(
            f"only {len(text)} characters recovered from the {source} "
            "(scanned or image-only paper?)"
        )

    truncated = False
    if len(text) > max_chars:
        truncated = True
        head = int(max_chars * 0.75)
        text = (
            text[:head]
            + "\n\n[... middle of the paper elided to fit the context budget ...]\n\n"
            + text[-(max_chars - head):]
        )

    if not n_pages and paper.comment:
        m = re.search(r"(\d+)\s*pages", paper.comment, re.I)
        if m:
            n_pages = int(m.group(1))

    paper.text, paper.source, paper.n_pages, paper.truncated = text, source, n_pages, truncated
