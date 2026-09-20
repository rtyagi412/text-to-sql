from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.schemas.catalog import TableSearchResult
from app.services.embedding_service import embed

settings = get_settings()

_SCORE_QUERY = """
SELECT
    id,
    schema_name,
    table_name,
    description,
    ts_rank(tsv, plainto_tsquery('english', :query)) AS keyword_score,
    1 - (embedding <=> CAST(:query_embedding AS vector)) AS semantic_score
FROM schema_tables
"""


def _to_vector_literal(values: list[float]) -> str:
    return "[" + ",".join(map(str, values)) + "]"


def _normalize(scores: list[float]) -> list[float]:
    """Min-max normalize to [0, 1] so keyword (unbounded ts_rank) and semantic (~[-1, 1]) scores combine fairly."""
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-12:
        return [0.0 for _ in scores]
    return [(s - lo) / (hi - lo) for s in scores]


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and len(word) > 1 else word


def _exact_table_name_index(query: str, results: list[TableSearchResult]) -> int | None:
    """Finds a table whose name exactly matches the query (singular/plural-insensitive).

    A one-doc-per-table embedding dilutes a wide table's own name relative to a small,
    unrelated table that happens to mention that word a lot (e.g. entity "Payment" can
    lose to junction table "settlement_payment" on both keyword and semantic score, since
    "payment" is a much larger fraction of that smaller table's total text). An entity name
    is very often just the target table's literal name, so an exact-name match should win
    outright rather than compete on those diluted scores.
    """
    if len(query.split()) > 3:
        return None
    normalized_query = " ".join(_singular(w) for w in query.strip().lower().split())
    for i, result in enumerate(results):
        normalized_name = " ".join(_singular(w) for w in result.table_name.replace("_", " ").split())
        if normalized_name == normalized_query:
            return i
    return None


def search_tables(
    catalog_db: Session,
    query: str,
    top_k: int | None = None,
    keyword_weight: float | None = None,
    semantic_weight: float | None = None,
) -> list[TableSearchResult]:
    top_k = settings.retrieval_top_k if top_k is None else top_k
    keyword_weight = settings.retrieval_keyword_weight if keyword_weight is None else keyword_weight
    semantic_weight = settings.retrieval_semantic_weight if semantic_weight is None else semantic_weight

    query_embedding = embed([query], "query")[0]

    rows = (
        catalog_db.execute(
            text(_SCORE_QUERY),
            {"query": query, "query_embedding": _to_vector_literal(query_embedding)},
        )
        .mappings()
        .all()
    )
    if not rows:
        return []

    keyword_scores = _normalize([row["keyword_score"] for row in rows])
    semantic_scores = _normalize([row["semantic_score"] for row in rows])

    results = [
        TableSearchResult(
            table_id=row["id"],
            schema_name=row["schema_name"],
            table_name=row["table_name"],
            description=row["description"],
            keyword_score=kw,
            semantic_score=sem,
            hybrid_score=keyword_weight * kw + semantic_weight * sem,
        )
        for row, kw, sem in zip(rows, keyword_scores, semantic_scores, strict=True)
    ]
    results.sort(key=lambda r: r.hybrid_score, reverse=True)

    exact_idx = _exact_table_name_index(query, results)
    if exact_idx is not None and exact_idx != 0:
        results.insert(0, results.pop(exact_idx))

    return results[:top_k]
