"""Synthetic patient cohort for training and evaluating the escalation-risk model.

=============================  READ THIS FIRST  =============================
Every record produced here is SYNTHETIC. No real patient data is used, and no
claim about real-world clinical performance can be made from metrics computed
on it. What a good score on this cohort demonstrates is that the *pipeline*
works: the features are extracted correctly, the model recovers a signal it
cannot see directly, the calibration step does what it should, and the
inference path in production agrees with the training path.

Establishing clinical validity would require a labelled retrospective cohort
of real consultations with recorded outcomes, and this project has none. That
limitation is stated in the README and in the model card rather than papered
over with an impressive-looking F1.
============================================================================

**The generative process.** A record is built in three layers:

1. A *latent archetype* is drawn (URTI, ankle sprain, ACS, stroke, sepsis...).
   The archetype is never a feature; the model only ever sees its observable
   manifestations, exactly as a clinician only sees the presentation.

2. Observations are sampled *conditional on the archetype*: complaint wording
   (including the Hindi-English forms this system's transcripts contain),
   vitals drawn from archetype-specific distributions, and realistic
   missingness — roughly a third of consultations record no vitals at all.

3. The label is produced by a rule that is deliberately **not** a linear
   function of the features:
       - a threshold on the *count* of deranged vitals (an interaction),
       - an age x severity interaction,
       - a comorbidity multiplier that only bites for cardiorespiratory
         presentations,
       - and 4% symmetric label noise, standing in for the genuine
         inter-clinician disagreement about who needs urgent review.

   Because the truth is non-linear and noisy, a logistic regression cannot
   reach a perfect score, and the reported metrics are informative rather than
   a tautology. If this file generated labels linearly, the evaluation would
   be measuring nothing.

Everything is seeded, so the cohort, the model and the reported numbers are
reproducible from a clean checkout.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field


# --------------------------------------------------------------------- #
# Archetypes
# --------------------------------------------------------------------- #
@dataclass(frozen=True)
class Archetype:
    name: str
    severity: float                       # latent seriousness, 0..1
    weight: float                         # relative frequency in general practice
    complaints: tuple[str, ...]
    hpi_fragments: tuple[str, ...]
    # (mean, sd) per vital; None means "typically normal for this archetype"
    vitals: dict[str, tuple[float, float]] = field(default_factory=dict)
    duration_days: tuple[float, float] = (3.0, 2.0)
    cardioresp: bool = False


NORMAL_VITALS = {
    "bp_sys": (122.0, 12.0), "bp_dia": (78.0, 8.0), "hr": (78.0, 10.0),
    "spo2": (98.0, 1.2), "temp": (98.4, 0.6), "rr": (16.0, 2.0),
}

ARCHETYPES: tuple[Archetype, ...] = (
    # ---- benign, high frequency -------------------------------------
    Archetype("urti", 0.05, 22.0,
              ("cold and cough", "sore throat", "runny nose", "khansi aur zukam"),
              ("mild fever for two days, eating normally", "no breathlessness, no chest pain"),
              {"temp": (99.6, 0.8)}, (3.0, 1.5)),
    Archetype("ankle_sprain", 0.03, 9.0,
              ("ankle pain after twisting", "sprained ankle", "pair mein moch"),
              ("twisted the ankle playing football, swelling present, able to weight bear",),
              {}, (2.0, 1.0)),
    Archetype("dyspepsia", 0.06, 11.0,
              ("burning in the stomach", "acidity", "pet mein jalan"),
              ("worse after spicy food, no vomiting blood, no black stools",),
              {}, (14.0, 10.0)),
    Archetype("mechanical_back_pain", 0.05, 10.0,
              ("low back pain", "back pain since lifting", "kamar dard"),
              ("no leg weakness, no bladder symptoms, worse on movement",),
              {}, (10.0, 8.0)),
    Archetype("dermatitis", 0.03, 6.0,
              ("itchy rash on the arms", "skin rash", "khujli"),
              ("no fever, no blistering, using a moisturiser",), {}, (7.0, 5.0)),
    Archetype("routine_followup", 0.04, 8.0,
              ("diabetes follow up", "blood pressure check", "routine review"),
              ("no new complaints, taking medicines regularly",), {}, (90.0, 30.0)),
    # ---- intermediate ------------------------------------------------
    Archetype("uti", 0.25, 7.0,
              ("burning while passing urine", "urine infection", "peshab mein jalan"),
              ("increased frequency, mild fever, no flank pain",),
              {"temp": (100.2, 1.0)}, (3.0, 2.0)),
    Archetype("gastroenteritis", 0.3, 7.0,
              ("loose motions and vomiting", "diarrhoea", "ulti aur dast",
               "vomiting and mild abdominal pain"),
              ("unable to keep fluids down since morning, feeling weak",
               "watery stools, mild stomach pain, no fever"),
              {"hr": (100.0, 12.0), "temp": (100.0, 1.0), "bp_sys": (110.0, 12.0)}, (2.0, 1.0)),
    Archetype("asthma_flare", 0.30, 5.0,
              ("wheezing and breathless", "asthma attack", "saans phoolna"),
              ("using the inhaler more often, worse at night",),
              {"spo2": (94.0, 2.0), "rr": (22.0, 4.0), "hr": (104.0, 12.0)}, (2.0, 1.5),
              cardioresp=True),
    Archetype("appendicitis", 0.9, 2.0,
              ("abdominal pain moving to the right side", "severe abdominal pain and vomiting",
               "pet mein dard aur ulti"),
              ("pain started around the navel and moved down, vomited twice, fever since morning",
               "abdominal pain with fever, tender on pressing, worse on movement"),
              {"temp": (100.8, 1.0), "hr": (102.0, 12.0)}, (1.0, 0.8)),
    Archetype("tuberculosis", 0.85, 1.5,
              ("chronic cough with weight loss", "cough for three weeks and night sweats",
               "purani khansi aur vazan kam"),
              ("night sweats for a month, appetite poor, blood in sputum twice",
               "cough for three weeks, losing weight, evening fever"),
              {"temp": (100.0, 1.0), "weight": (52.0, 8.0)}, (30.0, 12.0)),
    Archetype("cellulitis", 0.4, 4.0,
              ("red swollen leg", "spreading redness on the leg"),
              ("warm and tender, fever since yesterday",),
              {"temp": (101.0, 1.0), "hr": (98.0, 12.0)}, (3.0, 2.0)),
    # ---- serious, low frequency ---------------------------------------
    Archetype("acs", 0.95, 3.0,
              ("chest pain radiating to the left arm", "chest pressure with sweating",
               "seene mein dard"),
              ("started one hour ago at rest, sweating, feels breathless",),
              {"bp_sys": (104.0, 22.0), "hr": (104.0, 20.0), "spo2": (94.0, 3.0), "rr": (22.0, 4.0)},
              (0.2, 0.2), cardioresp=True),
    Archetype("stroke", 0.95, 2.0,
              ("weakness one side and slurred speech", "facial droop since morning"),
              ("sudden onset, cannot lift the right arm, speech is unclear",),
              {"bp_sys": (168.0, 25.0), "hr": (88.0, 14.0)}, (0.3, 0.3)),
    Archetype("sepsis", 0.95, 2.0,
              ("high fever with confusion", "fever and very weak", "tez bukhar"),
              ("drowsy since last night, not passing much urine",),
              {"temp": (102.4, 1.4), "hr": (118.0, 16.0), "bp_sys": (94.0, 14.0),
               "rr": (26.0, 5.0), "spo2": (93.0, 3.0)}, (2.0, 1.0), cardioresp=True),
    Archetype("pulmonary_embolism", 0.9, 1.5,
              ("sudden breathlessness with chest pain", "breathless and leg swelling"),
              ("came on suddenly, one calf is swollen and tender",),
              {"hr": (112.0, 16.0), "spo2": (92.0, 3.0), "rr": (26.0, 5.0)}, (1.0, 1.0),
              cardioresp=True),
    Archetype("gi_bleed", 0.9, 1.5,
              ("vomiting blood", "black stools and dizziness", "khoon ki ulti"),
              ("passed black tarry stools twice, feels faint on standing",),
              {"bp_sys": (98.0, 16.0), "hr": (114.0, 16.0)}, (1.0, 1.0)),
    Archetype("meningitis", 0.95, 1.0,
              ("severe headache with neck stiffness and fever",),
              ("cannot tolerate light, vomited twice, drowsy",),
              {"temp": (102.0, 1.2), "hr": (108.0, 14.0)}, (1.0, 0.8)),
    Archetype("dka", 0.9, 1.0,
              ("vomiting with rapid breathing", "very thirsty and vomiting",
               "excessive thirst with abdominal pain"),
              ("known diabetic, stopped insulin, passing a lot of urine",
               "deep rapid breathing, vomiting since morning, excessive thirst"),
              {"rr": (26.0, 4.0), "hr": (116.0, 14.0), "bp_sys": (100.0, 14.0)}, (2.0, 1.0)),
    Archetype("ectopic", 0.95, 0.8,
              ("lower abdominal pain with vaginal bleeding",),
              ("missed period last month, pain is severe, feels dizzy",),
              {"bp_sys": (98.0, 16.0), "hr": (110.0, 16.0)}, (1.0, 1.0)),
    Archetype("subarachnoid", 0.95, 0.8,
              ("worst headache of my life", "sudden severe headache with vomiting"),
              ("started like a thunderclap while straining, vomited",),
              {"bp_sys": (170.0, 24.0)}, (0.2, 0.2)),
)

COMORBIDITY_TEXT = {
    "hx_cardiac": "ischaemic heart disease, on aspirin",
    "hx_diabetes": "type 2 diabetes on metformin",
    "hx_respiratory": "asthma, uses a salbutamol inhaler",
    "hx_renal": "chronic kidney disease stage 3",
    "hx_immunocompromised": "on long-term steroids",
    "hx_anticoagulant": "on warfarin for atrial fibrillation",
}


# --------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------- #
def _pick(rng: random.Random, items, weights) -> object:
    return rng.choices(items, weights=weights, k=1)[0]


def _sample_vitals(rng: random.Random, arch: Archetype) -> dict[str, str]:
    """Vitals conditional on the archetype, with realistic missingness.

    In this clinic's own data roughly a third of consultations record no
    vitals, and partial recording is common. Training on a cohort where every
    record has a full set would produce a model that quietly degrades in
    production, which is the most common way a demo model fails in the field.
    """
    if rng.random() < 0.30:
        return {}

    out: dict[str, str] = {}
    sbp = sdp = None
    for key, (mean, sd) in {**NORMAL_VITALS, **arch.vitals}.items():
        if rng.random() < 0.18:          # this particular vital not taken
            continue
        value = rng.gauss(mean, sd)
        if key == "bp_sys":
            sbp = max(60.0, min(value, 240.0))
        elif key == "bp_dia":
            sdp = max(35.0, min(value, 140.0))
        elif key == "spo2":
            out["spo2"] = str(int(max(70.0, min(value, 100.0))))
        elif key == "temp":
            out["temp"] = f"{max(94.0, min(value, 106.0)):.1f}"
        else:
            out[key] = str(int(max(1.0, value)))
    if sbp is not None:
        out["bp"] = f"{int(sbp)}/{int(sdp if sdp is not None else sbp * 0.64)}"
    return out


def _deranged_count(vitals: dict[str, str]) -> int:
    """How many vitals sit outside the standard adult ranges."""
    from .features import VITAL_THRESHOLDS, Encounter, extract

    f = extract(Encounter(vitals=vitals))
    # Count each physiological system once: the paired thresholds
    # (sbp_lt_90 / sbp_lt_100) must not double-count one low blood pressure.
    systems = {
        "bp": max(f["sbp_lt_90"], f["sbp_lt_100"], f["sbp_gt_180"], f["dbp_gt_110"]),
        "hr": max(f["hr_gt_110"], f["hr_lt_50"]),
        "spo2": max(f["spo2_lt_92"], f["spo2_lt_95"]),
        "temp": max(f["temp_ge_101"], f["temp_lt_96"]),
        "rr": max(f["rr_ge_22"], f["rr_ge_25"]),
    }
    assert set(systems) <= {k.split("_")[0] for k in VITAL_THRESHOLDS} | {"bp", "spo2", "temp"}
    return int(sum(systems.values()))


def _label(rng: random.Random, arch: Archetype, age: float, vitals: dict, comorbid: set[str]) -> int:
    """Ground truth: does this encounter warrant urgent clinician review?

    Non-linear on purpose — see the module docstring.
    """
    deranged = _deranged_count(vitals)

    risk = arch.severity

    # Interaction 1: two or more deranged systems is a step change in concern,
    # not a linear accumulation. One abnormal vital is common and often benign.
    if deranged >= 2:
        risk += 0.30
    elif deranged == 1:
        risk += 0.06

    # Interaction 2: age amplifies severity rather than adding to it. A 25-year
    # old and an 80-year old with an ankle sprain are both fine; with chest
    # pain they are not the same patient.
    if age >= 65:
        risk *= 1.25
    elif age <= 5:
        risk *= 1.20

    # Interaction 3: comorbidity matters for cardiorespiratory presentations
    # and barely at all for a rash.
    if arch.cardioresp and comorbid:
        risk += 0.12 * min(len(comorbid), 2)
    if "hx_anticoagulant" in comorbid and arch.name in ("gi_bleed", "subarachnoid", "stroke"):
        risk += 0.15

    label = 1 if risk >= 0.55 else 0
    # 4% symmetric label noise: real triage decisions are not deterministic and
    # two competent clinicians disagree at roughly this rate on borderline cases.
    if rng.random() < 0.04:
        label = 1 - label
    return label


def generate(n: int = 4000, seed: int = 20260911) -> list[dict]:
    """Produce `n` synthetic encounters with ground-truth escalation labels."""
    rng = random.Random(seed)
    archetypes = list(ARCHETYPES)
    weights = [a.weight for a in archetypes]

    rows: list[dict] = []
    for _ in range(n):
        arch: Archetype = _pick(rng, archetypes, weights)  # type: ignore[assignment]

        age = max(1.0, min(rng.gauss(42.0, 19.0), 95.0))
        comorbid: set[str] = set()
        base_p = 0.06 + (age / 100.0) * 0.35
        for key in COMORBIDITY_TEXT:
            if rng.random() < base_p:
                comorbid.add(key)
        if arch.name == "dka":
            comorbid.add("hx_diabetes")
        if arch.name == "asthma_flare":
            comorbid.add("hx_respiratory")

        vitals = _sample_vitals(rng, arch)
        duration = max(0.05, rng.gauss(*arch.duration_days))

        rows.append({
            "archetype": arch.name,                      # kept for analysis, never a feature
            "age": round(age, 1),
            "vitals": vitals,
            "complaints": [{"text": rng.choice(arch.complaints),
                            "duration": _duration_text(duration)}],
            "hpi": rng.choice(arch.hpi_fragments),
            "past_history": ", ".join(sorted(COMORBIDITY_TEXT[c] for c in comorbid)),
            "medications": "",
            "duration_days": round(duration, 2),
            "label": _label(rng, arch, age, vitals, comorbid),
        })
    return rows


def _duration_text(days: float) -> str:
    if days < 1:
        return f"{max(int(days * 24), 1)} hours"
    if days < 14:
        return f"{round(days)} days"
    if days < 60:
        return f"{round(days / 7)} weeks"
    return f"{round(days / 30)} months"


def split(rows: list[dict], *, test_fraction: float = 0.25, seed: int = 7) -> tuple[list[dict], list[dict]]:
    """Deterministic shuffle then split.

    A random split is legitimate here because the records are i.i.d. draws from
    the generative process — there is no patient identity to leak across the
    boundary, which is the usual reason a random split is wrong in clinical ML.
    """
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    cut = int(len(shuffled) * (1 - test_fraction))
    return shuffled[:cut], shuffled[cut:]
