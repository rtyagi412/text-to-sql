# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service that turns a RITM (a ticket requesting a report) into a reviewed SQL query, and optionally publishes it as an SSRS report. The source database is SQL Server (via `pyodbc`); a separate Postgres + pgvector "catalog" database holds the schema catalog, embeddings and the approved-RITM pool. Learning project: only new-report RITMs are in scope (no modify/delete flows).

## Commands

Python 3.12, managed with `uv`.

```
uv sync                                   # install deps (incl. dev group)
docker compose up -d                      # catalog Postgres+pgvector on localhost:5435
cp .env.example .env                      # then fill DEEPSEEK_API_KEY, VOYAGE_API_KEY, DB_*, SSRS_*
uv run uvicorn app.main:app --reload      # API on :8000 (docs at /docs); VS Code launch config also exists
```

- There is no `tests/` directory and no linter configured. `pyproject.toml` points pytest at `tests/`, but the owner has asked not to write or restore tests unless explicitly requested.
- First-time setup: `POST /catalog/sync` introspects the source DB and populates the catalog + embeddings. Changing `EMBEDDING_MODEL`/`EMBEDDING_DIM` requires dropping the catalog tables and re-syncing.
- The app starts even if the catalog DB is down (`lifespan` in `app/main.py` only logs); routes needing it fail on their own.

## Architecture

Routes (`app/api/routes/`) → services (`app/services/`) → schemas (`app/schemas/`, pydantic). Settings come from `app/core/config.py` (env / `.env`). Two DB sessions: `app/db/session.py` (source SQL Server) and `app/db/catalog_session.py` (catalog Postgres).

### SQL generation pipeline (`/sql/*`)

`extract → map → write → approve`, each step a human-confirmed checkpoint. Code lives in `app/services/sql_generation/` with one module per step and matching request/answer/response models in `app/schemas/sql_generation/`. Prompts are versioned in `app/prompts/versions/` and selected by `*_PROMPT_VERSION` env vars through `app/prompts/registry.py`.

- `/sql/extract` – pulls output fields, filters and assumptions out of the RITM text.
- `/sql/map` – maps each confirmed field/filter to a catalog column (or a "derived" field with `sources`). Fully documented in `docs/sql-map-flow.md`; read it before touching `mapping.py` / `mapping_validation.py`.
- `/sql/write` – writes the SQL; `join_plan.py` computes FK join paths shown to the model; results are checked by `sql_check_service`, `filter_check_service`, and optionally compiled against the source DB (`WRITE_COMPILE_CHECK`, nothing executes).
- `/sql/approve` (explicit, never automatic) – stores the RITM+SQL in the `approved_ritms` table (catalog DB, `approved_ritm_service`). Approved SQL is retrieved by embedding similarity as few-shot examples for later `/map` and `/write` calls. `GET /sql/approve/{ritm}` reads one back.

Shared mechanics worth knowing:
- Every LLM call goes through `common.generate_checked`: JSON-mode reply, validated, and sent back **once** with the reasons if unusable or rejected. HTTP-level retries (timeouts/429/5xx) are separate, in `llm_service._post`. A reply may request `tables_needed` once, which extends the schema slice.
- The LLM is DeepSeek over REST in JSON mode (`llm_service`); embeddings are Voyage AI (`embedding_service`). No local models.
- Schema context is a *slice*: tables retrieved by relevance (`retrieval_service`) plus foreign-key neighbours, plus a one-line index of all tables (`schema_context_service`). Validators reject any table/column outside the slice.
- Composite foreign keys are one join edge on all column pairs. Tables matching `CATALOG_HUB_TABLES` (or referenced by many FKs) are excluded from join paths.

### Catalog (`/catalog/*`)

`introspection_service` reads the source DB's tables, columns, FKs and CHECK-constraint allowed values; `catalog_service` syncs them with embeddings into Postgres (`app/models/catalog.py`); `join_service` finds FK paths.

### Reports (`/report/*`)

`app/services/reporting/`: `rdl.py` builds an RDL from a single SELECT, `ssrs_client.py` publishes/subscribes through the SSRS REST API (NTLM or basic auth), `service.py` orchestrates. Flow and request shapes in `docs/report-flow.md`; UI contract in `docs/report-ui-spec.md`.

### Known gaps

- Prompts still mention a glossary feature that was removed.
- `/write` window-function grammar and OR-filters are not fully supported.
- DeepSeek output quality has not been measured against live runs.
- `ritm_samples.json` holds sample RITMs for manual testing.
