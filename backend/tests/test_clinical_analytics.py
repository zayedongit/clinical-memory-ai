"""Completeness scoring, longitudinal statistics, and the escalation-risk model.

These are the deterministic clinical layers — no network, no model provider —
so they can be tested for the behaviour that actually matters rather than for
"does it return something".
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.clinical import completeness, longitudinal, risk

# ===================================================================== #
# Documentation completeness
# ===================================================================== #
THOROUGH_NOTE = {
    "chief_complaints": [{"text": "cough", "duration": "3 days"}],
    "hpi": "Dry cough for three days, no fever, no breathlessness.",
    "past_history": "No significant past history",
    "allergies": "No known drug allergies",
    "medications": "None",
    "vitals": {"bp": "120/80", "hr": "78", "spo2": "98"},
    "general_exam": "Comfortable at rest, afebrile",
    "systemic_exam": "Chest clear",
    "assessment": "Viral upper respiratory tract infection",
    "plan": "Symptomatic treatment, review if breathless",
}


def test_a_thorough_note_scores_well_and_grades_thorough():
    result = completeness.score(THOROUGH_NOTE)
    assert result["score_pct"] >= 90
    assert result["grade"] == "thorough"
    assert result["missing_required"] == []


def test_an_empty_note_scores_zero():
    result = completeness.score({})
    assert result["score_pct"] == 0
    assert result["grade"] == "incomplete"
    assert set(result["missing_required"]) == {"chief_complaint", "allergies", "assessment", "plan"}


def test_a_detailed_note_with_no_plan_is_incomplete_regardless_of_score():
    """A beautifully documented history with no plan is not an 85% note.

    Without the cap, optional detail can buy back the score of a missing
    required section, which is exactly the wrong incentive.
    """
    note = {**THOROUGH_NOTE, "plan": "", "prescription": [], "investigations": []}
    result = completeness.score(note)
    assert "plan" in result["missing_required"]
    assert result["grade"] == "incomplete"


def test_no_known_allergies_counts_as_documented():
    """Crediting only a positive allergy list teaches clinicians to write noise."""
    for phrasing in ["No known drug allergies", "NKDA", "nil known", "Patient denies allergies"]:
        note = {**THOROUGH_NOTE, "allergies": phrasing}
        assert "allergies" not in completeness.score(note)["missing_required"], phrasing


def test_a_prescription_satisfies_the_plan_requirement():
    note = {**THOROUGH_NOTE, "plan": "", "prescription": [{"generic": "paracetamol"}]}
    assert "plan" not in completeness.score(note)["missing_required"]


def test_scoring_is_deterministic():
    """The whole reason this replaced a model-generated percentage."""
    scores = {completeness.score(THOROUGH_NOTE)["score_pct"] for _ in range(50)}
    assert len(scores) == 1


def test_every_missing_item_comes_with_an_actionable_hint():
    result = completeness.score({})
    for item in result["items"]:
        if not item["present"]:
            assert item["hint"], f"{item['key']} has no hint"


def test_score_survives_a_malformed_note():
    for junk in [{"vitals": "not a dict"}, {"chief_complaints": "text"}, {"entities": 7}]:
        assert 0 <= completeness.score(junk)["score_pct"] <= 100


def test_result_states_it_is_not_a_correctness_measure():
    assert "not a measure of clinical correctness" in completeness.score({})["disclaimer"]


@pytest.mark.parametrize(("text", "expected"), [
    ("No known drug allergies", {"documented": True, "none_known": True, "allergens": []}),
    ("NKDA", {"documented": True, "none_known": True, "allergens": []}),
    ("The patient has no known drug allergies.", {"documented": True, "none_known": True,
                                                  "allergens": []}),
    ("", {"documented": False, "none_known": False, "allergens": []}),
    ("penicillin, sulfa", {"documented": True, "none_known": False,
                           "allergens": ["penicillin", "sulfa"]}),
])
def test_allergy_statement_parsing(text, expected):
    assert completeness.normalise_allergy_statement(text) == expected


@pytest.mark.parametrize("bare", ["None", "none", "Nil", "NKA", "N/A", "not known",
                                  "Unknown", "nothing", "None reported"])
def test_a_one_word_negative_answer_is_not_an_allergen(bare):
    """Found by the extraction evaluation: a model asked for an allergy list
    often answers with a single word, and parsing that as an allergen printed
    an allergy called "None" in red on the patient's chart."""
    parsed = completeness.normalise_allergy_statement(bare)
    assert parsed["none_known"] is True
    assert parsed["allergens"] == []


