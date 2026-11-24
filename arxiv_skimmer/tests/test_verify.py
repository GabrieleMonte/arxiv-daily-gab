"""Hand-made checks for skimmer/verify.py and the inference tag in skimmer/tex.py.

    python3 -m pytest tests/        # or simply: python3 tests/test_verify.py
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skimmer import tex, verify  # noqa: E402
from skimmer.agents import Result  # noqa: E402
from skimmer.fetch import Paper  # noqa: E402


def found(summary: str, source: str) -> bool:
    """True when every number in a one-section summary is found in the source."""
    check = verify.check_numbers({"results": summary}, source)
    assert check.checked > 0, f"nothing extracted from {summary!r}"
    return not check.unverified


# ============================================================================
# Extraction
# ============================================================================

def test_extracts_plain_scientific_percent_and_uncertainties():
    values = verify.extract_numbers(
        r"$H_0 = 67.40 \pm 0.53$, $\beta=0.077^{+0.047}_{-0.042}$, "
        r"$\sigma < 1.2\times10^{-3}$, 2.5e-4, $12\%$ and $1{,}000$ draws"
    )
    expected = ["0.047", "0.042", "0.0012", "0.00025", "67.4", "0.53", "0.077", "12", "1000"]
    assert sorted(values) == sorted(Decimal(v) for v in expected)


def test_ignores_labels_references_years_and_ids():
    text = (
        r"See Sec. 3.2 and Eqs. (12)-(14), Fig. 10; Planck 2018 and DESI DR2 (arXiv:2608.05032),"
        r" $N_{\rm eff}$, $H_0$, $\sigma_8$, $k^{-1}$, $\tfrac94 H^2$, CLASS v3.2.1, "
        r"$\accentset{\rule{4pt}{0.4pt}}{q}$, and 3 bullets."
    )
    assert verify.extract_numbers(text) == []


def test_negative_signs_and_unicode_minus_are_ignored():
    assert verify.extract_numbers("w = −1.25") == [Decimal("1.25")]
    assert verify.extract_numbers("$w_0=-0.75$") == [Decimal("0.75")]


# ============================================================================
# Normalisation: formatting differences are not misses
# ============================================================================

def test_trailing_zeros_and_thousands_separators():
    assert found("$0.50$", "we find 0.5 here")
    assert found("$1{,}000$ and 12,500", "N = 1000 and 12500 samples")
    assert found("1000", r"$N=1\,000$")


def test_scientific_notation_forms_meet():
    summary = r"$1.2\times10^{-3}$"
    assert found(summary, "eps = 1.2e-3")
    assert found(summary, "eps = 0.0012")
    assert found(summary, "eps = 1.2 × 10−3")  # PDF-flattened superscript
    assert found(summary, r"$\epsilon=1.2\text{\times}{10}^{-3}$")  # LaTeXML siunitx
    assert found(r"$10^{-3}$", r"$(2.0\pm 0.5)\times 10^{-3}$")
    assert found(r"$1.5\times10^{-10}$", r"Br $=1.5^{+1.3}_{-0.9}\times 10^{-10}$")


def test_percentages_match_fractions():
    assert found(r"$12\%$", "a 12 per cent shift")
    assert found(r"$12\%$", "f = 0.12")
    assert found("0.12", r"a $12\%$ shift")


def test_rounding_is_accepted_only_with_enough_digits():
    assert found("13.7", r"$\Delta\chi^2 = -13.68$")
    assert found("963", "962.9 GeV")
    assert not found("0.3", "0.31")  # one significant digit: must match exactly
    assert not found("50", "49.8")  # two-digit integer: must match exactly


# ============================================================================
# Genuine misses and the result structure
# ============================================================================

def test_flags_numbers_absent_from_the_source():
    check = verify.check_numbers(
        {"tldr": "$H_0 = 73.04$", "results": r"bound $< 3\times10^{-26}$, and 73.04 again"},
        "We measure H_0 = 67.4 with 10 parameters.",
    )
    assert check.checked == 2
    assert [(u.text, u.section) for u in check.unverified] == [
        ("73.04", "tldr"), (r"3\times10^{-26}", "results"),
    ]
    assert "73.04" in check.unverified[0].context
    assert "2 not found" in verify.describe(check)


def test_missing_source_is_reported_not_raised():
    check = verify.check_numbers({"tldr": "0.5"}, "")
    assert not check.ok and check.error
    assert "not checked" in verify.describe(check)
    assert "not checked" in verify.describe(None)


# ============================================================================
# LaTeX rendering
# ============================================================================

def test_inference_tag_survives_escaping():
    out = tex._prose(r"Likely a 50% effect on $S_8$ (inference).")
    assert r"\inferencetag{}" in out
    assert r"50\%" in out and "$S_8$" in out
    assert "(inference)" not in out


def test_number_note_lists_unverified_numbers():
    paper = Paper(key="2608.00001", url="https://arxiv.org/abs/2608.00001")
    result = Result(paper=paper, sections={"results": "0.73"})
    result.numbers = verify.check_numbers(result.sections, "nothing relevant, 10 items")
    note = tex._number_note(result)
    assert "$0.73$ (Key results)" in note
    result.numbers = verify.check_numbers(result.sections, "0.73")
    assert tex._number_note(result) == ""


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} tests passed")
