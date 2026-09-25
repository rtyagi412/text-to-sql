"""Step 3, /sql/approve: reviewed SQL joins the approved pool."""

import json
import logging

from sqlalchemy.orm import Session

from app.schemas.sql_generation import ApproveSqlResponse
from app.services import approved_ritm_service, schema_context_service, sql_check_service
from app.services.approved_ritm_service import ApprovedRitm
from app.services.ritm_service import get_ritm_by_id

logger = logging.getLogger(__name__)


def approve_sql(ritm_number: str, sql: str, catalog_db: Session) -> ApproveSqlResponse | None:
    """Validates and formats reviewed SQL and stores it in the approved pool against the RITM, overwriting any
    earlier entry for it. The record keeps the ticket's own output_fields and report_criteria: retrieval matches
    on them, and later requests read what this SQL does beyond them as the conventions it carries."""
    ritm = get_ritm_by_id(ritm_number)
    if ritm is None:
        return None
    tables = sql_check_service.referenced_tables(sql)
    context = schema_context_service.build_exact_schema_context(catalog_db, tables)
    checked = sql_check_service.check_sql(sql, context.columns)

    replaced = approved_ritm_service.save(
        ApprovedRitm(
            number=ritm.number,
            output_fields=ritm.variables.output_fields,
            report_criteria=ritm.variables.report_criteria,
            tables=checked.tables,
            sql=checked.sql,
        )
    )
    logger.info("sql approved %s", json.dumps({"ritm": ritm.number, "tables": checked.tables, "replaced": replaced}))
    return ApproveSqlResponse(
        ritm_number=ritm.number, sql=checked.sql, tables=checked.tables, warnings=checked.warnings, replaced=replaced
    )