@pytest.mark.parametrize("allergen", ["Nonoxynol-9", "Nitrofurantoin", "Novocaine"])
def test_real_allergens_that_start_like_a_negative_are_not_swallowed(allergen):
    """The bare-negative pattern is anchored precisely so this cannot happen."""
    parsed = completeness.normalise_allergy_statement(allergen)
    assert parsed["none_known"] is False
    assert parsed["allergens"] == [allergen]


# ===================================================================== #
# Longitudinal statistics
# ===================================================================== #
def _series(values: list[float], metric: str = "hr", start: str = "2026-01-01") -> list[dict]:
    d0 = date.fromisoformat(start)
    return [{"date": (d0 + timedelta(days=30 * i)).isoformat(), "value": str(v)}
            for i, v in enumerate(values)]


def test_a_steadily_rising_vital_is_detected_as_a_significant_trend():
    a = longitudinal.analyse_series("bp", [
        {"date": "2026-01-01", "value": "122/80"},
        {"date": "2026-02-01", "value": "130/84"},
        {"date": "2026-03-01", "value": "138/88"},
        {"date": "2026-04-01", "value": "146/90"},
        {"date": "2026-05-01", "value": "152/94"},
    ])
    assert a.direction == "rising"
    assert a.significant
    assert a.p_value < longitudinal.ALPHA
    assert a.slope_per_30d and a.slope_per_30d > 0


def test_noise_alone_is_not_reported_as_a_trend():
    """The failure mode that makes trend panels useless is crying wolf."""
    a = longitudinal.analyse_series("hr", _series([72, 75, 71, 76, 73, 74]))
    assert a.direction == "stable"
    assert not a.significant


def test_one_bad_reading_cannot_manufacture_a_trend():
    """Least squares would rotate on the outlier; Mann-Kendall and Theil-Sen
    are chosen precisely so a transcription error does not become a finding."""
    a = longitudinal.analyse_series("hr", _series([72, 74, 73, 71, 72, 210]))
    assert not a.significant


def test_a_step_change_is_caught_even_when_no_monotonic_trend_exists():
    """The two tests are complementary: a flat history followed by a jump has
    no monotonic trend but is the most clinically interesting pattern here."""
    a = longitudinal.analyse_series("hr", _series([72, 75, 71, 74, 73, 120]))
    assert not a.significant
    assert a.step_change and a.step_change["detected"]
    assert a.step_change["z"] > 3


def test_too_few_points_makes_no_claim():
    a = longitudinal.analyse_series("hr", _series([72, 96]))
    assert a.direction == "insufficient_data"
    assert "not enough" in a.note.lower()


def test_falling_spo2_is_flagged_as_worth_reviewing():
    a = longitudinal.analyse_series("spo2", _series([99, 98, 96, 95, 93, 92]))
    assert a.direction == "falling"
    assert "worth reviewing" in a.note


@pytest.mark.parametrize(("metric", "reading", "expected"), [
    ("bp", "138/86", 138.0),
    ("bp_dia", "138/86", 86.0),
    ("hr", "92 bpm", 92.0),
    ("temp", "100.4", 100.4),
    ("spo2", "97%", 97.0),
    ("hr", "not recorded", None),
    ("hr", "", None),
])
def test_reading_parsing(metric, reading, expected):
    assert longitudinal.parse_reading(metric, reading) == expected


def test_mann_kendall_matches_a_hand_computed_case():
    """Perfectly increasing: S = n(n-1)/2 = 10 for n = 5."""
    s, p = longitudinal.mann_kendall([1, 2, 3, 4, 5])
    assert s == 10
    assert p < 0.05
    s_flat, p_flat = longitudinal.mann_kendall([5, 5, 5, 5, 5])
    assert s_flat == 0
    assert p_flat == 1.0


def test_theil_sen_is_the_median_pairwise_slope():
    assert longitudinal.theil_sen_slope([0, 1, 2, 3], [0, 2, 4, 6]) == 2.0


# --------------------------------------------------------------------- #
# Fact-level analytics
# --------------------------------------------------------------------- #
def _fact(fact_type, value, visit, day, **structured):
    return {"fact_type": fact_type, "value": value, "visit_id": visit,
            "asserted_at": f"{day}T10:00:00+00:00", "status": "confirmed",
            "clinical_status": structured.pop("clinical_status", "current"),
            "structured": structured}


def test_recurrence_counts_visits_not_repeated_mentions():
    """The same word said three times in one consultation is one occurrence."""
    facts = [
        _fact("symptom", "headache", "v1", "2026-01-01"),
        _fact("symptom", "headache", "v1", "2026-01-01"),
        _fact("symptom", "headache", "v2", "2026-03-01"),
    ]
    result = longitudinal.recurrence(facts)
    assert len(result) == 1
    assert result[0]["occurrences"] == 2
    assert result[0]["span_days"] == 59


