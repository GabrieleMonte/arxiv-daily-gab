"""Fan a fleet of headless `claude -p` readers out over the papers, in parallel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional

from . import fetch, prompts, verify
from .fetch import Paper


@dataclass
class Config:
    model: str = "claude-opus-5"
    workers: int = 6
    timeout: int = 900
    retries: int = 2
    max_chars: int = 600_000
    cache_dir: str = "cache"
    refresh: bool = False
    refresh_pdfs: bool = False


class RunState:
    """Shared across workers so a usage-limit hit stops the fleet instead of 95 retries."""

    def __init__(self):
        self.stop = threading.Event()
        self.message = ""
        self._lock = threading.Lock()

    def hit_limit(self, message: str) -> None:
        with self._lock:
            if not self.message:
                self.message = message
        self.stop.set()


# Claude Code surfaces subscription exhaustion as plain text, not a status code.
_LIMIT_MARKERS = (
    "usage limit", "rate limit", "rate_limit", "429",
    "too many requests", "try again later", "quota",
)


def is_usage_limit(message: str) -> bool:
    low = message.lower()
    return any(marker in low for marker in _LIMIT_MARKERS)


@dataclass
class Result:
    paper: Paper
    sections: Dict[str, str] = field(default_factory=dict)
    cost_usd: float = 0.0
    seconds: float = 0.0
    cached: bool = False
    error: str = ""
    numbers: Optional[verify.NumberCheck] = None  # summary numbers vs. the paper text

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.sections)


class _Printer:
    """Serialised progress output so parallel workers don't interleave."""

    def __init__(self, total: int):
        self.lock = threading.Lock()
        self.total = total
        self.done = 0

    def line(self, msg: str) -> None:
        with self.lock:
            print(msg, flush=True)

    def step(self, msg: str) -> None:
        with self.lock:
            self.done += 1
            print(f"[{self.done:>3}/{self.total}] {msg}", flush=True)


def _agent_cwd(cache_dir: str) -> str:
    """An empty directory, so the agent never picks up project CLAUDE.md or settings."""
    path = os.path.join(cache_dir, "agent_cwd")
    os.makedirs(path, exist_ok=True)
    return path


def run_agent(
    prompt: str, system: str, model: str, timeout: int, cwd: str
) -> tuple:
    """One headless Claude invocation. Returns (text, cost_usd, error)."""
    if shutil.which("claude") is None:
        return "", 0.0, "the `claude` CLI is not on PATH"

    cmd = [
        "claude",
        "-p",
        "--model", model,
        "--output-format", "json",
        "--system-prompt", system,
        # Strip the interactive harness: no tools, no MCP, no project context, no
        # permission prompts. Cuts ~15k tokens of fixed overhead off every call.
        "--exclude-dynamic-system-prompt-sections",
        "--tools", "",
        "--strict-mcp-config",
        "--permission-prompts", "none",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return "", 0.0, f"agent timed out after {timeout}s"

    if proc.returncode != 0:
        return "", 0.0, f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}"

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "", 0.0, f"could not parse agent JSON: {proc.stdout[:200]}"

    cost = float(payload.get("total_cost_usd") or 0.0)
    if payload.get("is_error"):
        return "", cost, f"agent reported an error: {str(payload.get('result'))[:300]}"
    return payload.get("result") or "", cost, ""


def _summary_cache_path(cfg: Config, paper: Paper) -> str:
    safe = paper.key.replace("/", "_")
    if paper.key.startswith("http"):
        safe = fetch._cache_name(paper.key)
    folder = os.path.join(cfg.cache_dir, "summaries")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{safe}.{cfg.model}.txt")


def _source_text_path(cfg: Config, paper: Paper) -> str:
    """Where the exact text handed to the agent is kept, so its summary can be re-checked."""
    folder = os.path.join(cfg.cache_dir, "text")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, fetch._cache_name(paper.key) + ".txt")


