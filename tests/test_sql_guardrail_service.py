import pytest

from app.services import sql_guardrail_service as guardrails
from app.services.sql_compile_service import CompileOutcome
from app.services.sql_plan_service import PlanFindings, PlanOutcome
from app.services.sql_sample_service import SampleOutcome

SQL = "SELECT a.x AS x FROM d.a AS a"


@pytest.fixture
def source(monkeypatch):
    """Replaces the three source-database calls; each test sets what they return."""
    state = {
        "compile": CompileOutcome(columns=["x"]),
        "plan": PlanOutcome(findings=PlanFindings(estimated_cost=1.0, estimated_rows=5)),
        "sample": SampleOutcome(row_limit=10, columns=["x"], rows=[{"x": 1}], elapsed_ms=3),
        "calls": [],
    }

    def record(name):
        def call(*args, **kwargs):
            state["calls"].append(name)
            return state[name]

        return call

    monkeypatch.setattr(guardrails.sql_compile_service, "describe", record("compile"))
    monkeypatch.setattr(guardrails.sql_plan_service, "estimated_plan", record("plan"))
    monkeypatch.setattr(guardrails.sql_sample_service, "run_sample", record("sample"))
    return state


def test_all_checks_pass(source):
    report = guardrails.run_on_source(object(), SQL, ["x"])
    assert report.compiled
    assert report.plan.estimated_cost == 1.0
    assert report.sample.executed and report.sample.rows == [{"x": 1}]
    assert report.warnings == []


def test_compile_error_raises(source):
    source["compile"] = CompileOutcome(error="Invalid column name 'y'.")
    with pytest.raises(guardrails.SqlRuntimeError, match="Invalid column name"):
        guardrails.run_on_source(object(), SQL)
    assert source["calls"] == ["compile"]


def test_wrong_columns_raise(source):
    with pytest.raises(guardrails.SqlRuntimeError, match="expected"):
        guardrails.run_on_source(object(), SQL, ["Account Number"])


def test_unreachable_server_skips_everything(source):
    source["compile"] = CompileOutcome(unavailable="Login failed")
    report = guardrails.run_on_source(object(), SQL)
    assert not report.compiled and report.sample is None
    assert report.warnings == ["source database checks skipped: Login failed"]
    assert source["calls"] == ["compile"]


def test_runtime_error_raises(source):
    source["sample"] = SampleOutcome(row_limit=10, error="Divide by zero error encountered.")
    with pytest.raises(guardrails.SqlRuntimeError, match="Divide by zero"):
        guardrails.run_on_source(object(), SQL)


def test_plan_over_max_cost_raises(source, monkeypatch):
    monkeypatch.setattr(guardrails.settings, "sql_plan_max_cost", 10.0)
    source["plan"] = PlanOutcome(findings=PlanFindings(estimated_cost=99.0, estimated_rows=5, warnings=["big scan"]))
    with pytest.raises(guardrails.SqlRuntimeError, match="over the limit of 10: big scan"):
        guardrails.run_on_source(object(), SQL)


def test_plan_warnings_and_empty_sample_are_warnings(source):
    source["plan"] = PlanOutcome(findings=PlanFindings(estimated_cost=1.0, estimated_rows=5, warnings=["big scan"]))
    source["sample"] = SampleOutcome(row_limit=10, columns=["x"], rows=[], elapsed_ms=3)
    report = guardrails.run_on_source(object(), SQL)
    assert report.warnings[0] == "big scan"
    assert "returned no rows" in report.warnings[1]


def test_timeout_is_a_warning(source):
    source["sample"] = SampleOutcome(row_limit=10, timed_out=True, elapsed_ms=30000)
    report = guardrails.run_on_source(object(), SQL)
    assert report.sample.timed_out and not report.sample.executed
    assert "did not return its first 10 rows" in report.warnings[0]


def test_preview_can_be_switched_off(source, monkeypatch):
    monkeypatch.setattr(guardrails.settings, "sql_sample_preview", False)
    report = guardrails.run_on_source(object(), SQL)
    assert report.sample.rows == [] and report.sample.rows_returned == 1
