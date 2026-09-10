"""Runtime inference for the escalation-risk model — pure Python, no ML deps.

The model is trained with scikit-learn offline and exported to
`risk_model.json` as an intercept, one coefficient per named feature, and the
isotonic calibrator as a step function. Inference is then a dot product and a
table lookup, which has three consequences worth stating:

* **The API has no ML dependency.** scikit-learn, numpy and scipy stay in the
  `ml` dependency group, used only by the training script. The container that
  serves patients is smaller and has a smaller attack surface.
* **The artefact is a readable JSON file, not a pickle.** Loading a pickle is
  arbitrary code execution; loading this is `json.load`. For a model file that
  ships in a clinical system, that difference is the whole argument.
* **Every prediction explains itself for free.** In a linear model the
  contribution of a feature *is* coefficient x value, so the reason for a score
  falls out of the arithmetic rather than needing a separate explainer whose
  approximation could disagree with the model.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .features import FEATURE_ORDER, HUMAN_LABELS, Encounter, extract

ARTEFACT = Path(__file__).with_name("risk_model.json")


class ModelUnavailable(RuntimeError):
    """The artefact is missing or does not match the current feature set."""


@dataclass(frozen=True)
class Contribution:
    feature: str
    label: str
    value: float
    coefficient: float
    log_odds: float

    @property
    def direction(self) -> str:
        return "increases" if self.log_odds > 0 else "decreases"


@dataclass
class Prediction:
    probability: float
    threshold: float
    flagged: bool
    raw_log_odds: float
    uncalibrated_probability: float
    contributions: list[Contribution] = field(default_factory=list)
    model_version: str = ""

    def top_reasons(self, k: int = 5) -> list[Contribution]:
        """The features actually driving *this* case upwards.

        Only positive contributions from features that are present: telling a
        clinician the score is low partly because the patient is not 65 is
        noise, not an explanation.
        """
        return sorted(
            (c for c in self.contributions if c.log_odds > 0.01 and c.value != 0),
            key=lambda c: -c.log_odds,
        )[:k]


@lru_cache(maxsize=1)
def load(path: str | None = None) -> dict:
    p = Path(path) if path else ARTEFACT
    if not p.exists():
        raise ModelUnavailable(
            f"{p.name} is missing. Run: uv run --group ml python scripts/train_risk_model.py"
        )
    artefact = json.loads(p.read_text())
    if list(artefact.get("feature_order") or []) != list(FEATURE_ORDER):
        # A silent mismatch would apply the wrong coefficient to the wrong
        # feature and still produce a plausible-looking number, which is the
        # worst possible failure for a clinical score.
        raise ModelUnavailable(
            "risk_model.json was trained on a different feature set; retrain the model."
        )
    return artefact


def _isotonic(x: float, xs: list[float], ys: list[float]) -> float:
    """Piecewise-linear interpolation through the calibrator's knots.

    Matches scikit-learn's `IsotonicRegression` in its default
    `out_of_bounds='clip'` mode, so the served probability is the same number
    the training run evaluated.

    The x-axis is the **decision function** (log-odds), not the sigmoid output.
    `CalibratedClassifierCV` fits its calibrator on `decision_function` whenever
    the base estimator exposes one, which a logistic regression does. Feeding it
    a probability instead silently produces plausible-looking numbers that do
    not match the evaluated model — so the artefact records which quantity it
    expects and `train_risk_model.py` asserts parity between this code path and
    scikit-learn before writing the file.
    """
    if not xs:
        return x
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_right(xs, x)
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    if x1 == x0:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def predict(enc: Encounter, *, artefact: dict | None = None) -> Prediction:
    model = artefact or load()
    features = extract(enc)
    coefficients: dict[str, float] = model["coefficients"]

    log_odds = float(model["intercept"])
    contributions: list[Contribution] = []
    for name in FEATURE_ORDER:
        value = features[name]
        coef = float(coefficients.get(name, 0.0))
        term = coef * value
        log_odds += term
        contributions.append(Contribution(
            feature=name, label=HUMAN_LABELS.get(name, name),
            value=value, coefficient=coef, log_odds=round(term, 4),
        ))

    uncalibrated = 1.0 / (1.0 + math.exp(-max(min(log_odds, 40.0), -40.0)))
    cal = model.get("calibration") or {}
    calibrator_input = log_odds if cal.get("input", "decision_function") == "decision_function" else uncalibrated
    probability = _isotonic(calibrator_input, cal.get("x") or [], cal.get("y") or [])
    threshold = float(model.get("operating_threshold", 0.5))

    return Prediction(
        probability=round(probability, 4),
        threshold=threshold,
        flagged=probability >= threshold,
        raw_log_odds=round(log_odds, 4),
        uncalibrated_probability=round(uncalibrated, 4),
        contributions=contributions,
        model_version=f"{model.get('schema_version', '?')}@{model.get('created', 'unknown')}",
    )


def metrics_summary() -> dict:
    """The held-out metrics this artefact shipped with, for the model card and
    the /metrics dashboard. Reported next to the score so nobody has to guess
    how good it is."""
    try:
        return dict(load().get("metrics_summary") or {})
    except ModelUnavailable:
        return {}
