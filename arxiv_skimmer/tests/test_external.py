"""Offline round trip for the external-reader mode: --prepare, fake replies, --ingest, LaTeX.

Uses copies of papers already in the local cache (one HTML-sourced, one PDF-sourced with an
old-style id), so no arXiv request and no `claude` call is made. Replies are faked from the
cached summaries. Skipped when those cache files are absent.

    python3 -m pytest tests/        # or simply: python3 tests/test_external.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from skimmer import agents, external, prompts, tex  # noqa: E402
from skimmer.fetch import Paper  # noqa: E402

REAL_CACHE = os.path.join(HERE, "cache")
MODEL = "claude-opus-5"
# (key, cached source file) for one HTML-sourced and one PDF-only paper.
PAPERS = [("2608.05032", "html/2608.05032.html"), ("hep-th/9711200", "pdf/hep-th_9711200.pdf")]
UNREACHABLE = "http://localhost:9/not-a-paper.pdf"  # refused locally, never leaves the machine


def _cached_summary(key: str) -> str:
    return os.path.join(REAL_CACHE, "summaries", f"{key.replace('/', '_')}.{MODEL}.txt")


def _require_cache() -> None:
    for key, source in PAPERS:
        for path in (os.path.join(REAL_CACHE, source), _cached_summary(key)):
            if not os.path.exists(path):
                pytest.skip(f"needs the local cache file {path}")


def _sandbox(root: str) -> agents.Config:
    """A private cache holding only the HTML/PDF the test papers need."""
    cache = os.path.join(root, "cache")
    for _, source in PAPERS:
        os.makedirs(os.path.dirname(os.path.join(cache, source)), exist_ok=True)
        shutil.copy(os.path.join(REAL_CACHE, source), os.path.join(cache, source))
    return agents.Config(model=MODEL, cache_dir=cache)


def _pairs() -> list:
    papers = [
        Paper(key=key, url=f"https://arxiv.org/abs/{key}", title=f"Test paper {key}",
              authors=["A. Author"], abstract="An abstract with 0.077 in it.")
        for key, _ in PAPERS
    ]
    papers.append(Paper(key=UNREACHABLE, url=UNREACHABLE, title="Unreachable"))
    return [(None, p) for p in papers]


def test_prepare_then_ingest_round_trip():
    _require_cache()
    with tempfile.TemporaryDirectory() as root:
        cfg = _sandbox(root)
        work = os.path.join(root, "work")

        # --- prepare ----------------------------------------------------------------
        manifest = external.prepare(_pairs(), cfg, work, log=lambda _: None)
        with open(os.path.join(work, external.MANIFEST), encoding="utf-8") as fh:
            assert json.load(fh) == manifest

        entries = {e["id"]: e for e in manifest["papers"]}
        assert entries["2608.05032"]["source"] == "html"
        assert entries["hep-th/9711200"]["source"] == "pdf"
        assert entries["hep-th/9711200"]["prompt"].endswith("hep-th_9711200.prompt.md")
        assert entries[UNREACHABLE]["error"] and not entries[UNREACHABLE]["prompt"]

        for key, _ in PAPERS:
            with open(entries[key]["prompt"], encoding="utf-8") as fh:
                document = fh.read()
            paper = external.manifest_papers(manifest)[key]
            with open(agents._source_text_path(cfg, paper), encoding="utf-8") as fh:
                paper.text = fh.read()
            assert prompts.SYSTEM_PROMPT in document
            assert prompts.build_prompt(paper, paper.text) in document
            assert document.index("## SYSTEM PROMPT") < document.index("## USER PROMPT")
            assert entries[key]["response"] in document

        # --- fake replies: one good, one garbled, one missing ------------------------
        shutil.copy(_cached_summary("2608.05032"), entries["2608.05032"]["response"])
        with open(entries["hep-th/9711200"]["response"], "w", encoding="utf-8") as fh:
            fh.write("Sorry, I could not read this paper.")

        # --- ingest -------------------------------------------------------------------
        papers = external.manifest_papers(manifest)
        pairs = [(None, papers[key]) for key, _ in PAPERS] + [(None, Paper(UNREACHABLE, ""))]
        good, garbled, missing = external.ingest(pairs, cfg, work, log=lambda _: None)

        assert good.ok and not good.cached
        with open(_cached_summary("2608.05032"), encoding="utf-8") as fh:
            raw = fh.read()
        assert good.sections == prompts.parse_response(raw)
        with open(agents._summary_cache_path(cfg, good.paper), encoding="utf-8") as fh:
            assert fh.read() == raw  # stored exactly as a fresh agent reply
        assert good.numbers is not None and good.numbers.ok and good.numbers.checked > 0

        assert not garbled.ok and "section format" in garbled.error
        assert not missing.ok and "no response file" in missing.error
        assert not os.path.exists(agents._summary_cache_path(cfg, garbled.paper))

        # --- typeset and compile --------------------------------------------------------
        title = "Daily Arxiv Picks for Gab — Wed, Nov 25 2026"
        out = os.path.join(root, "digest.tex")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(tex.render([good, garbled, missing], title=title, subtitle="1 paper"))
        if shutil.which("latexmk"):
            assert tex.compile_pdf(out, log=lambda _: None)
            assert os.path.getsize(out[:-4] + ".pdf") > 10_000


def test_ingest_without_manifest_uses_default_paths():
    _require_cache()
    with tempfile.TemporaryDirectory() as root:
        cfg = _sandbox(root)
        work = os.path.join(root, "work")
        os.makedirs(work)
        paper = Paper(key="2608.05032", url="https://arxiv.org/abs/2608.05032")
        shutil.copy(_cached_summary("2608.05032"), os.path.join(work, "2608.05032.response.md"))

        (result,) = external.ingest([(None, paper)], cfg, work, log=lambda _: None)
        assert result.ok
        # No prepare step, so the number check rebuilt the text offline from the HTML cache.
        assert result.numbers.ok and result.numbers.checked > 0


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} tests passed")