def test_a_one_off_symptom_is_not_recurring():
    assert longitudinal.recurrence([_fact("symptom", "cough", "v1", "2026-01-01")]) == []


def test_medication_timeline_uses_prescriptions_not_reported_history():
    """Reported medications are history; conflating them makes the timeline
    claim a drug was stopped that this clinic never started."""
    facts = [
        _fact("medication", "metformin", "v1", "2026-01-01", context="prescribed"),
        _fact("medication", "aspirin", "v1", "2026-01-01", context="reported"),
        _fact("medication", "metformin", "v2", "2026-03-01", context="prescribed"),
        _fact("medication", "amlodipine", "v2", "2026-03-01", context="prescribed"),
    ]
    result = longitudinal.medication_timeline(facts)
    assert result["started"] == ["amlodipine"]
    assert result["continued"] == ["metformin"]
    assert result["stopped"] == []
    assert "aspirin" not in result["current"]


def test_medication_timeline_detects_a_stop():
    facts = [
        _fact("medication", "prednisolone", "v1", "2026-01-01", context="prescribed"),
        _fact("medication", "salbutamol", "v2", "2026-02-01", context="prescribed"),
    ]
    assert longitudinal.medication_timeline(facts)["stopped"] == ["prednisolone"]


def test_a_single_visit_reports_no_changes():
    facts = [_fact("medication", "metformin", "v1", "2026-01-01", context="prescribed")]
    result = longitudinal.medication_timeline(facts)
    assert result["started"] == [] and result["stopped"] == []
    assert result["current"] == ["metformin"]


def test_unresolved_surfaces_a_stale_current_problem():
    """The failure longitudinal memory exists to prevent: a complaint recorded
    once, never revisited, quietly treated as history."""
    facts = [_fact("diagnosis", "Anaemia under investigation", "v1", "2026-01-01")]
    result = longitudinal.unresolved(facts, as_of=date(2026, 9, 1))
    assert len(result) == 1
    assert result[0]["days_since"] > 90


def test_a_resolved_problem_is_not_unresolved():
    facts = [_fact("diagnosis", "Anaemia", "v1", "2026-01-01", clinical_status="resolved")]
    assert longitudinal.unresolved(facts, as_of=date(2026, 9, 1)) == []


def test_a_recently_revisited_problem_is_not_stale():
    facts = [
        _fact("diagnosis", "Hypertension", "v1", "2026-01-01"),
        _fact("diagnosis", "Hypertension", "v2", "2026-08-20"),
    ]
    assert longitudinal.unresolved(facts, as_of=date(2026, 9, 1)) == []


def test_build_reports_its_method_and_a_disclaimer():
    result = longitudinal.build([])
    assert "Mann-Kendall" in result["method"]["trend_test"]
    assert "Theil-Sen" in result["method"]["slope"]
    assert "Not a diagnosis" in result["disclaimer"]


def test_build_splits_systolic_and_diastolic_series():
    facts = [_fact("vital", f"bp: {v}", f"v{i}", f"2026-0{i + 1}-01", metric="bp", reading=v)
             for i, v in enumerate(["120/70", "126/78", "132/86", "138/94"])]
    result = longitudinal.build(facts)
    assert "bp" in result["trends"] and "bp_dia" in result["trends"]
    assert result["trends"]["bp"]["latest"] == 138.0
    assert result["trends"]["bp_dia"]["latest"] == 94.0


# ===================================================================== #
# Escalation risk
# ===================================================================== #
BENIGN = {
    "age": 28, "vitals": {"bp": "120/78", "hr": "74", "spo2": "99", "temp": "98.4"},
    "complaints": [{"text": "ankle pain after twisting", "duration": "2 days"}],
    "hpi": "able to weight bear, no chest pain",
}
SERIOUS = {
    "age": 62, "vitals": {"bp": "104/70", "hr": "112", "spo2": "93", "rr": "24"},
    "complaints": [{"text": "chest pain radiating to the left arm", "duration": "1 hour"}],
    "hpi": "sweating, feels breathless", "past_history": "type 2 diabetes on metformin",
}


def test_a_clearly_serious_presentation_is_escalated():
    result = risk.assess(SERIOUS)
    assert result["escalate"] is True
    assert result["band"] == "high"


def test_a_clearly_benign_presentation_is_not_escalated():
    """Alarm fatigue is the failure mode that makes a safety prompt useless."""
    result = risk.assess(BENIGN)
    assert result["escalate"] is False
    assert result["band"] in ("very_low", "low")


