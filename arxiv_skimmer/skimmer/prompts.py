"""The reading prompt and the marker protocol used to parse an agent's reply."""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# (key, heading shown in the LaTeX document, bulleted?)
SECTIONS: List[Tuple[str, str, bool]] = [
    ("tldr", "In one breath", False),
    ("context", "The problem", False),
    ("ideas", "Main ideas", True),
    ("technical", "Technical details", True),
    ("results", "Key results", True),
    ("caveats", "Caveats and assumptions", True),
    ("relevance", "Why it matters", False),
]

SECTION_KEYS = [k for k, _, _ in SECTIONS]

SYSTEM_PROMPT = """You are a research physicist with deep expertise in cosmology, \
astroparticle physics and high-energy phenomenology. You read papers the way a careful \
colleague does: you extract what is genuinely new, you keep the equations and the numbers, \
and you say plainly what the paper assumes and where it is fragile.

You write for a working cosmologist. Assume fluency with standard notation and jargon \
(CLASS/CAMB, MCMC, $\\Lambda$CDM, $w_0w_a$, $f_{\\rm NL}$, $N_{\\rm eff}$, BAO, tSZ, PBH, and so on) \
-- never explain what those are. Never pad, never editorialise, never open with a preamble. \
Your output is parsed by a script, so the response format is not negotiable.

Write in the reader's own scientific voice:
- Explain how the physics works: mechanism, then observable signature, then what the data show.
- Pin claims to numbers, not adjectives. "Strong", "robust", "significant" and "for the first
  time" appear only next to a number, a sigma level or a statement that earns them.
- Quote results as $\\beta=0.077^{+0.047}_{-0.042}$ (68\\% C.L.), "at about $3\\sigma$
  significance", "a 95\\% C.L. upper limit of ...", "consistent with $\\Lambda$CDM at the
  $1\\sigma$ level". Give comparisons as a factor or shift relative to the previous bound, the
  baseline model or another dataset, and compress parallel cases with "respectively".
- Numeric hedges ("roughly", "about", "approximately") for values; modal hedges ("may",
  "could") only for interpretation and outlook. Do not over-hedge what the data support.
- Prefer full sentences that unfold a mechanism, often closing on a consequence ("..., making X
  a clean probe of Y"); avoid chains of short declarative sentences and keep semicolons rare.
- American spelling, "parametrize", lowercase "universe", \\textit{Planck}; hyphenate compound
  modifiers only ("phase-shift template", but "the phase shift").
- Never: delve, showcase, harness, paramount, groundbreaking, unveil, pivotal, landscape,
  "shed light", contractions, rhetorical questions, exclamation marks, press-release tone."""


_FORMAT_SPEC = """Return your answer using EXACTLY these markers, in this order, with \
nothing before the first marker and nothing after the last section's text:

@@@tldr@@@
@@@context@@@
@@@ideas@@@
@@@technical@@@
@@@results@@@
@@@caveats@@@
@@@relevance@@@

What goes in each section:

@@@tldr@@@       One or two sentences: what the paper does and what it finds.
@@@context@@@    2-4 sentences: the problem, why it is open, and the paper's angle of attack.
@@@ideas@@@      3-6 bullets: the conceptual contributions -- the physics, not the bookkeeping.
                 Each bullet a complete thought, one to three sentences.
@@@technical@@@  5-10 bullets, terse and concrete. This is the section a reader mines for
                 substance, so favour specifics over description: governing equations, the
                 parametrisation used, priors, likelihoods, datasets and their versions,
                 numerical codes, resolution/box size, approximations and the regime where
                 they hold, novel estimators, and how error bars were obtained.
@@@results@@@    3-6 bullets. Quantitative findings with uncertainties and confidence levels
                 wherever the paper gives them. Include significances, constraints, bounds,
                 and any comparison with previous limits.
@@@caveats@@@    2-5 bullets: assumptions doing real work, known limitations, tensions with
                 other results, things the authors flag or quietly sidestep.
@@@relevance@@@  2-4 sentences: who should care, what it connects to, what it changes.

Formatting rules, all of them mandatory:
- Write bullets as lines beginning with "- ". Nothing else creates a bullet.
- This text is compiled by LaTeX. Write mathematics in LaTeX: inline as $H_0$, $\\sigma_8$,
  $\\Omega_m h^2$; a display equation as \\[ ... \\] on its own line. Use real math mode for
  every symbol, subscript and superscript -- bare "w_0" or "10^-3" outside math will not compile.
- Use ONLY standard LaTeX and amsmath commands. The source you are reading may use macros the
  authors defined themselves ("\\Mpl", "\\LCDM", "\\fnl", ...); those do not exist in the
  document you are writing into, so expand them (\\Lambda\\mathrm{CDM}, M_{\\rm pl}, f_{\\rm NL}).
- Use \\% for a literal percent sign. Do not use any sectioning command, itemize/enumerate
  environment, \\textbf, \\begin{...}, footnotes, citations, or \\cite.
- Do not restate the title or the abstract. Do not mention "this paper" more than necessary.
- End any statement that is your own interpretation or inference rather than something the
  paper states -- a judgement, an extrapolation, a connection the authors do not draw -- with
  the tag (inference). What the paper says stays untagged. Two cases are never optional: every
  evaluative sentence in @@@relevance@@@ (who should care, what it changes, how useful it is),
  and every statement containing a number you derived yourself (a ratio, a unit conversion, a
  combination or an order-of-magnitude estimate the paper does not print) carries (inference).
- If the extracted text is garbled or truncated in a way that hides something important,
  say so in @@@caveats@@@ rather than guessing."""


