#!/usr/bin/env python3
"""Extraction and citation-grounding evaluation.

    uv run python scripts/eval_extraction.py            # deterministic, no API key
    uv run python scripts/eval_extraction.py --live     # through the real providers
    uv run python scripts/eval_extraction.py --gate     # non-zero exit on regression

**What is measured.** In the default mode the model response is frozen (see
`eval/datasets/README.md`), so what is being scored is *this project's
post-processing pipeline*: entity normalisation, allergy-negation handling,
vitals parsing, and — the part that matters most — citation verification.

That framing is deliberate. A benchmark that scores "the model" tells you
almost nothing you can act on: the number moves when a provider ships a new
checkpoint, and you cannot fix it. A benchmark that scores your own pipeline
moves only when you change your own code, which makes it a regression test.

**Citation verification is the strongest measurement here** because its ground
truth is objective rather than judged: a quote either appears in the transcript
or it does not. Precision is "of the quotes we accepted, how many were real",
recall is "of the real quotes, how many did we keep". A verifier that drops
everything scores perfect precision and is useless, which is why both are
reported and gated.

Field-level extraction F1 is reported per entity type. Its ground truth is
annotation, so it is softer — matching is normalised and substring-tolerant,
and the numbers should be read as "does the pipeline preserve what the model
found" rather than as clinical accuracy.

`--live` runs the same cases through the configured provider chain instead, so
the same harness can measure the model when that is the question.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.ai import citations  # noqa: E402
from app.clinical import completeness  # noqa: E402

DATASET = BACKEND / "eval" / "datasets" / "extraction_gold.json"
RESULTS = BACKEND / "eval" / "results" / "extraction.json"

# Regression floors. Set from the measured baseline, below it by enough that
# noise does not trip the gate but a real regression does.
GATES = {
    "citation_precision": 1.00,   # accepting a fabricated quote is the one
                                  # failure this system must never have
    "citation_recall": 0.85,
    "symptoms_f1": 0.80,
    "medications_f1": 0.80,
    "allergies_accuracy": 1.00,
}


# --------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------- #
def _norm(text: str) -> str:
    return " ".join(citations.normalise(text).split())


def _matches(predicted: str, gold: str) -> bool:
    """Substring-tolerant match.

    "breathlessness on exertion" should count as finding "breathlessness";
    requiring string equality would measure phrasing, not extraction.
    """
    p, g = _norm(predicted), _norm(gold)
    return bool(p) and bool(g) and (g in p or p in g)


def prf(predicted: list[str], gold: list[str]) -> dict:
    """Set precision/recall/F1 with the tolerant matcher.

    Each gold item may be claimed by at most one prediction, so a prediction
    list that repeats the same finding cannot inflate recall.
    """
    unclaimed = list(gold)
    true_positives = 0
    for p in predicted:
        for i, g in enumerate(unclaimed):
            if _matches(p, g):
                unclaimed.pop(i)
                true_positives += 1
                break

    precision = true_positives / len(predicted) if predicted else (1.0 if not gold else 0.0)
    recall = true_positives / len(gold) if gold else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "tp": true_positives, "predicted": len(predicted), "gold": len(gold),
            "missed": list(unclaimed)}


def _aggregate(per_case: list[dict]) -> dict:
    """Micro-average: pool the counts, then compute. A macro-average over six
    cases would let one tiny case swing the headline."""
    tp = sum(c["tp"] for c in per_case)
    predicted = sum(c["predicted"] for c in per_case)
    gold = sum(c["gold"] for c in per_case)
    precision = tp / predicted if predicted else 1.0
    recall = tp / gold if gold else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "tp": tp, "predicted": predicted, "gold": gold}


# --------------------------------------------------------------------- #
# The pipeline under test
# --------------------------------------------------------------------- #
def run_pipeline(transcript: str, model_response: dict) -> dict:
    """Exactly the normalisation `/scribe/extract` applies, minus the network."""
    complaints = []
    for c in (model_response.get("chief_complaints") or []):
        if isinstance(c, dict) and c.get("text"):
            complaints.append({
                "text": str(c["text"]).strip()[:120],
                "duration": str(c.get("duration", "")).strip()[:40],
                "evidence": citations.verify_or_drop(c.get("evidence", ""), transcript)[:200],
            })

    raw_vitals = model_response.get("vitals") or {}
    vitals = {k: str(v).strip() for k, v in raw_vitals.items() if str(v).strip()}

    fields = {k: str(model_response.get(k, "")).strip()
              for k in ("hpi", "past_history", "allergies", "medications",
                        "general_exam", "systemic_exam")}
    raw_evidence = model_response.get("evidence") or {}
    evidence = {}
    for key in (*fields, "vitals"):
        verified = citations.verify_or_drop(raw_evidence.get(key, ""), transcript)[:200]
        if verified:
            evidence[key] = verified

    payload = {"chief_complaints": complaints, **fields, "vitals": vitals, "evidence": evidence}
    payload["grounding"] = citations.coverage(
        {**fields, "vitals": " ".join(vitals.values())}, evidence)
    payload["completeness"] = completeness.score(payload)
    return payload


async def run_live(transcript: str) -> dict:
    from app.ai import prompts, providers

    result = await providers.generate_json(
        prompts.extract("", transcript), capability="extract", max_tokens=4096)
    return result.data


# --------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------- #
def evaluate_extraction(cases: list[dict], *, live: bool) -> dict:
    per_type: dict[str, list[dict]] = {"symptoms": [], "medications": [], "vitals": []}
    allergy_correct, allergy_total = 0, 0
    case_reports = []

    for case in cases:
        transcript = case["transcript"]
        response = asyncio.run(run_live(transcript)) if live else case["model_response"]
        out = run_pipeline(transcript, response)
        gold = case["gold"]

        symptoms = prf([c["text"] for c in out["chief_complaints"]], gold["symptoms"])
        medications = prf(
            [out["medications"]] if out["medications"] else [], gold["medications"])
        vitals = prf(
            [f"{k} {v}" for k, v in out["vitals"].items()],
            [f"{k} {v}" for k, v in gold["vitals"].items()],
        )
        per_type["symptoms"].append(symptoms)
        per_type["medications"].append(medications)
        per_type["vitals"].append(vitals)

        # Allergy handling is scored as a three-way classification, because the
        # clinically important distinction is "documented none" vs "not asked"
        # vs "has allergens" — an F1 over allergen strings hides it entirely.
        parsed = completeness.normalise_allergy_statement(out["allergies"])
        expected = ("none_known" if gold["allergies_none_known"]
                    else "allergens" if gold["allergens"] else "not_recorded")
        actual = ("none_known" if parsed["none_known"]
                  else "allergens" if parsed["allergens"] else "not_recorded")
        allergy_total += 1
        allergy_correct += int(expected == actual)

        case_reports.append({
            "id": case["id"], "symptoms": symptoms, "medications": medications,
            "vitals": vitals,
            "allergy_expected": expected, "allergy_actual": actual,
            "evidence_coverage_pct": out["grounding"]["evidence_coverage_pct"],
            "completeness_pct": out["completeness"]["score_pct"],
            "completeness_grade": out["completeness"]["grade"],
        })

    return {
        "symptoms": _aggregate(per_type["symptoms"]),
        "medications": _aggregate(per_type["medications"]),
        "vitals": _aggregate(per_type["vitals"]),
        "allergies_accuracy": round(allergy_correct / allergy_total, 4) if allergy_total else 0.0,
        "cases": case_reports,
    }


def evaluate_citations(citation_cases: dict) -> dict:
    """Objective ground truth: is the quote in the transcript or not?"""
    tp = fp = tn = fn = 0
    failures = []

    for item in citation_cases["items"]:
        transcript = item["transcript"]
        for quote in item["supported"]:
            if citations.verify(quote, transcript).supported:
                tp += 1
            else:
                fn += 1
                failures.append({"verdict": "dropped a real quote", "quote": quote})
        for quote in item["fabricated"]:
            if citations.verify(quote, transcript).supported:
                fp += 1
                failures.append({"verdict": "accepted a fabricated quote", "quote": quote})
            else:
                tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "specificity": round(tn / (tn + fp), 4) if (tn + fp) else 1.0,
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "note": ("precision = of the quotes we showed the physician, how many were real. "
                 "recall = of the real quotes, how many we kept. A verifier that drops "
                 "everything scores perfect precision and is useless, so both are gated."),
        "failures": failures,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(DATASET))
    ap.add_argument("--out", default=str(RESULTS))
    ap.add_argument("--live", action="store_true",
                    help="call the configured providers instead of the recorded responses")
    ap.add_argument("--gate", action="store_true", help="exit non-zero if a metric is below floor")
    args = ap.parse_args()

    data = json.loads(Path(args.dataset).read_text())
    extraction = evaluate_extraction(data["cases"], live=args.live)
    citation = evaluate_citations(data["citation_cases"])

    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "live" if args.live else "recorded (deterministic)",
        "data_provenance": data["data_provenance"],
        "n_cases": len(data["cases"]),
        "citation_verification": citation,
        "extraction": extraction,
        "gates": GATES,
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")

    # ---- human-readable summary ----
    print(f"\nExtraction & grounding eval — {report['mode']}, {report['n_cases']} synthetic cases")
    print("=" * 74)
    c = citation
    print("Citation verification (objective ground truth)")
    print(f"  precision {c['precision']:.3f}   recall {c['recall']:.3f}   "
          f"F1 {c['f1']:.3f}   specificity {c['specificity']:.3f}")
    print(f"  {c['confusion_matrix']}")
    for f in c["failures"]:
        print(f"  ! {f['verdict']}: {f['quote'][:70]!r}")

    print("\nField-level extraction (annotated ground truth)")
    for field in ("symptoms", "medications", "vitals"):
        m = extraction[field]
        print(f"  {field:14s} P {m['precision']:.3f}  R {m['recall']:.3f}  "
              f"F1 {m['f1']:.3f}   ({m['tp']}/{m['gold']} found)")
    print(f"  {'allergy status':14s} accuracy {extraction['allergies_accuracy']:.3f}")

    print("\nPer case")
    for case in extraction["cases"]:
        flag = "" if case["allergy_expected"] == case["allergy_actual"] else \
            f"   ALLERGY {case['allergy_expected']} -> {case['allergy_actual']}"
        print(f"  {case['id']:22s} symptoms F1 {case['symptoms']['f1']:.2f}  "
              f"evidence {case['evidence_coverage_pct']:3d}%  "
              f"completeness {case['completeness_pct']:3d}% ({case['completeness_grade']}){flag}")

    measured = {
        "citation_precision": citation["precision"],
        "citation_recall": citation["recall"],
        "symptoms_f1": extraction["symptoms"]["f1"],
        "medications_f1": extraction["medications"]["f1"],
        "allergies_accuracy": extraction["allergies_accuracy"],
    }
    print("\n" + "=" * 74)
    breaches = [f"{k}: {v:.3f} < {GATES[k]:.2f}" for k, v in measured.items() if v < GATES[k]]
    if breaches:
        print("BELOW GATE: " + "; ".join(breaches))
    else:
        print("All gates met.")
    print(f"wrote {args.out}\n")

    return 1 if (args.gate and breaches) else 0


if __name__ == "__main__":
    raise SystemExit(main())
