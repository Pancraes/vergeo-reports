"""Parser exceptions.

Two failure levels, on purpose:

- ReportFormatError: the report as a whole is unusable (no header, no APP line, ...). The upload
  is rejected (HTTP 422) and nothing touches the database.
- MalformedEventLine: one diagnostic line looked like an event but broke the grammar. The report
  parser catches it, records a parse warning, and keeps going. One corrupted line must not throw
  away an otherwise useful 8,000-line report.
"""


class ReportFormatError(ValueError):
    """The report's required structure is missing or invalid."""


class MalformedEventLine(ValueError):
    """A line that starts like an event (a date) but violates the event grammar."""

    def __init__(self, line_no: int, reason: str):
        super().__init__(f"line {line_no}: {reason}")
        self.line_no = line_no
        self.reason = reason
