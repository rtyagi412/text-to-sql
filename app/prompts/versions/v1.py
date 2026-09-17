import json

from app.prompts.models import FewShotExample, PromptVersion

_SYSTEM_PROMPT = """You are a data analyst assistant for a text-to-SQL system.
Given a database report request (RITM) below, extract a single JSON object with:
- ritm_number: copy the RITM number exactly as given in the input.
- entities: normalized business entities involved (e.g. "Customer", "Account"), not table names.
  Each entity needs a "name" and the exact "evidence" text from the RITM that supports it.
  Add an entity only when either is true:
    1. The report's population, or a filter/condition in report_criteria, is defined in terms of
       that entity's own state (e.g. "excluding test-mode merchants" -> Merchant), even if no
       field of that entity is ever displayed.
    2. output_fields requests a real descriptive attribute of that entity (a name, status, amount,
       date, etc.), not just an identifier.
  Do NOT add an entity for a bare "<Entity> ID" field with no other criteria/filter tied to it —
  by relational convention that's a foreign key already sitting on the primary record's own row,
  not evidence of a separate entity.
- requested_fields: normalized business fields/columns requested, not database column names.
  Each field needs a "name" and supporting "evidence" text.
- filters: conditions to apply, each with a "field", an "operator", supporting "evidence", and a
  "value". Format "field" as "EntityName.attribute_name" even when only one entity is involved.
  There is no separate time-range concept — any reporting window (absolute or relative) is just
  another filter on a date/timestamp attribute:
    - An absolute window with literal dates ("created between 2026-01-01 and 2026-03-31") becomes
      one BETWEEN filter with value [start, end], or two GREATER_THAN_OR_EQUAL/LESS_THAN_OR_EQUAL
      filters — using the literal dates exactly as written.
    - A relative window ("within the last 30 days", "in the last 12 months") becomes a
      GREATER_THAN_OR_EQUAL filter whose value is "-N UNIT" (UNIT is one of MINUTES, HOURS, DAYS,
      WEEKS, MONTHS, YEARS) — e.g. "last 30 days" -> value "-30 DAYS". Never invent an absolute
      date for language that is only relative.
  Choose the operator that matches the condition's actual comparison, not just the closest-sounding
  one — do not default to EQUALS/IN when a more precise operator applies:
    - "greater/more than X" -> GREATER_THAN; "at least X" -> GREATER_THAN_OR_EQUAL
    - "less/fewer than X" -> LESS_THAN; "at most X" -> LESS_THAN_OR_EQUAL
    - "between X and Y" -> BETWEEN, with value [X, Y]
    - "not X" / "excluding X" (single value) -> NOT_EQUALS; (multiple values) -> NOT_IN
    - "has/have not been verified/replayed/etc." (a missing timestamp or reference) -> IS_NULL;
      the positive case (it has happened) -> IS_NOT_NULL
    - "contains/includes X" -> CONTAINS; "starts with X" -> STARTS_WITH; "ends with X" -> ENDS_WITH
  Write "evidence" first, then read back over the evidence text you just wrote to pull "value" out
  of it — value must be the concrete literal it contains: a number, date, string, or boolean, never
  null. "an amount greater than 5000" -> value 5000; "the payment method used was Card" -> value
  "CARD". value is null ONLY when the operator is IS_NULL or IS_NOT_NULL — every other operator must
  carry a non-null value derived from the evidence you already wrote for this same filter.
- ambiguities: empty list when nothing is missing or unclear. Otherwise one entry per issue,
  each with a "category", a "description", and a "clarification_question" to ask the requester.
- status: READY when ambiguities is empty, NEEDS_CLARIFICATION when ambiguities is non-empty.

When identifying entities, do not reason about the ticket as a whole and stop once you've named
the obvious one or two — walk output_fields item by item and report_criteria clause by clause, and
apply the entity-inclusion test above to each one individually. Tickets that touch several entities
(a chain like Payment Transition -> Order -> Merchant) require checking every field and clause this
way, not just the ones that stand out first.

Use the examples below as a guide for style and granularity. If a request has no data to extract
for a given list, return an empty list rather than guessing. Never invent evidence text that does
not appear in the RITM.

Respond with JSON only, matching the required schema exactly."""

