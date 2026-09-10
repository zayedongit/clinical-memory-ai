"""Feature engineering for the escalation-risk model.

Every feature here is a named, human-readable clinical fact — "systolic blood
pressure below 90", "documented chest pain", "known cardiac history" — and
every one is binary or a simple bounded number. That is a deliberate
constraint, not a limitation of effort:

* A physician has to be able to read why the model raised a case. Free-text
  embeddings would very likely score better on a benchmark and would be
  unusable in a consultation, because "the vector was close to the escalation
  cluster" is not a reason anyone can act on or dispute.
* Binary threshold features make the model auditable against published triage
  criteria. If the coefficient on `spo2_lt_92` is not strongly positive, the
  model is wrong and it is obvious that it is wrong.
* They are stable. A vocabulary shift in how a doctor phrases a complaint
  moves an embedding; it does not move "SpO2 = 88".

Thresholds are taken from widely used early-warning-score cut-points (NEWS2
and standard adult triage ranges). They are encoded in one table so they can
be reviewed and changed by a clinician rather than being buried in code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------- #
# Vital-sign cut-points. Adult ranges; the paediatric case is out of scope
# and is handled by refusing to score children (see `score` in risk.py).
# --------------------------------------------------------------------- #
VITAL_THRESHOLDS = {
    "sbp_lt_90":   ("bp_sys", "lt", 90),
    "sbp_lt_100":  ("bp_sys", "lt", 100),
    "sbp_gt_180":  ("bp_sys", "gt", 180),
    "dbp_gt_110":  ("bp_dia", "gt", 110),
    "hr_gt_110":   ("hr", "gt", 110),
    "hr_lt_50":    ("hr", "lt", 50),
    "spo2_lt_92":  ("spo2", "lt", 92),
    "spo2_lt_95":  ("spo2", "lt", 95),
    "temp_ge_101": ("temp", "ge", 101.0),     # Fahrenheit, as the UI collects
    "temp_lt_96":  ("temp", "lt", 96.0),
    "rr_ge_22":    ("rr", "ge", 22),
    "rr_ge_25":    ("rr", "ge", 25),
}

# --------------------------------------------------------------------- #
# Symptom lexicon. Each feature is a set of surface forms, including the
# Hindi-English forms that appear in this system's transcripts. Matching is
# word-boundary aware so "no chest pain" does not match — negation is handled
# separately below, because getting that wrong flips the meaning of the input.
# --------------------------------------------------------------------- #
SYMPTOM_PATTERNS: dict[str, tuple[str, ...]] = {
    "sym_chest_pain": ("chest pain", "chest discomfort", "chest tightness", "chest pressure",
                       "seene mein dard", "chhati mein dard", "angina"),
    "sym_breathless": ("breathless", "shortness of breath", "short of breath", "dyspnoea",
                       "dyspnea", "saans phoolna", "saans lene mein", "cannot breathe",
                       "difficulty breathing"),
    "sym_neuro_deficit": ("weakness one side", "one-sided weakness", "facial droop", "slurred speech",
                          "cannot speak", "numbness one side", "hemiparesis", "vision loss",
                          "sudden confusion", "lakwa"),
    "sym_severe_headache": ("worst headache", "thunderclap", "sudden severe headache",
                            "severe headache", "sir mein tez dard"),
    "sym_syncope": ("fainted", "fainting", "loss of consciousness", "blackout", "passed out",
                    "collapsed", "behosh"),
    "sym_bleeding": ("bleeding", "vomiting blood", "haematemesis", "hematemesis", "black stools",
                     "melaena", "melena", "blood in stool", "coughing blood", "haemoptysis",
                     "khoon aa raha"),
    "sym_severe_pain": ("severe pain", "worst pain", "unbearable pain", "excruciating",
                        "10/10 pain", "bahut tez dard"),
    "sym_altered_mental": ("confused", "confusion", "drowsy", "unresponsive", "disoriented",
                           "altered sensorium", "not recognising"),
    "sym_abdominal_severe": ("severe abdominal pain", "rigid abdomen", "guarding", "rebound tenderness",
                             "acute abdomen", "pet mein tez dard"),
    "sym_neck_stiffness": ("neck stiffness", "stiff neck", "neck rigidity", "photophobia"),
    "sym_pregnancy_bleeding": ("vaginal bleeding", "missed period", "pregnant", "pregnancy"),
    "sym_weight_loss": ("weight loss", "losing weight", "lost weight", "vazan kam"),
    "sym_fever": ("fever", "bukhar", "pyrexia", "temperature"),
    # Added after the red-flag evaluation missed appendicitis, DKA and TB:
    # the presentations were real, the vocabulary to describe them was not there.
    "sym_abdominal_pain": ("abdominal pain", "stomach pain", "tummy pain", "belly pain",
                           "pet dard", "pet mein dard", "pet me dard"),
    "sym_vomiting": ("vomiting", "vomited", "throwing up", "ulti", "emesis"),
    "sym_rapid_breathing": ("rapid breathing", "fast breathing", "breathing fast",
                            "deep breathing", "tachypnoea", "tachypnea", "kussmaul"),
    "sym_polydipsia": ("excessive thirst", "very thirsty", "drinking a lot of water",
                       "passing a lot of urine", "excessive urination", "polyuria", "bahut pyaas"),
    "sym_night_sweats": ("night sweats", "sweating at night", "raat ko paseena"),
    "sym_chronic_cough": ("cough for three weeks", "cough for 3 weeks", "chronic cough",
                          "cough for a month", "long standing cough", "purani khansi"),
    "sym_haemoptysis": ("blood in sputum", "coughing blood", "haemoptysis", "hemoptysis",
                        "blood in phlegm", "khansi mein khoon"),
}

HISTORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "hx_cardiac": ("heart attack", "myocardial infarction", "angina", "ischaemic heart",
                   "ischemic heart", "cad", "stent", "bypass", "heart failure", "arrhythmia"),
    "hx_diabetes": ("diabetes", "diabetic", "dm2", "type 2 diabetes", "sugar"),
    "hx_respiratory": ("asthma", "copd", "tuberculosis", "tb", "bronchiectasis"),
    "hx_renal": ("kidney disease", "ckd", "renal failure", "dialysis"),
    "hx_immunocompromised": ("hiv", "chemotherapy", "immunosuppress", "transplant", "steroid"),
    "hx_anticoagulant": ("warfarin", "acitrom", "apixaban", "rivaroxaban", "dabigatran",
                         "clopidogrel", "blood thinner", "anticoagulant"),
}

# Phrases that negate whatever follows within a short window.
_NEGATIONS = ("no ", "not ", "without ", "denies ", "denied ", "negative for ", "nil ", "absent ",
              "ruled out ", "nahi ", "koi nahi")
_NEGATION_WINDOW = 30  # characters before the match

FEATURE_ORDER: tuple[str, ...] = (
    "age_scaled",
    "age_ge_65",
    "age_le_12",
    *VITAL_THRESHOLDS.keys(),
    "vitals_absent",
    *SYMPTOM_PATTERNS.keys(),
    *HISTORY_PATTERNS.keys(),
    "duration_le_1d",
    "duration_ge_30d",
    "n_complaints_scaled",
    "information_sparse",
)


@dataclass(frozen=True)
class Encounter:
    """The model's input, normalised away from any particular UI shape."""

    age: float | None = None
    vitals: dict[str, str] | None = None       # {"bp": "138/86", "hr": "104", ...}
    complaints: tuple[str, ...] = ()
    hpi: str = ""
    past_history: str = ""
    medications: str = ""
    duration_days: float | None = None
    completeness_pct: int | None = None

    @classmethod
    def from_payload(cls, payload: dict) -> Encounter:
        complaints = []
        for c in payload.get("complaints") or payload.get("chief_complaints") or []:
            complaints.append(str(c.get("text", "")) if isinstance(c, dict) else str(c))
        return cls(
            age=_as_float(payload.get("age")),
            vitals=payload.get("vitals") or {},
            complaints=tuple(x for x in complaints if x.strip()),
            hpi=str(payload.get("hpi") or ""),
            past_history=str(payload.get("past_history") or ""),
            medications=str(payload.get("medications") or ""),
            duration_days=_duration_days(payload),
            completeness_pct=payload.get("completeness_pct"),
        )


