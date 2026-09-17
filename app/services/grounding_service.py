import json
import re
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.catalog import SchemaColumn, SchemaTable
from app.schemas.catalog import GroundedColumn, GroundedEntity, GroundedField, GroundedFilter, GroundedSchemaMapping
from app.schemas.extraction import ExtractionResult, Status
from app.services.join_service import resolve_join_paths
from app.services.ollama_service import generate
from app.services.retrieval_service import search_tables

settings = get_settings()


@dataclass
class ColumnCandidate:
    table_id: int
    schema_name: str
    table_name: str
    column_name: str
    data_type: str
    description: str | None


@dataclass
class FieldTableVote:
    table_id: int
    schema_name: str
    table_name: str
    best_score: float
    hit_count: int


def _normalize(text: str) -> str:
    return text.replace("_", " ").replace("-", " ").strip().lower()


_STOPWORDS = {"state"}


def _mention_words(text: str) -> set[str]:
    """Splits on separators and camelCase boundaries (filter fields arrive as e.g.
    "PaymentStateTransition.event") so entity-name words can be matched against field text
    regardless of which casing convention the extraction step produced."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    spaced = re.sub(r"[._\-]", " ", spaced)
    return {w for w in spaced.lower().split() if w not in _STOPWORDS and len(w) > 2}


def _field_mentions_entity(field_text: str, entity_name: str) -> bool:
    return bool(_mention_words(entity_name) & _mention_words(field_text))


_BARE_ID_FIELD = re.compile(r"^[\w\s]+\bid$", re.IGNORECASE)


def _is_bare_id_field(field_text: str) -> bool:
    """A field whose text is only "<Entity> ID" (e.g. "Merchant ID" on a Settlement report) is
    almost always a foreign key already sitting on the primary table's own row, not a signal that
    the named entity's own table needs to be pulled in as a join target -- same convention already
    applied when deciding whether a bare ID field justifies extracting a whole separate entity.
    Excluding these from table-discovery voting matters because they otherwise vote confidently
    (and often top-ranked) for their named entity's table purely because that table's own name
    appears in the field text, independent of whether the report actually needs that table."""
    return bool(_BARE_ID_FIELD.match(field_text.strip()))


def _score_candidate(field_text: str, candidate: ColumnCandidate) -> float:
    norm_field = _normalize(field_text)
    name_score = fuzz.WRatio(norm_field, _normalize(candidate.column_name))
    desc_score = fuzz.WRatio(norm_field, _normalize(candidate.description)) if candidate.description else 0.0
    return max(name_score, desc_score)


def _fuzzy_score_pool(field_text: str, pool: list[ColumnCandidate]) -> list[tuple[float, ColumnCandidate]]:
    scored = [(_score_candidate(field_text, c), c) for c in pool]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def _column_pool_for_tables(catalog_db: Session, table_ids: list[int]) -> list[ColumnCandidate]:
    if not table_ids:
        return []
    rows = (
        catalog_db.query(SchemaColumn, SchemaTable)
        .join(SchemaTable, SchemaColumn.table_id == SchemaTable.id)
        .filter(SchemaColumn.table_id.in_(table_ids))
        .all()
    )
    return [
        ColumnCandidate(
            table_id=table.id,
            schema_name=table.schema_name,
            table_name=table.table_name,
            column_name=column.column_name,
            data_type=column.data_type,
            description=column.description,
        )
        for column, table in rows
    ]


def _discover_field_tables(
    catalog_db: Session, field_texts: list[str], top_k: int, min_score: float
) -> dict[int, FieldTableVote]:
    """Runs each requested field/filter's own text through the same hybrid table search used for
    entities, and aggregates per-table support across all fields. A single field's top hit can be
    coincidental (many tables share generic column names like "status" or "amount"), but when
    multiple independent fields from the same ticket agree on a table, that's corroborating
    evidence worth trusting even when no entity was ever extracted for it."""
    votes: dict[int, FieldTableVote] = {}
    for field_text in field_texts:
        for r in search_tables(catalog_db, field_text, top_k=top_k):
            if r.hybrid_score < min_score:
                continue
            existing = votes.get(r.table_id)
            if existing is None:
                votes[r.table_id] = FieldTableVote(r.table_id, r.schema_name, r.table_name, r.hybrid_score, 1)
            else:
                existing.best_score = max(existing.best_score, r.hybrid_score)
                existing.hit_count += 1
    return votes


def _to_grounded_column(candidate: ColumnCandidate, score: float, method: str) -> GroundedColumn:
    return GroundedColumn(
        schema_name=candidate.schema_name,
        table_name=candidate.table_name,
        column_name=candidate.column_name,
        data_type=candidate.data_type,
        description=candidate.description,
        confidence=round(score / 100, 3),
        method=method,
    )


def _llm_disambiguate(
    field_text: str, scored: list[tuple[float, ColumnCandidate]], model: str
) -> ColumnCandidate | None:
    options = scored[:5]
    lines = [
        "You are matching a business-requested report field to the correct database column.",
        f'Requested field: "{field_text}"',
        "Candidates:",
    ]
    for i, (score, candidate) in enumerate(options):
        desc = f" - {candidate.description}" if candidate.description else ""
        lines.append(
            f"{i}: {candidate.schema_name}.{candidate.table_name}.{candidate.column_name} "
            f"[{candidate.data_type}]{desc} (similarity={score:.0f})"
        )
    lines.append(f"{len(options)}: none of the above match")
    lines.append(
        "Pick the single best matching candidate index. Only pick a candidate if it represents "
        "the same business concept as the requested field -- an id/foreign-key column is NOT a "
        "match for a name/label field even if it references the right entity, and an unrelated "
        "column that merely sounds similar is NOT a match either. If nothing truly matches, you "
        f"MUST pick {len(options)} (none of the above) rather than guess."
    )
    prompt = "\n".join(lines)

    schema = {
        "type": "object",
        "properties": {
            "selected_index": {"type": "integer"},
            "reasoning": {"type": "string"},
        },
        "required": ["selected_index", "reasoning"],
    }

    try:
        response = generate(model, prompt, format=schema, think=False)
        raw = json.loads(response.response)
    except Exception:
        # LLM disambiguation is a best-effort tiebreaker; any failure here should fall back
        # to the fuzzy-match result rather than aborting the whole grounding request.
        return None

    idx = raw.get("selected_index")
    if not isinstance(idx, int) or idx < 0 or idx >= len(options):
        return None
    return options[idx][1]


def _try_pool(
    field_text: str,
    pool: list[ColumnCandidate],
    model: str,
    method: str,
    allow_llm: bool = True,
    min_accept_score: float | None = None,
) -> tuple[GroundedColumn, list[GroundedColumn]] | None:
    min_accept_score = settings.catalog_column_min_accept_score if min_accept_score is None else min_accept_score
    scored = _fuzzy_score_pool(field_text, pool)
    if not scored:
        return None

    alternates = [_to_grounded_column(c, s, method) for s, c in scored[1:4]]
    best_score, best_candidate = scored[0]
    # A pool spanning several tables (primary_pool/anchor_pool/fallback_pool, unlike a
    # single-table entity pool) can have two DIFFERENT tables' columns tie on pure text score --
    # e.g. settlement.processed_at and refund.processed_at both score 100 against "Processed At".
    # Auto-accepting scored[0] there just picks whichever table the DB happened to return first,
    # silently reporting a confident, resolved answer that may be for the wrong table entirely.
    tied_tables = {(c.schema_name, c.table_name) for s, c in scored if s == best_score}
    unambiguous = len(tied_tables) <= 1

    if best_score >= settings.catalog_column_auto_accept_score and unambiguous:
        return _to_grounded_column(best_candidate, best_score, method), alternates

    # Below auto-accept (or tied across tables at/above it), text similarity alone isn't
    # reliable -- let the LLM use business-language understanding, but only when the pool has
    # at least a plausible signal worth showing it, and only for entity-anchored pools: an
    # unanchored global-fallback pool gives the LLM no real corroboration to work with, so a
    # confident-looking wrong pick there is worse than surfacing the field as unresolved
    # (NEEDS_CLARIFICATION) for a human to check.
    if allow_llm and best_score >= settings.catalog_column_llm_trigger_score:
        chosen = _llm_disambiguate(field_text, scored, model)
        if chosen is not None:
            return _to_grounded_column(chosen, 80.0, "llm"), alternates

    if best_score >= min_accept_score and unambiguous:
        return _to_grounded_column(best_candidate, best_score, method), alternates

    return None


def _resolve_column(
    catalog_db: Session,
    field_text: str,
    entity_pools: list[tuple[str, list[ColumnCandidate]]],
    primary_pool: list[ColumnCandidate],
    anchor_pool: list[ColumnCandidate],
    model: str,
) -> tuple[GroundedColumn | None, list[GroundedColumn]]:
    """Tries progressively broader, less-trustworthy candidate pools, stopping at the first
    tier that produces a confident (or LLM-confirmed) match.

    Trying a single entity table first (rather than flattening all resolved entities' tables
    into one pool) matters because a RITM with multiple entities (e.g. Refund + Payment) will
    have identically-named columns on both (id, status) that tie on pure text similarity --
    when the field text names one entity specifically ("Refund Status"), that entity's own
    table must be tried alone first, or the tie is broken arbitrarily instead of correctly.
    """
    for entity_name, entity_pool in entity_pools:
        if _field_mentions_entity(field_text, entity_name):
            result = _try_pool(field_text, entity_pool, model, "fuzzy")
            if result is not None:
                return result
            break

    for pool, method in ((primary_pool, "fuzzy"), (anchor_pool, "fuzzy")):
        result = _try_pool(field_text, pool, model, method)
        if result is not None:
            return result

    fallback_tables = search_tables(catalog_db, field_text, top_k=settings.catalog_fallback_search_top_k)
    fallback_pool = _column_pool_for_tables(catalog_db, [t.table_id for t in fallback_tables])
    result = _try_pool(
        field_text,
        fallback_pool,
        model,
        "fallback_search",
        allow_llm=False,
        min_accept_score=settings.catalog_column_auto_accept_score,
    )
    if result is not None:
        return result

    return None, []


def ground_extraction(extraction: ExtractionResult, catalog_db: Session, model: str) -> GroundedSchemaMapping:
    tables_used: set[str] = set()

    grounded_entities: list[GroundedEntity] = []
    entity_pools: list[tuple[str, list[ColumnCandidate]]] = []
    primary_table_ids: set[int] = set()
    anchor_table_ids: set[int] = set()
    for entity in extraction.entities:
        results = search_tables(catalog_db, entity.name, top_k=settings.catalog_entity_top_k)
        candidates = [r for r in results if r.hybrid_score >= settings.catalog_entity_min_score] or results[:1]
        anchor_table_ids.update(c.table_id for c in candidates)
        if candidates:
            primary_table_ids.add(candidates[0].table_id)
            tables_used.add(f"{candidates[0].schema_name}.{candidates[0].table_name}")
            entity_pools.append((entity.name, _column_pool_for_tables(catalog_db, [candidates[0].table_id])))
        grounded_entities.append(
            GroundedEntity(
                requested_name=entity.name,
                evidence=entity.evidence,
                candidate_tables=candidates,
                resolved=bool(candidates),
            )
        )

    field_texts = [f.name for f in extraction.requested_fields if not _is_bare_id_field(f.name)] + [
        f.field for f in extraction.filters
    ]
    field_table_votes = _discover_field_tables(
        catalog_db, field_texts, settings.catalog_field_table_top_k, settings.catalog_field_table_min_score
    )
    anchor_table_ids.update(field_table_votes.keys())
    for table_id, vote in field_table_votes.items():
        # Two gates, not one: hit_count alone is fooled by generic fields ("Merchant ID",
        # "Processed At") that nearly every table matches a little after per-field min-max
        # normalization exaggerates small score gaps. Only promote a table into the trusted
        # primary/name-anchored tier when it's both repeatedly AND strongly matched.
        if (
            vote.hit_count >= settings.catalog_field_table_promote_min_hits
            and vote.best_score >= settings.catalog_field_table_promote_min_score
        ):
            primary_table_ids.add(table_id)
            # Give a strongly-corroborated field-discovered table the same name-anchored solo-pool
            # treatment an extracted entity gets (see _resolve_column's docstring): without this,
            # a field like "Refund ID" would tie against payment.id/refund.id once both tables
            # are flattened into one shared pool, and get resolved to whichever happens to sort
            # first rather than the table its own text actually names.
            entity_pools.append((vote.table_name, _column_pool_for_tables(catalog_db, [table_id])))

    primary_pool = _column_pool_for_tables(catalog_db, list(primary_table_ids))
    anchor_pool = _column_pool_for_tables(catalog_db, list(anchor_table_ids))

    grounded_fields: list[GroundedField] = []
    unresolved_fields: list[str] = []
    for field in extraction.requested_fields:
        column, alternates = _resolve_column(catalog_db, field.name, entity_pools, primary_pool, anchor_pool, model)
        if column is not None:
            tables_used.add(f"{column.schema_name}.{column.table_name}")
        else:
            unresolved_fields.append(field.name)
        grounded_fields.append(
            GroundedField(
                requested_name=field.name,
                evidence=field.evidence,
                column=column,
                alternates=alternates,
                resolved=column is not None,
            )
        )

    grounded_filters: list[GroundedFilter] = []
    unresolved_filters: list[str] = []
    for filt in extraction.filters:
        column, alternates = _resolve_column(catalog_db, filt.field, entity_pools, primary_pool, anchor_pool, model)
        if column is not None:
            tables_used.add(f"{column.schema_name}.{column.table_name}")
        else:
            unresolved_filters.append(filt.field)
        grounded_filters.append(
            GroundedFilter(
                requested_field=filt.field,
                operator=filt.operator,
                value=filt.value,
                evidence=filt.evidence,
                column=column,
                alternates=alternates,
                resolved=column is not None,
            )
        )

    join_paths = resolve_join_paths(catalog_db, sorted(tables_used))

    needs_clarification = (
        extraction.status is Status.NEEDS_CLARIFICATION
        or any(not e.resolved for e in grounded_entities)
        or bool(unresolved_fields)
        or bool(unresolved_filters)
        or bool(join_paths.unreachable_tables)
        or bool(join_paths.ambiguous_joins)
    )

    return GroundedSchemaMapping(
        ritm_number=extraction.ritm_number,
        entities=grounded_entities,
        fields=grounded_fields,
        filters=grounded_filters,
        tables_used=sorted(tables_used),
        unresolved_fields=unresolved_fields,
        unresolved_filters=unresolved_filters,
        join_paths=join_paths,
        status=Status.NEEDS_CLARIFICATION if needs_clarification else Status.READY,
    )