_EXAMPLES = [
    FewShotExample(
        input=json.dumps(
            {
                "ritm_number": "RITM0030001",
                "output_fields": "Merchant ID, Merchant Name, Business Type, Status, Onboarded Date",
                "report_criteria": "Merchants currently in an active status.",
                "summary": "Sales Ops wants a weekly directory of currently active merchants for account coverage planning.",
            },
            indent=2,
        ),
        output=json.dumps(
            {
                "ritm_number": "RITM0030001",
                "entities": [
                    {"name": "Merchant", "evidence": "Merchants currently in an active status."},
                ],
                "requested_fields": [
                    {"name": "Merchant ID", "evidence": "Merchant ID"},
                    {"name": "Merchant Name", "evidence": "Merchant Name"},
                    {"name": "Business Type", "evidence": "Business Type"},
                    {"name": "Status", "evidence": "Status"},
                    {"name": "Onboarded Date", "evidence": "Onboarded Date"},
                ],
                "filters": [
                    {
                        "field": "Merchant.status",
                        "operator": "EQUALS",
                        "evidence": "Merchants currently in an active status.",
                        "value": "ACTIVE",
                    },
                ],
                "ambiguities": [],
                "status": "READY",
            },
            indent=2,
        ),
    ),
    FewShotExample(
        input=json.dumps(
            {
                "ritm_number": "RITM0030004",
                "output_fields": "Order ID, Merchant ID, Amount, Currency, Order Status, Created At",
                "report_criteria": "Orders with a status of Paid or Cancelled, created between 2026-01-01 and 2026-03-31.",
                "summary": "Finance wants a one-time Excel export of paid and cancelled orders from Q1 2026 for quarter-close reconciliation.",
            },
            indent=2,
        ),
        output=json.dumps(
            {
                "ritm_number": "RITM0030004",
                "entities": [
                    {"name": "Order", "evidence": "Orders with a status of Paid or Cancelled"},
                ],
                "requested_fields": [
                    {"name": "Order ID", "evidence": "Order ID"},
                    {"name": "Merchant ID", "evidence": "Merchant ID"},
                    {"name": "Amount", "evidence": "Amount"},
                    {"name": "Currency", "evidence": "Currency"},
                    {"name": "Order Status", "evidence": "Order Status"},
                    {"name": "Created At", "evidence": "Created At"},
                ],
                "filters": [
                    {
                        "field": "Order.order_status",
                        "operator": "IN",
                        "evidence": "Orders with a status of Paid or Cancelled",
                        "value": ["PAID", "CANCELLED"],
                    },
                    {
                        "field": "Order.created_at",
                        "operator": "BETWEEN",
                        "evidence": "created between 2026-01-01 and 2026-03-31",
                        "value": ["2026-01-01", "2026-03-31"],
                    },
                ],
                "ambiguities": [],
                "status": "READY",
            },
            indent=2,
        ),
    ),
    FewShotExample(
        input=json.dumps(
            {
                "ritm_number": "RITM0030008",
                "output_fields": "DLQ Event ID, Merchant ID, Webhook Event ID, Event Type, Final Error, Moved At",
                "report_criteria": "Dead-lettered webhook events that have not been replayed, moved to the DLQ within the last 30 days.",
                "summary": "Platform Engineering wants a daily SSRS report of unreplayed dead-lettered webhook events from the last 30 days to prioritize manual replay triage.",
            },
            indent=2,
        ),
        output=json.dumps(
            {
                "ritm_number": "RITM0030008",
                "entities": [
                    {"name": "DLQ Event", "evidence": "Dead-lettered webhook events that have not been replayed"},
                    {"name": "Webhook Event", "evidence": "webhook events that have not been replayed"},
                ],
                "requested_fields": [
                    {"name": "DLQ Event ID", "evidence": "DLQ Event ID"},
                    {"name": "Merchant ID", "evidence": "Merchant ID"},
                    {"name": "Webhook Event ID", "evidence": "Webhook Event ID"},
                    {"name": "Event Type", "evidence": "Event Type"},
                    {"name": "Final Error", "evidence": "Final Error"},
                    {"name": "Moved At", "evidence": "Moved At"},
                ],
                "filters": [
                    {
                        "field": "DlqEvent.replayed_at",
                        "operator": "IS_NULL",
                        "evidence": "Dead-lettered webhook events that have not been replayed",
                        "value": None,
                    },
                    {
                        "field": "WebhookEvent.status",
                        "operator": "EQUALS",
                        "evidence": "Dead-lettered webhook events",
                        "value": "DEAD",
                    },
                    {
                        "field": "DlqEvent.moved_at",
                        "operator": "GREATER_THAN_OR_EQUAL",
                        "evidence": "moved to the DLQ within the last 30 days",
                        "value": "-30 DAYS",
                    },
                ],
                "ambiguities": [],
                "status": "READY",
            },
            indent=2,
        ),
    ),
    FewShotExample(
        input=json.dumps(
            {
                "ritm_number": "RITM0030012",
                "output_fields": "",
                "report_criteria": "Dashboard users belonging to merchants in an active status, with a role of Owner or Admin.",
                "summary": "Merchant Success wants a monthly Excel export of owner/admin dashboard users at active merchants, but hasn't said which fields to include yet.",
            },
            indent=2,
        ),
        output=json.dumps(
            {
                "ritm_number": "RITM0030012",
                "entities": [
                    {"name": "App User", "evidence": "Dashboard users belonging to merchants"},
                    {"name": "Merchant", "evidence": "merchants in an active status"},
                ],
                "requested_fields": [],
                "filters": [
                    {
                        "field": "Merchant.status",
                        "operator": "EQUALS",
                        "evidence": "merchants in an active status",
                        "value": "ACTIVE",
                    },
                    {
                        "field": "AppUser.role",
                        "operator": "IN",
                        "evidence": "with a role of Owner or Admin",
                        "value": ["OWNER", "ADMIN"],
                    },
                ],
                "ambiguities": [
                    {
                        "category": "MISSING_OUTPUT_FIELDS",
                        "description": "The RITM does not state which fields/columns the report should include (output_fields is blank).",
                        "clarification_question": "Which fields/columns should this report include?",
                    },
                ],
                "status": "NEEDS_CLARIFICATION",
            },
            indent=2,
        ),
    ),
    FewShotExample(
        input=json.dumps(
            {
                "ritm_number": "RITM0030009",
                "output_fields": "Transition ID, Payment ID, From Status, To Status, Event, Occurred At, Order Receipt, Merchant Name",
                "report_criteria": "Payment state transitions with an event of Capture Fail, for orders with an amount greater than 2000, occurring in the last 90 days.",
                "summary": "Risk team wants a weekly SSRS audit trail of capture failures on high-value orders, including order receipt and merchant identity, to investigate processor-side capture issues.",
            },
            indent=2,
        ),
        output=json.dumps(
            {
                "ritm_number": "RITM0030009",
                "entities": [
                    {
                        "name": "Payment Transition",
                        "evidence": "Payment state transitions with an event of Capture Fail",
                    },
                    {"name": "Order", "evidence": "for orders with an amount greater than 2000"},
                    {"name": "Merchant", "evidence": "Merchant Name"},
                ],
                "requested_fields": [
                    {"name": "Transition ID", "evidence": "Transition ID"},
                    {"name": "Payment ID", "evidence": "Payment ID"},
                    {"name": "From Status", "evidence": "From Status"},
                    {"name": "To Status", "evidence": "To Status"},
                    {"name": "Event", "evidence": "Event"},
                    {"name": "Occurred At", "evidence": "Occurred At"},
                    {"name": "Order Receipt", "evidence": "Order Receipt"},
                    {"name": "Merchant Name", "evidence": "Merchant Name"},
                ],
                "filters": [
                    {
                        "field": "PaymentTransition.event",
                        "operator": "EQUALS",
                        "evidence": "an event of Capture Fail",
                        "value": "CAPTURE_FAIL",
                    },
                    {
                        "field": "Order.amount_units",
                        "operator": "GREATER_THAN",
                        "evidence": "orders with an amount greater than 2000",
                        "value": 2000,
                    },
                    {
                        "field": "PaymentTransition.occurred_at",
                        "operator": "GREATER_THAN_OR_EQUAL",
                        "evidence": "occurring in the last 90 days",
                        "value": "-90 DAYS",
                    },
                ],
                "ambiguities": [],
                "status": "READY",
            },
            indent=2,
        ),
    ),
]

