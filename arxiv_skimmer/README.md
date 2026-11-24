# arxiv_skimmer

Reads every arXiv paper you have open in Chrome, sends one Claude agent per paper in
parallel, and typesets the whole batch into a single LaTeX digest.

```bash
arxiv-skim --dry-run      # what would it read?
arxiv-skim --limit 5      # try five
arxiv-skim                # the lot
```

Output lands in `out/arxiv_skim_<date>.tex` and, if `latexmk` is installed, the
compiled `.pdf` beside it.

## How it works

1. **Tabs** — AppleScript asks Chrome for every open tab, then `skimmer/tabs.py`
   pulls arXiv ids out of the URLs (`/abs/`, `/pdf/`, `/html/`, ar5iv, alphaxiv,
   HuggingFace papers; new-style `2608.05032` and old-style `hep-th/9711200`).
   Listing pages, mail, and everything else are skipped; the same paper open in
   three tabs is read once.
2. **Metadata** — one batched call to the arXiv API for titles, authors, abstracts,
   categories and journal refs (throttled to arXiv's 1-request-per-3-seconds policy,
   with backoff for their habitual 503s).
3. **Text** — arXiv's LaTeXML **HTML** rendering is preferred, because the math comes
   back as the authors' own LaTeX (`alttext`) rather than PDF-mangled glyphs, and
   tables survive as tables. Papers older than Dec 2023, or ones where LaTeXML choked,
   fall back to the PDF via PyMuPDF.
4. **Reading** — one headless `claude -p` process per paper, `--workers` of them at a
   time. Each agent gets the full text and returns a fixed set of marker-delimited
   sections. No API key needed: it uses your Claude Code login.
5. **Typesetting** — `skimmer/tex.py` renders one section per paper (plus an "At a
   glance" index and a table of contents) and compiles with `latexmk`.

Each agent runs with `--tools "" --strict-mcp-config --exclude-dynamic-system-prompt-sections`,
which strips the interactive harness and cuts ~15k tokens of fixed overhead off every
call (~40× less than a default `claude -p`).

## What each summary contains

`In one breath` · `The problem` · `Main ideas` · `Technical details` · `Key results` ·
`Caveats and assumptions` · `Why it matters`, then the abstract as posted. The prompt in
`skimmer/prompts.py` is tuned for a working cosmologist — it keeps equations, datasets,
priors, codes and numbers with uncertainties, and never explains what $\Lambda$CDM is.
`SYSTEM_PROMPT` also carries a short block on house style, distilled from the
`gm-scientific-writer` style guide: mechanism before data, claims pinned to numbers, results
quoted as "$0.077^{+0.047}_{-0.042}$ (68% C.L.)" or "at about $3\sigma$ significance",
numeric rather than modal hedges, American spelling, and a list of words to avoid.
Edit `SECTIONS` and `_FORMAT_SPEC` there to change the shape of the output.

## Checks on the summaries

**Inference labels.** The prompt asks the reader to end anything that is its own interpretation
rather than something the paper states (a judgement, an extrapolation, a connection the authors
do not draw) with `(inference)`. Two cases are mandatory: every evaluative sentence in "Why it
matters", and every statement containing a number the reader derived itself (a ratio, a unit
conversion, an order-of-magnitude estimate the paper does not print). Statements taken from the
paper stay untagged. The digest typesets the tag as a small grey italic *(inference)*.
Summaries cached before this rule was added carry no tags until they are re-read with
`--refresh`. Tagging is still the model's own judgement, so the number check below is the
harder backstop for derived numbers.

**Number check.** After each summary is parsed, `skimmer/verify.py` pulls every number out of
its sections and looks it up in the text the agent was actually given (full text plus title,
abstract, author comment and journal ref). The run summary prints one line per paper, e.g.

```
Number check (summary vs. paper text):
  2608.05032              90 numbers checked, 1 not found: 10^{-30}
```

and the digest adds a small grey line under that paper listing the numbers that were not found,
with the section they came from. The check is advisory: it never stops a run or changes a
summary. It is meant to surface numbers the agent derived, converted, recalled or invented.

- *Matched as equal:* `0.50` and `0.5`; `1{,}000`, `1\,000` and `1000`; `1.2\times10^{-3}`,
  `1.2e-3`, `0.0012`, LaTeXML's `1.2\text{\times}{10}^{-3}` and the PDF-flattened `1.2 × 10−3`;
  `12\%` and `0.12`; signs and unicode minus are ignored. A summary value also matches a more
  precise source value that rounds to it (`13.7` from `13.68`, `963` from `962.9`) if it has at
  least 2 significant digits (3 for integers); `0.3` or `50` must appear exactly.
- *Not checked:* single-digit integers, 4-digit integers that read as years (1900–2099), arXiv
  ids, URLs, version strings, TeX dimensions, cross-references (`Sec. 3.2`, `Eq. (12)`,
  `Fig. 4`), labels such as `H_0`, `N_{\rm eff}`, `DR2`, and sub/superscripts other than powers
  of ten.
- *Cached papers:* the check runs on cached summaries too, so re-running on cached ids costs no
  agent calls. The exact text each agent read is kept in `cache/text/`. Summaries cached before
  that existed have their text rebuilt once, offline, from the cached HTML/PDF (about 1 s per
  paper, mostly HTML parsing); later runs just read it.

