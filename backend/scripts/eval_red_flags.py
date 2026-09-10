#!/usr/bin/env python3
"""Red-flag evaluation for the escalation path that actually ships.

    uv run python scripts/eval_red_flags.py
    uv run python scripts/eval_red_flags.py --gate

**What changed and why.** The previous version of this script evaluated
`kb_ground_red_flags()`, a knowledge-base fuzzy matcher that had already been
removed from the request path — it surfaced conditions unrelated to the
presentation (liver cancer for an ankle sprain), so the scribe stopped calling
it. The evaluation kept running and kept reporting a recall number for code no
patient's consultation ever touched, which is worse than having no evaluation:
it produced a metric that looked like evidence.

It also required a live `DATABASE_URL`, so it could not run in CI, which is how
the drift went unnoticed.

This version evaluates the path in production today:

  1. the deterministic hard criteria in `app/clinical/risk.py`, and
  2. the calibrated escalation-risk model.

It needs no database, no API key and no network, so it runs on every push.

Two numbers, and both matter:

* **Sensitivity** on the red-flag cases — a missed emergency is the failure
  that hurts a patient.
* **False-alarm rate** on the benign controls — a system that flags a common
  cold teaches clinicians to dismiss the panel, which converts into missed
  emergencies at one remove. Alarm fatigue is a patient-safety problem, not a
  UX complaint, so it is gated too.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.clinical import risk  # noqa: E402

DEFAULT_CASES = BACKEND / "eval" / "red_flag_cases.json"
RESULTS = BACKEND / "eval" / "results" / "red_flags.json"

GATES = {"sensitivity": 0.90, "false_alarm_rate": 0.20}


def assess(case: dict) -> dict:
    payload = {
        "age": case.get("age", 45),
        "vitals": case.get("vitals", {}),
        "complaints": [{"text": s} for s in case.get("symptoms", [])],
        "hpi": case.get("hpi", " ".join(case.get("symptoms", []))),
        "past_history": case.get("past_history", ""),
        "medications": case.get("medications", ""),
    }
    return risk.assess(payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--out", default=str(RESULTS))
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.cases).read_text())
    positives = data.get("cases", [])
    negatives = data.get("negative_cases", [])
    if not positives:
        print("No cases found.", file=sys.stderr)
        return 2

    print(f"\nRed-flag escalation eval — {len(positives)} red-flag, "
          f"{len(negatives)} benign (synthetic)")
    print("=" * 78)

    caught, missed = 0, []
    positive_rows = []
    for case in positives:
        result = assess(case)
        hit = bool(result.get("escalate"))
        caught += hit
        why = ", ".join(result.get("hard_criteria_met", [])) or (
            f"model p={result.get('probability')}" if result.get("model_flagged") else "")
        positive_rows.append({"name": case["name"], "escalated": hit,
                              "probability": result.get("probability"),
                              "hard_criteria": result.get("hard_criteria_met", [])})
        if hit:
            print(f"  [CATCH] {case['name']:34s} {why[:60]}")
        else:
            missed.append(case["name"])
            print(f"  [MISS ] {case['name']:34s} p={result.get('probability')}")

    sensitivity = caught / len(positives)
    print("-" * 78)
    print(f"Sensitivity: {caught}/{len(positives)} = {sensitivity:.0%}\n")

    false_alarms, negative_rows = 0, []
    if negatives:
        print(f"Benign controls — alarm fatigue check ({len(negatives)} cases)")
        print("-" * 78)
        for case in negatives:
            result = assess(case)
            flagged = bool(result.get("escalate"))
            false_alarms += flagged
            negative_rows.append({"name": case["name"], "escalated": flagged,
                                  "probability": result.get("probability")})
            mark = "FALSE" if flagged else "OK   "
            detail = ", ".join(result.get("hard_criteria_met", [])) if flagged else ""
            print(f"  [{mark}] {case['name']:34s} p={result.get('probability')} {detail[:40]}")
        false_alarm_rate = false_alarms / len(negatives)
        print("-" * 78)
        print(f"False-alarm rate: {false_alarms}/{len(negatives)} = {false_alarm_rate:.0%}\n")
    else:
        false_alarm_rate = 0.0

    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_provenance": "synthetic — classic presentations written for evaluation",
        "evaluates": "app/clinical/risk.py — deterministic criteria plus the calibrated model",
        "sensitivity": round(sensitivity, 4),
        "missed": missed,
        "false_alarm_rate": round(false_alarm_rate, 4),
        "n_red_flag_cases": len(positives),
        "n_benign_cases": len(negatives),
        "gates": GATES,
        "red_flag_cases": positive_rows,
        "benign_cases": negative_rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")

    breaches = []
    if sensitivity < GATES["sensitivity"]:
        breaches.append(f"sensitivity {sensitivity:.2f} < {GATES['sensitivity']}")
    if false_alarm_rate > GATES["false_alarm_rate"]:
        breaches.append(f"false-alarm rate {false_alarm_rate:.2f} > {GATES['false_alarm_rate']}")

    print("=" * 78)
    print("BELOW GATE: " + "; ".join(breaches) if breaches else "All gates met.")
    print(f"wrote {args.out}\n")
    return 1 if (args.gate and breaches) else 0


if __name__ == "__main__":
    raise SystemExit(main())
