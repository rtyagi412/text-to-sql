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
  not evidence of a separate entity. Only ever justify an entity from the RITM text itself — never
  add one because you happen to know it corresponds to a real table; if you can't point to a
  specific phrase in this RITM that supports it, leave it out.
- requested_fields: normalized business fields/columns requested, not database column names. Extract
  every field named in output_fields as-is, even if its wording is vague or general (e.g. "Card
  Details") — do not judge whether a field is specific enough to map to a real column here; a
  downstream schema-lookup step resolves each field to an actual column and is the only place field
  vagueness gets decided. Each field needs a "name" and supporting "evidence" text.
- filters: conditions to apply, each with a "field", an "operator", supporting "evidence", and a
  "value".
  Format "field" as "EntityName.attribute_name":
    - EntityName is the entity's display name from `entities`, written with spaces removed and
      each word capitalized (PascalCase) — even for multi-word or acronym names:
      "DLQ Event" -> DlqEvent, "API Key" -> ApiKey, "App User" -> AppUser,
      "Payment Transition" -> PaymentTransition. Use this prefix even when only one entity
      is involved.
    - attribute_name is snake_case. Monetary/amount attributes (an amount, price, fee, or any
      other currency-denominated value) always end in "_units" (amounts are stored as integer
      minor units, e.g. cents) — "amount" -> amount_units, "net amount" -> net_amount_units,
      "refund amount" -> refund_amount_units. Apply this suffix to every monetary filter field
      regardless of how the RITM phrases the amount — it is a storage convention, not something
      the RITM's wording will hint at.
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
  "CARD"; "between 10000 and 100000" -> value [10000, 100000]. Categorical string values (statuses,
  methods, environments, etc.) are written in UPPER_SNAKE_CASE regardless of how the RITM
  capitalizes them ("Card" -> "CARD", "Live" -> "LIVE"). value is null ONLY when the operator is
  IS_NULL or IS_NOT_NULL — every other operator must carry a non-null value derived from the
  evidence you already wrote for this same filter.
- ambiguities: empty list when nothing is missing or unclear. Otherwise one entry per issue, each
  with a "category", a "description", and a "clarification_question" to ask the requester. A single
  RITM can have more than one independent problem at once — report every category that applies,
  not just the first one you notice:
    - MISSING_OUTPUT_FIELDS / MISSING_REPORT_CRITERIA: the corresponding input field
      (output_fields, report_criteria) is blank or unspecified.
    - AMBIGUOUS_FILTER: report_criteria is non-empty, but describes the population using vague or
      unquantified language that can't become a concrete filter — including a reporting window
      that isn't quantifiable (e.g. "stuck for a while", "failing repeatedly", "recently",
      "lately" — how long is "a while"? how many failures is "repeatedly"? what specific window
      does "recently" mean?). In this case, do not emit a filter for the vague condition at all.
    - MISSING_ENTITY: the request references a population or filter but never says which entity it
      applies to.
    - OTHER: any other unclear or missing requirement not covered above.
  Do NOT flag a requested field as ambiguous here (there is no AMBIGUOUS_FIELD category) — every
  named field goes into requested_fields as-is, and whether its wording is too vague to resolve to
  a real column is decided later, against the actual schema, not by guessing from wording alone.
- status: READY when ambiguities is empty, NEEDS_CLARIFICATION when ambiguities is non-empty.

