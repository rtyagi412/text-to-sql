"""A SELECT to a delivered report on SSRS, behind /report/*.

    rdl.py          builds the report definition (RDL) from the SQL, its columns and a layout template
    ssrs_client.py  the SSRS REST API v2.0 calls
    service.py      generate_rdl, publish_report, create_subscription, schedule_report (the last two in one call); SSRS itself runs and delivers the subscription
"""

from app.services.reporting.service import (
    ReportExistsError,
    create_subscription,
    generate_rdl,
    publish_report,
    schedule_report,
)
from app.services.reporting.ssrs_client import SsrsApiError, SsrsConfigError

__all__ = [
    "ReportExistsError",
    "SsrsApiError",
    "SsrsConfigError",
    "create_subscription",
    "generate_rdl",
    "publish_report",
    "schedule_report",
]
