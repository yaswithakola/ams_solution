"""
Compatibility wrapper for report SQL validation and parameters.
"""

from agents.report_generation_agent import build_report_parameters, validate_select_only_sql

__all__ = ["build_report_parameters", "validate_select_only_sql"]
