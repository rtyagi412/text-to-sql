from collections.abc import Iterator
from contextlib import contextmanager

import httpx2
from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.catalog_session import get_catalog_db
from app.db.session import get_db
from app.schemas.sql_generation import (
    ApprovedRitmResponse,
    ApproveSqlRequest,
    ApproveSqlResponse,
    ColumnMappingRequest,
    ColumnMappingResponse,
    ExtractionRequest,
    RitmExtractionResponse,
    SqlValidateRequest,
    SqlValidateResponse,
    SqlWriteRequest,
    SqlWriteResponse,
)
from app.services.llm_service import LlmApiError, LlmConfigError, LlmOutputError, LlmRefusalError
from app.services.embedding_service import EmbeddingConfigError
from app.services.schema_context_service import CatalogEmptyError
from app.services.sql_generation import (
    approve_sql,
    extract_requirements,
    get_approval,
    map_columns,
    validate_sql,
    write_sql,
)

router = APIRouter(prefix="/sql", tags=["sql"])


@contextmanager
def _http_errors() -> Iterator[None]:
    # ValueError comes last: pydantic's ValidationError is a ValueError, and a malformed model response
    # is a 502, not the caller's 400.
    try:
        yield
    except CatalogEmptyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LlmRefusalError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LlmConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (LlmOutputError, ValidationError) as exc:
        raise HTTPException(status_code=502, detail=f"The model returned an unusable response: {exc}") from exc
    except LlmApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except EmbeddingConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx2.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Embedding service (Voyage AI) unavailable: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/extract", response_model=RitmExtractionResponse)
def extract(body: ExtractionRequest) -> RitmExtractionResponse:
    """Step 0, shown to the requester first: reads only the RITM and has the model state the fields the report
    displays and the conditions it filters on, in the ticket's own business terms. No catalog, embeddings or
    approved examples are used and no column names are guessed; those come after the requester confirms."""
    with _http_errors():
        result = extract_requirements(body.ritm_number, body.user_input, body.model, body.prompt_version)
    if result is None:
        raise HTTPException(status_code=404, detail=f"RITM '{body.ritm_number}' not found")
    return result


@router.post("/map", response_model=ColumnMappingResponse)
def map_to_columns(
    body: ColumnMappingRequest,
    catalog_db: Session = Depends(get_catalog_db),
) -> ColumnMappingResponse:
    """Step 1, after the requester confirms the /sql/extract result (post that response back, edited if they
    changed anything): maps each field and condition to a real column, with values in stored form. Also reads
    the SQL of similar approved RITMs for filters and considerations the ticket doesn't state, each cited to the
    RITM it came from, for the requester to review. No SQL is written."""
    with _http_errors():
        result = map_columns(body, catalog_db)
    if result is None:
        raise HTTPException(status_code=404, detail=f"RITM '{body.ritm_number}' not found")
    return result


@router.post("/write", response_model=SqlWriteResponse)
def write(
    body: SqlWriteRequest,
    catalog_db: Session = Depends(get_catalog_db),
    source_db: Session = Depends(get_db),
) -> SqlWriteResponse:
    """Step 2, after the requester confirms the /sql/map result (post that response back, with any proposed
    additional filter they rejected deleted): writes one T-SQL SELECT, validated against the catalog, formatted,
    then put through the source-database guardrails: compiled, its estimated plan read for performance problems,
    and run capped at a few rows (then rolled back) to prove it executes. A failure is sent back to the model to
    correct; performance findings are returned as warnings. Nothing is saved: call /sql/approve once the SQL has
    been reviewed."""
    with _http_errors():
        result = write_sql(body, catalog_db, source_db)
    if result is None:
        raise HTTPException(status_code=404, detail=f"RITM '{body.ritm_number}' not found")
    return result


@router.post("/approve", response_model=ApproveSqlResponse)
def approve(
    body: ApproveSqlRequest,
    catalog_db: Session = Depends(get_catalog_db),
    source_db: Session = Depends(get_db),
) -> ApproveSqlResponse:
    """Saves reviewed SQL in the approved_ritms table against the RITM (overwriting any earlier entry, and
    bumping its version), so later requests can draw on it. The SQL is validated and formatted first; SQL that is
    not a single plain SELECT over tables and columns in the catalog is refused, and so is SQL that SQL Server
    cannot compile or fails to run on a capped sample (400)."""
    with _http_errors():
        result = approve_sql(body, catalog_db, source_db)
    if result is None:
        raise HTTPException(status_code=404, detail=f"RITM '{body.ritm_number}' not found")
    return result


@router.post("/validate", response_model=SqlValidateResponse)
def validate(
    body: SqlValidateRequest,
    catalog_db: Session = Depends(get_catalog_db),
    source_db: Session = Depends(get_db),
) -> SqlValidateResponse:
    """Runs every SQL guardrail on any SELECT and reports the result without saving anything: the static rules (one
    plain read-only SELECT over catalog tables and columns) and performance checks, then SQL Server's compile check,
    estimated plan and a run capped at a few rows (rolled back). `valid` is false with `errors` when a check fails;
    use it on hand-edited SQL before /sql/approve."""
    return validate_sql(body, catalog_db, source_db)


@router.get("/approve/{ritm_number}", response_model=ApprovedRitmResponse)
def approval(ritm_number: str, catalog_db: Session = Depends(get_catalog_db)) -> ApprovedRitmResponse:
    """The stored approval for a RITM: 404 when it has not been approved. The UI uses it to show that approving
    again will overwrite, and to display who approved what, when."""
    result = get_approval(ritm_number, catalog_db)
    if result is None:
        raise HTTPException(status_code=404, detail=f"RITM '{ritm_number}' has no approved SQL")
    return result
