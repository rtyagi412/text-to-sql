import json
from functools import lru_cache

from app.core.config import get_settings
from app.schemas.ritm import Ritm

settings = get_settings()


@lru_cache
def _load_ritms() -> dict[str, dict]:
    with settings.ritm_samples_path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {ritm["number"]: ritm for ritm in data}


def get_ritm_by_id(ritm_number: str) -> Ritm | None:
    raw = _load_ritms().get(ritm_number)
    if raw is None:
        return None
    return Ritm.model_validate(raw)
