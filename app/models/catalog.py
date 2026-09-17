from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


class SchemaTable(Base):
    """One row per source table. `doc_text` and `embedding` are populated during sync (Phase 2)."""

    __tablename__ = "schema_tables"
    __table_args__ = (
        UniqueConstraint("schema_name", "table_name", name="uq_schema_tables_name"),
        Index("ix_schema_tables_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim), nullable=True)
    tsv: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    columns: Mapped[list["SchemaColumn"]] = relationship(back_populates="table", cascade="all, delete-orphan")


class SchemaColumn(Base):
    """Structured column metadata, resolved by fuzzy match once a table is chosen — never embedded directly."""

    __tablename__ = "schema_columns"
    __table_args__ = (UniqueConstraint("table_id", "column_name", name="uq_schema_columns_table_column"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("schema_tables.id", ondelete="CASCADE"), nullable=False)
    column_name: Mapped[str] = mapped_column(String(128), nullable=False)
    data_type: Mapped[str] = mapped_column(String(64), nullable=False)
    is_nullable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_primary_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_foreign_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    table: Mapped["SchemaTable"] = relationship(back_populates="columns")


class SchemaRelationship(Base):
    """A single FK edge, used later for join-path resolution (Phase 5)."""

    __tablename__ = "schema_relationships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    constraint_name: Mapped[str] = mapped_column(String(256), nullable=False)
    fk_table_id: Mapped[int] = mapped_column(ForeignKey("schema_tables.id", ondelete="CASCADE"), nullable=False)
    fk_column_name: Mapped[str] = mapped_column(String(128), nullable=False)
    pk_table_id: Mapped[int] = mapped_column(ForeignKey("schema_tables.id", ondelete="CASCADE"), nullable=False)
    pk_column_name: Mapped[str] = mapped_column(String(128), nullable=False)
