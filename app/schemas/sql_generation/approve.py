"""Step 3, /sql/approve: reviewed SQL joins the approved pool."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.sql_generation.validate import PlanEstimate, SampleRun


class ApproveSqlRequest(BaseModel):
    ritm_number: str
    sql: str = Field(..., description="The reviewed SQL for this RITM, as returned by /sql/write or edited by hand.")
    approved_by: str | None = Field(
        default=None, max_length=128, description="Who approved it: the signed-in user's name or id, as the UI knows it."
    )
    note: str | None = Field(default=None, description="Optional reviewer comment kept with the approval.")


class ApprovedRitmResponse(BaseModel):
    """One approval as stored."""

    ritm_number: str
    ritm_name: str
    output_fields: str
    report_criteria: str
    sql: str
    tables: list[str]
    warnings: list[str]
    approved_by: str | None
    note: str | None
    version: int = Field(..., description="1 on first approval, +1 each time it is approved again.")
    approved_at: datetime


class ApproveSqlResponse(BaseModel):
    ritm_number: str
    sql: str = Field(..., description="The SQL as stored: validated and formatted.")
    tables: list[str]
    warnings: list[str]
    replaced: bool = Field(..., description="True when the RITM was already in the approved pool and was overwritten.")
    version: int
    approved_at: datetime
    approved_by: str | None
    compiled: bool = Field(default=False, description="True when SQL Server compiled the query before it was saved.")
    plan: PlanEstimate | None = None
    sample: SampleRun | None = Field(default=None, description="The capped run that proved the query executes.")
