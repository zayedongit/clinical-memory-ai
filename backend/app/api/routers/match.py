"""Free-text -> canonical matcher endpoint.

Takes a symptom phrase, resolves it to KB canonical terms (fuzzy), and returns
the candidate conditions + red flags. This is the deterministic core of the
Live Consultation Assistant. Requires an authenticated clinic user.
"""
from fastapi import APIRouter, Depends, Query

from ..deps import CurrentUser, get_current_user
from ...core.supabase import rest, user_headers

router = APIRouter()


@router.get("/match")
async def match(
    q: str = Query(min_length=2, description="symptom / finding phrase"),
    user: CurrentUser = Depends(get_current_user),
):
    resp = await rest(
        "POST", "rpc/match_terms", headers=user_headers(user.token), json={"q": q}
    )
    rows = resp.json() if resp.status_code == 200 else []

    terms: dict[str, dict] = {}
    conditions: dict[str, dict] = {}
    for r in rows:
        terms.setdefault(r["canonical_id"], {
            "canonical_id": r["canonical_id"],
            "label": r["label"],
            "kind": r["kind"],
            "similarity": round(r["similarity"], 3),
        })
        cid = r["condition_id"]
        c = conditions.setdefault(cid, {
            "condition_id": cid,
            "name": r["condition_name"],
            "specialty": r["specialty"],
            "cant_miss": False,
            "matched_terms": [],
        })
        if r["canonical_id"] not in c["matched_terms"]:
            c["matched_terms"].append(r["canonical_id"])
        if r["cant_miss"]:
            c["cant_miss"] = True

    ranked = sorted(conditions.values(), key=lambda x: (not x["cant_miss"], x["name"]))
    return {
        "query": q,
        "matched_terms": sorted(terms.values(), key=lambda x: -x["similarity"]),
        "candidate_conditions": ranked,
        "red_flags": [c for c in ranked if c["cant_miss"]],
    }
