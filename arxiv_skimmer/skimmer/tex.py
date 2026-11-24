"""Render the collected summaries into a compilable LaTeX document."""

from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import unicodedata
from typing import List

from . import prompts
from .agents import Result

# Symbols that need LaTeX math mode. Bare in a math span, wrapped in $...$ outside one.
_MATH_SYMBOLS = {
    "×": r"\times", "±": r"\pm", "∓": r"\mp", "·": r"\cdot", "∘": r"\circ",
    "≈": r"\approx", "≃": r"\simeq", "≅": r"\cong", "∼": r"\sim", "≡": r"\equiv",
    "≤": r"\leq", "≥": r"\geq", "≲": r"\lesssim", "≳": r"\gtrsim", "≪": r"\ll",
    "≫": r"\gg", "≠": r"\neq", "∝": r"\propto", "∞": r"\infty", "√": r"\sqrt{}",
    "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∂": r"\partial", "∇": r"\nabla",
    "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow", "⇒": r"\Rightarrow",
    "⇔": r"\Leftrightarrow", "∈": r"\in", "∉": r"\notin", "⊂": r"\subset",
    "∩": r"\cap", "∪": r"\cup", "∅": r"\emptyset", "∀": r"\forall", "∃": r"\exists",
    "°": r"^\circ", "′": r"'", "″": r"''", "ℓ": r"\ell", "ħ": r"\hbar",
    "⊙": r"\odot", "⊕": r"\oplus", "⊗": r"\otimes", "†": r"\dagger", "∥": r"\parallel",
    "⟨": r"\langle", "⟩": r"\rangle", "⌈": r"\lceil", "⌉": r"\rceil",
    "⁰": r"^{0}", "¹": r"^{1}", "²": r"^{2}", "³": r"^{3}", "⁴": r"^{4}", "⁵": r"^{5}",
    "⁶": r"^{6}", "⁷": r"^{7}", "⁸": r"^{8}", "⁹": r"^{9}", "⁻": r"^{-}",
    "₀": r"_{0}", "₁": r"_{1}", "₂": r"_{2}", "₃": r"_{3}", "₄": r"_{4}", "₅": r"_{5}",
    "₆": r"_{6}", "₇": r"_{7}", "₈": r"_{8}", "₉": r"_{9}",
}
# Greek, which arXiv abstracts carry as literal codepoints (the "$\Lambda$CDM" problem).
_GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
    "ζ": "zeta", "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa",
    "λ": "lambda", "μ": "mu", "µ": "mu", "ν": "nu", "ξ": "xi", "π": "pi",
    "ρ": "rho", "σ": "sigma", "ς": "varsigma", "τ": "tau", "υ": "upsilon",
    "φ": "phi", "ϕ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
    "ϑ": "vartheta", "ϵ": "epsilon", "ϖ": "varpi", "ϱ": "varrho",
    "Γ": "Gamma", "Δ": "Delta", "Θ": "Theta", "Λ": "Lambda", "Ξ": "Xi",
    "Π": "Pi", "Σ": "Sigma", "Υ": "Upsilon", "Φ": "Phi", "Ψ": "Psi", "Ω": "Omega",
}
_MATH_SYMBOLS.update({ch: "\\" + name for ch, name in _GREEK.items()})

# Punctuation that only makes sense outside math mode.
_TEXT_SYMBOLS = {
    "—": "---", "–": "--", "‘": "`", "’": "'", "“": "``", "”": "''",
    "…": r"\ldots{}", "\u00a0": "~", "−": "-", "®": "", "™": "", "•": r"$\bullet$",
    "§": r"\S{}", "¶": r"\P{}", "©": r"\copyright{}", "‐": "-", "‑": "-",
}

# Math spans are passed through untouched; everything else gets escaped.
_MATH_SPAN = re.compile(
    r"(\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|\$(?:\\.|[^$\\])*\$)", re.DOTALL
)


def _fix_unicode(s: str, math_mode: bool = False) -> str:
    """Map codepoints pdfLaTeX cannot set onto LaTeX, correctly for the surrounding mode."""
    out = []
    for ch in s:
        if ch in _MATH_SYMBOLS:
            cmd = _MATH_SYMBOLS[ch]
            out.append(cmd if math_mode else f"${cmd}$")
        elif ch in _TEXT_SYMBOLS:
            out.append("" if math_mode else _TEXT_SYMBOLS[ch])
        elif ord(ch) < 128 or ord(ch) <= 0x24F:  # ASCII + Latin-1/Ext-A: pdfLaTeX copes
            out.append(ch)
        else:
            folded = "".join(
                c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
            )
            out.append(folded if folded and folded.isascii() else "")
    return "".join(out)


