"""
Approved report catalog.

The LLM can ask for a report by name, but this catalog decides which SQL
file and filters are allowed for that report.
"""
import json
from pathlib import Path
from typing import Dict

from common.models import ReportDefinition
from common.report_sql import validate_select_only_sql


class ReportCatalog:
    def __init__(self, catalog_path: str, sql_dir: str):
        self.catalog_path = Path(catalog_path)
        self.sql_dir = Path(sql_dir)
        self._reports = self._load_reports()

    def get(self, report_name: str) -> ReportDefinition:
        key = normalize_report_name(report_name)
        if key not in self._reports:
            raise KeyError(f"Unknown report: {report_name}")
        return self._reports[key]

    def load_sql(self, definition: ReportDefinition) -> str:
        sql_path = (self.sql_dir / definition.sql_file).resolve()
        sql_root = self.sql_dir.resolve()

        if sql_path != sql_root and sql_root not in sql_path.parents:
            raise ValueError(f"SQL file escapes report SQL directory: {definition.sql_file}")

        sql = sql_path.read_text(encoding="utf-8")
        validate_select_only_sql(sql)
        return sql

    def _load_reports(self) -> Dict[str, ReportDefinition]:
        raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        reports = {}
        for item in raw.get("reports", []):
            definition = ReportDefinition(**item)
            reports[normalize_report_name(definition.report_name)] = definition
        return reports


def normalize_report_name(value: str) -> str:
    return (value or "").strip().lower().replace("-", "_").replace(" ", "_")
