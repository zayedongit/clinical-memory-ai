"""Free-text -> canonical matcher (Live Consultation Assistant core).

Accepts one or several findings ("chest pain + sweating + breathless"),
normalizes common abbreviations, matches each against the KB, then ranks
candidate conditions by how many findings they explain, red-flag status,
India prevalence, and (optionally) patient age/sex applicability.
"""
import re

from fastapi import APIRouter, Depends, Query

from ..deps import CurrentUser, get_current_user
from ...core.supabase import rest, user_headers

router = APIRouter()

# Common clinical abbreviations -> canonical phrasing (symptom-focused).
ABBREV = {
    "sob": "breathlessness",
    "shortness of breath": "breathlessness",
    "short of breath": "breathlessness",
    "doe": "exertional breathlessness",
    "cp": "chest pain",
    "loc": "loss of consciousness",
    "n/v": "nausea and vomiting",
    "abd pain": "abdominal pain",
    "htn": "hypertension",
    "dm": "diabetes",
    "wt loss": "weight loss",
    "loss of weight": "weight loss",
}

PREVALENCE_RANK = {
    "common": 0, "high": 0, "frequent": 0,
    "moderate": 1, "medium": 1,
    "uncommon": 2, "low": 2,
    "rare": 3, "very rare": 3,
}


def _normalize(token: str) -> str:
    t = token.strip().lower()
    return ABBREV.get(t, token.strip())


def split_findings(q: str) -> list[str]:
    parts = re.split(r"[,;+/&]| and | with | plus ", q, flags=re.IGNORECASE)
    out = [_normalize(p) for p in parts if len(p.strip()) >= 2]
    return out or [q.strip()]


def _prev_rank(tier: str | None) -> int:
    return PREVALENCE_RANK.get((tier or "").lower(), 4)


@router.get("/match")
async def match(
    q: str = Query(min_length=2, description="one or more findings"),
    age: int | None = Query(default=None, ge=0, le=120),
    sex: str | None = Query(default=None),
    user: CurrentUser = Depends(get_current_user),
):
    findings = split_findings(q)
    terms: dict[str, dict] = {}
    conditions: dict[str, dict] = {}

    for finding in findings:
        resp = await rest("POST", "rpc/match_terms", headers=user_headers(user.token), json={"q": finding})
        for r in (resp.json() if resp.status_code == 200 else []):
            terms.setdefault(r["canonical_id"], {
                "canonical_id": r["canonical_id"], "label": r["label"],
                "kind": r["kind"], "similarity": round(r["similarity"], 3),
            })
            cid = r["condition_id"]
            c = conditions.setdefault(cid, {
                "condition_id": cid, "name": r["condition_name"], "specialty": r["specialty"],
                "cant_miss": False, "prevalence_tier": r.get("prevalence_tier"),
                "age_min": r.get("age_min"), "age_max": r.get("age_max"), "sex_appl": r.get("sex"),
                "findings": set(), "matched_via": [],
            })
            c["findings"].add(finding)
            if r["cant_miss"]:
                c["cant_miss"] = True
            seen = {(m["label"], m["term_type"]) for m in c["matched_via"]}
            if (r["label"], r["term_type"]) not in seen:
                c["matched_via"].append({"label": r["label"], "term_type": r["term_type"]})

    sex_l = (sex or "").strip().lower() or None
    out = []
    for c in conditions.values():
        applies, reasons = True, []
        if age is not None:
            if c["age_min"] is not None and age < c["age_min"]:
                applies, _ = False, reasons.append("age below typical range")
            if c["age_max"] is not None and age > c["age_max"]:
                applies, _ = False, reasons.append("age above typical range")
        if sex_l and c["sex_appl"] in ("male", "female") and c["sex_appl"] != sex_l:
            applies, _ = False, reasons.append(f"typically {c['sex_appl']}")
        out.append({
            "condition_id": c["condition_id"], "name": c["name"], "specialty": c["specialty"],
            "cant_miss": c["cant_miss"], "prevalence_tier": c["prevalence_tier"],
            "findings_matched": len(c["findings"]), "matched_via": c["matched_via"],
            "applies": applies, "applies_note": "; ".join(reasons) or None,
        })

    out.sort(key=lambda x: (
        not x["applies"],
        -x["findings_matched"],
        not x["cant_miss"],
        _prev_rank(x["prevalence_tier"]),
        -len(x["matched_via"]),
        x["name"],
    ))
    return {
        "query": q,
        "findings": findings,
        "matched_terms": sorted(terms.values(), key=lambda x: -x["similarity"]),
        "candidate_conditions": out,
        "red_flags": [c for c in out if c["cant_miss"]],
    }
