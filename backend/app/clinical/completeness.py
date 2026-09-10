"""Documentation completeness scoring for a consultation note.

**What this measures, and what it does not.** This is a *documentation
quality* metric. It answers "does this note contain the sections a defensible
clinical record needs?" It says nothing about whether the content is clinically
correct, whether the diagnosis is right, or whether the treatment is
appropriate. A note can score 100 and be clinically wrong. That distinction is
laboured here on purpose, because a number labelled "completeness" next to a
clinical note invites exactly the wrong reading.

**Why it is deterministic.** The previous implementation asked the language
model for a `completeness_pct`. That number was unstable across runs on the
same note, unauditable, and cost a token budget to produce. A rubric is
reproducible, explains itself item by item, is unit-testable, and costs
nothing. The physician can see precisely which items are missing rather than
being handed a score with no derivation.

The rubric weights are a defensible reading of what a general-practice note
needs (chief complaint, history, allergies, examination, assessment, plan);
they are a documentation convention, not a clinical guideline, and they are in
one table here so they can be argued with and changed.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# "No known allergies" is a *complete* allergy history, not a missing one.
# Failing to credit it is the classic way a completeness score teaches
# clinicians to write noise.
#
# Two patterns, because they have to behave differently:
#
# _NEGATIVE_PHRASE matches anywhere in the text — "the patient has no known
# drug allergies" is a negative however it is wrapped.
#
# _NEGATIVE_BARE must match the *whole* field. A model asked for an allergy
# list frequently answers with a single word ("None", "Nil", "NKA"), and the
# extraction eval caught that being parsed as an allergen literally called
# "None" — which would then print in red on the patient's chart as an allergy.
# It is anchored because "none" as a substring appears inside real allergens.
_NEGATIVE_PHRASE = re.compile(
    r"\b(nkda|nka|no known (drug )?allerg|none known|nil known|no allerg|"
    r"denies allerg|denied allerg|not allergic|no drug allerg|no history of allerg)",
    re.IGNORECASE,
)
_NEGATIVE_BARE = re.compile(
    r"^(none|nil|no|nka|nkda|n/?a|not known|unknown|negative|nothing|"
    r"no allergies|none reported|not applicable)[.!]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RubricItem:
    key: str
    label: str
    weight: int
    required: bool
    check: Callable[[dict], bool]
    hint: str


def _text(note: dict, *path: str) -> str:
    cur: Any = note
    for p in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(p)
    if isinstance(cur, (list, tuple)):
        return " ".join(str(x) for x in cur)
    return str(cur or "").strip()


def _complaints(note: dict) -> list[dict]:
    items = note.get("chief_complaints")
    if isinstance(items, list):
        return [c for c in items if isinstance(c, dict) and str(c.get("text", "")).strip()]
    # Fall back to the extracted-entity shape used by the scribe lane.
    syms = ((note.get("entities") or {}).get("symptoms")) if isinstance(note.get("entities"), dict) else None
    return [{"text": s.strip()} for s in (syms or []) if isinstance(s, str) and s.strip()]


def _vitals_recorded(note: dict) -> int:
    v = note.get("vitals")
    if not isinstance(v, dict):
        return 0
    return sum(1 for x in v.values() if x is not None and str(x).strip())


def _has_allergy_statement(note: dict) -> bool:
    if _text(note, "allergies"):
        return True
    ents = note.get("entities")
    return bool(isinstance(ents, dict) and ents.get("allergies"))


RUBRIC: tuple[RubricItem, ...] = (
    RubricItem(
        "chief_complaint", "Chief complaint recorded", 15, True,
        lambda n: len(_complaints(n)) > 0,
        "Every note needs at least one presenting complaint.",
    ),
    RubricItem(
        "complaint_duration", "Duration recorded for the complaint", 6, False,
        lambda n: any(str(c.get("duration", "")).strip() for c in _complaints(n)),
        "How long a symptom has been present changes the differential.",
    ),
    RubricItem(
        "hpi", "History of present illness", 12, False,
        lambda n: len(_text(n, "hpi")) >= 15 or len(_text(n, "subjective")) >= 15,
        "A narrative of the current problem, not just the complaint label.",
    ),
    RubricItem(
        "past_history", "Past medical history addressed", 8, False,
        lambda n: bool(_text(n, "past_history")),
        "Record it even when it is nil — 'no significant past history' counts.",
    ),
    RubricItem(
        "allergies", "Allergy status documented", 12, True,
        _has_allergy_statement,
        "'No known drug allergies' is a complete answer; blank is not.",
    ),
    RubricItem(
        "medications", "Current medications documented", 8, False,
        lambda n: bool(_text(n, "medications"))
        or bool((n.get("entities") or {}).get("medications") if isinstance(n.get("entities"), dict) else None),
        "Needed for interaction and duplicate-therapy checking.",
    ),
    RubricItem(
        "vitals", "At least two vitals recorded", 10, False,
        lambda n: _vitals_recorded(n) >= 2,
        "Vitals are the objective anchor of the encounter.",
    ),
    RubricItem(
        "examination", "Examination findings recorded", 10, False,
        lambda n: bool(_text(n, "general_exam") or _text(n, "systemic_exam") or _text(n, "objective")),
        "Record the examination performed, including relevant negatives.",
    ),
    RubricItem(
        "assessment", "Assessment / working diagnosis", 15, True,
        lambda n: len(_text(n, "assessment")) >= 3,
        "The clinical conclusion the rest of the note supports.",
    ),
    RubricItem(
        "plan", "Plan recorded", 14, True,
        lambda n: len(_text(n, "plan")) >= 3
        or bool(n.get("prescription"))
        or bool(n.get("investigations")),
        "Investigations, treatment, advice, or follow-up.",
    ),
)

_TOTAL_WEIGHT = sum(item.weight for item in RUBRIC)
assert _TOTAL_WEIGHT == 110, "rubric weights changed; update the normalisation comment"


def score(note: dict) -> dict:
    """Score one note. `note` accepts either the consultation-wizard shape or
    the SOAP-note shape; the checks look in both places."""
    note = note or {}
    items = []
    earned = 0
    for item in RUBRIC:
        try:
            present = bool(item.check(note))
        except Exception:
            present = False
        if present:
            earned += item.weight
        items.append({
            "key": item.key, "label": item.label, "weight": item.weight,
            "required": item.required, "present": present,
            "hint": "" if present else item.hint,
        })

    pct = round(100 * earned / _TOTAL_WEIGHT)
    missing_required = [i["key"] for i in items if i["required"] and not i["present"]]

    # A note missing a required section is capped below "adequate" no matter
    # how much optional detail it carries. A beautifully documented history
    # with no plan is not an 85% note.
    grade = _grade(pct, missing_required)

    return {
        "score_pct": pct,
        "grade": grade,
        "earned_weight": earned,
        "total_weight": _TOTAL_WEIGHT,
        "items": items,
        "missing_required": missing_required,
        "missing_optional": [i["key"] for i in items if not i["required"] and not i["present"]],
        "metric": "documentation_completeness",
        "disclaimer": (
            "Measures whether the record contains the expected sections. "
            "It is not a measure of clinical correctness."
        ),
    }


def _grade(pct: int, missing_required: list[str]) -> str:
    if missing_required:
        return "incomplete"
    if pct >= 90:
        return "thorough"
    if pct >= 75:
        return "adequate"
    if pct >= 55:
        return "sparse"
    return "incomplete"


def missing_information(note: dict) -> list[str]:
    """The physician-facing 'what's still missing' list, derived from the same
    rubric so the prompt panel and the score can never disagree."""
    result = score(note)
    by_key = {i["key"]: i for i in result["items"]}
    ordered = [i["key"] for i in result["items"] if i["required"]] + result["missing_optional"]
    out = []
    for key in ordered:
        item = by_key[key]
        if not item["present"]:
            out.append(f"{item['label']} — {item['hint']}")
    return out


def normalise_allergy_statement(text: str) -> dict:
    """Distinguish 'no known allergies' from a real allergy list.

    Downstream allergy-conflict checking must not try to match a prescription
    against the literal string "no known drug allergies" — which is how a
    naive substring check produces an allergy warning for every drug
    containing the letter "n".
    """
    t = str(text or "").strip()
    if not t:
        return {"documented": False, "none_known": False, "allergens": []}
    if _NEGATIVE_PHRASE.search(t) or _NEGATIVE_BARE.match(t):
        return {"documented": True, "none_known": True, "allergens": []}
    allergens = [a.strip() for a in re.split(r"[,;/]+|\band\b", t, flags=re.IGNORECASE) if len(a.strip()) > 2]
    return {"documented": True, "none_known": False, "allergens": allergens}
