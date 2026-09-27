"""Step 3, /sql/approve: reviewed SQL joins the approved pool."""

import json
import logging

from sqlalchemy.orm import Session

from app.schemas.sql_generation import ApprovedRitmResponse, ApproveSqlRequest, ApproveSqlResponse
from app.services import approved_ritm_service, schema_context_service, sql_check_service, sql_guardrail_service
from app.services.approved_ritm_service import ApprovedRitm
from app.services.ritm_service import get_ritm_by_id
from app.services.sql_guardrail_service import RuntimeReport

logger = logging.getLogger(__name__)


def approve_sql(
    request: ApproveSqlRequest, catalog_db: Session, source_db: Session | None = None
) -> ApproveSqlResponse | None:
    """Validates and formats reviewed SQL and stores it in the approved_ritms table against the RITM, overwriting
    any earlier entry for it. With `source_db`, the SQL must also compile and run on a capped sample
    (sql_guardrail_service): hand-edited SQL that SQL Server refuses is never approved (SqlRuntimeError, a 400).
    The record keeps the ticket's own output_fields and report_criteria: retrieval matches on them, and later
    requests read what this SQL does beyond them as the conventions it carries."""
    ritm = get_ritm_by_id(request.ritm_number)
    if ritm is None:
        return None
    tables = sql_check_service.referenced_tables(request.sql)
    context = schema_context_service.build_exact_schema_context(catalog_db, tables)
    checked = sql_check_service.check_sql(request.sql, context.columns)
    runtime = sql_guardrail_service.run_on_source(source_db, checked.sql) if source_db is not None else RuntimeReport()
    warnings = [*checked.warnings, *runtime.warnings]

    saved = approved_ritm_service.save(
        catalog_db,
        ApprovedRitm(
            number=ritm.number,
            name=ritm.name,
            output_fields=ritm.variables.output_fields,
            report_criteria=ritm.variables.report_criteria,
            tables=checked.tables,
            sql=checked.sql,
            warnings=warnings,
            approved_by=request.approved_by,
            note=request.note,
        ),
    )
    logger.info(
        "sql approved %s",
        json.dumps({"ritm": ritm.number, "tables": checked.tables, "replaced": saved.replaced, "version": saved.version}),
    )
    return ApproveSqlResponse(
        ritm_number=ritm.number,
        sql=checked.sql,
        tables=checked.tables,
        warnings=warnings,
        replaced=saved.replaced,
        version=saved.version,
        approved_at=saved.approved_at,
        approved_by=request.approved_by,
        compiled=runtime.compiled,
        plan=runtime.plan,
        sample=runtime.sample,
    )


def get_approval(ritm_number: str, catalog_db: Session) -> ApprovedRitmResponse | None:
    """The stored approval for a RITM, or None when it has none (or is an approved set of clarifications, which
    this API does not create)."""
    record = approved_ritm_service.get_approved(catalog_db, ritm_number)
    if record is None or record.sql is None or record.approved_at is None:
        return None
    return ApprovedRitmResponse(
        ritm_number=record.number,
        ritm_name=record.name,
        output_fields=record.output_fields,
        report_criteria=record.report_criteria,
        sql=record.sql,
        tables=record.tables,
        warnings=record.warnings,
        approved_by=record.approved_by,
        note=record.note,
        version=record.version,
        approved_at=record.approved_at,
    )