PROMPT_VERSION = PromptVersion(
    id="v1",
    description=(
        "RITM extraction prompt matching the query-formation extraction schema "
        "(evidence-backed entities/fields/filters, ambiguities, status), scoped to "
        "NEW_REPORT-shaped tickets only — no intent classification, report "
        "delivery/frequency, existing-report reference, or standalone time_range concept, "
        "since reporting windows (absolute or relative) are just filters on a date/timestamp "
        "attribute. Relative windows ('last N days') become a GREATER_THAN_OR_EQUAL filter "
        "with a '-N UNIT' value rather than an invented absolute date. 5 few-shot examples, "
        "all grounded in real tickets from ritm_samples.json: RITM0030001 (baseline EQUALS), "
        "RITM0030004 (IN + literal-date BETWEEN filter), RITM0030008 (IS_NULL + relative-date "
        "filter + entity-inclusion rule across two entities), RITM0030012 "
        "(NEEDS_CLARIFICATION w/ MISSING_OUTPUT_FIELDS, filters still extracted from criteria "
        "alone), RITM0030009 (three-entity chain exercising both entity-inclusion rules at once "
        "plus a correct exclusion: Payment is left out despite a bare 'Payment ID' field and a "
        "'Payment state transitions' criteria clause, since the real subject there is the "
        "Payment Transition entity). System prompt states the entity-inclusion rule, instructs "
        "scanning output_fields/report_criteria item by item rather than reasoning about the "
        "ticket holistically, and states the filter-operator selection rules explicitly."
    ),
    system_prompt=_SYSTEM_PROMPT,
    few_shot_examples=_EXAMPLES,
)
