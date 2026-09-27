"""The three /report/* operations: generate an RDL from a SELECT, publish it to SSRS, subscribe to it.
Delivery is SSRS's own job: once the subscription exists, the report server runs the query on schedule and sends the result."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from app.core.config import get_settings
from app.schemas.report import (
    CreateSubscriptionRequest,
    CreateSubscriptionResponse,
    Frequency,
    GenerateReportRequest,
    GenerateReportResponse,
    PublishReportRequest,
    PublishReportResponse,
    ReportColumn,
    ScheduleReportRequest,
    ScheduleReportResponse,
    SubscriptionOptions,
    SubscriptionSchedule,
    Weekday,
)
from app.services import sql_check_service
from app.services.reporting import rdl
from app.services.reporting.ssrs_client import SsrsApiError, SsrsClient, SsrsConfigError
from app.services.sql_check_service import SqlRejectedError

logger = logging.getLogger(__name__)
settings = get_settings()

_EMAIL_EXTENSION = "Report Server Email"
# Keys that put a login into a connection string. The RDL is returned to the caller and stored on the report server
# as plain XML, so it may only carry integrated security.
_CREDENTIAL_KEY = re.compile(r"(?:^|;)\s*(?:password|pwd|uid|user\s*id|user|access\s*token)\s*=", re.IGNORECASE)
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


class ReportExistsError(ValueError):
    """A report of that name is already on the server and overwriting was not asked for."""


@dataclass(frozen=True)
class _Report:
    name: str
    description: str
    sql: str
    columns: list[str]
    data_source: str
    data_source_path: str | None
    rdl: bytes


def _select_columns(available: list[str], requested: list[ReportColumn] | None) -> list[ReportColumn]:
    if requested is None:
        return [ReportColumn(name=name) for name in available]
    if not requested:
        raise ValueError("columns is empty; omit it to show every column")
    known = set(available)
    unknown = [c.name for c in requested if c.name not in known]
    if unknown:
        raise ValueError(f"not output columns of the SQL: {', '.join(unknown)}. It selects: {', '.join(available)}")
    if len({c.name for c in requested}) != len(requested):
        raise ValueError("columns lists a column twice")
    return requested


def _data_source() -> tuple[str | None, str | None, str]:
    """(shared data source path, connection string, description) for the RDL. Refuses a connection string that holds
    a login: the report authenticates as the viewer (or through the shared data source's own stored credentials)."""
    path, connection_string = settings.ssrs_data_source_path, settings.ssrs_connection_string
    if path:
        if not path.startswith("/"):
            raise SsrsConfigError("SSRS_DATA_SOURCE_PATH must be the path of a shared data source, e.g. /Data Sources/Banking")
        return path, None, f"shared data source {path}"
    if connection_string:
        if _CREDENTIAL_KEY.search(connection_string):
            raise SsrsConfigError(
                "SSRS_CONNECTION_STRING holds a user name or password, which would be written into the report definition; "
                "use integrated security, or point SSRS_DATA_SOURCE_PATH at a shared data source that stores the login on the server"
            )
        return None, connection_string, "embedded connection string, integrated security"
    raise SsrsConfigError("no data source: set SSRS_DATA_SOURCE_PATH (a shared data source) or SSRS_CONNECTION_STRING")


def _build(request: GenerateReportRequest) -> _Report:
    data_source_path, connection_string, data_source = _data_source()
    sql = request.sql.strip().rstrip(";").rstrip()
    try:
        sql_check_service.referenced_tables(sql)  # one read-only SELECT over schema-qualified tables
        available = sql_check_service.output_columns(sql)
        sql_parameters = sql_check_service.query_parameters(sql)
    except SqlRejectedError as exc:
        raise ValueError(f"the SQL cannot back a report: {exc}") from exc
    if sql_parameters:
        raise ValueError(
            f"the SQL reads {', '.join('@' + p for p in sql_parameters)}; reports take no parameters, so write the values into the SQL"
        )
    title = request.layout.title or request.report_name
    columns = _select_columns(available, request.columns)
    definition = rdl.build_rdl(
        title=title,
        description=title,
        sql=sql,
        columns=columns,
        template=request.layout.template,
        landscape=request.layout.landscape,
        data_source_path=data_source_path,
        connection_string=connection_string,
    )
    return _Report(
        name=request.report_name,
        description=title,
        sql=sql,
        columns=[c.name for c in columns],
        data_source=data_source,
        data_source_path=data_source_path,
        rdl=definition,
    )


def generate_rdl(request: GenerateReportRequest) -> GenerateReportResponse:
    report = _build(request)
    return GenerateReportResponse(
        report_name=report.name,
        file_name=f"{report.name}.rdl",
        sql=report.sql,
        columns=report.columns,
        data_source=report.data_source,
        rdl=report.rdl.decode("utf-8"),
    )


def _report_location(request: PublishReportRequest) -> tuple[str, str]:
    """(folder, full path of the report) that publishing `request` writes to."""
    folder = (request.folder or settings.ssrs_report_folder).rstrip("/") or "/"
    return folder, f"{folder.rstrip('/')}/{request.report_name}"


def publish_report(request: PublishReportRequest) -> PublishReportResponse:
    """Builds the RDL and uploads it into an existing SSRS folder, replacing the report of that name when `overwrite`
    allows, then binds the report to the shared data source (an uploaded RDL only names one; it does not link it)."""
    report = _build(request)
    folder, path = _report_location(request)

    with SsrsClient() as client:
        shared = client.shared_data_source(report.data_source_path) if report.data_source_path else None
        if folder != "/" and not client.folder_exists(folder):
            raise ValueError(f"folder '{folder}' does not exist on the SSRS server; create it first")
        existing = client.find_report(path)
        if existing and not request.overwrite:
            raise ReportExistsError(f"a report already exists at '{path}'; set overwrite to replace it")
        item_id = client.upload_report(path, report.name, report.rdl, existing["Id"] if existing else None, report.description)
        if shared:
            client.bind_data_source(item_id, rdl.DATA_SOURCE_NAME, shared)

    logger.info("report published %s", json.dumps({"path": path, "created": existing is None}))
    return PublishReportResponse(
        report_path=path,
        item_id=item_id,
        created=existing is None,
        url=f"{settings.ssrs_url.rstrip('/')}/report{quote(path)}",
        columns=report.columns,
    )


def _recurrence(schedule: SubscriptionSchedule) -> dict:
    """SSRS's Recurrence is one object with a property per kind ("DailyRecurrence": {...}), not a typed object."""
    match schedule.frequency:
        case Frequency.DAILY:
            return {"DailyRecurrence": {"DaysInterval": schedule.interval}}
        case Frequency.WEEKLY:
            days = {d.value for d in schedule.days_of_week} or {schedule.start.strftime("%A")}
            return {
                "WeeklyRecurrence": {
                    "WeeksInterval": schedule.interval,
                    "WeeksIntervalSpecified": True,
                    "DaysOfWeek": {d.value: d.value in days for d in Weekday},
                }
            }
        case Frequency.MONTHLY:
            days = sorted(set(schedule.days_of_month)) or [schedule.start.day]
            if not all(1 <= d <= 31 for d in days):
                raise ValueError("days_of_month must be between 1 and 31")
            return {"MonthlyRecurrence": {"Days": ",".join(map(str, days)), "MonthsOfYear": {m: True for m in _MONTHS}}}


def _subscription_body(request: CreateSubscriptionRequest) -> dict:
    schedule = request.schedule
    if schedule.end is not None and schedule.end <= schedule.start:
        raise ValueError("schedule.end must be after schedule.start")
    if schedule.start <= datetime.now(schedule.start.tzinfo):
        raise ValueError("schedule.start is in the past; the first run must be a future time")
    definition: dict = {
        "StartDateTime": schedule.start.isoformat(),
        "EndDateSpecified": schedule.end is not None,
        "Recurrence": _recurrence(schedule),
    }
    if schedule.end is not None:
        definition["EndDate"] = schedule.end.isoformat()

    email = {
        "TO": ";".join(request.recipients),
        "CC": ";".join(request.cc),
        "RenderFormat": request.render_format.value,
        "Subject": request.subject,
        "Comment": request.comment or "",
        "IncludeReport": str(request.include_report),
        "IncludeLink": str(request.include_link),
        "Priority": "NORMAL",
    }
    return {
        "Description": request.description or f"{request.report_path} ({schedule.frequency.value})",
        "Report": request.report_path,
        "IsActive": request.active,
        "IsDataDriven": False,
        "EventType": "TimedSubscription",
        "DeliveryExtension": _EMAIL_EXTENSION,
        "ExtensionSettings": {
            "Extension": _EMAIL_EXTENSION,
            "ParameterValues": [{"Name": name, "Value": value} for name, value in email.items() if value != ""],
        },
        "Schedule": {"Definition": definition},
        "ParameterValues": [],
    }


def create_subscription(request: CreateSubscriptionRequest) -> CreateSubscriptionResponse:
    """Creates an email subscription on a published report: schedule, recipients and format."""
    body = _subscription_body(request)
    with SsrsClient() as client:
        created = client.create_subscription(body)
    logger.info(
        "subscription created %s",
        json.dumps({"report": request.report_path, "id": created.get("Id"), "recipients": len(request.recipients)}),
    )
    return CreateSubscriptionResponse(
        subscription_id=created["Id"],
        report_path=request.report_path,
        schedule=created.get("ScheduleDescription") or request.schedule.frequency.value,
        recipients=request.recipients,
        render_format=request.render_format,
        active=created.get("IsActive", request.active),
    )


def schedule_report(request: ScheduleReportRequest) -> ScheduleReportResponse:
    """Publishes the report, then subscribes recipients to it, as one operation. The schedule is checked before
    anything is uploaded. When SSRS then refuses the subscription, a report this call created is deleted again, so a
    failed request leaves nothing behind; a report it replaced cannot be restored and is left, and the error says so."""
    _, path = _report_location(request)
    subscription_request = CreateSubscriptionRequest(
        report_path=path, **request.model_dump(include=set(SubscriptionOptions.model_fields))
    )
    _subscription_body(subscription_request)

    published = publish_report(request)
    try:
        subscription = create_subscription(subscription_request)
    except (SsrsApiError, SsrsConfigError) as exc:
        removed = False
        if published.created:
            try:
                with SsrsClient() as client:
                    client.delete_item(published.item_id)
                removed = True
            except (SsrsApiError, SsrsConfigError):
                logger.exception("could not remove report %s after its subscription failed", published.report_path)
        outcome = "the new report was removed again" if removed else f"the report is still at '{published.report_path}'"
        raise SsrsApiError(
            f"the report was published but the subscription could not be created ({exc}); {outcome}", getattr(exc, "status", None)
        ) from exc
    return ScheduleReportResponse(report=published, subscription=subscription)
