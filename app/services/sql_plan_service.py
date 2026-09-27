"""The estimated execution plan of a query, from SQL Server itself, read for performance problems.

`SET SHOWPLAN_XML ON` makes SQL Server compile each statement and return its estimated plan instead of running it:
no rows are read. The plan shows what the SQL text alone cannot: which tables are scanned rather than sought,
implicit conversions that defeat an index, joins with no join predicate, and the indexes the optimizer wished it had."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.sql_compile_service import server_message

settings = get_settings()

_NS = {"p": "http://schemas.microsoft.com/sqlserver/2004/07/showplan"}
_SCANS = {"Table Scan", "Clustered Index Scan", "Index Scan"}


@dataclass(frozen=True)
class PlanFindings:
    estimated_cost: float  # the optimizer's estimated subtree cost of the whole statement
    estimated_rows: float
    warnings: list[str] = field(default_factory=list)
    missing_indexes: list[str] = field(default_factory=list)  # CREATE INDEX suggestions, as SQL Server words them
    cartesian: bool = False  # a join with no join predicate


@dataclass(frozen=True)
class PlanOutcome:
    findings: PlanFindings | None = None
    unavailable: str | None = None  # why no plan could be had (no SHOWPLAN permission, server unreachable, ...)


def estimated_plan(source_db: Session, sql: str) -> PlanOutcome:
    """The estimated plan of `sql` (already checked to be one plain SELECT). Nothing is executed. SHOWPLAN_XML must be
    the only statement in its batch, so it is switched on and off around the query on the same connection."""
    connection = source_db.connection()
    try:
        connection.exec_driver_sql("SET SHOWPLAN_XML ON")
        try:
            rows = connection.exec_driver_sql(sql).fetchall()
        finally:
            _showplan_off(connection)
    except SQLAlchemyError as exc:
        source_db.rollback()
        return PlanOutcome(unavailable=server_message(exc))
    source_db.rollback()
    if not rows or not rows[0][0]:
        return PlanOutcome(unavailable="SQL Server returned no plan")
    try:
        return PlanOutcome(
            findings=read_plan(str(rows[0][0]), settings.sql_plan_scan_min_rows, settings.sql_plan_warn_cost)
        )
    except ET.ParseError as exc:
        return PlanOutcome(unavailable=f"the plan could not be read: {exc}")


def _showplan_off(connection: Connection) -> None:
    """A connection left in SHOWPLAN mode would return plans instead of running queries for whoever gets it from the
    pool next, so one that cannot be switched back is discarded."""
    try:
        connection.exec_driver_sql("SET SHOWPLAN_XML OFF")
    except Exception:
        connection.invalidate()
        raise


def _float(element: ET.Element, name: str) -> float:
    try:
        return float(element.get(name, 0))
    except ValueError:
        return 0.0


def _object_name(element: ET.Element) -> str:
    obj = element.find(".//p:Object", _NS)
    if obj is None:
        return "a table"
    parts = [obj.get("Schema"), obj.get("Table")]
    name = ".".join(part.strip("[]") for part in parts if part)
    index = obj.get("Index")
    return f"{name} (index {index.strip('[]')})" if index else name


def _missing_index(group: ET.Element) -> str | None:
    index = group.find("p:MissingIndex", _NS)
    if index is None:
        return None
    table = ".".join(part.strip("[]") for part in (index.get("Schema"), index.get("Table")) if part)
    columns: dict[str, list[str]] = {}
    for column_group in index.findall("p:ColumnGroup", _NS):
        names = [c.get("Name", "").strip("[]") for c in column_group.findall("p:Column", _NS)]
        columns[column_group.get("Usage", "")] = names
    keys = columns.get("EQUALITY", []) + columns.get("INEQUALITY", [])
    if not keys:
        return None
    suggestion = f"ON {table} ({', '.join(keys)})"
    if columns.get("INCLUDE"):
        suggestion += f" INCLUDE ({', '.join(columns['INCLUDE'])})"
    impact = group.get("Impact")
    return f"{suggestion}, estimated improvement {float(impact):.0f}%" if impact else suggestion


def read_plan(plan_xml: str, scan_min_rows: int = 100_000, warn_cost: float = 50.0) -> PlanFindings:
    """What a showplan XML document says about the query's performance. A scan is flagged only when it reads a large
    table (`scan_min_rows` rows or more) to keep a small part of it (under 10%): a report that needs most of a table
    is right to scan it."""
    root = ET.fromstring(plan_xml)
    statement = root.find(".//p:StmtSimple", _NS)
    if statement is None:
        raise ET.ParseError("no statement in the plan")
    cost = _float(statement, "StatementSubTreeCost")
    rows = _float(statement, "StatementEstRows")
    warnings: list[str] = []

    if cost > warn_cost:
        warnings.append(
            f"estimated query cost is {cost:.1f} (above {warn_cost:g}): SQL Server expects this query to be expensive"
        )

    cartesian = False
    for warning in root.iter(f"{{{_NS['p']}}}Warnings"):
        if warning.get("NoJoinPredicate") == "true":
            cartesian = True
        for convert in warning.findall("p:PlanAffectingConvert", _NS):
            if convert.get("ConvertIssue") == "Seek Plan":
                expression = convert.get("Expression", "")
                warnings.append(
                    f"implicit conversion {expression[:120]} prevents an index seek; match the literal's type to the column"
                )
        if warning.find("p:ColumnsWithNoStatistics", _NS) is not None:
            warnings.append("some columns have no statistics, so SQL Server's row estimates may be poor")
    if cartesian:
        warnings.append("the plan has a join with no join predicate (a cartesian product): every row pairs with every other")

    seen: set[str] = set()
    for rel_op in root.iter(f"{{{_NS['p']}}}RelOp"):
        if rel_op.get("PhysicalOp") not in _SCANS:
            continue
        table_rows = _float(rel_op, "TableCardinality")
        kept = _float(rel_op, "EstimateRows")
        name = _object_name(rel_op)
        if table_rows >= scan_min_rows and kept < table_rows * 0.1 and name not in seen:
            seen.add(name)
            warnings.append(
                f"{rel_op.get('PhysicalOp')} on {name}: reads about {table_rows:,.0f} rows to keep about {kept:,.0f}; "
                "a sargable predicate or an index on the filtered columns would let it seek"
            )

    missing = [s for group in root.iter(f"{{{_NS['p']}}}MissingIndexGroup") if (s := _missing_index(group))]
    warnings.extend(f"SQL Server suggests an index {suggestion}" for suggestion in missing)
    return PlanFindings(estimated_cost=cost, estimated_rows=rows, warnings=warnings, missing_indexes=missing, cartesian=cartesian)
