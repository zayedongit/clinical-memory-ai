"""The evaluation harnesses run in CI as tests, so a regression fails the build.

A benchmark that is not wired into CI drifts. This project already had that
failure: the red-flag harness kept reporting a recall number for a function
that had been removed from the request path months earlier, and nothing caught
it because nothing ran it.

These tests execute the same code the scripts do and assert the same gates, so
"the evaluation passes" is a fact about the current commit rather than about
whenever someone last ran it by hand.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    import importlib.util
    import sys

    sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(name, BACKEND / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


# ===================================================================== #
# Citation verification and extraction
# ===================================================================== #
@pytest.fixture(scope="module")
def extraction_eval():
    module = _load_script("eval_extraction")
    data = json.loads((BACKEND / "eval" / "datasets" / "extraction_gold.json").read_text())
    return module, data


def test_citation_verification_never_accepts_a_fabricated_quote(extraction_eval):
    """The one failure this system must not have.

    A fabricated quote shown as verified evidence is worse than showing no
    evidence: it converts the physician's scepticism into misplaced trust.
    """
    module, data = extraction_eval
    result = module.evaluate_citations(data["citation_cases"])
    assert result["confusion_matrix"]["fp"] == 0, (
        f"accepted fabricated quotes: {[f['quote'] for f in result['failures']]}")
    assert result["precision"] == 1.0


def test_citation_verification_keeps_real_quotes(extraction_eval):
    """A verifier that drops everything has perfect precision and is useless."""
    module, data = extraction_eval
    result = module.evaluate_citations(data["citation_cases"])
    assert result["recall"] >= module.GATES["citation_recall"]


def test_extraction_gates_are_met(extraction_eval):
    module, data = extraction_eval
    result = module.evaluate_extraction(data["cases"], live=False)
    assert result["symptoms"]["f1"] >= module.GATES["symptoms_f1"]
    assert result["medications"]["f1"] >= module.GATES["medications_f1"]
    assert result["allergies_accuracy"] >= module.GATES["allergies_accuracy"]


def test_the_evaluation_dataset_declares_itself_synthetic(extraction_eval):
    """Guards against a real transcript being pasted in during a debugging
    session and quietly becoming a committed dataset."""
    _, data = extraction_eval
    assert "synthetic" in data["data_provenance"].lower()


def test_the_evaluation_is_deterministic(extraction_eval):
    module, data = extraction_eval
    first = module.evaluate_citations(data["citation_cases"])
    second = module.evaluate_citations(data["citation_cases"])
    assert first == second


# ===================================================================== #
# Red-flag escalation
# ===================================================================== #
@pytest.fixture(scope="module")
def red_flag_eval():
    module = _load_script("eval_red_flags")
    data = json.loads((BACKEND / "eval" / "red_flag_cases.json").read_text())
    return module, data


def test_every_red_flag_presentation_is_escalated(red_flag_eval):
    module, data = red_flag_eval
    missed = [c["name"] for c in data["cases"] if not module.assess(c).get("escalate")]
    sensitivity = 1 - len(missed) / len(data["cases"])
    assert sensitivity >= module.GATES["sensitivity"], f"missed: {missed}"


def test_benign_presentations_do_not_raise_the_alarm(red_flag_eval):
    """Alarm fatigue is a patient-safety problem, not a UX complaint: a system
    that flags a common cold teaches clinicians to dismiss the panel."""
    module, data = red_flag_eval
    negatives = data["negative_cases"]
    flagged = [c["name"] for c in negatives if module.assess(c).get("escalate")]
    rate = len(flagged) / len(negatives)
    assert rate <= module.GATES["false_alarm_rate"], f"false alarms: {flagged}"


def test_the_benign_set_contains_near_misses(red_flag_eval):
    """A false-alarm rate measured only on obviously-benign cases is not
    evidence. Each hard criterion needs a control that almost fires it."""
    _, data = red_flag_eval
    names = " ".join(c["name"].lower() for c in data["negative_cases"])
    assert "near-miss" in names
    assert len(data["negative_cases"]) >= 10


# ===================================================================== #
# The shipped model artefact
# ===================================================================== #
def test_the_shipped_model_matches_the_current_feature_set():
    """A feature added without retraining would apply the wrong coefficient to
    the wrong feature and still produce a plausible-looking number."""
    from app.ml import risk_model
    from app.ml.features import FEATURE_ORDER

    artefact = risk_model.load()
    assert tuple(artefact["feature_order"]) == FEATURE_ORDER


def test_a_stale_artefact_is_refused_rather_than_silently_mis_scored(tmp_path):
    from app.ml import risk_model

    stale = {**risk_model.load(), "feature_order": ["age_scaled", "age_ge_65"]}
    path = tmp_path / "stale_model.json"
    path.write_text(json.dumps(stale))

    with pytest.raises(risk_model.ModelUnavailable, match="different feature set"):
        risk_model.load(str(path))


def test_a_missing_artefact_says_how_to_rebuild_it(tmp_path):
    from app.ml import risk_model

    with pytest.raises(risk_model.ModelUnavailable, match="train_risk_model"):
        risk_model.load(str(tmp_path / "does_not_exist.json"))


def test_risk_scoring_degrades_to_the_hard_criteria_without_the_model(monkeypatch):
    """Losing the model artefact must not lose the deterministic safety checks."""
    from app.clinical import risk
    from app.ml import risk_model

    def unavailable(*args, **kwargs):
        raise risk_model.ModelUnavailable("artefact missing")

    monkeypatch.setattr(risk_model, "predict", unavailable)
    result = risk.assess({"age": 55, "vitals": {"spo2": "88"},
                          "complaints": [{"text": "breathless"}]})
    assert result["scored"] is False
    assert result["escalate"] is True
    assert "SpO2 below 92% — hypoxia" in result["hard_criteria_met"]


def test_the_artefact_carries_the_metrics_it_shipped_with():
    from app.ml import risk_model

    summary = risk_model.metrics_summary()
    for key in ("roc_auc", "pr_auc", "precision", "recall", "specificity", "f1", "ece"):
        assert key in summary, f"missing {key}"
    # Sanity floors: well above the 0.20 positive rate a trivial model would get.
    assert summary["roc_auc"] > 0.80
    assert summary["pr_auc"] > 0.60


def test_calibration_is_recorded_and_used():
    """Class weighting deliberately distorts the intercept, so the raw output
    is not a probability. The calibrator is what makes the number mean what it
    says, and it must be exported with the artefact."""
    from app.ml import risk_model

    calibration = risk_model.load()["calibration"]
    assert calibration["method"] == "isotonic"
    assert calibration["input"] == "decision_function"
    assert len(calibration["x"]) == len(calibration["y"]) > 2


def test_the_artefact_is_json_not_a_pickle():
    """Loading a pickle is arbitrary code execution. For a model file shipping
    in a clinical system that difference is the whole argument."""
    path = BACKEND / "app" / "ml" / "risk_model.json"
    assert path.suffix == ".json"
    json.loads(path.read_text())
    assert not list((BACKEND / "app" / "ml").glob("*.pkl"))
    assert not list((BACKEND / "app" / "ml").glob("*.joblib"))


def test_committed_results_exist_so_readme_claims_are_checkable():
    for name in ("risk_model.json", "extraction.json", "red_flags.json"):
        path = BACKEND / "eval" / "results" / name
        assert path.exists(), f"{name} has not been generated"
        json.loads(path.read_text())