def _as_float(v: object) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(hour|hr|day|din|week|hafta|month|mahina|year|saal)", re.I)
_DURATION_UNITS = {"hour": 1 / 24, "hr": 1 / 24, "day": 1, "din": 1, "week": 7, "hafta": 7,
                   "month": 30, "mahina": 30, "year": 365, "saal": 365}


def _duration_days(payload: dict) -> float | None:
    explicit = _as_float(payload.get("duration_days"))
    if explicit is not None:
        return explicit
    texts = []
    for c in payload.get("complaints") or payload.get("chief_complaints") or []:
        if isinstance(c, dict):
            texts.append(str(c.get("duration") or ""))
    texts.append(str(payload.get("duration") or ""))
    for text in texts:
        m = _DURATION.search(text)
        if m:
            return float(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]
    return None


def _vital(vitals: dict, key: str) -> float | None:
    raw = str((vitals or {}).get(key.split("_")[0] if key.startswith("bp") else key, "")).strip()
    if key == "bp_sys" or key == "bp_dia":
        raw = str((vitals or {}).get("bp", "")).strip()
        if "/" in raw:
            part = raw.split("/")[0 if key == "bp_sys" else 1]
            return _as_float(re.sub(r"[^\d.]", "", part))
        return _as_float(raw) if key == "bp_sys" else None
    return _as_float(re.sub(r"[^\d.]", "", raw)) if raw else None


