from app.services.sql_check_service import check_sql

COLUMNS = {
    "d.a": {"x": "int", "k": "int", "p": "int", "r": "int"},
    "d.b": {"y": "int", "k": "int"},
    "d.c": {"k": "int", "q": "int", "w": "int"},
}


def _warnings(sql: str) -> list[str]:
    return check_sql(sql, COLUMNS).warnings


def test_clean_query_has_no_warnings():
    assert _warnings("SELECT a.x AS x FROM d.a AS a INNER JOIN d.c AS c ON c.k = a.k WHERE a.p IN (1, 2)") == []


def test_cross_join_is_reported_as_unjoined():
    checked = check_sql("SELECT a.x AS x FROM d.a AS a CROSS JOIN d.b AS b", COLUMNS)
    assert checked.unjoined == ["d.b AS b"]
    assert any("no join condition" in w for w in checked.warnings)


def test_comma_join_is_reported_as_unjoined():
    assert check_sql("SELECT a.x AS x FROM d.a AS a, d.b AS b WHERE b.k = a.k", COLUMNS).unjoined == ["d.b AS b"]


def test_not_in_subquery():
    assert any("NOT EXISTS" in w for w in _warnings("SELECT a.x AS x FROM d.a AS a WHERE a.k NOT IN (SELECT b.k FROM d.b AS b)"))


def test_not_in_list_is_fine():
    assert _warnings("SELECT a.x AS x FROM d.a AS a WHERE a.k NOT IN (1, 2)") == []


def test_subquery_in_select_list():
    sql = "SELECT a.x AS x, (SELECT MAX(b.y) FROM d.b AS b WHERE b.k = a.k) AS m FROM d.a AS a"
    assert any("subquery in the select list" in w for w in _warnings(sql))


def test_or_across_columns():
    assert any("ORs conditions on different columns" in w for w in _warnings("SELECT a.x AS x FROM d.a AS a WHERE a.p = 1 OR a.r = 2"))


def test_or_on_one_column_is_fine():
    assert _warnings("SELECT a.x AS x FROM d.a AS a WHERE a.p = 1 OR a.p = 2") == []


def test_left_join_filtered_in_where():
    sql = "SELECT a.x AS x FROM d.a AS a LEFT JOIN d.c AS c ON c.k = a.k WHERE c.q = 1"
    assert any("LEFT JOINed but WHERE filters on" in w for w in _warnings(sql))


def test_left_join_is_null_is_fine():
    assert _warnings("SELECT a.x AS x FROM d.a AS a LEFT JOIN d.c AS c ON c.k = a.k WHERE c.w IS NULL") == []
    assert _warnings("SELECT a.x AS x FROM d.a AS a LEFT JOIN d.c AS c ON c.k = a.k WHERE (c.q = 1 OR c.q IS NULL)") == []
