"""Request and response types for /report/*: a SELECT turned into an SSRS report, published, and subscribed to."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# SSRS refuses items named with any of  : ? ; @ & = + $ , \ * < > | " /  and names may not start or end with a space.
# One to 100 characters; \w is letters, digits and underscore in any script.
_ITEM_NAME = r"^(?:[\w()\-.]|[\w()\-.][\w ()\-.]{0,98}[\w()\-.])$"
# A .NET format string ("N2", "C0", "yyyy-MM-dd", "#,##0.00"): no quotes, backslashes or "=", so it can't turn into an expression.
_FORMAT = r"^[A-Za-z0-9#.,:/%\- ]{1,40}$"
_EMAIL = r"^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$"


class ReportColumn(BaseModel):
    name: str = Field(..., description="Output column of the SQL, exactly as its alias reads.")
    header: str | None = Field(default=None, description="Heading shown above the column; defaults to the name.")
    format: str | None = Field(
        default=None, pattern=_FORMAT, description='.NET format for the values, e.g. "N2", "C0", "#,##0", "yyyy-MM-dd", "P1".'
    )
    align: Literal["Left", "Center", "Right"] | None = Field(
        default=None, description="Alignment of heading and values; omit for text on the left and numbers on the right."
    )
    width_cm: float | None = Field(default=None, ge=1.0, le=20.0, description="Column width; defaults to the template's.")


class ReportLayout(BaseModel):
    template: Literal["tabular"] = "tabular"
    landscape: bool = True
    title: str | None = Field(default=None, description="Heading at the top of the report; defaults to the report name.")


class GenerateReportRequest(BaseModel):
    sql: str = Field(
        ...,
        min_length=1,
        max_length=20000,
        description="One T-SQL SELECT: schema-qualified tables, named output columns, no SELECT *, no INTO or CTE. "
        "No @parameters: write values into the SQL. It runs under the report's data source, so the caller owns what it reads.",
    )
    report_name: str = Field(
        ..., pattern=_ITEM_NAME, description="Name of the report on the server (what /report/publish uses, and the .rdl file name here)."
    )
    columns: list[ReportColumn] | None = Field(
        default=None,
        description="The columns to show, in order, with optional headings. Omit to show every column the SQL selects.",
    )
    layout: ReportLayout = Field(default_factory=ReportLayout)

    @field_validator("report_name")
    @classmethod
    def _not_dots(cls, name: str | None) -> str | None:
        if name is not None and not name.strip("."):
            raise ValueError("report_name cannot consist only of dots")
        return name


class GenerateReportResponse(BaseModel):
    report_name: str
    file_name: str = Field(..., description="Suggested file name to save the RDL under.")
    sql: str
    columns: list[str]
    data_source: str = Field(
        ..., description="How the report reaches its data: a shared data source on the server, or an embedded connection "
        "string that uses integrated security. The RDL never holds a user name or password."
    )
    rdl: str = Field(..., description="The report definition, ready to upload or to open in Report Builder.")


class PublishReportRequest(GenerateReportRequest):
    folder: str | None = Field(
        default=None, pattern=r"^/[^:?;@&=+$,\\*<>|\"]*$", description="Existing SSRS folder; defaults to SSRS_REPORT_FOLDER."
    )
    overwrite: bool = Field(default=True, description="Replace the report when one of that name is already there.")

    @field_validator("folder")
    @classmethod
    def _no_dot_segments(cls, folder: str | None) -> str | None:
        if folder is not None and any(part in (".", "..") for part in folder.split("/")):
            raise ValueError("folder cannot contain '.' or '..' segments")
        return folder


class PublishReportResponse(BaseModel):
    report_path: str = Field(..., description='Where the report lives on the server, e.g. "/Text2SQL Reports/Overdue Loans".')
    item_id: str
    created: bool = Field(..., description="False when an existing report was replaced.")
    url: str = Field(..., description="The report in the web portal.")
    columns: list[str]


class Frequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class Weekday(StrEnum):
    MONDAY = "Monday"
    TUESDAY = "Tuesday"
    WEDNESDAY = "Wednesday"
    THURSDAY = "Thursday"
    FRIDAY = "Friday"
    SATURDAY = "Saturday"
    SUNDAY = "Sunday"


class SubscriptionSchedule(BaseModel):
    frequency: Frequency
    start: datetime = Field(..., description="First run. Give an offset (2026-10-01T08:00:00+05:30) unless the server's own time zone is meant.")
    end: datetime | None = Field(default=None, description="No runs after this; omit to run indefinitely.")
    interval: int = Field(default=1, ge=1, le=52, description="Every N days (daily) or N weeks (weekly). Not used for monthly.")
    days_of_week: list[Weekday] = Field(default_factory=list, description="Weekly only; defaults to the weekday of `start`.")
    days_of_month: list[int] = Field(default_factory=list, description="Monthly only, 1-31; defaults to the day of `start`.")


class RenderFormat(StrEnum):
    PDF = "PDF"
    EXCEL = "EXCELOPENXML"
    CSV = "CSV"
    WORD = "WORDOPENXML"
    POWERPOINT = "PPTX"
    HTML = "MHTML"
    XML = "XML"
    IMAGE = "IMAGE"


class SubscriptionOptions(BaseModel):
    """What a subscription sends, to whom and when: everything except which report."""

    schedule: SubscriptionSchedule
    recipients: list[str] = Field(..., min_length=1, max_length=50, description="Email addresses the report is sent to.")
    cc: list[str] = Field(default_factory=list)
    render_format: RenderFormat = Field(
        default=RenderFormat.HTML,
        description="HTML (MHTML) puts the report in the email body; every other format is sent as an attachment.",
    )
    subject: str = "@ReportName was executed at @ExecutionTime"
    comment: str | None = None
    include_report: bool = Field(
        default=True, description="Send the rendered report: in the body for HTML, as an attachment for other formats."
    )
    include_link: bool = Field(default=False, description="Add a link to the report in the portal.")
    description: str | None = None
    active: bool = True

    @field_validator("recipients", "cc")
    @classmethod
    def _emails(cls, addresses: list[str]) -> list[str]:
        bad = [a for a in addresses if not re.match(_EMAIL, a)]
        if bad:
            raise ValueError(f"not an email address: {', '.join(bad)}")
        return addresses


class CreateSubscriptionRequest(SubscriptionOptions):
    report_path: str = Field(
        ..., pattern=r"^/.+", description="The published report, as returned by /report/publish, e.g. /Text2SQL Reports/Overdue Loans."
    )


class CreateSubscriptionResponse(BaseModel):
    subscription_id: str
    report_path: str
    schedule: str = Field(..., description="The schedule as SSRS describes it.")
    recipients: list[str]
    render_format: RenderFormat
    active: bool


class ScheduleReportRequest(PublishReportRequest, SubscriptionOptions):
    """/report/publish and /report/subscription in one request: the report's SQL, name and layout, then who gets it and when."""

    overwrite: bool = Field(
        default=False, description="Replace a report of that name that is already on the server; by default that is a 409."
    )


class ScheduleReportResponse(BaseModel):
    report: PublishReportResponse
    subscription: CreateSubscriptionResponse