def _escape_plain(s: str) -> str:
    """Escape LaTeX specials in a run of non-math text, leaving existing escapes alone."""
    s = re.sub(r"(?<!\\)&", r"\\&", s)
    s = re.sub(r"(?<!\\)%", r"\\%", s)
    s = re.sub(r"(?<!\\)#", r"\\#", s)
    s = re.sub(r"(?<!\\)_", r"\\_", s)
    s = re.sub(r"(?<!\\)\^", r"\\^{}", s)
    s = re.sub(r"(?<!\\)~", r"\\textasciitilde{}", s)
    return s


def _collapse_scripts(s: str) -> str:
    """Merge adjacent super/subscript groups so "10^-3" reads as one exponent."""
    pattern = re.compile(r"\$(\^|_)\{([^{}]*)\}\$\$\1\{([^{}]*)\}\$")
    while True:
        merged = pattern.sub(r"$\1{\2\3}$", s)
        if merged == s:
            return s
        s = merged


def _balance_dollars(s: str) -> str:
    """Drop a trailing unmatched '$' so one stray symbol can't break the whole build."""
    positions = [m.start() for m in re.finditer(r"(?<!\\)\$", s)]
    if len(positions) % 2 == 0:
        return s
    cut = positions[-1]
    return s[:cut] + s[cut + 1 :]


def _balance_display_math(s: str) -> str:
    """Drop an unmatched \\[ or \\] so a stray delimiter can't swallow the document."""
    opens = [m.start() for m in re.finditer(r"\\\[", s)]
    closes = [m.start() for m in re.finditer(r"\\\]", s)]
    if len(opens) == len(closes):
        return s
    cut = (opens if len(opens) > len(closes) else closes)[-1]
    return s[:cut] + s[cut + 2 :]


def _balance_braces(s: str) -> str:
    """Close any group the model left open; LaTeX would otherwise eat the rest of the page."""
    depth = 0
    i = 0
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            if depth == 0:  # a stray closer: neutralise it
                s = s[:i] + s[i + 1 :]
                continue
            depth -= 1
        i += 1
    return s + "}" * depth


# Commands that only mean anything inside maths. A summary quoting a formula as prose
# ("Eq. (58) is garbled: \frac{\partial f}{.}") would otherwise abort the build.
_MATH_ONLY = frozenset("""
frac dfrac tfrac sqrt sum prod int iint iiint oint lim limits nolimits
partial nabla infty cdot cdots ldots vdots ddots times div pm mp
approx simeq sim equiv propto neq ne leq le geq ge ll gg lesssim gtrsim
in notin ni subset supset subseteq supseteq cap cup setminus emptyset varnothing
to rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow mapsto
langle rangle lvert rvert lVert rVert mid parallel perp
mathrm mathbf mathit mathsf mathtt mathcal mathbb mathfrak mathord bm boldsymbol
bar hat tilde vec dot ddot check breve acute grave overline widehat widetilde
ell odot oplus ominus otimes star ast circ bullet dagger top bot
ln log exp sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth
max min det sup inf operatorname slashed accentset
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda
mu nu xi omicron pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega
Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
""".split())

# Delimiter sizers are only valid as a matched pair, so in prose we drop them and keep
# the bracket they were sizing.
_SIZERS = frozenset("left right middle big Big bigg Bigg bigl bigr Bigl Bigr".split())

_COMMAND = re.compile(r"\\([a-zA-Z]+)")


def _group_end(text: str, start: int) -> int:
    """Index just past the balanced {...} group at `start`, or `start` if it is unbalanced."""
    depth, i = 0, start
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
    return start


