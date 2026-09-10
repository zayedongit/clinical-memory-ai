"""Verifying that a model's quoted evidence actually appears in the transcript.

The extraction prompt asks the model to justify every field it fills with the
verbatim words that support it. That request is worth nothing on its own —
models will happily produce a fluent quote that was never said. This module
checks each quote against the transcript and throws away the ones that fail,
so a quote shown to the physician is always something the microphone heard.

Two verdicts count as supported:

``exact``
    The quote appears in the transcript after normalisation (case, unicode
    punctuation, whitespace). This is the overwhelming majority of real hits.

``near``
    The quote does not appear verbatim, but a window of the transcript matches
    it closely *and* contains every content word of the quote. This catches the
    common benign failure — the model tidies "uh, chest pain since, since two
    days" into "chest pain since two days" — without accepting a rewrite that
    introduces a word nobody said. The content-word requirement is what makes
    this safe: a fabricated symptom cannot pass, because its own noun is
    missing from the transcript.

Anything else is ``unsupported`` and is dropped.

The verdict distribution is exported as a metric, so "how often does the model
fabricate a citation" is a number this system can report.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..core.metrics import registry

# Similarity a near-match window must reach. Calibrated on the synthetic
# extraction set in backend/eval/datasets/: below ~0.80 paraphrases that had
# swapped a clinical noun started passing, which is exactly what must not.
NEAR_MATCH_THRESHOLD = 0.82

# Shortest quote worth checking. Anything shorter matches by accident.
MIN_QUOTE_CHARS = 4

# Ceiling on how many anchor positions the near-match search will explore.
# Verification runs inside an async request handler, so an unbounded scan is an
# event-loop stall, not just slow code.
MAX_ANCHORS = 24

_FILLER = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "was", "it", "that",
    "this", "for", "on", "at", "as", "with", "from", "has", "have", "had",
    "uh", "um", "so", "you", "your", "i", "me", "my", "he", "she", "they",
}

_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Verification:
    quote: str
    verdict: str          # "exact" | "near" | "unsupported"
    similarity: float
    matched_span: str = ""

    @property
    def supported(self) -> bool:
        return self.verdict in ("exact", "near")


def normalise(text: str) -> str:
    """Fold away everything that is not content: unicode form, case,
    punctuation (including the smart quotes models emit), whitespace runs."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = t.replace("’", "'").replace("‘", "'")
    t = t.replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-")
    t = _PUNCT.sub(" ", t.lower())
    return _WS.sub(" ", t).strip()


def _content_words(text: str) -> set[str]:
    return {w for w in normalise(text).split() if len(w) > 2 and w not in _FILLER}


def verify(quote: str, transcript: str) -> Verification:
    """Check one quote against one transcript."""
    raw = str(quote or "").strip()
    if len(raw) < MIN_QUOTE_CHARS:
        return Verification(raw, "unsupported", 0.0)

    nq = normalise(raw)
    nt = normalise(transcript)
    if not nq or not nt:
        return Verification(raw, "unsupported", 0.0)

    if nq in nt:
        return Verification(raw, "exact", 1.0, raw)

    quote_words = nq.split()
    transcript_words = nt.split()
    if not quote_words or len(transcript_words) < 1:
        return Verification(raw, "unsupported", 0.0)

    needed = _content_words(raw)
    # A quote whose content words are not all present cannot be supported,
    # whatever the string similarity says. Check that first — it is cheap and
    # it is the guarantee that stops fabrications passing as "near".
    if not needed or not needed.issubset(set(transcript_words)):
        return Verification(raw, "unsupported", 0.0)

    # Search only windows that could possibly match, rather than every offset.
    #
    # A near match must contain every content word of the quote (checked above),
    # so it must overlap a position where the quote's *rarest* content word
    # occurs. Anchoring on those positions turns a full scan into a handful of
    # comparisons. On a 60,000-character transcript the naive scan cost 0.56 s
    # of CPU per quote — and `/scribe/extract` verifies up to seventeen quotes,
    # so a long consultation blocked the event loop for ten seconds, stalling
    # every other request in the process including other doctors' live polls.
    span = len(quote_words)
    positions: dict[str, list[int]] = {}
    for index, word in enumerate(transcript_words):
        if word in needed:
            positions.setdefault(word, []).append(index)
    if not positions:
        return Verification(raw, "unsupported", 0.0)

    rarest = min(positions.values(), key=len)
    widths = sorted({span, span + 2, max(span - 2, 1)})

    # Cap the work regardless: a pathological transcript where the anchor word
    # appears thousands of times must not reintroduce the stall.
    anchors = rarest[:MAX_ANCHORS]

    best, best_window = 0.0, ""
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(nq)
    seen: set[tuple[int, int]] = set()
    for anchor in anchors:
        for width in widths:
            # The anchor can sit anywhere inside the window, so slide the window
            # across it rather than assuming the anchor starts it.
            for start in range(max(anchor - width + 1, 0), min(anchor + 1, len(transcript_words))):
                key = (start, width)
                if key in seen:
                    continue
                seen.add(key)
                window = " ".join(transcript_words[start:start + width])
                matcher.set_seq1(window)
                if matcher.real_quick_ratio() < best or matcher.quick_ratio() < best:
                    continue
                ratio = matcher.ratio()
                if ratio > best:
                    best, best_window = ratio, window

    if best >= NEAR_MATCH_THRESHOLD:
        return Verification(raw, "near", round(best, 3), best_window)
    return Verification(raw, "unsupported", round(best, 3))


def verify_or_drop(quote: str, transcript: str) -> str:
    """The API-facing helper: return the quote if supported, else empty.

    Records the verdict so citation reliability is measurable in production,
    not only in the evaluation harness.
    """
    v = verify(quote, transcript)
    if str(quote or "").strip():
        registry.inc("cma_citations_total", {"verdict": v.verdict})
    return v.quote if v.supported else ""


def coverage(field_values: dict[str, str], verified_quotes: dict[str, str]) -> dict:
    """How much of what the model filled in is actually evidenced.

    `evidence_coverage_pct` is the share of populated fields that carry a
    verified quote. It is a grounding metric, not a correctness metric: a
    field can be perfectly evidenced and still be the wrong clinical
    interpretation of what was said.
    """
    populated = [k for k, v in field_values.items() if str(v or "").strip()]
    evidenced = [k for k in populated if str(verified_quotes.get(k, "")).strip()]
    return {
        "fields_populated": len(populated),
        "fields_evidenced": len(evidenced),
        "unevidenced_fields": sorted(set(populated) - set(evidenced)),
        "evidence_coverage_pct": round(100 * len(evidenced) / len(populated)) if populated else 0,
    }
