from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

catalog_engine = create_engine(settings.catalog_database_url, pool_pre_ping=True)

CatalogSessionLocal = sessionmaker(bind=catalog_engine, autoflush=False, autocommit=False)


def get_catalog_db() -> Generator[Session, None, None]:
    db = CatalogSessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_catalog_db() -> None:
    """Create the pgvector extension and any catalog tables that don't exist yet."""
    from app.models.catalog import Base

    with catalog_engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=catalog_engine)
