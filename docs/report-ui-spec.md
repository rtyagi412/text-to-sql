# UI spec: "Schedule a report" form

The user pastes an already-written SQL query, says who should get it and when, and clicks Submit. **One API call does
everything**: builds the report, publishes it to SSRS and creates the email subscription.

```
POST {API_BASE}/report/schedule        Content-Type: application/json        -> 201
```

## Request body

Send only what the user filled in; fields marked optional have defaults.

| JSON field | Form control | Required | Rules |
|---|---|---|---|
| `sql` | Textarea | yes | 1 to 20,000 characters. See "SQL rules" |
| `report_name` | Text | yes | 1 to 100 characters: letters, digits, spaces and `_ ( ) - .` only; not just dots. Must not already exist (see 409) |
| `layout.title` | Text | no | Heading on the report; defaults to the report name |
| `layout.landscape` | Checkbox | no | Default `true` |
| `columns` | Column editor | no | Omit to show every column. Each item: `name` (must equal an alias in the SQL), `header`, `format`, `align` (`Left`/`Center`/`Right`), `width_cm` (1 to 20). `format` is a .NET format such as `N2`, `C0`, `P1`, `yyyy-MM-dd`, `#,##0.00` |
| `folder` | Hidden / admin | no | SSRS folder that must already exist; default is the server's configured one. Leave it out |
| `overwrite` | Checkbox | no | Default `false`. Only set `true` to knowingly replace an existing report |
| `schedule.frequency` | Select | yes | `daily`, `weekly` or `monthly` |
| `schedule.start` | Date and time | yes | First run. **Must be in the future and must carry a UTC offset** |
| `schedule.end` | Date | no | No runs after this. Omit to repeat forever. Must be after `start` |
| `schedule.interval` | Number | no | 1 to 52. Every N days (daily) or N weeks (weekly). Ignored for monthly |
| `schedule.days_of_week` | Multi-select | no | Weekly only: `Monday` to `Sunday`. Defaults to the weekday of `start` |
| `schedule.days_of_month` | Multi-select | no | Monthly only: 1 to 31. Defaults to the day of `start` |
| `recipients` | Email chips | yes | 1 to 50 valid addresses |
| `cc` | Email chips | no | Valid addresses |
| `render_format` | Select | no | `MHTML` (default) shows the report **in the email body**. Others are attachments: `PDF`, `EXCELOPENXML`, `CSV`, `WORDOPENXML`, `PPTX`, `XML`, `IMAGE` |
| `subject` | Text | no | Default `@ReportName was executed at @ExecutionTime` |
| `comment` | Textarea | no | Text added to the email |
| `include_link` | Checkbox | no | Add a link to the report. Default `false` |
| `include_report` | Checkbox | no | Default `true`. Leave on |
| `description` | Text | no | Label for the subscription in the SSRS portal |
| `active` | Checkbox | no | Default `true`. `false` creates it switched off |

### Example

```json
{
  "sql": "SELECT a.AccountId AS AccountId, a.Balance AS Balance FROM core.Account AS a WHERE a.Status = 'Active'",
  "report_name": "Active Accounts",
  "layout": {"title": "Active accounts", "landscape": true},
  "columns": [{"name": "Balance", "header": "Balance (INR)", "format": "N2"}],
  "schedule": {"frequency": "weekly", "start": "2026-10-05T08:00:00+05:30", "days_of_week": ["Monday", "Friday"]},
  "recipients": ["tyagi.rahul.ind@gmail.com"],
  "render_format": "MHTML"
}
```

## Response (201)

```json
{
  "report": {
    "report_path": "/Text2SQL Reports/Active Accounts",
    "item_id": "0b7f...",
    "created": true,
    "url": "http://<ssrs-host>/reports/report/Text2SQL%20Reports/Active%20Accounts",
    "columns": ["AccountId", "Balance"]
  },
  "subscription": {
    "subscription_id": "6073612d-...",
    "report_path": "/Text2SQL Reports/Active Accounts",
    "schedule": "",
    "recipients": ["tyagi.rahul.ind@gmail.com"],
    "render_format": "MHTML",
    "active": true
  }
}
```

Show a confirmation: the report `url` (opens the report in the SSRS portal), the recipients, and the schedule the
user chose. `subscription.schedule` is SSRS's own description and is often empty, so do not rely on it for display.

## Errors

Every failure is `{"detail": ...}`. Nothing is left half-done: the schedule is validated before anything is
uploaded, and if SSRS refuses the subscription after the upload, a report this call created is deleted again.

