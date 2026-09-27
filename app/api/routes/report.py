from collections.abc import Generator
from contextlib import contextmanager
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response

from app.schemas.report import (
    CreateSubscriptionRequest,
    CreateSubscriptionResponse,
    GenerateReportRequest,
    GenerateReportResponse,
    PublishReportRequest,
    PublishReportResponse,
    ScheduleReportRequest,
    ScheduleReportResponse,
)
from app.services.reporting import (
    ReportExistsError,
    SsrsApiError,
    SsrsConfigError,
    create_subscription,
    generate_rdl,
    publish_report,
    schedule_report,
)

router = APIRouter(prefix="/report", tags=["report"])


@contextmanager
def _http_errors() -> Generator[None]:
    # The ValueError subclasses come before ValueError, which is the caller's 400 (bad columns or SQL).
    try:
        yield
    except ReportExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SsrsConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SsrsApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rdl", response_model=GenerateReportResponse)
def rdl(body: GenerateReportRequest) -> GenerateReportResponse:
    """Builds the report definition (RDL) from a SELECT, its output columns and a layout
    template. Nothing is sent to SSRS, so the RDL can be reviewed or opened in Report Builder first. The RDL
    holds no credentials: it uses a shared data source, or a connection string with integrated security."""
    with _http_errors():
        return generate_rdl(body)


@router.post("/rdl/file", response_class=Response, responses={200: {"content": {"application/xml": {}}}})
def rdl_file(body: GenerateReportRequest) -> Response:
    """Same request as /report/rdl, but answers with the .rdl file itself (raw XML, not JSON) so it downloads and
    opens in Report Builder as is."""
    with _http_errors():
        result = generate_rdl(body)
    return Response(
        content=result.rdl.encode("utf-8"),
        media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(result.file_name)}"},
    )


@router.post("/publish", response_model=PublishReportResponse)
def publish(body: PublishReportRequest) -> PublishReportResponse:
    """Builds the RDL as /report/rdl does and uploads it to SSRS through its REST API. The target folder must exist."""
    with _http_errors():
        return publish_report(body)


@router.post("/schedule", response_model=ScheduleReportResponse, status_code=201)
def schedule(body: ScheduleReportRequest) -> ScheduleReportResponse:
    """The whole workflow in one call: builds the RDL from the SQL, publishes it to SSRS and creates the email
    subscription. If the subscription cannot be created, a report this call created is removed again."""
    with _http_errors():
        return schedule_report(body)


@router.post("/subscription", response_model=CreateSubscriptionResponse, status_code=201)
def subscription(body: CreateSubscriptionRequest) -> CreateSubscriptionResponse:
    """Creates an email subscription on a published report: schedule, recipients and output format. SSRS then runs the query and delivers the report on that schedule."""
    with _http_errors():
        return create_subscription(body)
