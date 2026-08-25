"""
SQL safety checks and parameter preparation for approved reports.
"""
import re
from typing import Any, Dict

from common.models import ReportDefinition, ReportRequestDetails, ResolvedReportDateRange

_BLOCKED_SQL = re.compile(
    r"\b(ALTER|CALL|CREATE|DELETE|DROP|EXEC|EXECUTE|GRANT|INSERT|MERGE|REVOKE|TRUNCATE|UPDATE)\b",
    re.IGNORECASE,
)


def validate_select_only_sql(sql: str) -> str:
    cleaned = _strip_sql_comments(sql).strip()
    if not cleaned:
        raise ValueError("Report SQL is empty")

    if ";" in cleaned.rstrip(";"):
        raise ValueError("Report SQL must contain a single statement")

    statement = cleaned.rstrip(";").lstrip()
    if not re.match(r"^(SELECT|WITH)\b", statement, flags=re.IGNORECASE):
        raise ValueError("Report SQL must start with SELECT or WITH")

    if _BLOCKED_SQL.search(statement):
        raise ValueError("Report SQL contains a blocked write/admin statement")

    return sql


def build_report_parameters(
    request: ReportRequestDetails,
    definition: ReportDefinition,
    date_range: ResolvedReportDateRange,
) -> Dict[str, Any]:
    allowed_filters = {item.name: item for item in definition.allowed_filters}
    unknown_filters = sorted(set(request.filters) - set(allowed_filters))
    if unknown_filters:
        raise ValueError(f"Unsupported filters for {definition.report_name}: {', '.join(unknown_filters)}")

    params = {
        "start_date": date_range.start_date,
        "end_date": date_range.end_date,
    }
    for filter_definition in definition.allowed_filters:
        raw_value = request.filters.get(filter_definition.name)
        if raw_value in (None, "") and filter_definition.required:
            raise ValueError(f"Missing required filter: {filter_definition.name}")
        params[filter_definition.name] = _coerce_filter(raw_value, filter_definition)
    return params


def _coerce_filter(value: Any, filter_definition) -> Any:
    if value in (None, ""):
        return None

    if filter_definition.value_type == "string":
        result = str(value).strip()
    elif filter_definition.value_type == "integer":
        result = int(value)
    elif filter_definition.value_type == "number":
        result = float(value)
    elif filter_definition.value_type == "boolean":
        if isinstance(value, bool):
            result = value
        elif str(value).strip().lower() in ("true", "false"):
            result = str(value).strip().lower() == "true"
        else:
            raise ValueError(f"Filter {filter_definition.name} must be true or false")
    else:
        raise ValueError(f"Unsupported filter type: {filter_definition.value_type}")

    if filter_definition.allowed_values and result not in filter_definition.allowed_values:
        raise ValueError(f"Filter {filter_definition.name} has unsupported value: {result}")
    return result


def _strip_sql_comments(sql: str) -> str:
    no_block_comments = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return re.sub(r"--.*?$", "", no_block_comments, flags=re.MULTILINE)
