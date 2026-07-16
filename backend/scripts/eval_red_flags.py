#!/usr/bin/env python3
"""Red-flag grounding eval.

Scores whether the curated KB grounding (kb_ground_red_flags) surfaces the
expected can't-miss condition for a set of classic red-flag presentations.
Runs directly against Postgres — no LLM, no API key — so it's cheap and CI-safe.

Usage:
  export DATABASE_URL='postgresql://postgres.<ref>:<pw>@<host>:5432/postgres'
  cd backend && uv run python scripts/eval_red_flags.py
  # options: --cases path/to.json  --sim 0.45  --verbose

Exit code is non-zero if recall falls below --min-recall (default 0.0, i.e.
report-only) so it can gate CI once you're happy with coverage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2

DEFAULT_CASES = Path(__file__).resolve().parent.parent / "eval" / "red_flag_cases.json"


def ground(cur, findings: list[str], sim: float) -> list[dict]:
    cur.execute(
        "select condition_id, condition_name, acuity, any_cantmiss, matched_count, "
        "redflag_label, action from kb_ground_red_flags(%s::text[], %s::real, 6)",
        (findings, sim),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def case_hit(rows: list[dict], expect_any: list[str]) -> tuple[bool, str]:
    hay = " || ".join(
        f"{(r.get('condition_name') or '')} {(r.get('redflag_label') or '')}".lower()
        for r in rows
    )
    for term in expect_any:
        if term.lower() in hay:
            return True, term
    return False, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--sim", type=float, default=0.45)
    ap.add_argument("--min-recall", type=float, default=0.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: set DATABASE_URL (session-pooler URI).", file=sys.stderr)
        return 2

    data = json.loads(Path(args.cases).read_text())
    cases = data.get("cases", [])
    if not cases:
        print("No cases found.", file=sys.stderr)
        return 2

    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()

    passed = 0
    print(f"\nRed-flag grounding eval — {len(cases)} cases (sim>={args.sim})\n" + "-" * 64)
    for c in cases:
        rows = ground(cur, c["symptoms"], args.sim)
        hit, term = case_hit(rows, c["expect_any"])
        passed += hit
        mark = "PASS" if hit else "MISS"
        conds = ", ".join(sorted({r["condition_name"] for r in rows})[:4]) or "(no can't-miss match)"
        print(f"[{mark}] {c['name']:32s} via '{term}'" if hit else f"[{mark}] {c['name']:32s} -> {conds}")
        if args.verbose and rows:
            for r in rows[:6]:
                print(f"         · {r['condition_name']}  ⟶  {r.get('redflag_label')}")

    recall = passed / len(cases)
    print("-" * 64)
    print(f"Recall: {passed}/{len(cases)} = {recall:.0%}\n")

    # Precision guard: benign presentations should NOT raise a LOUD (can't-miss)
    # red flag for a serious condition. Routine grey notes are acceptable.
    negatives = data.get("negative_cases", [])
    if negatives:
        clean = 0
        print(f"Precision / alarm-fatigue — {len(negatives)} benign cases\n" + "-" * 64)
        for c in negatives:
            rows = ground(cur, c["symptoms"], args.sim)
            loud = [r for r in rows if r.get("any_cantmiss")]
            bad = sorted({r["condition_name"] for r in loud
                          if any(t.lower() in (r["condition_name"] or "").lower() for t in c["forbid_any"])})
            ok = not bad
            clean += ok
            if ok:
                extra = f" ({len(loud)} quiet can't-miss note(s))" if loud else ""
                print(f"[OK  ] {c['name']:28s} no false alarm{extra}")
            else:
                print(f"[FALSE] {c['name']:28s} loud flag(s): {', '.join(bad)}")
        print("-" * 64)
        print(f"No-false-alarm: {clean}/{len(negatives)} = {clean/len(negatives):.0%}\n")

    cur.close()
    conn.close()
    return 0 if recall >= args.min_recall else 1


if __name__ == "__main__":
    raise SystemExit(main())
