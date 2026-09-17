from fastapi import APIRouter, HTTPException, Path

from app.schemas.ritm import Ritm
from app.services.ritm_service import get_ritm_by_id

router = APIRouter(prefix="/ritm", tags=["ritm"])


@router.get("/{ritm_number}", response_model=Ritm)
def get_ritm(
    ritm_number: str = Path(..., pattern=r"^RITM\d+$"),
) -> Ritm:
    ritm = get_ritm_by_id(ritm_number)
    if ritm is None:
        raise HTTPException(status_code=404, detail=f"RITM '{ritm_number}' not found")
    return ritm
