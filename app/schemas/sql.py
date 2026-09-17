from pydantic import BaseModel, ConfigDict

from app.schemas.extraction import FilterValue


class GeneratedSql(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ritm_number: str
    sql: str
    parameters: dict[str, FilterValue] = {}
    tables: list[str]