When identifying entities, do not reason about the ticket as a whole and stop once you've named
the obvious one or two — walk output_fields item by item and report_criteria clause by clause, and
apply the entity-inclusion test above to each one individually. Tickets that touch several entities
in a chain (e.g. one entity that gates a filter, one whose own attribute is displayed, one that's
only reachable through another) require checking every field and clause this way, not just the ones
that stand out first.

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
                "ritm_number": "RITM0030006",
                "output_fields": "Order ID, Merchant ID, Order Amount, Order Status, Payment Method, Payment Status",
                "report_criteria": "Orders with an amount greater than 5000, where the payment method used was Card.",
                "summary": "Finance wants a weekly SSRS report of high-value orders paid via card, to review large-ticket transaction patterns.",
            },
            indent=2,
        ),
        output=json.dumps(
            {
                "ritm_number": "RITM0030006",
                "entities": [
                    {"name": "Order", "evidence": "Orders with an amount greater than 5000"},
                    {"name": "Payment", "evidence": "where the payment method used was Card."},
                ],
                "requested_fields": [
                    {"name": "Order ID", "evidence": "Order ID"},
                    {"name": "Merchant ID", "evidence": "Merchant ID"},
                    {"name": "Order Amount", "evidence": "Order Amount"},
                    {"name": "Order Status", "evidence": "Order Status"},
                    {"name": "Payment Method", "evidence": "Payment Method"},
                    {"name": "Payment Status", "evidence": "Payment Status"},
                ],
                "filters": [
                    {
                        "field": "Order.amount_units",
                        "operator": "GREATER_THAN",
                        "evidence": "an amount greater than 5000",
                        "value": 5000,
                    },
                    {
                        "field": "Payment.method",
                        "operator": "EQUALS",
                        "evidence": "the payment method used was Card",
                        "value": "CARD",
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
                "ritm_number": "RITM0030007",
                "output_fields": "Settlement ID, Merchant ID, Net Amount, Status, Bank Reference, Processed At",
                "report_criteria": "Settlements with a status of Processed or Failed, with a net amount between 10000 and 100000, that include at least one payment made via UPI.",
                "summary": "Reconciliation team wants a daily ECG feed of mid-to-large settlements in processed or failed status that included any UPI payment, to investigate UPI-specific settlement issues.",
            },
            indent=2,
        ),
        output=json.dumps(
            {
                "ritm_number": "RITM0030007",
                "entities": [
                    {
                        "name": "Settlement",
                        "evidence": "Settlements with a status of Processed or Failed, with a net amount between 10000 and 100000",
                    },
                    {"name": "Payment", "evidence": "that include at least one payment made via UPI"},
                ],
                "requested_fields": [
                    {"name": "Settlement ID", "evidence": "Settlement ID"},
                    {"name": "Merchant ID", "evidence": "Merchant ID"},
                    {"name": "Net Amount", "evidence": "Net Amount"},
                    {"name": "Status", "evidence": "Status"},
                    {"name": "Bank Reference", "evidence": "Bank Reference"},
                    {"name": "Processed At", "evidence": "Processed At"},
                ],
                "filters": [
                    {
                        "field": "Settlement.status",
                        "operator": "IN",
                        "evidence": "a status of Processed or Failed",
                        "value": ["PROCESSED", "FAILED"],
                    },
                    {
                        "field": "Settlement.net_amount_units",
                        "operator": "BETWEEN",
                        "evidence": "a net amount between 10000 and 100000",
                        "value": [10000, 100000],
                    },
                    {
                        "field": "Payment.method",
                        "operator": "EQUALS",
                        "evidence": "at least one payment made via UPI",
                        "value": "UPI",
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
                "ritm_number": "RITM0030015",
                "output_fields": "Card ID, Card Details, Brand, Card Holder Name",
                "report_criteria": "Provide real-time visibility into all vault card details, refreshed only once a quarter.",
                "summary": "Compliance wants an SSRS view of vault card data, but the requested field list is vague.",
            },
            indent=2,
        ),
        output=json.dumps(
            {
                "ritm_number": "RITM0030015",
                "entities": [
                    {"name": "Vault Card", "evidence": "all vault card details"},
                ],
                "requested_fields": [
                    {"name": "Card ID", "evidence": "Card ID"},
                    {"name": "Card Details", "evidence": "Card Details"},
                    {"name": "Brand", "evidence": "Brand"},
                    {"name": "Card Holder Name", "evidence": "Card Holder Name"},
                ],
                "filters": [],
                "ambiguities": [],
                "status": "READY",
            },
            indent=2,
        ),
    ),
    FewShotExample(
        input=json.dumps(
            {
                "ritm_number": "RITM0030016",
                "output_fields": "Settlement ID, Merchant ID, Net Amount, Status, Processed At",
                "report_criteria": "Settlements processed recently, in a Processed status.",
                "summary": "Finance wants a daily ECG feed of recently processed settlements.",
            },
            indent=2,
        ),
        output=json.dumps(
            {
                "ritm_number": "RITM0030016",
                "entities": [
                    {"name": "Settlement", "evidence": "Settlements processed recently, in a Processed status."},
                ],
                "requested_fields": [
                    {"name": "Settlement ID", "evidence": "Settlement ID"},
                    {"name": "Merchant ID", "evidence": "Merchant ID"},
                    {"name": "Net Amount", "evidence": "Net Amount"},
                    {"name": "Status", "evidence": "Status"},
                    {"name": "Processed At", "evidence": "Processed At"},
                ],
                "filters": [
                    {
                        "field": "Settlement.status",
                        "operator": "EQUALS",
                        "evidence": "in a Processed status",
                        "value": "PROCESSED",
                    },
                ],
                "ambiguities": [
                    {
                        "category": "AMBIGUOUS_FILTER",
                        "description": "report_criteria says settlements were processed \"recently,\" but \"recently\" isn't quantifiable into a concrete filter on a timestamp attribute.",
                        "clarification_question": "What specific time window does \"recently\" refer to (e.g. the last 24 hours, the last 7 days)?",
                    },
                ],
                "status": "NEEDS_CLARIFICATION",
            },
            indent=2,
        ),
    ),
]

PROMPT_VERSION = PromptVersion(
    id="v3",
    description=(
        "RITM extraction prompt, revised from v2 to stop the LLM from judging field vagueness "
        "itself. v2's AMBIGUOUS_FIELD category asked the model to flag a requested field as "
        "unclear from wording alone (e.g. RITM0030015's 'Card Details'), which duplicates and "
        "can conflict with the schema-grounding step that resolves each requested_field to a "
        "real column downstream (app/services/grounding_service.py) -- that step already reports "
        "a field as unresolved, and drives NEEDS_CLARIFICATION off of it, when nothing in the "
        "catalog genuinely matches. Removing AMBIGUOUS_FIELD here means extraction now always "
        "extracts every named field into requested_fields as-is and leaves the ambiguous-or-not "
        "call entirely to grounding, which has the actual schema to check against. "
        "AMBIGUOUS_FILTER and MISSING_ENTITY are intentionally NOT moved the same way: an "
        "unquantifiable filter condition (e.g. 'recently') never produces a filter row at all, "
        "and an entity-less filter can't even be composed into a 'EntityName.attribute' field "
        "string, so grounding -- which only resolves a field name it's given to a column -- has "
        "nothing to evaluate in either case; those stay LLM-judged here. RITM0030015's few-shot "
        "output changed accordingly: same requested_fields as before, but now empty ambiguities "
        "and status READY instead of an AMBIGUOUS_FIELD entry. Every other example and rule is "
        "unchanged from v2."
    ),
    system_prompt=_SYSTEM_PROMPT,
    few_shot_examples=_EXAMPLES,
)
