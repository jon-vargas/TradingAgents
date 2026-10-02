"""
TradingAgents Reporting Module

Professional-grade PDF and HTML report generation for trading research.
"""

from .pdf_generator import (
    generate_pdf_report,
    generate_html_report,
    generate_report,
)
from .database import (
    ResearchDatabase,
    get_db,
    Analysis,
    AgentReport,
    BacktestRun,
    create_analysis_from_state,
)

__all__ = [
    # Report generation
    "generate_pdf_report",
    "generate_html_report",
    "generate_report",
    # Database
    "ResearchDatabase",
    "get_db",
    "Analysis",
    "AgentReport",
    "BacktestRun",
    "create_analysis_from_state",
]
