"""Vergeo problem-report parser. Pure Python: no FastAPI, no database."""

from .errors import MalformedEventLine, ReportFormatError
from .events import ParsedEvent, parse_event_line
from .report import PARSER_VERSION, ParsedReport, parse_report

__all__ = [
    "PARSER_VERSION",
    "MalformedEventLine",
    "ParsedEvent",
    "ParsedReport",
    "ReportFormatError",
    "parse_event_line",
    "parse_report",
]
