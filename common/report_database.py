"""
Read-only PostgreSQL execution for approved reports.
"""
import contextlib
from typing import Dict, List

from common.report_sql import validate_select_only_sql


class PostgresReportClient:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def query(self, sql: str, params: Dict) -> List[Dict]:
        validate_select_only_sql(sql)
        with contextlib.closing(self._connect()) as conn:
            conn.set_session(readonly=True, autocommit=False)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            conn.rollback()
        return [dict(row) for row in rows]

    def _connect(self):
        import psycopg2
        import psycopg2.extras

        return psycopg2.connect(self.dsn, cursor_factory=psycopg2.extras.RealDictCursor)