def _wrap_stray_math(text: str) -> str:
    """Put $...$ around maths commands that were written as prose."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] != "\\":
            out.append(text[i])
            i += 1
            continue
        m = _COMMAND.match(text, i)
        if not m:
            out.append(text[i : i + 2])
            i += 2
            continue
        name, j = m.group(1), m.end()
        if name in _SIZERS:
            i = j  # the delimiter that follows can stand on its own in text
            continue
        if name not in _MATH_ONLY:
            out.append(m.group(0))
            i = j
            continue
        while j < n and text[j] == "{":  # take every argument glued to the command
            end = _group_end(text, j)
            if end == j:
                break
            j = end
        out.append("$" + text[i:j] + "$")
        i = j
    return "".join(out)


def _rescue_text_math(s: str) -> str:
    """Apply _wrap_stray_math to the prose only, leaving real math spans untouched."""
    pieces = _MATH_SPAN.split(s)
    for i in range(0, len(pieces), 2):
        pieces[i] = _wrap_stray_math(pieces[i])
    return "".join(pieces)


def tex_escape(s: str) -> str:
    """Make text safe for LaTeX while preserving any math the model wrote."""
    if not s:
        return ""
    # Rescue first: stray maths in prose becomes a real math span before we split.
    pieces = _MATH_SPAN.split(
        _rescue_text_math(_balance_display_math(_balance_dollars(s)))
    )
    # Odd indices are the math spans: Unicode still needs mapping there, but bare,
    # and LaTeX specials inside math (^, _, &) must be left exactly as written.
    return _balance_braces("".join(
        _fix_unicode(piece, math_mode=True) if i % 2
        else _collapse_scripts(_fix_unicode(_escape_plain(piece), math_mode=False))
        for i, piece in enumerate(pieces)
    ))


# The reading agent ends its own interpretations with "(inference)" (see prompts.py).
# Plain text to tex_escape, so the tag is swapped for a macro after escaping.
_INFERENCE_TAG = re.compile(r"\(\s*inference\s*\)", re.I)


def _prose(s: str) -> str:
    """tex_escape for summary text, with the agent's "(inference)" tags typeset distinctly."""
    return _INFERENCE_TAG.sub(r"\\inferencetag{}", tex_escape(s))


def _authors(paper, limit: int = 8) -> str:
    if not paper.authors:
        return ""
    if len(paper.authors) <= limit:
        return tex_escape(", ".join(paper.authors))
    return tex_escape(", ".join(paper.authors[:limit])) + r" \textit{et al.} " + (
        f"({len(paper.authors)} authors)"
    )


def _bullets(body: str) -> str:
    items = prompts.split_bullets(body)
    if not items:
        return ""
    lines = [r"\begin{itemize}[leftmargin=1.2em,itemsep=2pt,topsep=3pt,parsep=0pt]"]
    lines += [r"  \item " + _prose(item) for item in items]
    lines.append(r"\end{itemize}")
    return "\n".join(lines)


def _paragraphs(body: str) -> str:
    chunks = [c.strip() for c in re.split(r"\n\s*\n", body) if c.strip()]
    return "\n\n".join(_prose(re.sub(r"\s*\n\s*", " ", c)) for c in chunks)


PREAMBLE = r"""\documentclass[11pt,a4paper]{article}
\usepackage[margin=0.95in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{amsmath,amssymb,amsfonts,bm}
\usepackage{slashed,accents}  % \slashed / \accentset turn up in HEP abstracts
\usepackage{enumitem}
\usepackage{microtype}
\usepackage{xcolor}
\usepackage{titlesec}
\usepackage{parskip}
\definecolor{linkblue}{HTML}{1A4F8B}
\definecolor{rulegrey}{HTML}{999999}
\usepackage[colorlinks=true,linkcolor=linkblue,urlcolor=linkblue,citecolor=linkblue]{hyperref}
\usepackage{bookmark}

\titleformat{\section}{\normalfont\large\bfseries}{\thesection.}{0.6em}{}
\titleformat{\subsection}[runin]{\normalfont\bfseries\scshape}{}{0em}{}[\;---\;]
\titlespacing*{\subsection}{0pt}{8pt}{0.6em}
\setlist{nosep}

\newcommand{\meta}[1]{{\small\color{black!65}#1\par}}
\newcommand{\paperrule}{\vspace{3pt}{\color{rulegrey}\hrule height 0.4pt}\vspace{6pt}}
\newcommand{\inferencetag}{{\footnotesize\itshape\color{black!55}(inference)}}
"""


