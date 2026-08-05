"""Drug autocomplete for the prescription module.

Primary source: the real hospital formulary (kb_formulary, category Pharma) — so
prescriptions map to the clinic's actual brands, MRP and therapeutic class.
Falls back to the kb_drugs reference (strength/dosage-form detail) if the
formulary returns nothing.
"""
from fastapi import APIRouter, Depends, Query

from ..deps import CurrentUser, get_current_user
from ...core.supabase import rest, user_headers

router = APIRouter()


def _from_formulary(rows: list[dict]) -> list[dict]:
    return [{
        "brand_name": r.get("brand_name"),
        "generic_name": r.get("generic_name"),
        "strength": r.get("dose_size"),
        "form": r.get("uom_pack_type"),
        "mrp": r.get("mrp"),
        "drug_class": r.get("subcategory"),
        "source": "formulary",
    } for r in rows]


def _from_kb_drugs(rows: list[dict]) -> list[dict]:
    return [{
        "brand_name": r.get("brand_name"),
        "generic_name": r.get("generic_name"),
        "strength": r.get("strength"),
        "form": r.get("dosage_form"),
        "mrp": r.get("mrp"),
        "drug_class": None,
        "source": "reference",
    } for r in rows]


@router.get("/drugs")
async def search_drugs(q: str = Query(min_length=2), user: CurrentUser = Depends(get_current_user)):
    h = user_headers(user.token)
    like = f"(brand_name.ilike.*{q}*,generic_name.ilike.*{q}*)"

    # 1) Hospital formulary — drugs only (Pharma), brand match ranked first.
    resp = await rest("GET", "kb_formulary", headers=h, params={
        "or": like, "category": "ilike.pharma",
        "select": "brand_name,generic_name,dose_size,uom_pack_type,mrp,subcategory",
        "order": "brand_name.asc", "limit": "15",
    })
    rows = resp.json() if resp.status_code == 200 else []
    if rows:
        return {"items": _from_formulary(rows)}

    # 2) Fallback: kb_drugs reference catalogue.
    resp = await rest("GET", "kb_drugs", headers=h, params={
        "or": like, "select": "brand_name,generic_name,strength,dosage_form,mrp", "limit": "12",
    })
    rows = resp.json() if resp.status_code == 200 else []
    return {"items": _from_kb_drugs(rows)}
