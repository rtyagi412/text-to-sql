"""Step 3, /sql/approve: reviewed SQL joins the approved pool."""

from pydantic import BaseModel, Field


class ApproveSqlRequest(BaseModel):
    ritm_number: str
    sql: str = Field(..., description="The reviewed SQL for this RITM, as returned by /sql/write or edited by hand.")


class ApproveSqlResponse(BaseModel):
    ritm_number: str
    sql: str = Field(..., description="The SQL as stored: validated and formatted.")
    tables: list[str]
    warnings: list[str]
    replaced: bool = Field(..., description="True when the RITM was already in the approved pool and was overwritten.")
