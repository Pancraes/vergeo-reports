"""Read-only access to the Vergeo field corpus (docs/ml/FIELD_CORPUS.md in the Vergeo repo).

The corpus is the hand-written record of past rides: field chains, classifications, root causes.
The triage agent may search it for similar incidents. During evaluation it must not see the
answer to the case it is grading, so CorpusIndex.visible_before() hides every section dated on or
after the case's ride date: the agent only knows what had been written before that ride.

Sections are split on level-2 headings ("## 2026-11-15 evening: ..."); ### subsections stay with
their parent, because they are follow-ups to that ride.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_HEADING_DATE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})")
DEFAULT_CORPUS = Path(
    os.environ.get(
        "VERGEO_FIELD_CORPUS",
        str(Path.home() / "AndroidStudioProjects" / "Vergeo" / "docs" / "ml" / "FIELD_CORPUS.md"),
    )
)


@dataclass(frozen=True)
class Section:
    index: int
    title: str
    ride_date: date | None
    body: str


class CorpusIndex:
    def __init__(self, sections: list[Section]):
        self.sections = sections

    @classmethod
    def load(cls, path: Path = DEFAULT_CORPUS) -> CorpusIndex:
        if not path.exists():
            return cls([])
        return cls.parse(path.read_text(encoding="utf-8"))

    @classmethod
    def parse(cls, text: str) -> CorpusIndex:
        sections: list[Section] = []
        title, lines = None, []

        def flush():
            if title is not None:
                m = _HEADING_DATE.match(title)
                d = date.fromisoformat(m.group(1)) if m else None
                sections.append(
                    Section(len(sections), title.lstrip("# ").strip(), d, "\n".join(lines).strip())
                )

        for line in text.splitlines():
            if line.startswith("## "):
                flush()
                title, lines = line, []
            elif title is not None:
                lines.append(line)
        flush()
        return cls(sections)

    def visible_before(self, cutoff: date | None) -> CorpusIndex:
        if cutoff is None:
            return self
        return CorpusIndex([s for s in self.sections if s.ride_date and s.ride_date < cutoff])

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Rank sections by how many query words they contain (title words count double)."""
        words = [w for w in re.findall(r"[a-z0-9_]+", query.lower()) if len(w) > 2]
        scored = []
        for s in self.sections:
            body, title = s.body.lower(), s.title.lower()
            score = sum(body.count(w) + 2 * title.count(w) for w in words)
            if score:
                scored.append((score, s))
        scored.sort(key=lambda t: -t[0])
        return [
            {"section": s.index, "heading": s.title, "score": score, "preview": s.body[:400]}
            for score, s in scored[:limit]
        ]

    def read(self, index: int, max_chars: int = 6000) -> dict:
        for s in self.sections:
            if s.index == index:
                return {
                    "section": s.index,
                    "heading": s.title,
                    "body": s.body[:max_chars],
                    "truncated": len(s.body) > max_chars,
                }
        return {"error": f"no visible section {index}"}

    def titles(self) -> list[dict]:
        return [{"section": s.index, "heading": s.title} for s in self.sections]
