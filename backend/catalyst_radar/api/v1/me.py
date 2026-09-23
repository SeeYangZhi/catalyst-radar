from fastapi import APIRouter

from catalyst_radar.api.deps import CurrentUser
from catalyst_radar.schemas.auth import UserOut

router = APIRouter()


@router.get("/me", response_model=UserOut)
async def read_me(current_user: CurrentUser) -> UserOut:
    return UserOut.model_validate(current_user)
