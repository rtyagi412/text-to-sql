"""/sql/validate, and the source-database guardrail results that /sql/write and /sql/approve also return."""

from typing import Any

from pydantic import BaseModel, Field


class PlanEstimate(BaseModel):
    """SQL Server's estimated plan for the query (SET SHOWPLAN_XML: nothing was run)."""

    estimated_cost: float = Field(..., description="The optimizer's estimated subtree cost for the whole query.")
    estimated_rows: float = Field(..., description="The optimizer's estimate of the rows the query returns.")
    missing_indexes: list[str] = Field(default_factory=list, description="Indexes SQL Server says would help this query.")


class SampleRun(BaseModel):
    """The query run on the source database capped at a few rows (TOP n), then rolled back."""

    executed: bool = Field(..., description="True when it ran to completion without an error or a timeout.")
    row_limit: int
    rows_returned: int
    elapsed_ms: int
    timed_out: bool = False
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(
        default_factory=list, description="The sample rows, for a preview; empty when SQL_SAMPLE_PREVIEW is off."
    )


class SqlValidateRequest(BaseModel):
    sql: str = Field(..., description="One T-SQL SELECT, for example as edited by hand before /sql/approve.")
    expected_columns: list[str] | None = Field(
        default=None, description="When given, the query's output column names must be exactly these, in order."
    )


class SqlValidateResponse(BaseModel):
    valid: bool = Field(..., description="True when every check passed: warnings do not make the SQL invalid.")
    errors: list[str] = Field(default_factory=list, description="Why the SQL was refused: static rules or SQL Server.")
    sql: str = Field(..., description="The SQL formatted, when it parsed; otherwise as sent.")
    tables: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(
        default_factory=list,
        description="Performance findings from the SQL text and SQL Server's plan and sample run, and notes on skipped checks.",
    )
    compiled: bool = False
    plan: PlanEstimate | None = None
    sample: SampleRun | None = None
