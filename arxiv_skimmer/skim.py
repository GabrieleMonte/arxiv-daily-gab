#!/usr/bin/env python3
"""
arxiv_skimmer -- read every paper open in Chrome and write one LaTeX digest.

    python3 skim.py                     # every arXiv tab open in Chrome
    python3 skim.py --limit 5           # try it on five first
    python3 skim.py --dry-run           # just show what it would read
    python3 skim.py --ids 2608.05032 hep-th/9711200

External readers (e.g. a cloud routine's own subagents do the reading, no `claude -p`):
    python3 skim.py --ids 2608.05032 2609.11833 --prepare work/
    # ... readers answer work/<id>.prompt.md into work/<id>.response.md ...
    python3 skim.py --ids 2608.05032 2609.11833 --ingest work/ --title "Daily Picks"
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from skimmer import agents, external, fetch, tabs, tex, verify  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# Per-token list price relative to Claude Opus 5. Only used to translate the
# reported figures for people actually billed per token.
MODEL_COST_SCALE = {
    "claude-opus-5": 1.0, "claude-opus-4-8": 1.0, "claude-opus-4-7": 1.0,
    "claude-sonnet-5": 0.4, "claude-sonnet-4-6": 0.6,
    "claude-haiku-4-5": 0.2,
}

SECONDS_PER_PAPER = 80  # measured median, HTML source, Opus 5


def on_subscription() -> bool:
    """True when `claude` bills against a plan rather than per token.

    Claude Code always reports `total_cost_usd`, but on a subscription it is a
    list-price equivalent (`costBasis: "list"`), not money leaving an account.
    """
    try:
        with open(os.path.expanduser("~/.claude.json"), encoding="utf-8") as fh:
            account = json.load(fh).get("oauthAccount") or {}
        return str(account.get("billingType", "")).endswith("subscription")
    except Exception:  # noqa: BLE001 - absence of the file just means "assume API"
        return False


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Summarise every paper open in Chrome into a single LaTeX document.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_argument_group("what to read")
    src.add_argument("--ids", nargs="+", metavar="ID",
                     help="read these arXiv ids/URLs instead of reading Chrome")
    src.add_argument("--window", type=int, metavar="N",
                     help="only tabs in Chrome window N (1 = frontmost)")
    src.add_argument("--limit", type=int, metavar="N", help="stop after N papers")
    src.add_argument("--include-pdf-urls", action="store_true",
                     help="also read non-arXiv tabs whose URL ends in .pdf")
    src.add_argument("--dry-run", action="store_true",
                     help="list the papers that would be read, then exit")

    run = p.add_argument_group("how to read")
    run.add_argument("--model", default="claude-opus-5",
                     help="model for the reader agents (default: claude-opus-5)")
    run.add_argument("--workers", type=int, default=8,
                     help="agents running in parallel (default: 8)")
    run.add_argument("--timeout", type=int, default=900,
                     help="seconds before an agent is abandoned (default: 900)")
    run.add_argument("--retries", type=int, default=2,
                     help="retries per paper (default: 2)")
    run.add_argument("--max-chars", type=int, default=600_000,
                     help="per-paper character budget before the middle is elided")
    run.add_argument("--refresh", action="store_true",
                     help="ignore cached summaries and re-read everything")
    run.add_argument("--refresh-pdfs", action="store_true", help="re-download cached PDFs")
    run.add_argument("-y", "--yes", action="store_true", help="skip the cost confirmation")

    ext = p.add_argument_group("external readers (no `claude -p` calls)")
    mode = ext.add_mutually_exclusive_group()
    mode.add_argument("--prepare", metavar="DIR",
                      help="write DIR/<id>.prompt.md per paper plus DIR/manifest.json, then stop")
    mode.add_argument("--ingest", metavar="DIR",
                      help="read DIR/<id>.response.md per paper, cache, check and typeset "
                           "(ids default to DIR/manifest.json when --ids is omitted)")

    out = p.add_argument_group("output")
    out.add_argument("--out", metavar="PATH", help="output .tex path")
    out.add_argument("--title", default="arXiv Skim", help="document title")
    out.add_argument("--subtitle", help='line under the title (default: "N papers")')
    out.add_argument("--no-pdf", action="store_true", help="write the .tex but do not compile")
    out.add_argument("--cache", default=os.path.join(HERE, "cache"), help="cache directory")
    out.add_argument("--open", dest="open_pdf", action="store_true",
                     help="open the finished PDF when it is done")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # ------------------------------------------------------------------ pick papers
    manifest = external.load_manifest(args.ingest) if args.ingest else {}
    if not args.ids and args.ingest and external.manifest_ids(manifest):
        targets = tabs.targets_from_ids(external.manifest_ids(manifest))
        skipped = []
        print(f"Ingesting {len(targets)} paper(s) listed in "
              f"{os.path.join(args.ingest, external.MANIFEST)}.")
    elif args.ids:
        targets = tabs.targets_from_ids(args.ids)
        skipped = []
        print(f"Reading {len(targets)} paper(s) given on the command line.")
    else:
        try:
            open_tabs = tabs.list_tabs(window=args.window)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        targets, skipped = tabs.resolve(open_tabs, include_pdf_urls=args.include_pdf_urls)
        print(f"{len(open_tabs)} tabs open -> {len(targets)} unique papers, "
              f"{len(skipped)} tabs skipped.")

    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("Nothing to read. Open some arXiv tabs, or pass --ids.", file=sys.stderr)
        if skipped:
            print("\nSkipped tabs:", file=sys.stderr)
            for t in skipped[:20]:
                print(f"  {t.url}", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------ metadata
    arxiv_ids = [t.base_id for t in targets if t.kind == "arxiv"]
    # An ingest reuses the metadata --prepare stored, so the digest matches the prompts.
    meta = external.manifest_papers(manifest)
    missing = [i for i in arxiv_ids if i not in meta]
    if missing:
        print(f"Looking up metadata for {len(missing)} arXiv ids...")
        meta.update(fetch.fetch_metadata(missing))

    pairs = []
    for t in targets:
        paper = meta.get(t.base_id) or meta.get(t.key)
        if paper is None:
            # Metadata missed (brand-new posting, or a plain PDF URL): fall back to the tab.
            paper = fetch.Paper(
                key=t.key, url=t.url, title=(t.tab.title if t.tab else "").strip()
            )
        pairs.append((t, paper))

    if args.dry_run:
        print()
        for i, (t, paper) in enumerate(pairs, 1):
            print(f"{i:>3}. {t.key:<22} {paper.display_title[:88]}")
        if skipped:
            print(f"\nSkipped {len(skipped)} tabs:")
            for t in skipped:
                print(f"     {t.url[:110]}")
        return 0

    cfg = agents.Config(
        model=args.model, workers=args.workers, timeout=args.timeout,
        retries=args.retries, max_chars=args.max_chars, cache_dir=args.cache,
        refresh=args.refresh, refresh_pdfs=args.refresh_pdfs,
    )

    # ------------------------------------------------------------------ external: prepare
    if args.prepare:
        print(f"\nPreparing {len(pairs)} prompt file(s) in {args.prepare}\n")
        manifest = external.prepare(pairs, cfg, args.prepare)
        ready = [e for e in manifest["papers"] if not e["error"]]
        print(f"\n{len(ready)} prompt(s) written, {len(pairs) - len(ready)} failed. "
              f"Manifest: {os.path.join(args.prepare, external.MANIFEST)}")
        for e in manifest["papers"]:
            if e["error"]:
                print(f"  ! {e['id']}: {e['error']}")
        if ready:
            print(f"Each reader saves its reply to the matching *{external.RESPONSE_SUFFIX}; "
                  f"then run --ingest {args.prepare}.")
        return 0 if ready else 1

    # ------------------------------------------------------------------ cost gate
    uncached = 0 if args.ingest else sum(
        1 for _, paper in pairs
        if args.refresh or not os.path.exists(agents._summary_cache_path(cfg, paper))
    )
    if uncached and not args.yes:
        minutes = uncached * SECONDS_PER_PAPER / max(args.workers, 1) / 60
        print(f"\n{uncached} paper(s) to read on {args.model}, {args.workers} at a time "
              f"({len(pairs) - uncached} already cached).")
        print(f"Estimated wall clock: {minutes:.0f} min.")
        if on_subscription():
            print("Billing: your Claude subscription -- this draws on your usage window, "
                  "not your wallet.")
            print("         If you hit the limit the run stops cleanly and resumes from "
                  "cache next time.")
        else:
            scale = MODEL_COST_SCALE.get(args.model, 1.0)
            print(f"Estimated cost: ${0.30 * uncached * scale:.2f}-"
                  f"${0.85 * uncached * scale:.2f} at API list price.")
        try:
            if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1

    # ------------------------------------------------------------------ read (or ingest)
    if args.ingest:
        print(f"\nIngesting {len(pairs)} response(s) from {args.ingest}\n")
        results = external.ingest(pairs, cfg, args.ingest)
    else:
        results = agents.run_all(pairs, cfg)

    good = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    cost = sum(r.cost_usd for r in results)
    fresh = [r for r in results if not r.cached]
    summary = f"\n{len(good)} read, {len(bad)} failed."
    if fresh and not args.ingest:
        per = cost / len(fresh)
        if on_subscription():
            summary += (f" Usage drawn from your subscription: ${cost:.2f} list-price "
                        f"equivalent (${per:.2f}/paper), not billed.")
        else:
            summary += f" Cost: ${cost:.2f} (${per:.2f} per paper)."
    print(summary)
    for r in bad:
        print(f"  ! {r.paper.key}: {r.error}")

    # Advisory grounding check: numbers in each summary vs. the text its agent read.
    if good:
        print("\nNumber check (summary vs. paper text):")
        for r in good:
            print(f"  {r.paper.key:<22} {verify.describe(r.numbers)}")

    if not good:
        print("Nothing to typeset.", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------ typeset
    out_path = args.out or os.path.join(
        HERE, "out", f"arxiv_skim_{_dt.date.today():%Y-%m-%d}.tex"
    )
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    subtitle = f"{len(good)} papers"
    if bad:
        subtitle += f", {len(bad)} unreadable"
    if args.subtitle is not None:
        subtitle = args.subtitle
    document = tex.render(results, title=args.title, subtitle=subtitle)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(document)
    print(f"Wrote {out_path}")

    if not args.no_pdf:
        print("Compiling...")
        if tex.compile_pdf(out_path):
            pdf_path = os.path.splitext(out_path)[0] + ".pdf"
            print(f"Wrote {pdf_path}")
            if args.open_pdf:
                subprocess.run(["open", pdf_path], check=False)
        else:
            print("PDF compilation did not succeed; the .tex is still there.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
