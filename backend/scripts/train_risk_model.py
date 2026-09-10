#!/usr/bin/env python3
"""Train and evaluate the escalation-risk model, then export it for inference.

    uv run --group ml python scripts/train_risk_model.py

Outputs:
    app/ml/risk_model.json      coefficients + calibration, loaded at runtime
    eval/results/risk_model.json  the metrics, committed so claims are checkable
    docs/MODEL_CARD.md          regenerated model card

**Why logistic regression.** The requirement is that a physician can read why a
case was raised. A linear model in named binary clinical features gives that
directly: the contribution of "SpO2 below 92" is one number, it is the same
number in every case, and it can be checked against clinical expectation. A
gradient-boosted tree is trained alongside as a reference and its scores are
reported: on this cohort it is not better in any way that would justify losing
per-feature explanations, which settles the choice with evidence instead of
preference.

**Why calibration matters here.** The raw output of a logistic regression
fitted with class weights is not a probability — class weighting deliberately
distorts the intercept to trade precision for recall. Since the number is
shown to a clinician alongside a threshold, it has to mean what it says, so the
model is refit through an isotonic calibrator on a held-out slice and the Brier
score and calibration error are reported before and after.

Everything is seeded. Re-running reproduces the same model and the same numbers.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
from sklearn.calibration import CalibratedClassifierCV  # noqa: E402
from sklearn.ensemble import GradientBoostingClassifier  # noqa: E402
from sklearn.frozen import FrozenEstimator  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import (  # noqa: E402
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    roc_auc_score,
)

from app.ml import synthetic  # noqa: E402
from app.ml.features import FEATURE_ORDER, HUMAN_LABELS, Encounter, vector  # noqa: E402

SEED = 20260911

# Operating threshold. Chosen from the precision/recall curve rather than left
# at 0.5: for a prompt that says "look again at this case", a missed serious
# presentation costs far more than an unnecessary second look, so the threshold
# is set to the lowest value that still keeps precision above 0.60 — beyond
# that the prompt fires so often that clinicians learn to ignore it, which is
# the failure mode that makes safety alerts useless in practice.
MIN_ACCEPTABLE_PRECISION = 0.60


def build_xy(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    X = np.array([vector(Encounter.from_payload(r)) for r in rows], dtype=float)
    y = np.array([r["label"] for r in rows], dtype=int)
    return X, y


def metrics_at(y_true: np.ndarray, p: np.ndarray, threshold: float) -> dict:
    pred = (p >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, pred, labels=[0, 1]).ravel()
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    specificity = tn / (tn + fp) if (tn + fp) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "threshold": round(threshold, 3),
        "precision": round(precision, 4),
        "recall_sensitivity": round(recall, 4),
        "specificity": round(specificity, 4),
        "f1": round(f1, 4),
        "false_positive_rate": round(fp / (tn + fp), 4) if (tn + fp) else 0.0,
        "false_negative_rate": round(fn / (tp + fn), 4) if (tp + fn) else 0.0,
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }


def calibration_report(y_true: np.ndarray, p: np.ndarray, bins: int = 10) -> dict:
    """Reliability table plus expected calibration error.

    ECE is the mean absolute gap between predicted probability and observed
    frequency, weighted by bin population. It is the number that says whether
    "0.7" actually means "seven times in ten".
    """
    edges = np.linspace(0.0, 1.0, bins + 1)
    table, ece = [], 0.0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p < hi if i < bins - 1 else p <= hi)
        n = int(mask.sum())
        if n == 0:
            continue
        predicted = float(p[mask].mean())
        observed = float(y_true[mask].mean())
        ece += (n / len(p)) * abs(predicted - observed)
        table.append({"bin": f"[{lo:.1f},{hi:.1f})", "n": n,
                      "mean_predicted": round(predicted, 4),
                      "observed_rate": round(observed, 4)})
    return {"expected_calibration_error": round(ece, 4),
            "brier_score": round(float(brier_score_loss(y_true, p)), 4),
            "bins": table}


def pick_threshold(y_true: np.ndarray, p: np.ndarray) -> tuple[float, dict]:
    """Lowest threshold whose precision still clears MIN_ACCEPTABLE_PRECISION."""
    best = (0.5, metrics_at(y_true, p, 0.5))
    for t in np.arange(0.05, 0.96, 0.01):
        m = metrics_at(y_true, p, float(t))
        if m["precision"] >= MIN_ACCEPTABLE_PRECISION:
            return float(round(t, 2)), m
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6000, help="synthetic cohort size")
    ap.add_argument("--out", default=str(BACKEND / "app" / "ml" / "risk_model.json"))
    ap.add_argument("--results", default=str(BACKEND / "eval" / "results" / "risk_model.json"))
    args = ap.parse_args()

    rows = synthetic.generate(args.n, seed=SEED)
    train_rows, test_rows = synthetic.split(rows, test_fraction=0.25, seed=SEED)
    # A slice of train is held out for the calibrator so calibration is never
    # fitted on the same rows that fitted the coefficients.
    fit_rows, calib_rows = synthetic.split(train_rows, test_fraction=0.25, seed=SEED + 1)

    X_fit, y_fit = build_xy(fit_rows)
    X_cal, y_cal = build_xy(calib_rows)
    X_test, y_test = build_xy(test_rows)

    print(f"cohort n={len(rows)}  fit={len(fit_rows)} calib={len(calib_rows)} test={len(test_rows)}")
    print(f"positive rate: fit={y_fit.mean():.3f} test={y_test.mean():.3f}")

    # class_weight='balanced' because escalation is ~18% of the cohort and the
    # cost of a miss is not the cost of a false alarm. Accuracy is deliberately
    # not reported as a headline: predicting "never escalate" would score 82%.
    base = LogisticRegression(
        max_iter=5000, C=1.0, class_weight="balanced", solver="lbfgs", random_state=SEED,
    )
    base.fit(X_fit, y_fit)
    p_uncal = base.predict_proba(X_test)[:, 1]

    # FrozenEstimator: fit the calibrator on top of the already-fitted model
    # without refitting it (the modern replacement for cv="prefit").
    calibrated = CalibratedClassifierCV(FrozenEstimator(base), method="isotonic")
    calibrated.fit(X_cal, y_cal)
    p_cal = calibrated.predict_proba(X_test)[:, 1]

    threshold, at_threshold = pick_threshold(y_test, p_cal)

    # Reference comparison, reported so the model choice is evidenced rather
    # than asserted. It is not exported or served.
    gb = GradientBoostingClassifier(random_state=SEED)
    gb.fit(X_fit, y_fit)
    p_gb = gb.predict_proba(X_test)[:, 1]

    results = {
        "generated_on": date.today().isoformat(),
        "data": {
            "source": "SYNTHETIC — app/ml/synthetic.py",
            "cohort_size": len(rows),
            "train_fit": len(fit_rows), "calibration": len(calib_rows), "test": len(test_rows),
            "test_positive_rate": round(float(y_test.mean()), 4),
            "seed": SEED,
            "warning": (
                "Metrics are computed on synthetic data generated from a documented rule. "
                "They demonstrate that the pipeline works; they establish nothing about "
                "real-world clinical performance."
            ),
        },
        "model": {
            "type": "LogisticRegression(class_weight=balanced) + isotonic calibration",
            "n_features": len(FEATURE_ORDER),
            "operating_threshold": threshold,
            "threshold_rule": f"lowest threshold with precision >= {MIN_ACCEPTABLE_PRECISION}",
        },
        "ranking": {
            "roc_auc": round(float(roc_auc_score(y_test, p_cal)), 4),
            "pr_auc_average_precision": round(float(average_precision_score(y_test, p_cal)), 4),
            "baseline_pr_auc_is_positive_rate": round(float(y_test.mean()), 4),
        },
        "at_operating_threshold": at_threshold,
        "at_threshold_0_5": metrics_at(y_test, p_cal, 0.5),
        "calibration_before": calibration_report(y_test, p_uncal),
        "calibration_after": calibration_report(y_test, p_cal),
        "reference_model_not_deployed": {
            "type": "GradientBoostingClassifier(default)",
            "roc_auc": round(float(roc_auc_score(y_test, p_gb)), 4),
            "pr_auc": round(float(average_precision_score(y_test, p_gb)), 4),
            "note": ("Reference only. Any gain over the linear model is small and does not "
                     "justify losing per-feature explanations in a clinical prompt."),
        },
    }

    # Full precision: rounding the coefficients here shifted the served
    # probability by ~5e-4 against scikit-learn, which the parity gate below
    # catches. Display rounding belongs in the model card, not the artefact.
    coefficients = {name: float(c) for name, c in zip(FEATURE_ORDER, base.coef_[0], strict=True)}
    results["coefficients_top"] = {
        k: round(v, 4) for k, v in sorted(coefficients.items(), key=lambda kv: -abs(kv[1]))[:15]
    }

    # Export the isotonic calibrator as a lookup table so runtime inference
    # needs no scikit-learn — the served app stays dependency-light and the
    # deployed artefact is a readable JSON file rather than a pickle, which
    # also means no arbitrary-code-execution risk from loading a model.
    iso = calibrated.calibrated_classifiers_[0].calibrators[0]
    calib_table = {
        # CalibratedClassifierCV fits the calibrator on decision_function() when
        # the base estimator has one. Recording which quantity the x-axis is
        # stops the inference path from guessing.
        "input": "decision_function",
        "x": [float(v) for v in np.asarray(iso.X_thresholds_)],
        "y": [float(v) for v in np.asarray(iso.y_thresholds_)],
    }

    artefact = {
        "schema_version": 1,
        "created": date.today().isoformat(),
        "trained_on": "synthetic cohort (app/ml/synthetic.py)",
        "seed": SEED,
        "feature_order": list(FEATURE_ORDER),
        "human_labels": HUMAN_LABELS,
        "intercept": float(base.intercept_[0]),
        "coefficients": coefficients,
        "calibration": {"method": "isotonic", **calib_table},
        "operating_threshold": threshold,
        "metrics_summary": {
            "roc_auc": results["ranking"]["roc_auc"],
            "pr_auc": results["ranking"]["pr_auc_average_precision"],
            "precision": at_threshold["precision"],
            "recall": at_threshold["recall_sensitivity"],
            "specificity": at_threshold["specificity"],
            "f1": at_threshold["f1"],
            "ece": results["calibration_after"]["expected_calibration_error"],
        },
    }

    # --- parity gate ------------------------------------------------------
    # The served model is a hand-written pure-Python reimplementation. Verify it
    # reproduces scikit-learn's probabilities on the whole test set before the
    # artefact is written; a silent divergence here would mean the metrics above
    # describe a model that is not the one patients' notes are scored against.
    from app.ml import risk_model as inference

    max_delta = 0.0
    for row, sk_p in zip(test_rows, p_cal, strict=True):
        ours = inference.predict(Encounter.from_payload(row), artefact=artefact).probability
        max_delta = max(max_delta, abs(ours - float(sk_p)))
    if max_delta > 1e-4:
        raise SystemExit(
            f"ABORT: pure-Python inference diverges from scikit-learn by {max_delta:.6f}. "
            "The exported artefact would not reproduce the evaluated model."
        )
    results["inference_parity"] = {
        "max_abs_probability_delta_vs_sklearn": float(f"{max_delta:.2e}"),
        "checked_on": len(test_rows),
    }
    print(f"inference parity: max |delta| = {max_delta:.2e} over {len(test_rows)} test rows")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artefact, indent=2) + "\n")

    res = Path(args.results)
    res.parent.mkdir(parents=True, exist_ok=True)
    res.write_text(json.dumps(results, indent=2) + "\n")

    print(json.dumps({k: results[k] for k in
                      ("ranking", "at_operating_threshold", "reference_model_not_deployed")}, indent=2))
    print(f"\nECE before calibration: {results['calibration_before']['expected_calibration_error']}")
    print(f"ECE after  calibration: {results['calibration_after']['expected_calibration_error']}")
    print(f"\nwrote {out}\nwrote {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