@pytest.mark.parametrize(("name", "payload"), [
    ("hypoxia", {"age": 55, "vitals": {"spo2": "88"}, "complaints": [{"text": "breathless"}]}),
    ("hypotension", {"age": 50, "vitals": {"bp": "84/58"}, "complaints": [{"text": "dizzy"}]}),
    ("neuro deficit", {"age": 60, "complaints": [{"text": "weakness one side and slurred speech"}]}),
    ("syncope", {"age": 40, "complaints": [{"text": "fainted at work"}]}),
    ("meningism", {"age": 22, "vitals": {"temp": "102.4"},
                   "complaints": [{"text": "severe headache with neck stiffness and fever"}]}),
    ("bleeding on anticoagulant", {"age": 68, "complaints": [{"text": "black stools"}],
                                   "past_history": "on warfarin for atrial fibrillation",
                                   "hpi": "bleeding since yesterday"}),
])
def test_published_red_flag_criteria_always_escalate(name, payload):
    """Deterministic criteria are checked independently of the model.

    A learned model — especially one trained on synthetic data — must never be
    able to talk the system out of a hard safety rule.
    """
    result = risk.assess(payload)
    assert result["escalate"] is True, name
    assert result["hard_criteria_met"], f"{name} met no deterministic criterion"


def test_children_are_not_scored_because_the_thresholds_are_adult_ranges():
    result = risk.assess({"age": 6, "vitals": {"temp": "102", "hr": "130"},
                          "complaints": [{"text": "fever"}]})
    assert result["scored"] is False
    assert "paediatric" in result["reason"].lower()


def test_an_empty_encounter_is_not_scored():
    result = risk.assess({})
    assert result["scored"] is False
    assert result["escalate"] is False


def test_negated_symptoms_do_not_fire():
    """'No chest pain' must not read as chest pain."""
    result = risk.assess({
        "age": 30, "vitals": {"bp": "120/80", "hr": "72"},
        "complaints": [{"text": "sore throat"}],
        "hpi": "no chest pain, denies breathlessness, no bleeding",
    })
    assert result["escalate"] is False


def test_every_score_carries_an_explanation():
    result = risk.assess(SERIOUS)
    assert result["reasons"], "a flagged case with no reason is unusable"
    for reason in result["reasons"]:
        assert reason["label"] and reason["contribution_pct"] is not None


def test_the_response_says_what_it_is_not():
    result = risk.assess(SERIOUS)
    assert result["kind"] == "documentation_prompt"
    assert "not a diagnosis" in result["disclaimer"].lower()
    assert "synthetic" in result["disclaimer"].lower()


def test_shipped_model_performance_is_reported_alongside_the_score():
    """A score with no stated accuracy invites the reader to assume it is good."""
    perf = risk.assess(SERIOUS)["model_performance"]
    for key in ("roc_auc", "precision", "recall", "specificity", "f1", "ece"):
        assert key in perf


def test_hard_criteria_win_when_the_model_disagrees():
    features = {"spo2_lt_92": 1.0}
    assert risk.hard_criteria_met(features) == ["SpO2 below 92% — hypoxia"]
    # And a combination that needs two features together.
    combo = {"sym_chest_pain": 1.0, "sym_breathless": 1.0}
    assert "Chest pain with breathlessness" in risk.hard_criteria_met(combo)
    assert risk.hard_criteria_met({"sym_chest_pain": 1.0}) == []


# ===================================================================== #
# Feature matching across whitespace
# ===================================================================== #
@pytest.mark.parametrize("text", [
    "Patient reports chest\npain radiating to the left arm",
    "Patient reports chest  pain",
    "- chest pain\n- sweating\n- breathlessness",
    "chest\t pain",
    "chest\r\npain",
])
def test_a_phrase_split_across_whitespace_still_matches(text):
    """The HPI is model-generated prose and routinely contains newlines and
    bullet lists. A doubled escape in the whitespace regex meant any multi-word
    phrase straddling a line break was missed — which silently disabled the
    deterministic red-flag criteria that depend on it."""
    from app.ml.features import SYMPTOM_PATTERNS, _matches

    assert _matches(text, SYMPTOM_PATTERNS["sym_chest_pain"]) is True


def test_negation_still_wins_across_whitespace():
    from app.ml.features import SYMPTOM_PATTERNS, _matches

    assert _matches("no chest\npain", SYMPTOM_PATTERNS["sym_chest_pain"]) is False


def test_a_multiline_hpi_reaches_the_hard_criteria():
    """End-to-end version of the same bug: the criterion must fire on prose."""
    result = risk.assess({
        "age": 58,
        "complaints": [{"text": "unwell"}],
        "hpi": "Patient describes chest\npain since this morning.\nAlso reports breathlessness\non exertion.",
    })
    assert "Chest pain with breathlessness" in result["hard_criteria_met"]
    assert result["escalate"] is True