# Every control sequence our own preamble, LaTeX, amsmath/amssymb/bm, enumitem,
# hyperref, xcolor and titlesec already provide. Anything outside this set is
# assumed to be an author macro that rode in with an abstract.
_KNOWN_COMMANDS = frozenset("""
documentclass usepackage newcommand renewcommand providecommand definecolor color textcolor
begin end item section subsection subsubsection paragraph label ref pageref cite
title author date maketitle tableofcontents addcontentsline clearpage newpage pagebreak
hyperref href url texttt textbf textit textsl textsc emph underline small large Large huge
normalfont bfseries itshape scshape rmfamily sffamily ttfamily rm bf it sl sc tt sf
par vspace hspace quad qquad hrule rule noindent centering raggedright linewidth textwidth
thesection thesubsection titleformat titlespacing setlist verb footnote thanks
frac dfrac tfrac sqrt sum prod int iint iiint oint lim max min sup inf det exp log ln lg
sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth operatorname
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu
xi omicron pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega
Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
mathrm mathbf mathit mathsf mathtt mathcal mathbb mathfrak mathord boldsymbol bm text
bar hat tilde vec dot ddot dddot overline underline widehat widetilde check breve acute grave
left right big Big bigg Bigg langle rangle lvert rVert lVert rvert mid parallel perp
times div pm mp cdot cdots ldots dots vdots ddots ast star circ oplus otimes bullet dagger
approx simeq sim equiv propto neq ne leq le geq ge ll gg lesssim gtrsim in notin ni subset
supset subseteq supseteq cap cup setminus emptyset varnothing forall exists nabla partial
infty to rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow mapsto
uparrow downarrow longrightarrow implies iff not
ell hbar imath jmath Re Im wp aleph angle triangle top bot odot oplus ominus
prime degree celsius textasciitilde textbackslash textasciicircum textunderscore
nonumber notag qedhere hfill vfill smallskip medskip bigskip newline linebreak
meta paperrule inferencetag
""".split())

# Author macros are usually a symbol glued to a label ("\muT", "\Omegah", "\DeltaN").
# Split on the longest symbol that starts the name; the rest becomes ordinary math.
_PREFIXABLE = tuple(sorted(
    """alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda
    mu nu xi pi varpi rho varrho sigma tau upsilon phi varphi chi psi omega
    Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
    ell nabla partial infty sum int prod bar hat tilde dot vec""".split(),
    key=len, reverse=True,
))


def _macro_shim(name: str) -> str:
    """A definition for a macro we have no source for, chosen so the maths still reads."""
    for prefix in _PREFIXABLE:
        if name.startswith(prefix) and len(name) > len(prefix):
            return r"\ensuremath{\%s %s}" % (prefix, name[len(prefix):])
    return r"\ensuremath{\mathrm{%s}}" % name


def _macro_shims(body: str) -> str:
    """Define every control sequence the body uses that nothing else defines.

    arXiv hands back abstracts with the authors' own macros intact (\\muT, \\keV,
    \\sigmav, \\slashed ...), and one of those is enough to kill a 95-paper build.
    \\providecommand is a no-op when the name already exists, so shimming generously
    costs nothing and cannot override a real definition.
    """
    used = {m.group(1) for m in re.finditer(r"\\([a-zA-Z]+)", body)}
    unknown = sorted(used - _KNOWN_COMMANDS)
    if not unknown:
        return ""
    lines = [r"% --- fallbacks for author macros that came in with the abstracts ---"]
    lines += [r"\providecommand{\%s}{%s}" % (n, _macro_shim(n)) for n in unknown]
    return "\n".join(lines) + "\n"


def render(results: List[Result], title: str = "arXiv Skim", subtitle: str = "") -> str:
    good = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    today = _dt.date.today().strftime("%d %B %Y")

    out = []
    out.append(r"\title{\textbf{" + tex_escape(title) + r"}\\[2pt]\large " + tex_escape(subtitle or f"{len(good)} papers") + "}")
    out.append(r"\date{" + today + "}")
    out.append(r"\author{}")
    out.append(r"\begin{document}")
    out.append(r"\maketitle")
    out.append(r"\vspace{-2em}")

    # --- contents at a glance ------------------------------------------------
    if len(good) > 1:
        out.append(r"\section*{At a glance}")
        out.append(r"\addcontentsline{toc}{section}{At a glance}")
        out.append(r"\begin{enumerate}[leftmargin=1.6em,itemsep=4pt]")
        for r in good:
            link = r"\hyperref[paper:" + _label(r) + r"]{" + tex_escape(r.paper.display_title) + "}"
            out.append(r"  \item \textbf{" + link + r"}\\ " + _prose(r.sections.get("tldr", "")))
        out.append(r"\end{enumerate}")
        out.append(r"\clearpage")
        out.append(r"\tableofcontents")
        out.append(r"\clearpage")

    # --- one section per paper -----------------------------------------------
    for r in good:
        out.append(_render_paper(r))

    if bad:
        out.append(r"\clearpage")
        out.append(r"\section*{Papers that could not be read}")
        out.append(r"\addcontentsline{toc}{section}{Papers that could not be read}")
        out.append(r"\begin{itemize}[leftmargin=1.2em]")
        for r in bad:
            ref = r.paper.key if not r.paper.key.startswith("http") else r.paper.url
            out.append(
                r"  \item \texttt{" + tex_escape(ref) + "} --- " + tex_escape(r.error)
            )
        out.append(r"\end{itemize}")

    out.append(r"\end{document}")
    body = "\n".join(out)
    return PREAMBLE + _macro_shims(body) + body + "\n"


