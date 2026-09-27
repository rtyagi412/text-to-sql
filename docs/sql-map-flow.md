# How `POST /sql/map` works

`/sql/map` is step 1 of the flow (`extract → map → write → approve`). The requester has already confirmed the
`/sql/extract` result. `/sql/map` turns each confirmed **field** and **condition** into a real **catalog column**
(`schema.table` + `column`), with filter values written the way the column stores them. A field no column stores
(a count, a total, a "used in" label) becomes a **derived field**, see below. **No SQL is written.**

Code: `app/services/sql_generation/mapping.py` (the checks are in `mapping_validation.py`, shared helpers in `common.py`).

## Inputs and output

| | |
|---|---|
| In | `ColumnMappingRequest`: `ritm_number`, confirmed `output_fields` and `filters` (business words), `assumptions` |
| Uses | schema catalog (tables/columns/FKs/CHECK values), approved RITMs (`approved_ritms` table in the catalog database), the LLM |
| Out | `ColumnMappingResponse`: `output_fields` (each a column or a derived value)/`filters` mapped to columns, `additional_filters` and `considerations` learned from similar approved SQL, `clarifications`, `status`, `matched_ritms`, `audit` |

## The five steps

```mermaid
sequenceDiagram
    participant R as routes/sql.py
    participant M as mapping.py
    participant A as approved_ritm_service
    participant S as schema_context_service
    participant L as LLM (generate_checked)
    participant V as mapping_validation.py

    R->>M: map_columns(request)
    M->>M: look up the RITM (404 if unknown)
    M->>A: 1. similar approved RITMs (matched on the confirmed fields + conditions, own number excluded)
    M->>S: 2. schema slice: tables retrieved by relevance + tables of those examples, plus an index of ALL tables
    loop up to 2 rounds
        M->>L: 3. system = rules + table index; user = schema + examples + confirmed request
        L->>V: 4. validate the answer
        V-->>L: rejected? send it back ONCE with the reasons
        L-->>M: answer
        Note over M: answer.tables_needed? add those tables to the slice and ask again
    end
    M-->>R: 5. status + matched_ritms + audit added
```

1. **Find similar approved RITMs** (`mapping._find_similar_examples`). Approved SQL for tickets like this one is shown to
   the model as examples, so it can copy conventions and propose extra filters the ticket didn't state.
   Examples that mention a table no longer in the catalog are dropped.
2. **Build the schema slice** (`mapping._build_schema_slice`). Retrieval picks the tables most relevant to the fields and
   conditions, and `SCHEMA_NEIGHBOUR_RESERVE` of the places (5 of 20) are held for the foreign-key neighbours of the top
   hits, so the tables that reference a hit are not crowded out by weaker search hits. The model also gets a one-line index of every table so it knows what exists beyond the slice.
3. **Ask the model** (`mapping._ask_model`). One JSON answer per round: a `ColumnMappingAnswer`.
4. **Validate the answer** (`mapping._parse_answer` → `mapping_validation.validate_mapping`, run on every reply).
   Four independent checks, each returning a list of problems that are sent back to the model if any are found:

   | Check | Rejects |
   |---|---|
   | `_check_columns_exist` | a table or column that isn't in the slice, including the columns a derived field reads |
   | `_check_filter_values` | a filter value the column can't hold (type, or CHECK-constraint values) |
   | `_check_coverage` | a field or filter that was never requested, or dropped without a clarification |
   | `_check_citations` | an additional filter or consideration citing an example the model wasn't shown |
5. **Assemble the response** (end of `map_columns`). `status` is derived (`NEEDS_CLARIFICATION` if there are clarifications, else `READY`);
   `matched_ritms` shows what the mapping drew on; `audit` records prompt version, model, token usage, calls made.

## Derived fields

A `MappedField` is either a column (`table` + `column`) or a **derived value** (`table` and `column` null, `derived` set):

```json
{"requested": "Usage Count", "table": null, "column": null,
 "derived": {"definition": "rows referencing the code per place; one row per code and place; 0 when unused",
             "sources": [{"table": "core.Account", "column": "ClosureReasonCodeId"}, {"table": "pay.PaymentEvent", "column": "ReasonCodeId"}]}}
```

`sources` is what the pipeline acts on, prose in `assumptions` is not: `/write` puts every source table in the schema the
model sees and rejects SQL that does not read one. The join plan treats source tables as **branches**: each is attached to
the report's own tables on its own, never to another branch (chaining them multiplies rows), and the write prompt has the
model aggregate each branch in a derived table (`GROUP BY` the key, `UNION ALL` for one-row-per-place, `LEFT JOIN` +
`COALESCE` so unmatched rows still appear).

`/write` has the same one-time `tables_needed` escape as `/map` (it is given `<table_index>` too), so it never asks the
requester to supply schema the catalog already holds. Foreign keys with several columns are one edge that is joined on
every pair (`a.t(ColA, ColB) -> b.u(ColX, ColY)`), not several parallel paths.

## Three retry mechanisms (max 4 model answers)

| Loop | Where | What triggers it | Limit |
|---|---|---|---|
| Correction | `common.generate_checked` | Reply unusable (empty, cut off, not JSON) **or** rejected by step 4 | 1 retry, with the reasons sent back |
| More tables | `mapping._ask_model` | Answer has `tables_needed`: the slice lacked a table | 1 extra round; the tables are added and the model is asked again. A second request is refused |
| HTTP | `llm_service._post` | The API itself failed: timeout, connection error, 429 or 5xx (a 401/402/400 is not retried) | `LLM_MAX_RETRIES` more requests (default 2), pausing 1s, 2s, 4s... |

So the worst case is 2 rounds × 2 attempts = **4 model answers**. The `attempts` in the audit counts these. Each answer
can itself take up to `LLM_MAX_RETRIES + 1` HTTP requests (3 by default), so a badly failing run can make up to 12
requests. A failure on the last attempt surfaces as a 502 (`routes/sql.py::_http_errors`).

## Where things live

```
app/api/routes/sql.py                       HTTP layer, error → status mapping
app/services/sql_generation/
    __init__.py       the four public functions
    extract.py        step 0  /sql/extract
    mapping.py        step 1  /sql/map            <- this document
    mapping_validation.py                         step 4 above
    write.py          step 2  /sql/write
    join_plan.py                                  FK join paths shown to the write step
    approve.py        step 3  /sql/approve
    validate.py               /sql/validate       every SQL guardrail, reported (see sql-guardrails.md)
    common.py         prompt assembly, generate_checked (retry), audit, similar_examples
app/schemas/sql_generation/                 request/response/answer models, one module per step (+ common.py)
    mapping.py        ColumnMappingRequest → ColumnMappingAnswer (what the model returns) → ColumnMappingResponse
app/prompts/versions/mapping_v1.py          the mapping rules the model follows
```
