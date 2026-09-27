# /report: SQL to a report on SSRS

A single `SELECT` becomes an SSRS report (RDL), is uploaded to the report server, and can be emailed on a schedule.

| Endpoint | Does | Needs SSRS login |
|---|---|---|
| `POST /report/rdl` | Builds the RDL and returns it inside JSON | no |
| `POST /report/rdl/file` | Same, but returns the raw `.rdl` file (open it in Report Builder) | no |
| **`POST /report/publish`** | Builds the RDL and uploads it to SSRS | yes |
| `POST /report/subscription` | Emails a published report on a schedule | yes |
| **`POST /report/schedule`** | Publish and subscribe in one call (what a UI form should use; see `report-ui-spec.md`) | yes |

## Publish

`POST /report/publish`

```json
{
  "sql": "SELECT a.AccountId AS AccountId, a.Balance AS Balance FROM core.Account AS a WHERE a.Status = 'Active'",
  "report_name": "Active Accounts",
  "columns": [{"name": "Balance", "header": "Balance (INR)", "format": "N2"}],
  "layout": {"title": "Active accounts", "landscape": true},
  "folder": "/Text2SQL Reports",
  "overwrite": true
}
```

Only `sql` and `report_name` are required. `columns` (order, heading, `format`, `align`, `width_cm`), `layout`, `folder`
(default `SSRS_REPORT_FOLDER`) and `overwrite` (default `true`) are optional.

Response (200):

```json
{
  "report_path": "/Text2SQL Reports/Active Accounts",
  "item_id": "0b7f...",
  "created": true,
  "url": "http://your-ssrs-host/reports/report/Text2SQL%20Reports/Active%20Accounts",
  "columns": ["AccountId", "Balance"]
}
```

`created` is `false` when an existing report of that name was replaced. Open `url` in a browser to run the report.

### What it calls on SSRS (REST API v2.0, `<SSRS_URL>/api/v2.0`)

1. `GET /CatalogItems(Path='<folder>')`: the folder must exist and be a folder.
2. `GET /CatalogItems(Path='<folder>/<name>')`: is there already a report there?
3. `POST /CatalogItems` to create, or `PUT /CatalogItems(<id>)` to replace. The body is `#Model.Report` with the
   base64 RDL in `Content`, `Name`, and `Path` (the full path, name included).
4. `PUT /Reports(<id>)/DataSources` binds the report's data source `Source` to the shared data source. An uploaded
   RDL only records that it references one; without this step SSRS says `rsInvalidDataSourceReference`. Before
   step 1 the app checks with `GET /CatalogItems(Path='<SSRS_DATA_SOURCE_PATH>')` that it exists and is a data source.

### Setup

1. In `.env` set `SSRS_URL`, `SSRS_USERNAME`, `SSRS_PASSWORD` (and `SSRS_DOMAIN` with `SSRS_AUTH=ntlm`), plus the
   data source below. See `.env.example`. Restart the app: settings load at startup.
2. Create the target folder in the SSRS portal (New, then Folder). The API never creates folders.
3. Give the login **Content Manager** on that folder. Publisher is enough for `/report/publish` alone, but it has no
   subscription rights, so `/report/subscription` and `/report/schedule` get a 403. The login also needs at least
   Browser on the shared data source below (it is looked up and bound to the report).
4. Create a **shared data source** in the portal (for example `/Data Sources/Banking`, with the connection string and
   the credentials it should run under) and set `SSRS_DATA_SOURCE_PATH` to it. The RDL then holds no login.
   `SSRS_CONNECTION_STRING` (integrated security only) is meant for opening the file locally in Report Builder.

### Errors

| Status | Meaning |
|---|---|
| 400 | The SQL was refused (not one plain `SELECT`, `SELECT *`, unqualified table, unnamed column, `@parameter`, ...), bad `columns`, or the folder does not exist |
| 409 | A report of that name exists and `overwrite` is `false` |
| 422 | The request body is malformed |
| 502 | SSRS answered with an error, could not be reached, or something other than a report is at that path |
| 503 | SSRS settings or the data source are missing (including no shared data source at `SSRS_DATA_SOURCE_PATH` on the server), or the connection string holds a user name or password |

## Subscribe

`POST /report/subscription` (201) makes SSRS email a published report on a schedule. SSRS runs the query and sends the
file itself, so the app is not involved once the subscription exists.

```json
{
  "report_path": "/Text2SQL Reports/Active Accounts",
  "schedule": {"frequency": "weekly", "start": "2026-10-05T08:00:00+05:30", "days_of_week": ["Monday", "Friday"]},
  "recipients": ["someone@example.com"],
  "cc": [],
  "render_format": "MHTML",
  "subject": "@ReportName was executed at @ExecutionTime",
  "include_report": true,
  "include_link": false
}
```

- `report_path` is the `report_path` that `/report/publish` returned.
- `schedule.frequency` is `daily`, `weekly` or `monthly`. `start` is the first run; give an offset (`+05:30`) unless the
  server's own time zone is meant. `end` is optional. `interval` is every N days or weeks. `days_of_week` (weekly) and
  `days_of_month` (monthly) default to the day of `start`.
- `render_format` is `MHTML` by default, which puts the report in the **email body** (with `include_report` on). The
  other formats (`PDF`, `EXCELOPENXML`, `CSV`, `WORDOPENXML`, `PPTX`, `XML`, `IMAGE`) are sent as an attachment.
- `active: false` creates it switched off. Response: `subscription_id`, `report_path`, `schedule`, `recipients`,
  `render_format`, `active`.

SSRS side: the report server's email settings (Reporting Services Configuration Manager, then E-mail Settings) must be
set up, SQL Server Agent must be running (it fires the schedule), and the report's data source must use stored
credentials, which the shared data source does. `LastStatus` on the subscription in the portal shows whether the
last send worked.

## SQL rules

One T-SQL `SELECT`; schema-qualified tables; every output column named (alias or bare column); no `SELECT *`, `INTO`,
CTE, `OPENROWSET`/`OPENQUERY`/`OPENDATASOURCE`/`OPENXML`, and no `@parameters` (write values into the SQL). The query
runs under the data source's login, so use a read-only login limited to the tables you want exposed.
