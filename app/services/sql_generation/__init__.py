"""The four-step SQL generation flow behind /sql/*. Each step is one module and one public function; the
requester confirms the previous step's response before the next runs (see docs/sql-map-flow.md).

    extract.py   /sql/extract   ticket text                  -> the fields and conditions in business terms
    mapping.py   /sql/map       confirmed extraction         -> a real catalog column for each (checked by mapping_validation.py)
    write.py     /sql/write     confirmed mapping            -> one validated T-SQL SELECT (join_plan.py supplies the join keys)
    approve.py   /sql/approve   reviewed SQL                 -> saved in the approved_ritms table, later shown as an example
    validate.py  /sql/validate  any SQL                      -> every guardrail's verdict, without saving anything

SQL guardrails (static checks, then compile, estimated plan and a capped sample run on the source database)
run in write, approve and validate: see sql_check_service and sql_guardrail_service.

common.py holds what the steps share: prompt assembly, the retry loop around the model call, and audit."""

from app.services.sql_generation.approve import approve_sql, get_approval
from app.services.sql_generation.extract import extract_requirements
from app.services.sql_generation.mapping import map_columns
from app.services.sql_generation.validate import validate_sql
from app.services.sql_generation.write import write_sql

__all__ = ["approve_sql", "extract_requirements", "get_approval", "map_columns", "validate_sql", "write_sql"]
