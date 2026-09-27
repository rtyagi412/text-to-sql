from app.services.sql_plan_service import estimated_plan, read_plan
from tests.fakes import FakeResult, FakeSession

_NS = "http://schemas.microsoft.com/sqlserver/2004/07/showplan"


def _plan(cost: float, body: str = "", warnings: str = "") -> str:
    return f"""<ShowPlanXML xmlns="{_NS}"><BatchSequence><Batch><Statements>
<StmtSimple StatementSubTreeCost="{cost}" StatementEstRows="42"><QueryPlan>{warnings}{body}</QueryPlan></StmtSimple>
</Statements></Batch></BatchSequence></ShowPlanXML>"""


def _scan(table_rows: int, kept: int, op: str = "Clustered Index Scan") -> str:
    return f"""<RelOp PhysicalOp="{op}" EstimateRows="{kept}" TableCardinality="{table_rows}">
<IndexScan><Object Database="[Bank]" Schema="[core]" Table="[Account]" Index="[PK_Account]" /></IndexScan></RelOp>"""


def test_cheap_plan_has_no_warnings():
    findings = read_plan(_plan(0.5, _scan(1_000_000, 900_000)))
    assert findings.estimated_cost == 0.5 and findings.estimated_rows == 42
    assert findings.warnings == []


def test_expensive_plan():
    assert any("estimated query cost is 120.0" in w for w in read_plan(_plan(120)).warnings)


def test_big_scan_keeping_few_rows():
    warnings = read_plan(_plan(1, _scan(2_000_000, 10))).warnings
    assert any("core.Account (index PK_Account)" in w and "2,000,000" in w for w in warnings)


def test_small_table_scan_is_fine():
    assert read_plan(_plan(1, _scan(500, 1))).warnings == []


def test_implicit_conversion_and_cartesian():
    warnings = f"""<Warnings NoJoinPredicate="true"><PlanAffectingConvert ConvertIssue="Seek Plan"
Expression="CONVERT_IMPLICIT(nvarchar(20),[a].[Status],0)=[@1]" /></Warnings>"""
    findings = read_plan(_plan(1, warnings=warnings))
    assert findings.cartesian
    assert any("CONVERT_IMPLICIT" in w for w in findings.warnings)
    assert any("cartesian" in w for w in findings.warnings)


def test_missing_index():
    body = """<MissingIndexes><MissingIndexGroup Impact="87.5"><MissingIndex Database="[Bank]" Schema="[core]" Table="[Account]">
<ColumnGroup Usage="EQUALITY"><Column Name="[Status]" /></ColumnGroup>
<ColumnGroup Usage="INCLUDE"><Column Name="[Balance]" /></ColumnGroup></MissingIndex></MissingIndexGroup></MissingIndexes>"""
    findings = read_plan(_plan(1, body))
    assert findings.missing_indexes == ["ON core.Account (Status) INCLUDE (Balance), estimated improvement 88%"]


def test_estimated_plan_switches_showplan_off():
    session = FakeSession(lambda sql: None if sql.startswith("SET") else FakeResult(["plan"], [(_plan(2),)]))
    outcome = estimated_plan(session, "SELECT a.x AS x FROM d.a AS a")
    assert outcome.findings.estimated_cost == 2
    assert session.conn.executed == ["SET SHOWPLAN_XML ON", "SELECT a.x AS x FROM d.a AS a", "SET SHOWPLAN_XML OFF"]
    assert not session.conn.invalidated