| Status | Meaning | What the UI does |
|---|---|---|
| 400 | `detail` is one sentence: the SQL was refused, a `columns` name is wrong, the schedule is invalid (`end` before `start`, `start` in the past, a day outside 1 to 31) or the folder is missing | Show `detail` next to the form |
| 409 | A report named `report_name` already exists | "Name already in use", ask for another name |
| 422 | A field is malformed. `detail` is a list of `{loc, msg}` | Put `msg` on the field named by the last item of `loc` |
| 502 | SSRS refused or could not be reached. `detail` says whether the report was removed again or is still on the server (only when `overwrite` replaced an existing one) | Show `detail`, offer Retry |
| 503 | SSRS or its data source is not configured | "Reporting is not set up, contact an admin" |

Retrying after a 502 is safe when the message says the new report was removed. If it says the report is still at a
path, retry with `overwrite: true`.

## SQL rules (400 if broken; worth mirroring as hints in the form)

- Exactly one T-SQL `SELECT`, no other statement, no `;`-separated extras.
- Tables are schema-qualified: `core.Account`, not `Account`.
- Every output column has a name: an alias (`COUNT(*) AS Total`) or a bare column. Names are unique.
- No `SELECT *`, `INTO`, CTE (`WITH`), `OPENROWSET`, `OPENQUERY`, `OPENDATASOURCE` or `OPENXML`.
- No `@parameters`. Write the values into the SQL.

## Dates and time zones

`schedule.start` and `schedule.end` need an offset such as `+05:30`. A `datetime-local` input gives no offset, so add
the browser's:

```ts
const withOffset = (local: string): string => {          // "2026-10-05T08:00" -> "2026-10-05T08:00:00+05:30"
  const mins = -new Date(local).getTimezoneOffset();
  const p = (n: number) => String(Math.abs(n)).padStart(2, "0");
  return `${local}:00${mins >= 0 ? "+" : "-"}${p(Math.trunc(Math.abs(mins) / 60))}:${p(Math.abs(mins) % 60)}`;
};
```

Reject a `start` in the past in the form as well; the server refuses it with a 400.

**One-time report.** There is no `once` frequency. For a single run send `frequency: "daily"`, `start` at the wanted
time, and `end` at midnight starting the next day (same offset). This fired once at the scheduled time in testing; it
has not been checked that it stays quiet on later days.

## Client code

```ts
type Frequency = "daily" | "weekly" | "monthly";
type RenderFormat = "MHTML" | "PDF" | "EXCELOPENXML" | "CSV" | "WORDOPENXML" | "PPTX" | "XML" | "IMAGE";

interface ScheduleReportRequest {
  sql: string;
  report_name: string;
  layout?: { title?: string; landscape?: boolean };
  columns?: { name: string; header?: string; format?: string; align?: "Left" | "Center" | "Right"; width_cm?: number }[];
  overwrite?: boolean;
  schedule: {
    frequency: Frequency; start: string; end?: string; interval?: number;
    days_of_week?: string[]; days_of_month?: number[];
  };
  recipients: string[];
  cc?: string[];
  render_format?: RenderFormat;
  subject?: string;
  comment?: string;
  include_link?: boolean;
  description?: string;
  active?: boolean;
}

async function scheduleReport(body: ScheduleReportRequest) {
  const res = await fetch(`${API_BASE}/report/schedule`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(130_000),          // publish + subscribe can take a while
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw {status: res.status, detail: json.detail};
  return json as {
    report: {report_path: string; item_id: string; created: boolean; url: string; columns: string[]};
    subscription: {subscription_id: string; report_path: string; schedule: string; recipients: string[]; render_format: RenderFormat; active: boolean};
  };
}
```

## Behaviour notes

- Disable Submit and show a spinner while the call runs (a few seconds normally; allow up to two minutes).
- Do not send empty optional fields (`""`, `[]`); leave them out so the defaults apply.
- Ask the user for `report_name`, or suggest one (for example the ticket number plus a date). It must be unique, and
  the default `overwrite: false` protects existing reports.
- Optional preview: `POST {API_BASE}/report/rdl/file` takes the same fields as the report part of this request
  (`sql`, `report_name`, `columns`, `layout`) and returns the raw `.rdl` file to download and open in Report Builder.

## Not covered

- No login on the API. Anyone who can reach it can publish a report that reads the database; put your auth in front
  before real users.
- No CORS headers are set; a UI served from another origin will be blocked by the browser until they are added (or the
  UI calls the API through its own dev proxy or backend).
- No endpoints to list, edit or delete reports or subscriptions. Changes after submit are made in the SSRS portal.
- The server needs SSRS reachable, a shared data source, and the API's SSRS login configured (`docs/report-flow.md`).
