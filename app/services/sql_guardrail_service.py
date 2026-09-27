"""The guardrails that ask the source database about a query, after sql_check_service has passed it as one plain,
read-only SELECT over catalog tables. In order, each only once the one before has passed:

    1. compile       sp_describe_first_result_set: SQL Server compiles it; nothing runs (sql_compile_service)
    2. plan          SET SHOWPLAN_XML: the estimated plan, read for performance problems; nothing runs (sql_plan_service)
    3. sample run    the query capped at a few rows (TOP n), under a timeout, then rolled back (sql_sample_service)

A failure that is the query's fault raises SqlRuntimeError with SQL Server's own reason, so the write step can send it
back to the model and approve/validate can refuse the SQL. A server that cannot be asked is not the query's fault:
the remaining checks are skipped and a note says so. Performance findings are warnings, not failures, except a plan
costlier than SQL_PLAN_MAX_COST when that is set."""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.schemas.sql_generation.validate import PlanEstimate, SampleRun
from app.services import sql_compile_service, sql_plan_service, sql_sample_service

settings = get_settings()


class SqlRuntimeError(ValueError):
    """SQL Server refused or failed the query, or its estimated cost is over SQL_PLAN_MAX_COST."""


@dataclass(frozen=True)
class RuntimeReport:
    compiled: bool = False  # SQL Server compiled it (and its output columns are the expected ones, when given)
    plan: PlanEstimate | None = None
    sample: SampleRun | None = None
    warnings: list[str] = field(default_factory=list)  # performance findings, and notes on checks that were skipped


def run_on_source(source_db: Session, sql: str, expected_columns: list[str] | None = None) -> RuntimeReport:
    """Runs the enabled guardrails on `sql`. `expected_columns`, when given, must be the query's output column names."""
    warnings: list[str] = []

    compiled = False
    if settings.write_compile_check:
        outcome = sql_compile_service.describe(source_db, sql)
        if outcome.error:
            raise SqlRuntimeError(f"SQL Server could not compile the query: {outcome.error}")
        if outcome.columns is None:
            return RuntimeReport(warnings=[f"source database checks skipped: {outcome.unavailable}"])
        if expected_columns is not None and outcome.columns != expected_columns:
            raise SqlRuntimeError(f"SQL Server reports the result columns as {outcome.columns}, expected {expected_columns}")
        compiled = True

    plan = None
    if settings.sql_plan_check:
        plan_outcome = sql_plan_service.estimated_plan(source_db, sql)
        if plan_outcome.findings is None:
            warnings.append(f"plan check skipped: {plan_outcome.unavailable}")
        else:
            findings = plan_outcome.findings
            limit = settings.sql_plan_max_cost
            if limit is not None and findings.estimated_cost > limit:
                raise SqlRuntimeError(
                    f"the estimated query cost is {findings.estimated_cost:.1f}, over the limit of {limit:g}: "
                    + "; ".join(findings.warnings or ["make the query cheaper"])
                )
            warnings.extend(findings.warnings)
            plan = PlanEstimate(
                estimated_cost=findings.estimated_cost,
                estimated_rows=findings.estimated_rows,
                missing_indexes=findings.missing_indexes,
            )

    sample = None
    if settings.sql_sample_check:
        sample_outcome = sql_sample_service.run_sample(
            source_db, sql, settings.sql_sample_rows, settings.sql_sample_timeout_seconds
        )
        if sample_outcome.error:
            raise SqlRuntimeError(
                f"SQL Server failed while running the query (capped at {sample_outcome.row_limit} rows): {sample_outcome.error}"
            )
        if sample_outcome.unavailable:
            warnings.append(f"sample run skipped: {sample_outcome.unavailable}")
        else:
            warnings.extend(_sample_warnings(sample_outcome))
            sample = SampleRun(
                executed=sample_outcome.executed,
                row_limit=sample_outcome.row_limit,
                rows_returned=len(sample_outcome.rows),
                elapsed_ms=sample_outcome.elapsed_ms,
                timed_out=sample_outcome.timed_out,
                columns=sample_outcome.columns,
                rows=sample_outcome.rows if settings.sql_sample_preview else [],
            )

    return RuntimeReport(compiled=compiled, plan=plan, sample=sample, warnings=warnings)


def _sample_warnings(outcome: sql_sample_service.SampleOutcome) -> list[str]:
    if outcome.timed_out:
        return [
            f"the query did not return its first {outcome.row_limit} rows within "
            f"{settings.sql_sample_timeout_seconds}s: expect it to be slow on the full data"
        ]
    warnings = []
    if outcome.elapsed_ms > settings.sql_sample_slow_ms:
        warnings.append(
            f"returning the first {outcome.row_limit} rows took {outcome.elapsed_ms} ms: expect it to be slow on the full data"
        )
    if not outcome.rows:
        warnings.append("the query ran but returned no rows: check that the filter values match the stored data")
    return warnings