On the 97 summaries cached when this was added, 5510 numbers were checked and 77 flagged (in 40
papers). Spot checks found essentially all of them really are absent from the extracted text:
values the agent computed (`830/630\simeq1.32`, `1/\sqrt{0.1}\simeq3.16`), outside knowledge
(the canonical WIMP `3\times10^{-26}` cm³/s), unit conversions, or values the text never gives.

Tests: `python3 -m pytest tests/` (or `python3 tests/test_verify.py`).

## Time and usage

Measured on Claude Opus 5, reading the HTML: **60–100 s per paper**, largely independent of
length. With the default `--workers 8`, 95 papers take roughly **13 minutes**.

`claude -p` reports a `total_cost_usd` for every call, and the script prints the total. On a
subscription that number is a *list-price equivalent* (`costBasis: "list"` in the JSON), not
money leaving your account — the run draws on your usage window instead. The script checks
`~/.claude.json` and words its pre-flight summary accordingly.

If you do exhaust the usage window, the run stops cleanly rather than grinding through 90
failing retries: the first limit error halts the fleet, and everything already read stays in
`cache/`. Re-run the same command when the window resets and it resumes exactly where it
stopped. That also means `--refresh` is the only way to pay for a paper twice.

## Running it from a terminal

It is a plain Python script that shells out to `claude -p`, so it never needs a live Claude
Code session — any terminal will do. A launcher is installed at `~/.local/bin/arxiv-skim`:

```bash
arxiv-skim --dry-run
arxiv-skim --open            # compile and open the PDF when done
arxiv-skim --limit 20 --workers 8
```

It pins the interpreter that has the dependencies installed (`/Users/montefalcone/Desktop/Cosmo_Codes/cosmo_env/bin/python3`),
so it works regardless of which virtualenv happens to be active. Output and cache always land
beside the script, not in your current directory. Edit that one-line launcher if the
interpreter or the checkout ever moves.

## External readers (cloud routine)

Where `claude -p` is not available, for example in a Claude Code cloud routine whose own
subagents do the reading, the run splits in two and the script itself never calls Claude:

```bash
python3 skim.py --ids 2611.01234 2611.05678 --prepare work/
#   ... one reader per work/<id>.prompt.md, each writing its reply to work/<id>.response.md ...
python3 skim.py --ingest work/ --title "Daily Arxiv Picks for Gab — Wed, Nov 25 2026" \
                --out out/daily.tex
```

- **`--prepare DIR`** fetches metadata and text exactly as a local run would (HTML preferred,
  PDF fallback, same `--max-chars` elision) and stops. Per paper it writes
  `DIR/<id>.prompt.md`: a one-line comment naming the response path, then `## SYSTEM PROMPT`
  and `## USER PROMPT`. A reader follows the first, answers the second and saves its reply
  verbatim, markers included. `DIR/manifest.json` lists each paper's id, title, prompt and
  response paths, text source (`html`/`pdf`), length, truncation flag, any retrieval error,
  and its full arXiv metadata. The text itself also goes to `cache/text/` for the number check.
- **`--ingest DIR`** reads `DIR/<id>.response.md` for each paper, parses it with the usual
  marker protocol, stores it in `cache/summaries/` byte-for-byte as a fresh `claude -p` reply
  would be (named with `--model`, default `claude-opus-5`), runs the number check and
  typesets/compiles the digest. `--out`, `--no-pdf`, `--title` and `--subtitle` work as usual.
  The ids come from `--ids`, or from the manifest when `--ids` is omitted. Metadata comes from
  the manifest, so no arXiv API call is needed. A missing or unusable response is reported for
  that paper and listed under "Papers that could not be read"; the rest are typeset.
- `<id>` is the arXiv id with `/` replaced by `_` (`hep-th/9711200` → `hep-th_9711200`).
- The exit code is 1 if no prompt could be written, or if no response could be ingested.

## Useful flags

| Flag | Effect |
|---|---|
| `--dry-run` | list the papers, spend nothing |
| `--limit N` | stop after N papers |
| `--ids 2608.05032 hep-th/9711200` | read these instead of reading Chrome |
| `--window 1` | only tabs in Chrome window 1 |
| `--include-pdf-urls` | also read non-arXiv tabs whose URL ends in `.pdf` |
| `--workers N` | agents in parallel (default 8) |
| `--model NAME` | reader model (default `claude-opus-5`) |
| `--refresh` | ignore cached summaries |
| `--no-pdf` | write the `.tex`, skip `latexmk` |
| `--open` | open the finished PDF |
| `--out PATH` | where the `.tex` goes |
| `-y` | skip the cost confirmation |
| `--prepare DIR` | write reader prompts + `manifest.json` to `DIR`, no reading |
| `--ingest DIR` | typeset the replies readers left in `DIR`, no `claude` calls |
| `--title TEXT` / `--subtitle TEXT` | document title / the line under it (default "N papers") |

## Requirements

`pip install -r requirements.txt`, the `claude` CLI on your PATH and logged in, macOS
(the tab list is AppleScript), and `latexmk` if you want the PDF compiled for you.

On first run macOS will ask for permission for your terminal to control Chrome —
System Settings → Privacy & Security → Automation.
