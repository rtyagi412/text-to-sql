from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.catalog_session import get_catalog_db
from app.db.session import get_db
from app.schemas.catalog import (
    IntrospectionResult,
    JoinPathRequest,
    JoinPathResult,
    SearchResponse,
    SyncResult,
)
from app.services.catalog_service import sync_catalog
from app.services.introspection_service import introspect_schema
from app.services.join_service import resolve_join_paths
from app.services.retrieval_service import search_tables

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/introspect", response_model=IntrospectionResult)
def get_introspection(db: Session = Depends(get_db)) -> IntrospectionResult:
    """Live dump of the source SQL Server schema — verification endpoint for Phase 1, no Postgres writes."""
    return introspect_schema(db)


@router.post("/sync", response_model=SyncResult)
def sync(
    source_db: Session = Depends(get_db),
    catalog_db: Session = Depends(get_catalog_db),
) -> SyncResult:
    """Re-introspects the source SQL Server schema, builds per-table docs, embeds them, and rebuilds the catalog."""
    return sync_catalog(source_db, catalog_db)


@router.get("/search", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1),
    top_k: int = Query(5, ge=1, le=50),
    catalog_db: Session = Depends(get_catalog_db),
) -> SearchResponse:
    """Hybrid keyword + semantic search over synced tables (Phase 2 must have run at least once)."""
    return SearchResponse(query=q, results=search_tables(catalog_db, q, top_k=top_k))


@router.post("/joins", response_model=JoinPathResult)
def joins(
    body: JoinPathRequest,
    catalog_db: Session = Depends(get_catalog_db),
) -> JoinPathResult:
    """Resolves FK join paths connecting the given "schema.table" names -- standalone endpoint for manual testing."""
    return resolve_join_paths(catalog_db, body.tables)
