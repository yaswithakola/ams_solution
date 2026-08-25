"""
Deterministic date handling for report requests.
"""
from datetime import date, timedelta
from typing import Optional

from common.models import ReportRequestDetails, ResolvedReportDateRange


class ReportDateResolver:
    def __init__(self, today: Optional[date] = None):
        self.today = today or date.today()

    def resolve(self, request: ReportRequestDetails) -> ResolvedReportDateRange:
        label = (request.date_range or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "last_week": "previous_week",
            "last_month": "previous_month",
        }
        label = aliases.get(label, label)

        if label == "today":
            return ResolvedReportDateRange(start_date=self.today, end_date=self.today, label=label)

        if label == "yesterday":
            yesterday = self.today - timedelta(days=1)
            return ResolvedReportDateRange(start_date=yesterday, end_date=yesterday, label=label)

        if label == "previous_week":
            this_week_start = self.today - timedelta(days=self.today.weekday())
            start = this_week_start - timedelta(days=7)
            end = this_week_start - timedelta(days=1)
            return ResolvedReportDateRange(start_date=start, end_date=end, label=label)

        if label == "previous_month":
            first_this_month = self.today.replace(day=1)
            end = first_this_month - timedelta(days=1)
            start = end.replace(day=1)
            return ResolvedReportDateRange(start_date=start, end_date=end, label=label)

        if label == "custom":
            if not request.start_date or not request.end_date:
                raise ValueError("Custom report date range requires start_date and end_date")
            start = date.fromisoformat(request.start_date)
            end = date.fromisoformat(request.end_date)
            if start > end:
                raise ValueError("Report start_date must be on or before end_date")
            return ResolvedReportDateRange(start_date=start, end_date=end, label=label)

        raise ValueError(f"Unsupported report date range: {request.date_range}")
