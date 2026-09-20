from collections.abc import Iterator
from contextlib import contextmanager

import anthropic
import httpx2
from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.catalog_session import get_catalog_db
from app.schemas.sql_generation import (
    ApproveSqlRequest,
    ApproveSqlResponse,
    ColumnMappingRequest,
    ColumnMappingResponse,
    ExtractionRequest,
    RitmExtractionResponse,
    SqlWriteRequest,
    SqlWriteResponse,
)
from app.services.claude_service import ClaudeOutputError, ClaudeRefusalError
from app.services.embedding_service import EmbeddingConfigError
from app.services.schema_context_service import CatalogEmptyError
from app.services.sql_generation_service import (
    approve_sql,
    extract_requirements,
    map_columns,
    write_sql,
)

router = APIRouter(prefix="/sql", tags=["sql"])


@contextmanager
def _http_errors() -> Iterator[None]:
    # ValueError comes last: pydantic's ValidationError is a ValueError, and a malformed Claude response
    # is a 502, not the caller's 400.
    try:
        yield
    except CatalogEmptyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ClaudeRefusalError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ClaudeOutputError, ValidationError) as exc:
        raise HTTPException(status_code=502, detail=f"Claude returned an unusable response: {exc}") from exc
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API error: {exc}") from exc
    except EmbeddingConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx2.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Embedding service (Voyage AI) unavailable: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/extract", response_model=RitmExtractionResponse)
def extract(body: ExtractionRequest) -> RitmExtractionResponse:
    """Step 0, shown to the requester first: reads only the RITM and has Claude state the fields the report
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
) -> SqlWriteResponse:
    """Step 2, after the requester confirms the /sql/map result (post that response back, with any proposed
    additional filter they rejected deleted): writes one T-SQL SELECT, validated against the catalog, formatted,
    and checked for performance smells (returned as warnings). Nothing is saved: call /sql/approve once the SQL
    has been reviewed."""
    with _http_errors():
        result = write_sql(body, catalog_db)
    if result is None:
        raise HTTPException(status_code=404, detail=f"RITM '{body.ritm_number}' not found")
    return result


@router.post("/approve", response_model=ApproveSqlResponse)
def approve(
    body: ApproveSqlRequest,
    catalog_db: Session = Depends(get_catalog_db),
) -> ApproveSqlResponse:
    """Stores reviewed SQL in approved_ritms.json against the RITM (overwriting any earlier entry), so later
    requests can draw on it. The SQL is validated and formatted first; SQL that is not a single plain SELECT
    over tables and columns in the catalog is refused."""
    with _http_errors():
        result = approve_sql(body.ritm_number, body.sql, catalog_db)
    if result is None:
        raise HTTPException(status_code=404, detail=f"RITM '{body.ritm_number}' not found")
    return result
