from fastapi import APIRouter, Depends

from ..deps import CurrentUser, get_current_user
from ...core.supabase import rest, user_headers
from ...schemas import MeResponse

router = APIRouter()


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser = Depends(get_current_user)) -> MeResponse:
    # Fetch clinic name through the user's own token so RLS applies.
    resp = await rest(
        "GET",
        "clinics",
        headers=user_headers(user.token),
        params={"id": f"eq.{user.clinic_id}", "select": "name", "limit": "1"},
    )
    rows = resp.json() if resp.status_code == 200 else []
    clinic_name = rows[0]["name"] if rows else None
    return MeResponse(
        user_id=user.user_id,
        clinic_id=user.clinic_id,
        role=user.role,
        clinic_name=clinic_name,
    )