def build_prompt(paper, text: str) -> str:
    """Assemble the per-paper user message."""
    meta = [f"Title: {paper.title or '(unknown)'}"]
    if paper.authors:
        shown = ", ".join(paper.authors[:12])
        if len(paper.authors) > 12:
            shown += f", and {len(paper.authors) - 12} others"
        meta.append(f"Authors: {shown}")
    if paper.key and not paper.key.startswith("http"):
        meta.append(f"arXiv: {paper.key}{paper.version}")
    else:
        meta.append(f"Source: {paper.url}")
    if paper.primary_category:
        cats = ", ".join(dict.fromkeys([paper.primary_category] + paper.categories))
        meta.append(f"Categories: {cats}")
    if paper.published:
        meta.append(f"Submitted: {paper.published}")
    if paper.journal_ref:
        meta.append(f"Journal ref: {paper.journal_ref}")
    if paper.comment:
        meta.append(f"Author comment: {paper.comment}")
    if paper.abstract:
        meta.append(f"\nAbstract as posted:\n{paper.abstract}")

    note = ""
    if paper.truncated:
        note = (
            "\n\nNOTE: this paper was long enough that the middle was elided; you are seeing "
            "the opening ~75% and the closing ~25%. Work with what is present and flag any "
            "consequential gap under @@@caveats@@@."
        )

    if paper.source == "html":
        provenance = (
            "FULL TEXT -- from arXiv's LaTeXML HTML rendering, so the mathematics below is "
            "the authors' own LaTeX and the tables are intact. Headings are marked with #."
        )
    else:
        provenance = (
            "FULL TEXT -- extracted from the PDF, so expect two-column bleed, broken "
            "ligatures and equations flattened into unicode glyphs."
        )

    return f"""Read the paper below and produce a structured skim of it.

=== METADATA ===
{chr(10).join(meta)}

=== {provenance} ===
{text}
=== END OF PAPER ==={note}

{_FORMAT_SPEC}"""


_MARKER = re.compile(r"@@@(\w+)@@@")


def parse_response(raw: str) -> Dict[str, str]:
    """Split a marker-delimited reply into {section_key: text}."""
    out: Dict[str, str] = {}
    parts = _MARKER.split(raw)
    # parts == [preamble, key, body, key, body, ...]
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].strip().lower()
        if key in SECTION_KEYS:
            out[key] = parts[i + 1].strip()
    return out


def parse_is_usable(parsed: Dict[str, str]) -> bool:
    """A reply is usable once the load-bearing sections came back non-empty."""
    required = ("tldr", "ideas", "technical")
    return all(parsed.get(k, "").strip() for k in required)


def split_bullets(body: str) -> List[str]:
    """Pull '- ' lines out of a section body, joining wrapped continuations."""
    bullets: List[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "-\t")) or stripped in ("-",):
            bullets.append(stripped[1:].strip())
        elif re.match(r"^[\*•]\s+", stripped):
            bullets.append(re.sub(r"^[\*•]\s+", "", stripped))
        elif bullets:
            bullets[-1] += " " + stripped
        else:
            bullets.append(stripped)
    return [b for b in bullets if b]
