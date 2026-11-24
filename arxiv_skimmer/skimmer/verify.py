"""Check that the numbers quoted in a summary occur in the paper the agent actually read.

Every number in the summary's sections is pulled out, turned into an exact decimal value,
and looked up among the numbers found in the source the agent was given (the extracted
HTML/PDF text plus the title, abstract, author comment and journal ref). Numbers that cannot
be found are reported, never corrected: the check is advisory and must not fail a run.

Comparison is by absolute value, so "0.50" = "0.5" = "50\\%" (as 0.5) = "5.0\\times10^{-1}",
and LaTeX, plain, e-notation and PDF-flattened forms ("1.2 × 10−3") all meet in the middle.
A summary value also counts as found when a more precise source value rounds to it at the
summary's own precision ("13.7" from "13.68", "963" from "962.9"), provided it has enough
significant digits for that to be informative (see the constants below).

Deliberately ignored in the summary, because they are labels rather than results:
single-digit integers, 4-digit integers that read as years (1900-2099), arXiv ids, URLs,
version strings ("1.5.0"), TeX dimensions ("0.4pt"), cross-references ("Sec. 3.2",
"Eq. (12)", "Fig. 4"), sub- and superscripts other than powers of ten ("H_0", "h^{-1}",
"N_{\\rm eff}") and digits glued to a preceding letter ("DR2", "Y1", "S4").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Dict, List, Optional, Set, Tuple

from . import prompts

# Integers below this carry no information: every paper contains every digit.
MIN_INTEGER = 10
# Four-digit integers in this range are read as years ("Planck 2018", "DESI 2024").
YEAR_RANGE = (1900, 2099)
# Rounding tolerance only applies to summary values with at least this many significant
# digits; "0.3" or "50" must appear as such, or almost any paper would hold something near.
MIN_SIGFIGS_ROUNDED_DECIMAL = 2
MIN_SIGFIGS_ROUNDED_INTEGER = 3
# Characters of summary text kept either side of an unverified number, for diagnosis.
CONTEXT_CHARS = 40


# ============================================================================
# Result types
# ============================================================================

@dataclass
class UnverifiedNumber:
    text: str  # the number as written in the summary (LaTeX, safe inside $...$)
    section: str  # summary section key it came from, e.g. "results"
    context: str  # a snippet of the surrounding summary text


@dataclass
class NumberCheck:
    checked: int = 0  # distinct numbers looked up
    unverified: List[UnverifiedNumber] = field(default_factory=list)
    n_rounded: int = 0  # found only after rounding a more precise source value
    n_source_numbers: int = 0  # distinct values harvested from the source
    error: str = ""  # why the check could not run; empty when it did

    @property
    def ok(self) -> bool:
        return not self.error


# ============================================================================
# Normalisation shared by summary and source
# ============================================================================

_DASHES = dict.fromkeys(map(ord, "\u2212\u2010\u2011\u2012\u2013\u2014\ufe63\uff0d"), "-")
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
_SUPERSCRIPT_RUN = re.compile("[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+")


def _normalise(text: str) -> str:
    """Map unicode and LaTeX spellings of numbers onto one plain-ASCII LaTeX form."""
    text = text.translate(_DASHES)
    text = _SUPERSCRIPT_RUN.sub(
        lambda m: "^{" + m.group(0).translate(_SUPERSCRIPT_DIGITS) + "}", text
    )
    text = text.replace("\u00d7", r" \times ").replace("\u00b7", r" \cdot ")
    text = text.replace("\u2009", r"\,").replace("\u202f", r"\,").replace("\u00a0", " ")
    # LaTeXML renders siunitx as "9.0\text{\times}{10}^{-5}" and tables as "2\!\times\!10^{4}".
    text = re.sub(r"\\(?:text|mathrm)\s*\{\s*(\\times|\\cdot)\s*\}", r" \1 ", text)
    text = re.sub(r"\{\s*10\s*\}(?=\s*\^)", "10", text)
    text = text.replace(r"\!", "")
    # Unbraced fraction digits: \tfrac94 is 9/4, not ninety-four.
    text = re.sub(r"\\[dt]?frac\s*(\d)\s*(\d)", r"\\frac{\1}{\2}", text)
    # A space after every control word, so "\sim10" does not read as a word ending in 10.
    text = re.sub(r"(\\[A-Za-z]+)", r"\1 ", text)
    return text.replace("~", " ")


def _dec(s: str) -> Optional[Decimal]:
    """Decimal for a digit string with thousands separators removed, or None."""
    try:
        return abs(Decimal(re.sub(r"\{,\}|\\,|,", "", s)))
    except InvalidOperation:
        return None


def _pow10(mantissa: Optional[str], sign: str, exponent: str) -> Optional[Decimal]:
    """mantissa x 10^(sign exponent) as an exact Decimal."""
    base = _dec(mantissa) if mantissa else Decimal(1)
    if base is None or len(exponent) > 4:
        return None
    return base.scaleb(-int(exponent) if sign == "-" else int(exponent))


# Building blocks. Exponent: ^{-3}, ^{ - 3 }, ^-3, ^3.
_DECIMAL = r"\d+(?:\.\d+)?"
_TIMES = r"(?:\\times|\\cdot|x)"
_EXPONENT = r"\^\s*(?:\{\s*([+-]?)\s*(\d+)\s*\}|([+-]?)(\d+))"
_PERCENT = re.compile(r"\s*(?:\\%|%|per\s*cent\b|percent\b)", re.I)

# m \times 10^{e} or a bare 10^{e}; the mantissa is optional.
_SCI_LATEX = re.compile(
    r"(?:(?<![\w.])(" + _DECIMAL + r")\s*" + _TIMES + r"\s*|(?<![\w.]))10\s*" + _EXPONENT
)
_SCI_E = re.compile(r"(?<![\w.])(" + _DECIMAL + r")[eE]([+-]?)(\d+)(?![\w.])")


# ============================================================================
# Summary side: what is being claimed
# ============================================================================

_URL = re.compile(r"https?://\S+")
# New-style ids need a plausible YYMM so "1420.4058" (MHz) is not mistaken for one.
_ARXIV_ID = re.compile(
    r"(?:arXiv:\s*)?(?:\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}(?:v\d+)?"
    r"|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)",
    re.I,
)
_REF_ITEM = r"\(?[A-Z]?\d+(?:\.\d+)*[a-z]?\)?"
_CROSS_REF = re.compile(
    r"(?:\b(?:Sec|Sect|Section|Eqn?|Equation|Fig|Figure|Tab|Table|App|Appendix|Ref|"
    r"Reference|Chapter|Ch|Theorem|Lemma)s?\b\.?|\\S\s|§)"
    r"\s*" + _REF_ITEM + r"(?:\s*(?:,|-{1,3}|and|&|to)\s*" + _REF_ITEM + r")*",
    re.I,
)
# Asymmetric errors, ^{+a}_{-b} in either order: the only scripts that hold results.
_ASYM = re.compile(
    r"\^\s*\{\s*\+\s*(" + _DECIMAL + r")\s*\}\s*_\s*\{\s*-\s*(" + _DECIMAL + r")\s*\}"
    r"|_\s*\{\s*-\s*(" + _DECIMAL + r")\s*\}\s*\^\s*\{\s*\+\s*(" + _DECIMAL + r")\s*\}"
)
_TEX_DIMENSION = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:pt|em|ex|bp)\b")
_SCRIPT_OPEN = re.compile(r"[_^]\s*\{")
_SCRIPT_TOKEN = re.compile(r"[_^]\s*(?:\\[A-Za-z]+|[+-]?[A-Za-z0-9])")
# 1,000 / 1{,}000 / 1\,000 grouped thousands first, then plain (possibly dotted) numbers.
_NUMBER = re.compile(
    r"(?<![\w.])(\d{1,3}(?:(?:,|\{,\}|\\,)\d{3})+(?!\d)(?:\.\d+)?|\d+(?:\.\d+)*)"
)

# One claimed number: (display text, section, context, alternatives). It is found when
# every value in at least one alternative is found.
_Claim = Tuple[str, str, str, List[Tuple[Decimal, ...]]]


def _blank(text: str, start: int, end: int) -> str:
    """Overwrite text[start:end] with spaces, keeping every other offset unchanged."""
    return text[:start] + " " * (end - start) + text[end:]


def _blank_all(text: str, pattern: re.Pattern) -> str:
    for m in reversed(list(pattern.finditer(text))):
        text = _blank(text, m.start(), m.end())
    return text


def _group_end(text: str, start: int) -> int:
    """Index just past the balanced {...} group opening at `start`, or -1 if unbalanced."""
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _blank_scripts(text: str) -> str:
    """Blank every sub/superscript: braced groups (nested braces allowed), then x_0 tokens."""
    out = text
    for m in _SCRIPT_OPEN.finditer(text):
        end = _group_end(text, m.end() - 1)
        if end > 0:
            out = _blank(out, m.start(), end)
    return _blank_all(out, _SCRIPT_TOKEN)


def _context(text: str, start: int, end: int) -> str:
    snippet = text[max(0, start - CONTEXT_CHARS) : end + CONTEXT_CHARS]
    return re.sub(r"\s+", " ", snippet).strip()


def _claims_in(body: str, section: str) -> List[_Claim]:
    """Every checkable number in one summary section."""
    shown = _normalise(body)
    work = shown
    for pattern in (_URL, _ARXIV_ID, _CROSS_REF, _TEX_DIMENSION):
        work = _blank_all(work, pattern)
    claims: List[_Claim] = []

    # --- asymmetric uncertainties --------------------------------------------
    for m in _ASYM.finditer(work):
        for raw in m.groups():
            value = _dec(raw) if raw else None
            if value is not None:
                claims.append((raw, section, _context(shown, *m.span()), [(value,)]))
    work = _blank_all(work, _ASYM)

    # --- powers of ten --------------------------------------------------------
    for m in _SCI_LATEX.finditer(work):
        mant, sign = m.group(1), m.group(2) or m.group(4) or ""
        exponent = m.group(3) or m.group(5)
        value = _pow10(mant, sign, exponent)
        if value is not None:
            text = (f"{mant}\\times" if mant else "") + f"10^{{{sign}{exponent}}}"
            alternatives = [(value,)]
            if mant:  # "2\times\frac{10^9 ...}" in the paper becomes "2\times10^9" here
                alternatives.append((_dec(mant), _pow10(None, sign, exponent)))
            claims.append((text, section, _context(shown, *m.span()), alternatives))
    work = _blank_all(work, _SCI_LATEX)
    for m in _SCI_E.finditer(work):
        value = _pow10(m.group(1), m.group(2), m.group(3))
        if value is not None:
            claims.append((m.group(0), section, _context(shown, *m.span()), [(value,)]))
    work = _blank_all(work, _SCI_E)

    # --- remaining scripts are labels and unit powers: drop them ----------------
    work = _blank_scripts(work)

    # --- plain numbers ----------------------------------------------------------
    for m in _NUMBER.finditer(work):
        token = m.group(1)
        grouped = bool(re.search(r",|\\,", token))
        if not grouped and token.count(".") > 1:
            continue  # version string or section number
        value = _dec(token)
        if value is None:
            continue
        if "." not in token:
            if value < MIN_INTEGER:
                continue
            if not grouped and len(token) == 4 and YEAR_RANGE[0] <= value <= YEAR_RANGE[1]:
                continue
        alternatives: List[Tuple[Decimal, ...]] = [(value,)]
        if grouped:  # "128,256" may be a list rather than one number
            parts = [_dec(p) for p in re.split(r"\{,\}|\\,|,", token)]
            alternatives.append(tuple(p for p in parts if p is not None))
        text = token
        tail = _PERCENT.match(work, m.end())
        if tail:
            alternatives.append((value.scaleb(-2),))
            text += r"\%"
        claims.append((text, section, _context(shown, *m.span()), alternatives))

    return claims


def extract_numbers(text: str) -> List[Decimal]:
    """The primary value of every checkable number in a piece of summary text."""
    return [alts[0][0] for _, _, _, alts in _claims_in(text, "")]


# ============================================================================
# Source side: everything that could legitimately be quoted
# ============================================================================

_SRC_PLAIN = re.compile(r"(" + _DECIMAL + r")(\s*(?:\\%|%))?")
_SRC_GROUPED = re.compile(r"\d{1,3}(?:(?:,|\{,\}|\\,)\d{3})+(?:\.\d+)?")
_SRC_SCI = re.compile(
    r"(?:(" + _DECIMAL + r")\s*" + _TIMES + r"\s*)?10\s*" + _EXPONENT
)
# (1.2 \pm 0.3) \times 10^{-3}: every number in the bracket carries the power.
_SRC_BRACKET_SCI = re.compile(r"\(([^()]{1,80})\)\s*(?:\\times|\\cdot)\s*10\s*" + _EXPONENT)
# PDF extraction flattens superscripts: "1.2 × 10−3", "2 × 1012", "10−3".
_SRC_PDF_SCI = re.compile(
    r"(" + _DECIMAL + r")\s*(?:\\times|\\cdot)\s*10\s*(-?)\s*(\d{1,3})(?![\d.])"
)
_SRC_PDF_NEG = re.compile(r"(?<![\d.])10\s*-\s*(\d{1,3})(?![\d.])")


def source_numbers(text: str) -> Set[Decimal]:
    """Every value the source text could be said to contain, read as liberally as possible."""
    text = _normalise(text)
    values: Set[Decimal] = set()

    def add(value: Optional[Decimal]) -> None:
        if value is not None:
            values.add(value)

    for m in _SRC_PLAIN.finditer(text):
        value = _dec(m.group(1))
        add(value)
        if m.group(2) and value is not None:
            add(value.scaleb(-2))
    for m in _SRC_GROUPED.finditer(text):
        add(_dec(m.group(0)))
    # A second pass with ^{+a}_{-b} removed joins "1.5^{+1.3}_{-0.9}\\times10^{-10}".
    for variant in (text, _ASYM.sub(" ", text)):
        for m in _SRC_SCI.finditer(variant):
            sign, exponent = m.group(2) or m.group(4) or "", m.group(3) or m.group(5)
            add(_pow10(None, sign, exponent))
            if m.group(1):
                add(_pow10(m.group(1), sign, exponent))
    for m in _SRC_BRACKET_SCI.finditer(text):
        sign, exponent = m.group(2) or m.group(4) or "", m.group(3) or m.group(5)
        for inner in re.findall(_DECIMAL, m.group(1)):
            add(_pow10(inner, sign, exponent))
    for m in _SCI_E.finditer(text):
        add(_pow10(m.group(1), m.group(2), m.group(3)))
    for m in _SRC_PDF_SCI.finditer(text):
        add(_pow10(None, m.group(2), m.group(3)))
        add(_pow10(m.group(1), m.group(2), m.group(3)))
    for m in _SRC_PDF_NEG.finditer(text):
        add(_pow10(None, "-", m.group(1)))
    return values


# ============================================================================
# Matching
# ============================================================================

class _Lookup:
    """Exact membership, plus 'rounds to' membership at a given number of decimal places."""

    def __init__(self, values: Set[Decimal]):
        self.values = values
        self._rounded: Dict[int, Set[Decimal]] = {}

    def _at(self, places: int) -> Set[Decimal]:
        if places not in self._rounded:
            quantum = Decimal(1).scaleb(-places)
            rounded: Set[Decimal] = set()
            with localcontext() as ctx:
                ctx.prec = 200  # wide enough that quantize never runs out of digits
                for v in self.values:
                    if v.as_tuple().exponent < -places:  # only more precise values
                        try:
                            rounded.add(v.quantize(quantum, rounding=ROUND_HALF_UP))
                        except InvalidOperation:
                            pass
            self._rounded[places] = rounded
        return self._rounded[places]

    def find(self, value: Decimal) -> str:
        """'exact', 'rounded' or '' for a value claimed by the summary."""
        if value in self.values:
            return "exact"
        places = -value.as_tuple().exponent
        sigfigs = len(value.normalize().as_tuple().digits)
        needed = MIN_SIGFIGS_ROUNDED_DECIMAL if places > 0 else MIN_SIGFIGS_ROUNDED_INTEGER
        if places >= 0 and sigfigs >= needed and value in self._at(places):
            return "rounded"
        return ""


def paper_source(paper) -> str:
    """The text the reading agent was shown: metadata, abstract and full text."""
    parts = [paper.title, paper.abstract, paper.comment, paper.journal_ref, paper.text]
    return "\n\n".join(p for p in parts if p)


def check_numbers(sections: Dict[str, str], source: str) -> NumberCheck:
    """Look every number in the summary sections up in the source text.

    sections -- {section key: summary text}, as returned by prompts.parse_response
    source   -- the text the agent read (see paper_source)

    Each distinct value is counted once, attributed to the first section it appears in.
    """
    if not source or not source.strip():
        return NumberCheck(error="no source text available")

    lookup = _Lookup(source_numbers(source))
    result = NumberCheck(n_source_numbers=len(lookup.values))
    seen: Set[Decimal] = set()

    for key in prompts.SECTION_KEYS:
        for text, section, context, alternatives in _claims_in(sections.get(key, ""), key):
            primary = alternatives[0][0]
            if primary in seen:
                continue
            seen.add(primary)
            result.checked += 1

            verdicts = [[lookup.find(v) for v in alt] for alt in alternatives]
            exact = any(all(v == "exact" for v in vs) for vs in verdicts)
            found = exact or any(all(vs) for vs in verdicts)
            if not found:
                result.unverified.append(UnverifiedNumber(text, section, context))
            elif not exact:
                result.n_rounded += 1
    return result


def describe(check: Optional[NumberCheck], max_listed: int = 4) -> str:
    """One terminal line: how many numbers were checked and which were not found."""
    if check is None:
        return "numbers not checked"
    if check.error:
        return f"numbers not checked ({check.error})"
    if not check.checked:
        return "  0 numbers to check"
    if not check.unverified:
        return f"{check.checked:>3} numbers checked, all found in the source"
    shown = ", ".join(u.text for u in check.unverified[:max_listed])
    extra = len(check.unverified) - max_listed
    more = f" (+{extra} more)" if extra > 0 else ""
    return (
        f"{check.checked:>3} numbers checked, {len(check.unverified)} not found: {shown}{more}"
    )
