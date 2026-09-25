import json
import math
import os
import threading
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import get_settings
from app.services.embedding_service import embed

settings = get_settings()

_EVAL_PREFIX = "RITM-EVAL-"

_write_lock = threading.Lock()


class ApprovedRitm(BaseModel):
    """A solved, reviewed ticket. Exactly one outcome: approved `sql`, or the `clarifications` that were
    the right answer (kept so the model sees where the line between "ask" and "assume" was drawn)."""

    model_config = ConfigDict(extra="forbid")

    number: str
    output_fields: str
    report_criteria: str
    tables: list[str] = Field(default_factory=list, description='Tables the SQL uses, as "schema.table".')
    sql: str | None = None
    clarifications: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> "ApprovedRitm":
        if bool(self.sql) == bool(self.clarifications):
            raise ValueError(f"{self.number}: set exactly one of `sql` or `clarifications`")
        return self


@dataclass(frozen=True)
class SimilarRitm:
    ritm: ApprovedRitm
    similarity: float


# (file mtime, records, embeddings) -- reloaded when the file changes, so newly approved RITMs are
# picked up without a restart.
_cache: tuple[int, list[ApprovedRitm], list[list[float]]] | None = None


def similarity_text(output_fields: str, report_criteria: str) -> str:
    return f"{output_fields} | {report_criteria}"


def _load() -> tuple[list[ApprovedRitm], list[list[float]]]:
    global _cache
    path = settings.approved_ritms_path
    if not path.exists():
        return [], []
    mtime = path.stat().st_mtime_ns
    if _cache is not None and _cache[0] == mtime:
        return _cache[1], _cache[2]

    with path.open(encoding="utf-8") as f:
        # Eval tickets never enter the pool: reference solutions must not leak into the test set.
        records = [r for r in map(ApprovedRitm.model_validate, json.load(f)) if not r.number.startswith(_EVAL_PREFIX)]
    embeddings = embed(
        [similarity_text(r.output_fields, r.report_criteria) for r in records],
        "document",
        settings.approved_embedding_model,
    )
    _cache = (mtime, records, embeddings)
    return records, embeddings


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def find_similar(output_fields: str, report_criteria: str, exclude_number: str) -> list[SimilarRitm]:
    """The approved RITMs most similar to this ticket. The ticket itself is excluded, so re-running an
    already-approved RITM can't just hand its own answer back."""
    records, embeddings = _load()
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


def save(record: ApprovedRitm) -> bool:
    """Adds `record` to the approved pool, replacing any record with the same number. Returns True when it
    replaced one. The file is rewritten atomically, so a crash mid-write can't leave retrieval a half-written
    pool; the change is picked up on the next lookup, which notices the new modification time."""
    path = settings.approved_ritms_path
    with _write_lock:
        existing: list[ApprovedRitm] = []
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    existing = [ApprovedRitm.model_validate(r) for r in json.load(f)]
            except ValueError as exc:
                # Refuse rather than overwrite a file that was hand-edited into something unreadable.
                raise RuntimeError(f"{path} is not a valid approved-RITM file, so nothing was saved: {exc}") from exc
        replaced = any(r.number == record.number for r in existing)
        records = [record if r.number == record.number else r for r in existing]
        if not replaced:
            records.append(record)

        payload = json.dumps([r.model_dump(exclude_defaults=True) for r in records], indent=2, ensure_ascii=False)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(payload + "\n", encoding="utf-8")
        os.replace(temp, path)
    return replaced
