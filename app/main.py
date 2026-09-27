import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes.catalog import router as catalog_router
from app.api.routes.health import router as health_router
from app.api.routes.report import router as report_router
from app.api.routes.ritm import router as ritm_router
from app.api.routes.sql import router as sql_router
from app.core.config import get_settings
from app.db.catalog_session import init_catalog_db

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Creates the approved_ritms table (and any other missing catalog table). A catalog database that is down
    # must not stop the app starting: the routes that need it fail on their own.
    try:
        init_catalog_db()
    except Exception:
        logger.exception("Could not initialise the catalog database; is it running?")
    yield


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.include_router(health_router)
app.include_router(ritm_router)
app.include_router(catalog_router)
app.include_router(sql_router)
app.include_router(report_router)
