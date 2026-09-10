"""Citation verification — the guarantee that a quote shown to a physician was
actually said.

The interesting cases are the ones in the middle: a model that tidies filler
words out of a real quote should keep its evidence, and a model that invents a
fluent, plausible quote must lose it. Both are tested here, because a
verification step that only catches obvious nonsense is worse than none — it
lends credibility to whatever gets through.
"""
from __future__ import annotations

import pytest

from app.ai import citations

TRANSCRIPT = (
    "Doctor: namaste, kya problem hai? "
    "Patient: uh, mujhe chest pain hai since, since two days, aur bahut sweating ho rahi hai. "
    "Doctor: koi shortness of breath? Patient: haan thoda, especially stairs chadhte waqt. "
    "Doctor: any past history? Patient: sugar hai, metformin leta hoon. "
    "Doctor: koi dawai se allergy? Patient: nahi, no known allergies. "
    "Doctor: BP dekhte hain. It is one forty over ninety."
)


# --------------------------------------------------------------------- #
# Supported quotes
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("quote", [
    "mujhe chest pain hai",
    "bahut sweating ho rahi hai",
    "sugar hai, metformin leta hoon",
    "no known allergies",
])
def test_verbatim_quotes_verify_exactly(quote):
    assert citations.verify(quote, TRANSCRIPT).verdict == "exact"


def test_case_and_punctuation_differences_still_verify():
    v = citations.verify("Mujhe CHEST PAIN hai!", TRANSCRIPT)
    assert v.verdict == "exact"


def test_smart_quotes_and_dashes_are_normalised():
    transcript = "Patient: it's a sharp pain — worse on breathing in."
    v = citations.verify("it’s a sharp pain — worse on breathing in", transcript)
    assert v.verdict == "exact"


def test_tidied_paraphrase_of_a_real_quote_is_accepted_as_near():
    """The model removing the stutter is benign and should keep its evidence.

    Rejecting this trains the model's output to be useless: the physician sees
    fields with no evidence chip and stops trusting the ones that have it.
    """
    v = citations.verify("mujhe chest pain hai since two days", TRANSCRIPT)
    assert v.verdict == "near"
    assert v.similarity >= citations.NEAR_MATCH_THRESHOLD
    assert v.supported


# --------------------------------------------------------------------- #
# Fabrications
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("fabricated", [
    "patient has a severe headache",
    "crushing chest pain radiating to the left arm",
    "patient reports vomiting blood",
    "allergic to penicillin",
    "BP is one eighty over one ten",
    "no chest pain reported",
])
def test_invented_quotes_are_rejected(fabricated):
    v = citations.verify(fabricated, TRANSCRIPT)
    assert not v.supported, f"fabricated quote passed verification: {fabricated!r}"


def test_a_single_swapped_clinical_noun_is_rejected():
    """This is the dangerous near-miss: everything matches except the finding.

    The content-word rule is what catches it — 'headache' is not in the
    transcript, so no amount of surrounding string similarity can pass it.
    """
    v = citations.verify("mujhe headache hai since two days", TRANSCRIPT)
    assert not v.supported


def test_verify_or_drop_returns_empty_for_fabrications():
    assert citations.verify_or_drop("patient is pregnant", TRANSCRIPT) == ""
    assert citations.verify_or_drop("mujhe chest pain hai", TRANSCRIPT) == "mujhe chest pain hai"


# --------------------------------------------------------------------- #
# Degenerate input
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("quote", ["", "  ", "a", "ok", None])
def test_too_short_or_missing_quotes_are_unsupported(quote):
    assert citations.verify(quote, TRANSCRIPT).verdict == "unsupported"


def test_empty_transcript_supports_nothing():
    assert citations.verify("chest pain", "").verdict == "unsupported"


def test_verification_does_not_crash_on_regex_metacharacters():
    assert citations.verify("(.*)+[a-z]", TRANSCRIPT).verdict == "unsupported"


# --------------------------------------------------------------------- #
# Coverage metric
# --------------------------------------------------------------------- #
def test_coverage_counts_only_populated_fields():
    cov = citations.coverage(
        field_values={"hpi": "chest pain", "allergies": "", "medications": "metformin"},
        verified_quotes={"hpi": "mujhe chest pain hai"},
    )
    assert cov["fields_populated"] == 2
    assert cov["fields_evidenced"] == 1
    assert cov["evidence_coverage_pct"] == 50
    assert cov["unevidenced_fields"] == ["medications"]


def test_coverage_of_an_empty_extraction_is_zero_not_a_crash():
    cov = citations.coverage({}, {})
    assert cov["evidence_coverage_pct"] == 0
    assert cov["fields_populated"] == 0


def test_verdicts_are_recorded_as_metrics():
    from app.core.metrics import registry

    registry.reset()
    citations.verify_or_drop("mujhe chest pain hai", TRANSCRIPT)
    citations.verify_or_drop("patient has a severe headache", TRANSCRIPT)
    counters = registry.snapshot()["counters"]
    assert counters.get("cma_citations_total{verdict=exact}") == 1
    assert counters.get("cma_citations_total{verdict=unsupported}") == 1


# --------------------------------------------------------------------- #
# Cost — verification runs inside an async request handler
# --------------------------------------------------------------------- #
def test_verification_of_a_long_transcript_stays_fast():
    """This is an event-loop concern, not a micro-optimisation.

    The near-match search originally slid three window widths across every
    offset of the transcript. At the 60,000-character cap that cost ~0.56 s of
    CPU for a single quote, and /scribe/extract verifies up to seventeen — so a
    long consultation blocked the event loop for about ten seconds, stalling
    every other request in the process, including other doctors' live polls.

    The search is now anchored on the quote's rarest content word, which must
    appear in any window that could match.
    """
    import time

    transcript = " ".join(
        ["doctor namaste kya problem hai patient mujhe chest pain hai since two days aur sweating"]
        * 700
    )[:60_000]
    # A paraphrase whose content words are all present, so the cheap subset
    # pre-filter does not short-circuit and the full search runs.
    quote = "patient mujhe chest pain since days sweating problem"

    started = time.perf_counter()
    citations.verify(quote, transcript)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.10, f"verification took {elapsed:.2f}s on a 60k transcript"


def test_anchoring_did_not_change_any_verdict():
    """The optimisation must be behaviour-preserving: a near match still has to
    be found, and a fabrication still has to be rejected."""
    transcript = (
        "Doctor: kya problem hai? Patient: uh, mujhe chest pain hai since, since two days, "
        "aur bahut sweating ho rahi hai."
    )
    assert citations.verify("mujhe chest pain hai since two days", transcript).verdict == "near"
    assert citations.verify("mujhe chest pain hai", transcript).verdict == "exact"
    assert not citations.verify("mujhe headache hai since two days", transcript).supported


def test_a_repeated_anchor_word_does_not_reintroduce_the_stall():
    """A transcript where the anchor appears thousands of times must still be
    bounded — hence MAX_ANCHORS."""
    import time

    transcript = " ".join(["chest pain"] * 5000)
    started = time.perf_counter()
    citations.verify("chest pain reported repeatedly today", transcript)
    assert time.perf_counter() - started < 0.10