def store_source_text(cfg: Config, paper: Paper, text: str) -> None:
    """Keep the exact text handed to a reader in cache/text/.

    Best effort: a failed write only means the next cached run rebuilds the text.
    """
    try:
        with open(_source_text_path(cfg, paper), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def _check_numbers(result: Result, cfg: Config) -> None:
    """Ground the summary's numbers in the text the agent read. Advisory: never raises.

    A fresh read stores that text in cache/text/. A cached summary reads it back from there,
    or, for summaries cached before the check existed, rebuilds it once from the local
    HTML/PDF cache (offline, same --max-chars truncation; ~1 s per paper, dominated by HTML
    parsing). The rebuild works on a copy, so the digest's metadata line is unchanged.
    """
    try:
        text = result.paper.text
        path = _source_text_path(cfg, result.paper)
        if text:  # fresh read: keep exactly what the agent was given
            store = True
        elif os.path.exists(path):  # cached summary, text kept from an earlier run
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            store = False
        else:  # cached before the check existed: rebuild from the HTML/PDF cache
            probe = replace(result.paper)
            fetch.get_text(probe, cfg.cache_dir, cfg.max_chars, offline=True)
            text, store = probe.text, True
        if store:
            store_source_text(cfg, result.paper, text)

        source = verify.paper_source(replace(result.paper, text=text))
        result.numbers = verify.check_numbers(result.sections, source)
    except Exception as exc:  # noqa: BLE001 - the check must never sink a paper
        result.numbers = verify.NumberCheck(error=f"source text unavailable: {exc}")


def process(target, paper: Paper, cfg: Config, printer: _Printer, state: RunState) -> Result:
    """Download -> extract -> read -> parse, for a single paper."""
    started = time.monotonic()
    label = paper.key if not paper.key.startswith("http") else paper.key[:48]
    result = Result(paper=paper)

    if state.stop.is_set():
        result.error = "not attempted (run stopped early)"
        return result

    cache_path = _summary_cache_path(cfg, paper)
    if os.path.exists(cache_path) and not cfg.refresh:
        raw = open(cache_path, encoding="utf-8").read()
        parsed = prompts.parse_response(raw)
        if prompts.parse_is_usable(parsed):
            result.sections = parsed
            result.cached = True
            _check_numbers(result, cfg)
            result.seconds = time.monotonic() - started
            printer.step(f"{label:<24} cached")
            return result

    # --- retrieve (arXiv HTML when it exists, PDF otherwise) -----------------
    try:
        fetch.get_text(paper, cfg.cache_dir, cfg.max_chars, cfg.refresh_pdfs)
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
        printer.step(f"{label:<24} FAILED ({result.error})")
        return result

    text, n_pages, truncated = paper.text, paper.n_pages, paper.truncated

    # --- read ---------------------------------------------------------------
    prompt = prompts.build_prompt(paper, text)
    cwd = _agent_cwd(cfg.cache_dir)
    last_error = ""
    for attempt in range(cfg.retries + 1):
        raw, cost, err = run_agent(prompt, prompts.SYSTEM_PROMPT, cfg.model, cfg.timeout, cwd)
        result.cost_usd += cost
        if err:
            last_error = err
            if is_usage_limit(err):
                state.hit_limit(err)
                result.error = f"usage limit: {err}"
                printer.step(f"{label:<24} STOPPED (usage limit reached)")
                return result
        else:
            parsed = prompts.parse_response(raw)
            if prompts.parse_is_usable(parsed):
                result.sections = parsed
                with open(cache_path, "w", encoding="utf-8") as fh:
                    fh.write(raw)
                _check_numbers(result, cfg)
                result.seconds = time.monotonic() - started
                printer.step(
                    f"{label:<24} {paper.source:<4} {len(text)//1000:>3}kc  "
                    f"{result.seconds:>5.0f}s  ${result.cost_usd:.3f}"
                    + ("  [truncated]" if truncated else "")
                )
                return result
            last_error = "agent reply did not follow the section format"
        if attempt < cfg.retries:
            time.sleep(2 + 3 * attempt)

    result.error = last_error or "unknown agent failure"
    result.seconds = time.monotonic() - started
    printer.step(f"{label:<24} FAILED ({result.error})")
    return result


def run_all(
    pairs: List[tuple], cfg: Config, log: Callable[[str], None] = print
) -> List[Result]:
    """Run every (target, paper) pair concurrently, preserving input order on return."""
    printer = _Printer(total=len(pairs))
    results: Dict[int, Result] = {}
    state = RunState()

    log(f"\nDispatching {len(pairs)} readers ({cfg.workers} in parallel, model {cfg.model})\n")
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {
            pool.submit(process, target, paper, cfg, printer, state): i
            for i, (target, paper) in enumerate(pairs)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as exc:  # noqa: BLE001 - a worker must never sink the run
                results[i] = Result(paper=pairs[i][1], error=f"worker crashed: {exc}")

    if state.message:
        log(
            "\n  Stopped early: Claude usage limit reached.\n"
            "  Everything already read is cached -- re-run the same command when your\n"
            "  limit resets and it will pick up exactly where it left off."
        )

    return [results[i] for i in range(len(pairs))]
