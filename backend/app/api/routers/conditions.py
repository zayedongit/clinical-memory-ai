"""Condition detail — the click-to-expand panel behind a lookup result.
Reads the condition's stored ICMR record (symptoms, signs, investigations,
follow-up, source) plus its red-flag terms."""
from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import CurrentUser, get_current_user
from ...core.supabase import rest, user_headers

router = APIRouter()


def _names(items, key="term", limit=10) -> list[str]:
    out = []
    for it in (items or []):
        v = (it.get(key) or it.get("name")) if isinstance(it, dict) else it
        if v:
            out.append(v)
    return out[:limit]


@router.get("/conditions/{condition_id}")
async def condition_detail(
    condition_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    resp = await rest(
        "GET", "kb_conditions", headers=user_headers(user.token),
        params={"id": f"eq.{condition_id}",
                "select": "id,name,specialty,icd,record,provenance", "limit": "1"},
    )
    rows = resp.json() if resp.status_code == 200 else []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Condition not found")
    row = rows[0]
    rec = row.get("record") or {}
    feats = rec.get("features") or {}
    invs = rec.get("investigations") or {}
    prov = row.get("provenance") or {}
    sources = prov.get("sources") or []

    rf = await rest("POST", "rpc/condition_redflags",
                    headers=user_headers(user.token), json={"cid": condition_id})
    red_flags = [x["label"] for x in (rf.json() if rf.status_code == 200 else [])][:12]

    return {
        "id": row["id"],
        "name": row["name"],
        "specialty": row.get("specialty"),
        "icd": row.get("icd") or [],
        "symptoms": _names(feats.get("symptoms"), "term"),
        "signs": _names(feats.get("signs"), "term"),
        "red_flags": red_flags,
        "investigations": _names(invs.get("mandatory"), "name"),
        "followup": [f.get("actions") for f in (rec.get("followup") or [])
                     if isinstance(f, dict) and f.get("actions")][:6],
        "source": prov.get("evidence_base") or (sources[0] if sources else None),
    }