def _label(r: Result) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", r.paper.key)[:40] or "paper"


def _render_paper(r: Result) -> str:
    p = r.paper
    lines = [r"\section{" + tex_escape(p.display_title) + "}", r"\label{paper:" + _label(r) + "}"]

    authors = _authors(p)
    if authors:
        lines.append(r"\meta{\textit{" + authors + r"}}")

    bits = []
    if p.key.startswith("http"):
        bits.append(r"\href{" + p.url + r"}{source PDF}")
    else:
        bits.append(r"\href{" + p.url + r"}{arXiv:" + tex_escape(p.key + p.version) + "}")
    if p.primary_category:
        bits.append(tex_escape(p.primary_category))
    if p.published:
        bits.append(tex_escape(p.published))
    if p.n_pages:
        bits.append(f"{p.n_pages} pp.")
    if p.journal_ref:
        bits.append(tex_escape(p.journal_ref))
    lines.append(r"\meta{" + r" \;$\cdot$\; ".join(bits) + "}")
    lines.append(r"\paperrule")

    for key, heading, bulleted in prompts.SECTIONS:
        body = r.sections.get(key, "").strip()
        if not body:
            continue
        lines.append(r"\subsection{" + heading + "}")
        lines.append(_bullets(body) if bulleted else _paragraphs(body))

    note = _number_note(r)
    if note:
        lines.append(note)

    if p.abstract:
        lines.append(r"\subsection{Abstract as posted}")
        lines.append(r"{\small " + tex_escape(p.abstract) + r"\par}")

    lines.append(r"\clearpage")
    return "\n".join(lines)


MAX_NOTED_NUMBERS = 12  # unverified numbers spelled out per paper before "and N more"


def _number_note(r: Result) -> str:
    """A small grey line listing summary numbers not found in the paper text, if any.

    verify.py builds each number from digits, thousands separators, \\times10^{...} and \\%
    only, so it is safe inside $...$ without further escaping.
    """
    check = r.numbers
    if check is None or not check.unverified:
        return ""
    headings = {key: heading for key, heading, _ in prompts.SECTIONS}
    shown = check.unverified[:MAX_NOTED_NUMBERS]

    groups: List[str] = []
    for key, _, _ in prompts.SECTIONS:
        values = [f"${u.text}$" for u in shown if u.section == key]
        if values:
            groups.append(", ".join(values) + f" ({headings[key]})")
    extra = len(check.unverified) - len(shown)
    tail = f"; and {extra} more" if extra > 0 else ""

    return (
        r"\par\meta{\textit{Numbers not found in the paper text:} "
        + "; ".join(groups) + tail + r".}"
    )


def compile_pdf(tex_path: str, log=print) -> bool:
    """Run latexmk. Returns True on success; on failure prints the useful log lines."""
    workdir = os.path.dirname(os.path.abspath(tex_path)) or "."
    name = os.path.basename(tex_path)
    exe = "latexmk"
    cmd = [exe, "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-f", name]
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        log("  ! latexmk not found -- the .tex file is written, compile it yourself.")
        return False
    except subprocess.TimeoutExpired:
        log("  ! latexmk timed out.")
        return False

    pdf = os.path.splitext(tex_path)[0] + ".pdf"
    if proc.returncode == 0 and os.path.exists(pdf):
        return True

    log("  ! LaTeX reported errors:")
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.startswith("!") or "Undefined control" in line or line.startswith("l."):
            log("    " + line[:160])
    return os.path.exists(pdf)
