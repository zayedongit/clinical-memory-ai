"""Drug autocomplete for the prescription module (from kb_drugs)."""
from fastapi import APIRouter, Depends, Query

from ..deps import CurrentUser, get_current_user
from ...core.supabase import rest, user_headers

router = APIRouter()


@router.get("/drugs")
async def search_drugs(q: str = Query(min_length=2), user: CurrentUser = Depends(get_current_user)):
    resp = await rest(
        "GET", "kb_drugs", headers=user_headers(user.token),
        params={
            "or": f"(brand_name.ilike.*{q}*,generic_name.ilike.*{q}*)",
            "select": "brand_name,generic_name,strength,dosage_form,mrp",
            "limit": "12",
        },
    )
    return {"items": resp.json() if resp.status_code == 200 else []}
