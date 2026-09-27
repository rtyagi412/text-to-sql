import math
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.catalog import ApprovedRitmRecord
from app.services.embedding_service import embed

settings = get_settings()

_EVAL_PREFIX = "RITM-EVAL-"


class ApprovedRitm(BaseModel):
    """A solved, reviewed ticket. Exactly one outcome: approved `sql`, or the `clarifications` that were
    the right answer (kept so the model sees where the line between "ask" and "assume" was drawn).
    The trailing fields are audit data from the approvals table; the prompts do not use them."""

    model_config = ConfigDict(extra="forbid")

    number: str
    name: str = ""
    output_fields: str
    report_criteria: str
    tables: list[str] = Field(default_factory=list, description='Tables the SQL uses, as "schema.table".')
    sql: str | None = None
    clarifications: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list, description="Performance warnings shown when it was approved.")
    approved_by: str | None = None
    note: str | None = None
    version: int = 1
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> "ApprovedRitm":
        if bool(self.sql) == bool(self.clarifications):
            raise ValueError(f"{self.number}: set exactly one of `sql` or `clarifications`")
        return self


@dataclass(frozen=True)
class SimilarRitm:
    ritm: ApprovedRitm
    similarity: float


@dataclass(frozen=True)
class SaveResult:
    replaced: bool
    version: int
    approved_at: datetime


# Document embeddings by (number, version): approving a RITM embeds only that RITM, and re-approving it (which
# bumps its version) embeds it again. Entries for rows no longer in the table are dropped on the next lookup.
_embeddings: dict[tuple[str, int], list[float]] = {}


def similarity_text(output_fields: str, report_criteria: str) -> str:
    return f"{output_fields} | {report_criteria}"


def _to_ritm(row: ApprovedRitmRecord) -> ApprovedRitm:
    return ApprovedRitm(
        number=row.ritm_number,
        name=row.ritm_name,
        output_fields=row.output_fields,
        report_criteria=row.report_criteria,
        tables=row.tables,
        sql=row.sql,
        clarifications=row.clarifications,
        warnings=row.warnings,
        approved_by=row.approved_by,
        note=row.note,
        version=row.version,
        approved_at=row.approved_at,
    )


def _load(catalog_db: Session) -> tuple[list[ApprovedRitm], list[list[float]]]:
    global _embeddings
    # Eval tickets never enter the pool: reference solutions must not leak into the test set.
    rows = catalog_db.scalars(
        select(ApprovedRitmRecord)
        .where(~ApprovedRitmRecord.ritm_number.startswith(_EVAL_PREFIX))
        .order_by(ApprovedRitmRecord.id)
    ).all()
    records = [_to_ritm(r) for r in rows]
    keys = [(r.number, r.version) for r in records]

    missing = [(k, r) for k, r in zip(keys, records, strict=True) if k not in _embeddings]
    if missing:
        vectors = embed(
            [similarity_text(r.output_fields, r.report_criteria) for _, r in missing],
            "document",
            settings.approved_embedding_model,
        )
        kept = {k: v for k, v in _embeddings.items() if k in keys}
        _embeddings = {**kept, **{k: v for (k, _), v in zip(missing, vectors, strict=True)}}
    return records, [_embeddings[k] for k in keys]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def find_similar(catalog_db: Session, output_fields: str, report_criteria: str, exclude_number: str) -> list[SimilarRitm]:
    """The approved RITMs most similar to this ticket. The ticket itself is excluded, so re-running an
    already-approved RITM can't just hand its own answer back."""
    records, embeddings = _load(catalog_db)
    if not records:
        return []

    query = embed([similarity_text(output_fields, report_criteria)], "query", settings.approved_embedding_model)[0]
    scored = [
        SimilarRitm(ritm=record, similarity=_cosine(query, vector))
        for record, vector in zip(records, embeddings, strict=True)
        if record.number != exclude_number
    ]
    scored = [s for s in scored if s.similarity >= settings.approved_example_min_similarity]
    scored.sort(key=lambda s: s.similarity, reverse=True)
    return scored[: settings.approved_examples_k]


def get_approved(catalog_db: Session, number: str) -> ApprovedRitm | None:
    """The approved record for one RITM (no embeddings, so no network call)."""
    row = catalog_db.scalar(select(ApprovedRitmRecord).where(ApprovedRitmRecord.ritm_number == number))
    return _to_ritm(row) if row else None


def save(catalog_db: Session, record: ApprovedRitm) -> SaveResult:
    """Adds `record` to the approved pool, replacing any record with the same number (its version goes up by one
    and `approved_at` moves to now). One INSERT ... ON CONFLICT statement, so two approvals of the same RITM at once
    can't both insert. The new row is picked up by the next lookup."""
    values = {
        "ritm_name": record.name,
        "output_fields": record.output_fields,
        "report_criteria": record.report_criteria,
        "sql": record.sql,
        "tables": record.tables,
        "clarifications": record.clarifications,
        "warnings": record.warnings,
        "approved_by": record.approved_by,
        "note": record.note,
    }
    statement = (
        pg_insert(ApprovedRitmRecord)
        .values(ritm_number=record.number, **values)
        .on_conflict_do_update(
            index_elements=[ApprovedRitmRecord.ritm_number],
            set_={**values, "version": ApprovedRitmRecord.version + 1, "approved_at": func.now()},
        )
        # xmax is 0 on a row this statement inserted and non-zero on one it updated.
        .returning(
            literal_column("(xmax = 0)").label("inserted"), ApprovedRitmRecord.version, ApprovedRitmRecord.approved_at
        )
    )
    row = catalog_db.execute(statement).one()
    catalog_db.commit()
    return SaveResult(replaced=not row.inserted, version=row.version, approved_at=row.approved_at)
