"""The clinical wrapper around the escalation-risk model.

**What this is.** A documentation prompt. Given the structured encounter the
physician has recorded so far, it answers one question: *does this case warrant
a second look before the consultation ends?* It never names a diagnosis, never
orders anything, and never changes the record.

**What it is not.** Not a diagnosis, not a triage category, not a probability
of any disease. The number it produces is the model's estimate of whether a
clinician reviewing this documentation would want the case escalated — trained
on synthetic data (see `app/ml/synthetic.py`), so it is unvalidated against
real outcomes and is presented as such in the response itself.

**Guardrails that live here rather than in the model.**

* Children are not scored. Every vital threshold in the feature set is an
  adult range; applying them to a five-year-old produces confident nonsense.
  The response says so instead of returning a number.
* An encounter with nothing in it is not scored. A blank consultation would
  otherwise get the model's base rate, and a number attached to no information
  reads as a finding.
* Deterministic red-flag criteria are checked *independently* of the model and
  are reported alongside it. If the model says low and a published criterion
  says escalate, the criterion wins the display. A learned model must not be
  able to talk a system out of a hard safety rule — especially one trained on
  synthetic data.
"""
from __future__ import annotations

import logging

from ..ml import risk_model
from ..ml.features import Encounter, extract

log = logging.getLogger("clinical.risk")

MIN_AGE_YEARS = 13

# Deterministic criteria, checked outside the model. These are standard adult
# early-warning cut-points and classic red-flag presentations; they are not
# learned, cannot drift with a retrain, and are always shown when they fire.
HARD_CRITERIA: tuple[tuple[str, str], ...] = (
    ("spo2_lt_92", "SpO2 below 92% — hypoxia"),
    ("sbp_lt_90", "Systolic BP below 90 — hypotension"),
    ("rr_ge_25", "Respiratory rate 25 or above"),
    ("sym_neuro_deficit", "Focal neurological deficit"),
    ("sym_altered_mental", "Altered mental state"),
    ("sym_syncope", "Syncope or loss of consciousness"),
)

# Two-feature combinations that matter more than either feature alone. Kept
# short and explicit rather than learned, for the same reason as above.
HARD_COMBINATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("sym_chest_pain", "sym_breathless"), "Chest pain with breathlessness"),
    (("sym_severe_headache", "sym_neck_stiffness"), "Severe headache with neck stiffness"),
    (("sym_fever", "sym_neck_stiffness"), "Fever with neck stiffness"),
    (("sym_chest_pain", "hx_cardiac"), "Chest pain in a patient with known cardiac disease"),
    (("sym_bleeding", "hx_anticoagulant"), "Bleeding while on an anticoagulant"),
    # Added after the red-flag evaluation missed these three presentations.
    # Each is a standard "second look" combination rather than a diagnosis, and
    # each was re-measured against the benign controls before being kept.
    (("sym_abdominal_pain", "sym_fever"), "Abdominal pain with fever — consider an acute abdomen"),
    (("sym_abdominal_pain", "sym_vomiting", "sym_fever"),
     "Abdominal pain, vomiting and fever together"),
    (("sym_vomiting", "sym_rapid_breathing"),
     "Vomiting with rapid breathing — consider metabolic acidosis"),
    (("sym_polydipsia", "sym_vomiting"), "Thirst and polyuria with vomiting — consider DKA"),
    (("sym_chronic_cough", "sym_weight_loss"),
     "Chronic cough with weight loss — consider tuberculosis or malignancy"),
    (("sym_weight_loss", "sym_night_sweats"),
     "Weight loss with night sweats — consider tuberculosis or malignancy"),
    (("sym_haemoptysis",), "Blood in the sputum"),
)

BANDS = ((0.60, "high"), (0.30, "moderate"), (0.10, "low"))


def _band(p: float) -> str:
    for cut, name in BANDS:
        if p >= cut:
            return name
    return "very_low"


def hard_criteria_met(features: dict[str, float]) -> list[str]:
    met = [label for key, label in HARD_CRITERIA if features.get(key, 0.0) >= 1.0]
    for keys, label in HARD_COMBINATIONS:
        if all(features.get(k, 0.0) >= 1.0 for k in keys):
            met.append(label)
    return met


def _has_content(enc: Encounter) -> bool:
    return bool(enc.complaints or enc.hpi.strip() or (enc.vitals or {}))


def assess(payload: dict) -> dict:
    """Score one encounter. Always returns a response; never raises."""
    enc = Encounter.from_payload(payload)
    features = extract(enc)
    criteria = hard_criteria_met(features)

    base = {
        "kind": "documentation_prompt",
        "hard_criteria_met": criteria,
        "disclaimer": (
            "Physician-review-only prompt. Not a diagnosis, not a triage decision, and not a "
            "probability of any disease. The model is trained on synthetic data and is not "
            "clinically validated."
        ),
    }

    if not _has_content(enc):
        return {**base, "scored": False, "reason": "Not enough recorded yet to assess.",
                "escalate": bool(criteria)}

    if enc.age is not None and enc.age < MIN_AGE_YEARS:
        return {
            **base, "scored": False,
            "reason": (f"Not scored: the model's vital-sign thresholds are adult ranges and do "
                       f"not apply under {MIN_AGE_YEARS}. Paediatric cases need paediatric criteria."),
            "escalate": bool(criteria),
        }

    try:
        prediction = risk_model.predict(enc)
    except risk_model.ModelUnavailable as e:
        log.warning("risk_model_unavailable", extra={"extra_fields": {"error": str(e)}})
        # Degrade to the deterministic criteria rather than failing the
        # consultation. Losing the model must not lose the hard safety checks.
        return {**base, "scored": False,
                "reason": "Risk model artefact is not loaded; deterministic criteria still applied.",
                "escalate": bool(criteria)}

    reasons = [
        {"label": c.label, "log_odds": c.log_odds,
         "contribution_pct": None}
        for c in prediction.top_reasons(6)
    ]
    total_positive = sum(c.log_odds for c in prediction.contributions if c.log_odds > 0) or 1.0
    for r, c in zip(reasons, prediction.top_reasons(6), strict=True):
        r["contribution_pct"] = round(100 * c.log_odds / total_positive)

    return {
        **base,
        "scored": True,
        "probability": prediction.probability,
        "band": _band(prediction.probability),
        "threshold": prediction.threshold,
        # The model flags, OR a hard criterion fires. A deterministic criterion
        # is never overridden by a low model score.
        "escalate": bool(prediction.flagged or criteria),
        "model_flagged": prediction.flagged,
        "reasons": reasons,
        "model_version": prediction.model_version,
        "model_performance": risk_model.metrics_summary(),
    }
