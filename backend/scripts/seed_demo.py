#!/usr/bin/env python3
"""Load synthetic demo patients into a local database.

    export CMA_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/cma_dev
    uv run python scripts/seed_demo.py

**Everything created here is synthetic.** Invented names, invented histories,
invented readings. Nothing derives from a real patient.

It exists so the product can be demonstrated and the longitudinal analytics can
be seen doing something. A patient with one visit shows nothing interesting; the
statistics only mean anything across a series, so the seed creates patients with
histories designed to exercise each analysis path:

* **Ramesh Iyer** — blood pressure creeping up over five visits. Should produce
  a significant rising Mann-Kendall trend.
* **Sunita Devi** — stable readings, then one sudden jump. No monotonic trend;
  the step detector should catch it. This is the case that shows why both tests
  are needed.
* **Farhan Qureshi** — a recurring complaint and a medication swap.
* **Meera Nair** — a problem recorded once as current and never revisited, which
  is what the "unresolved" analysis exists to surface.

Refuses to run against anything but a loopback database.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import psycopg
except ImportError:  # pragma: no cover
    print("psycopg is required: uv sync --group dev", file=sys.stderr)
    raise SystemExit(2) from None

CLINIC = "11111111-1111-1111-1111-111111111111"
DOCTOR = "99999999-0000-0000-0000-0000000000d1"
AUTH_UID = "00000000-0000-0000-0000-0000000000d1"

TODAY = date.today()


def days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


PATIENTS: list[dict] = [
    {
        "name": "Ramesh Iyer (demo)", "gender": "male", "dob": "1968-04-12", "phone": "9000000101",
        "story": "Blood pressure rising steadily — a significant monotonic trend.",
        "visits": [
            {"days_ago": 400, "diagnosis": "Essential hypertension", "symptoms": ["headache"],
             "vitals": {"bp": "128/82", "hr": "76"}, "prescribed": ["amlodipine 5mg"]},
            {"days_ago": 300, "diagnosis": "Essential hypertension", "symptoms": [],
             "vitals": {"bp": "134/86", "hr": "78"}, "prescribed": ["amlodipine 5mg"]},
            {"days_ago": 200, "diagnosis": "Essential hypertension", "symptoms": ["headache"],
             "vitals": {"bp": "142/88", "hr": "80"}, "prescribed": ["amlodipine 5mg"]},
            {"days_ago": 100, "diagnosis": "Essential hypertension", "symptoms": [],
             "vitals": {"bp": "148/92", "hr": "82"}, "prescribed": ["amlodipine 5mg"]},
            {"days_ago": 10, "diagnosis": "Essential hypertension", "symptoms": ["headache"],
             "vitals": {"bp": "156/96", "hr": "84"}, "prescribed": ["amlodipine 10mg"]},
        ],
    },
    {
        "name": "Sunita Devi (demo)", "gender": "female", "dob": "1975-11-03", "phone": "9000000102",
        "story": "Stable, then a sudden jump. No trend; the step detector catches it.",
        "visits": [
            {"days_ago": 365, "diagnosis": "Iron deficiency anaemia", "symptoms": ["fatigue"],
             "vitals": {"hr": "74", "bp": "118/76"}, "prescribed": ["ferrous ascorbate"]},
            {"days_ago": 280, "diagnosis": "Iron deficiency anaemia", "symptoms": ["fatigue"],
             "vitals": {"hr": "78", "bp": "120/78"}, "prescribed": ["ferrous ascorbate"]},
            {"days_ago": 190, "diagnosis": "Iron deficiency anaemia", "symptoms": [],
             "vitals": {"hr": "72", "bp": "116/74"}, "prescribed": ["ferrous ascorbate"]},
            {"days_ago": 95, "diagnosis": "Iron deficiency anaemia", "symptoms": [],
             "vitals": {"hr": "76", "bp": "119/77"}, "prescribed": ["ferrous ascorbate"]},
            {"days_ago": 5, "diagnosis": "Palpitations for investigation",
             "symptoms": ["palpitations"], "vitals": {"hr": "126", "bp": "122/80"},
             "prescribed": ["ferrous ascorbate"]},
        ],
    },
    {
        "name": "Farhan Qureshi (demo)", "gender": "male", "dob": "1990-06-21", "phone": "9000000103",
        "story": "A recurring complaint and a medication change between visits.",
        "allergies": ["penicillin"],
        "visits": [
            {"days_ago": 240, "diagnosis": "Acid peptic disease",
             "symptoms": ["epigastric pain"], "vitals": {"bp": "122/78"},
             "prescribed": ["pantoprazole 40mg"]},
            {"days_ago": 120, "diagnosis": "Acid peptic disease",
             "symptoms": ["epigastric pain", "nausea"], "vitals": {"bp": "124/80"},
             "prescribed": ["pantoprazole 40mg"]},
            {"days_ago": 15, "diagnosis": "Acid peptic disease",
             "symptoms": ["epigastric pain"], "vitals": {"bp": "126/82"},
             "prescribed": ["esomeprazole 40mg", "domperidone 10mg"]},
        ],
    },
    {
        "name": "Meera Nair (demo)", "gender": "female", "dob": "1982-02-17", "phone": "9000000104",
        "story": "A problem recorded as current 8 months ago and never revisited.",
        "allergies": [],
        "no_known_allergies": True,
        "visits": [
            {"days_ago": 240, "diagnosis": "Thyroid nodule under investigation",
             "symptoms": ["neck swelling"], "vitals": {"bp": "118/74", "hr": "70"},
             "prescribed": []},
            {"days_ago": 30, "diagnosis": "Viral upper respiratory tract infection",
             "symptoms": ["cough", "sore throat"], "vitals": {"temp": "99.8", "hr": "84"},
             "prescribed": ["paracetamol 650mg"]},
        ],
    },
]


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "", "/tmp", "/var/run/postgresql"}


def guard(dsn: str, *, confirmed: bool) -> None:
    """Refuse anything that is not a loopback database.

    This script writes invented patients. An allow-list of loopback hosts is
    the only check that is actually safe here: a deny-list of known cloud
    providers misses every self-hosted database, and the earlier version also
    accepted any DSN whose *database* happened to be named `postgres` — which
    is the default name, so almost every connection string passed.

    Anything else requires --i-know-this-is-not-local, so seeding a real
    database is a deliberate act rather than a typo in an environment variable.
    """
    host = urlparse(dsn).hostname or ""
    if host.lower() in LOOPBACK_HOSTS:
        return
    if confirmed:
        print(f"Proceeding against non-loopback host {host!r} because you asked.", file=sys.stderr)
        return
    raise SystemExit(
        f"Refusing to seed synthetic demo patients into {host!r}, which is not a loopback "
        "address. Point CMA_TEST_DATABASE_URL at a local database, or pass "
        "--i-know-this-is-not-local if you are certain."
    )


def seed(conn) -> None:
    conn.execute(
        "insert into public.clinics (id, name) values (%s, 'Demo Clinic A') "
        "on conflict (id) do nothing", (CLINIC,))
    conn.execute(
        "insert into public.users (id, clinic_id, auth_uid, name, role, registration_no, "
        "qualifications) values (%s, %s, %s, 'Dr Demo', 'doctor', 'DEMO-00000', 'MBBS, MD') "
        "on conflict (id) do nothing", (DOCTOR, CLINIC, AUTH_UID))

    created = 0
    for spec in PATIENTS:
        existing = conn.execute(
            "select id from public.patients where clinic_id = %s and name = %s",
            (CLINIC, spec["name"])).fetchone()
        if existing:
            print(f"  = {spec['name']} already present, skipping")
            continue

        patient_id = conn.execute(
            "insert into public.patients (clinic_id, name, gender, dob, phone) "
            "values (%s, %s, %s, %s, %s) returning id",
            (CLINIC, spec["name"], spec["gender"], spec["dob"], spec["phone"])).fetchone()[0]

        for visit in spec["visits"]:
            when = days_ago(visit["days_ago"])
            visit_id = conn.execute(
                "insert into public.visits (patient_id, clinic_id, doctor_id, status, "
                "started_at, approved_at, consent_given, consent_at, consent_method) "
                "values (%s, %s, %s, 'approved', %s, %s, true, %s, 'verbal') returning id",
                (patient_id, CLINIC, DOCTOR, when, when, when)).fetchone()[0]

            conn.execute(
                "insert into public.soap_notes (visit_id, patient_id, clinic_id, created_by, "
                "subjective, objective, assessment, plan, entities, prescription, vitals, "
                "attested, attested_at, attested_by, created_at) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, "
                "true, %s, %s, %s)",
                (visit_id, patient_id, CLINIC, DOCTOR,
                 "; ".join(visit["symptoms"]) or "Routine review",
                 ", ".join(f"{k.upper()} {v}" for k, v in visit["vitals"].items()),
                 visit["diagnosis"],
                 "Continue current management." if visit["prescribed"] else "Reassurance and review.",
                 json.dumps({"symptoms": visit["symptoms"], "diagnoses": [visit["diagnosis"]]}),
                 json.dumps([{"generic": drug, "dose": "", "frequency": "", "duration": ""}
                             for drug in visit["prescribed"]]),
                 json.dumps(visit["vitals"]), when, DOCTOR, when))

            facts: list[tuple] = []
            for symptom in visit["symptoms"]:
                facts.append(("symptom", symptom, {}, "current"))
            facts.append(("diagnosis", visit["diagnosis"], {}, "current"))
            for drug in visit["prescribed"]:
                facts.append(("medication", drug, {"context": "prescribed"}, "current"))
            for metric, reading in visit["vitals"].items():
                facts.append(("vital", f"{metric}: {reading}",
                              {"metric": metric, "reading": reading}, "current"))
            if visit is spec["visits"][0]:
                for allergen in spec.get("allergies", []):
                    facts.append(("allergy", allergen, {}, "current"))
                if spec.get("no_known_allergies"):
                    facts.append(("allergy", "No known drug allergies",
                                  {"documented_negative": True}, "current"))

            for fact_type, value, structured, clinical_status in facts:
                conn.execute(
                    "insert into public.clinical_facts (clinic_id, patient_id, visit_id, "
                    "fact_type, value, structured, source, status, clinical_status, "
                    "asserted_by, asserted_at) "
                    "values (%s, %s, %s, %s, %s, %s::jsonb, 'doctor_confirmed_ai', 'confirmed', "
                    "%s, %s, %s)",
                    (CLINIC, patient_id, visit_id, fact_type, value, json.dumps(structured),
                     clinical_status, DOCTOR, when))

        created += 1
        print(f"  + {spec['name']}: {len(spec['visits'])} visits — {spec['story']}")

    print(f"\nSeeded {created} synthetic patient(s) into clinic {CLINIC}.")
    print("Sign in as any user, link them to that clinic, and open a patient to see the analytics.")


def main() -> int:
    dsn = os.getenv("CMA_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        print("Set CMA_TEST_DATABASE_URL (or DATABASE_URL) to a local database.", file=sys.stderr)
        return 2
    guard(dsn, confirmed="--i-know-this-is-not-local" in sys.argv)

    print("Seeding SYNTHETIC demo data. No real patient information is used.\n")
    with psycopg.connect(dsn, autocommit=False) as conn:
        seed(conn)
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