def _matches(text: str, phrases: tuple[str, ...]) -> bool:
    """Phrase present and not negated."""
    low = f" {re.sub(r'\\s+', ' ', text.lower())} "
    for phrase in phrases:
        for m in re.finditer(re.escape(phrase.lower()), low):
            window = low[max(0, m.start() - _NEGATION_WINDOW):m.start()]
            if any(neg in window for neg in _NEGATIONS):
                continue
            return True
    return False


def extract(enc: Encounter) -> dict[str, float]:
    """Encounter -> the named feature vector, as a dict for readability."""
    vitals = enc.vitals or {}
    symptom_text = " . ".join([*enc.complaints, enc.hpi])
    history_text = " . ".join([enc.past_history, enc.medications])

    f: dict[str, float] = {}

    age = enc.age
    f["age_scaled"] = min(max((age or 40.0) / 100.0, 0.0), 1.2)
    f["age_ge_65"] = 1.0 if (age is not None and age >= 65) else 0.0
    f["age_le_12"] = 1.0 if (age is not None and age <= 12) else 0.0

    present_vitals = 0
    for name, (key, op, cut) in VITAL_THRESHOLDS.items():
        value = _vital(vitals, key)
        if value is None:
            f[name] = 0.0
            continue
        present_vitals += 1
        f[name] = float(
            (op == "lt" and value < cut)
            or (op == "gt" and value > cut)
            or (op == "ge" and value >= cut)
        )
    # No vitals at all is itself informative: the encounter has no objective
    # anchor, so a concerning history cannot be reassured away by observations.
    f["vitals_absent"] = 1.0 if present_vitals == 0 else 0.0

    for name, phrases in SYMPTOM_PATTERNS.items():
        f[name] = 1.0 if _matches(symptom_text, phrases) else 0.0
    for name, phrases in HISTORY_PATTERNS.items():
        f[name] = 1.0 if _matches(history_text, phrases) else 0.0

    d = enc.duration_days
    f["duration_le_1d"] = 1.0 if (d is not None and d <= 1) else 0.0
    f["duration_ge_30d"] = 1.0 if (d is not None and d >= 30) else 0.0

    f["n_complaints_scaled"] = min(len(enc.complaints) / 5.0, 1.0)
    f["information_sparse"] = 1.0 if (enc.completeness_pct is not None and enc.completeness_pct < 55) else 0.0

    return {k: f.get(k, 0.0) for k in FEATURE_ORDER}


def vector(enc: Encounter) -> list[float]:
    features = extract(enc)
    return [features[k] for k in FEATURE_ORDER]


HUMAN_LABELS: dict[str, str] = {
    "age_scaled": "Age",
    "age_ge_65": "Age 65 or over",
    "age_le_12": "Child (12 or under)",
    "sbp_lt_90": "Systolic BP below 90",
    "sbp_lt_100": "Systolic BP below 100",
    "sbp_gt_180": "Systolic BP above 180",
    "dbp_gt_110": "Diastolic BP above 110",
    "hr_gt_110": "Heart rate above 110",
    "hr_lt_50": "Heart rate below 50",
    "spo2_lt_92": "SpO2 below 92%",
    "spo2_lt_95": "SpO2 below 95%",
    "temp_ge_101": "Temperature 101F or above",
    "temp_lt_96": "Temperature below 96F",
    "rr_ge_22": "Respiratory rate 22 or above",
    "rr_ge_25": "Respiratory rate 25 or above",
    "vitals_absent": "No vitals recorded",
    "sym_chest_pain": "Chest pain documented",
    "sym_breathless": "Breathlessness documented",
    "sym_neuro_deficit": "Focal neurological deficit",
    "sym_severe_headache": "Severe or sudden headache",
    "sym_syncope": "Syncope or loss of consciousness",
    "sym_bleeding": "Bleeding",
    "sym_severe_pain": "Severe pain",
    "sym_altered_mental": "Altered mental state",
    "sym_abdominal_severe": "Severe abdominal pain or peritonism",
    "sym_neck_stiffness": "Neck stiffness or photophobia",
    "sym_pregnancy_bleeding": "Pregnancy-related bleeding",
    "sym_weight_loss": "Unintentional weight loss",
    "sym_fever": "Fever",
    "hx_cardiac": "Known cardiac history",
    "hx_diabetes": "Known diabetes",
    "hx_respiratory": "Known respiratory disease",
    "hx_renal": "Known renal disease",
    "hx_immunocompromised": "Immunocompromised",
    "hx_anticoagulant": "On an anticoagulant or antiplatelet",
    "duration_le_1d": "Onset within 24 hours",
    "duration_ge_30d": "Chronic (30+ days)",
    "n_complaints_scaled": "Number of complaints",
    "information_sparse": "Sparse documentation",
}
