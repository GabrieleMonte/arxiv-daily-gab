"""External readers: hand the reading to someone else's agents, then typeset what they wrote.

The local path runs one `claude -p` per paper (agents.py). Where that is not available, e.g.
inside a Claude Code cloud routine whose own subagents do the reading, the work splits in two:

    prepare(DIR)  fetch metadata + text exactly as agents.process would, and write
                  DIR/<stem>.prompt.md   system prompt + user prompt, one file per paper
                  DIR/manifest.json      ids, titles, prompt/response paths, text source,
                                         and the paper metadata (so ingest needs no API call)
    ingest(DIR)   read DIR/<stem>.response.md for each paper, parse it with the usual marker
                  protocol, store it in cache/summaries exactly as a fresh agent reply, and
                  run the number check. No claude calls.

<stem> is the cache name of the paper key: the arXiv id with "/" replaced by "_"
(hep-th/9711200 -> hep-th_9711200), or a hash for plain PDF URLs.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import asdict, fields
from typing import Callable, Dict, List, Optional

from . import agents, fetch, prompts
from .agents import Config, Result
from .fetch import Paper

MANIFEST = "manifest.json"
PROMPT_SUFFIX = ".prompt.md"
RESPONSE_SUFFIX = ".response.md"
MANIFEST_VERSION = 1


def file_stem(paper: Paper) -> str:
    """File-name stem for a paper's prompt and response files."""
    return fetch._cache_name(paper.key)


def prompt_document(paper: Paper, response_path: str) -> str:
    """Everything a reader needs in one file: the system prompt, then the user prompt."""
    header = (
        f"<!-- arxiv_skimmer reader prompt for {paper.key}. Follow the SYSTEM PROMPT, answer "
        f"the USER PROMPT, and save the reply verbatim (markers and all, nothing else) to: "
        f"{response_path} -->"
    )
    return (
        f"{header}\n\n## SYSTEM PROMPT\n\n{prompts.SYSTEM_PROMPT}\n\n"
        f"## USER PROMPT\n\n{prompts.build_prompt(paper, paper.text)}\n"
    )


# ============================================================================
# Manifest
# ============================================================================

def _paper_record(paper: Paper) -> Dict:
    """Paper metadata for the manifest; the full text lives in the prompt file instead."""
    record = asdict(paper)
    record.pop("text", None)
    return record


def load_manifest(directory: str) -> Dict:
    """The manifest written by prepare, or {} when there is none or it cannot be read."""
    path = os.path.join(directory, MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def manifest_papers(manifest: Dict) -> Dict[str, Paper]:
    """{base arXiv id or URL key: Paper} rebuilt from the manifest's metadata records."""
    known = {f.name for f in fields(Paper)}
    out: Dict[str, Paper] = {}
    for entry in manifest.get("papers", []):
        record = {k: v for k, v in (entry.get("paper") or {}).items() if k in known}
        if record.get("key"):
            out[record["key"]] = Paper(**record)
    return out


def manifest_ids(manifest: Dict) -> List[str]:
    """Paper ids in manifest order, for --ingest without --ids."""
    return [entry["id"] for entry in manifest.get("papers", []) if entry.get("id")]


# ============================================================================
# Prepare
# ============================================================================

def prepare(
    pairs: List[tuple], cfg: Config, directory: str, log: Callable[[str], None] = print
) -> Dict:
    """Write one prompt file per paper plus manifest.json into `directory`.

    Text retrieval is the same as a fresh local read (HTML preferred, PDF fallback, the same
    cfg.max_chars elision). The exact text is also stored in cache/text/ so that ingest can
    run the number check against it. A paper whose text cannot be retrieved is listed in the
    manifest with its error and gets no prompt file; it never stops the others.
    """
    os.makedirs(directory, exist_ok=True)
    entries = []

    for i, (_, paper) in enumerate(pairs, 1):
        stem = file_stem(paper)
        prompt_path = os.path.abspath(os.path.join(directory, stem + PROMPT_SUFFIX))
        response_path = os.path.abspath(os.path.join(directory, stem + RESPONSE_SUFFIX))
        entry = {
            "id": paper.key, "title": paper.display_title, "prompt": prompt_path,
            "response": response_path, "source": "", "chars": 0, "truncated": False,
            "error": "",
        }
        try:
            fetch.get_text(paper, cfg.cache_dir, cfg.max_chars, cfg.refresh_pdfs)
            with open(prompt_path, "w", encoding="utf-8") as fh:
                fh.write(prompt_document(paper, response_path))
            agents.store_source_text(cfg, paper, paper.text)
            entry.update(source=paper.source, chars=len(paper.text), truncated=paper.truncated)
            log(f"[{i:>3}/{len(pairs)}] {paper.key:<24} {paper.source:<4} "
                f"{len(paper.text) // 1000:>3}kc  -> {os.path.basename(prompt_path)}"
                + ("  [truncated]" if paper.truncated else ""))
        except Exception as exc:  # noqa: BLE001 - one bad paper must not sink the batch
            entry.update(prompt="", error=str(exc))
            log(f"[{i:>3}/{len(pairs)}] {paper.key:<24} FAILED ({exc})")
        entry["paper"] = _paper_record(paper)
        entries.append(entry)

    manifest = {
        "version": MANIFEST_VERSION,
        "created": _dt.datetime.now().isoformat(timespec="seconds"),
        "model": cfg.model,
        "max_chars": cfg.max_chars,
        "papers": entries,
    }
    with open(os.path.join(directory, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    return manifest


# ============================================================================
# Ingest
# ============================================================================

def _response_path(directory: str, paper: Paper, manifest: Dict) -> str:
    """The manifest's response path for this paper, or the default DIR/<stem>.response.md."""
    for entry in manifest.get("papers", []):
        if entry.get("id") == paper.key and entry.get("response"):
            return entry["response"]
    return os.path.join(directory, file_stem(paper) + RESPONSE_SUFFIX)


def ingest_one(
    paper: Paper, cfg: Config, directory: str, manifest: Optional[Dict] = None
) -> Result:
    """Parse one reader's reply and cache it as a fresh agent result. Never raises."""
    result = Result(paper=paper)
    path = _response_path(directory, paper, manifest or {})
    try:
        if not os.path.exists(path):
            result.error = f"no response file ({path})"
            return result
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()

        parsed = prompts.parse_response(raw)
        if not prompts.parse_is_usable(parsed):
            missing = [k for k in ("tldr", "ideas", "technical") if not parsed.get(k, "").strip()]
            result.error = (
                "response did not follow the section format (missing or empty: "
                + ", ".join(missing) + ")"
            )
            return result

        # Byte-for-byte what agents.process writes after a successful `claude -p` read.
        with open(agents._summary_cache_path(cfg, paper), "w", encoding="utf-8") as fh:
            fh.write(raw)
        result.sections = parsed
        agents._check_numbers(result, cfg)
    except Exception as exc:  # noqa: BLE001 - report per paper, keep going
        result.error = f"could not ingest the response: {exc}"
    return result


def ingest(
    pairs: List[tuple], cfg: Config, directory: str, log: Callable[[str], None] = print
) -> List[Result]:
    """Ingest every paper's response, in input order. Failures are reported, not raised."""
    manifest = load_manifest(directory)
    results = []
    for i, (_, paper) in enumerate(pairs, 1):
        result = ingest_one(paper, cfg, directory, manifest)
        status = "ingested" if result.ok else f"SKIPPED ({result.error})"
        log(f"[{i:>3}/{len(pairs)}] {paper.key:<24} {status}")
        results.append(result)
    return results
