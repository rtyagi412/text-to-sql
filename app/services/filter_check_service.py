import re

from app.schemas.sql_generation import FilterValue, Operator

_RELATIVE_WINDOW = re.compile(r"^-\d+ (MINUTES|HOURS|DAYS|WEEKS|MONTHS|YEARS)$")
_ABSOLUTE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$")

_NUMERIC_TYPES = {"int", "bigint", "smallint", "tinyint", "decimal", "numeric", "float", "real", "money", "smallmoney"}
_DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
_CHAR_TYPES = {"varchar", "nvarchar", "char", "nchar", "text", "ntext"}
_BINARY_TYPES = {"binary", "varbinary", "image"}

_NULL_TESTS = {Operator.IS_NULL, Operator.IS_NOT_NULL}
_LIST_OPERATORS = {Operator.IN, Operator.NOT_IN}
_ORDERED = {
    Operator.GREATER_THAN,
    Operator.GREATER_THAN_OR_EQUAL,
    Operator.LESS_THAN,
    Operator.LESS_THAN_OR_EQUAL,
    Operator.BETWEEN,
}
_TEXT_MATCH = {Operator.CONTAINS, Operator.STARTS_WITH, Operator.ENDS_WITH}
_EQUALITY = {Operator.EQUALS, Operator.NOT_EQUALS, *_LIST_OPERATORS}


def _kind(data_type: str) -> str:
    base = data_type.split("(")[0].strip().lower()
    for kind, names in (("numeric", _NUMERIC_TYPES), ("date", _DATE_TYPES), ("char", _CHAR_TYPES), ("binary", _BINARY_TYPES)):
        if base in names:
            return kind
    return base  # bit, uniqueidentifier, time, ... : only the operator and value shape are checked


def _values(value: FilterValue) -> list:
    return list(value) if isinstance(value, list) else [value]


def check_filter(
    label: str, operator: Operator, value: FilterValue, data_type: str, allowed_values: list[str] | None
) -> list[str]:
    """What is wrong with a filter on one column, as problem sentences (empty when it is sound): value shape for
    the operator, the operator and literal against the column's type, and, for an enumerated column, a value
    its CHECK constraint doesn't allow. Judged from the catalog alone, without touching the source data."""
    if operator in _NULL_TESTS:
        return [f"{label}: {operator.value} takes no value, got {value!r}"] if value is not None else []
    if operator in _LIST_OPERATORS:
        if not isinstance(value, list) or not value:
            return [f"{label}: {operator.value} needs a non-empty list, got {value!r}"]
    elif operator is Operator.BETWEEN:
        if not isinstance(value, list) or len(value) != 2:
            return [f"{label}: BETWEEN needs exactly two values [low, high], got {value!r}"]
    elif value is None or isinstance(value, list):
        return [f"{label}: {operator.value} needs a single value, got {value!r}"]

    kind = _kind(data_type)
    if kind == "binary":
        return [f"{label}: a {data_type} column cannot be filtered on {operator.value}"]
    if operator in _TEXT_MATCH and kind != "char":
        return [f"{label}: {operator.value} only applies to text columns, and this one is {data_type}"]
    if operator in _ORDERED and kind not in ("numeric", "date"):
        return [f"{label}: {operator.value} needs a numeric or date column, and this one is {data_type}"]

    problems: list[str] = []
    for item in _values(value):
        if kind == "numeric" and (isinstance(item, bool) or not isinstance(item, (int, float))):
            problems.append(f"{label}: {item!r} is not a number, and the column is {data_type}")
        elif kind == "bit" and not (isinstance(item, bool) or item in (0, 1)):
            problems.append(f"{label}: {item!r} is not a boolean, and the column is bit")
        elif kind == "date" and not (
            isinstance(item, str) and (_RELATIVE_WINDOW.match(item) or _ABSOLUTE_DATE.match(item))
        ):
            problems.append(f"{label}: {item!r} is neither an ISO date nor a relative window like '-7 DAYS'")
        elif kind not in ("date",) and isinstance(item, str) and _RELATIVE_WINDOW.match(item):
            problems.append(f"{label}: the relative window {item!r} can only be applied to a date column, not {data_type}")
        elif kind == "char" and not isinstance(item, str):
            problems.append(f"{label}: {item!r} is not text, and the column is {data_type}")

    if allowed_values and operator in _EQUALITY:
        unknown = [item for item in _values(value) if item not in allowed_values]
        if unknown:
            problems.append(
                f"{label}: {', '.join(map(repr, unknown))} is not a value this column can hold; "
                f"it allows only {', '.join(map(repr, allowed_values))}"
            )
    return problems
