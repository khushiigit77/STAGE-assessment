"""Catalogue and episode index.

Catalogue text (titles, descriptions) is untrusted content. It is stored for display and audit only; nothing in it is
ever interpreted. ``Title.untrusted_text_flag`` marks descriptions that contain instruction-like text so the audit trail
shows that such text was present and ignored.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from .config import Pack, parse_ts
from .data_loader import ValidationReport

INSTRUCTION_PATTERN = re.compile(
    r"(ignore (your|all|previous)|system note|assistant instruction|skip all checks|pre-approved|for ai agents)",
    re.IGNORECASE)


@dataclass(frozen=True)
class Title:
    title_id: str
    slug: str
    name: str
    dialect: str
    language: str
    content_type: str
    genres: tuple[str, ...]
    release_date: date | None
    available_from: datetime
    available_until: datetime | None
    premium: bool
    description: str
    untrusted_text_flag: bool


class Catalogue:
    def __init__(self, titles: dict[str, Title], episodes: dict[str, list[tuple[int, int, str, int]]]):
        self.titles = titles
        self.episodes = episodes
        self.withdrawn: dict[str, datetime] = {}
        self.by_dialect: dict[str, list[Title]] = defaultdict(list)
        self.by_slug: dict[str, list[Title]] = defaultdict(list)
        for t in titles.values():
            self.by_dialect[t.dialect].append(t)
            self.by_slug[t.slug].append(t)
        self.dialects = frozenset(self.by_dialect)

    @classmethod
    def load(cls, pack: Pack, report: ValidationReport) -> "Catalogue":
        titles: dict[str, Title] = {}
        with open(pack.path("data", "catalogue.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    t = Title(
                        title_id=r["title_id"], slug=r["content_slug"], name=r["title"], dialect=r["dialect"],
                        language=r["language"], content_type=r["content_type"],
                        genres=tuple(g for g in r["genres"].split("|") if g),
                        release_date=date.fromisoformat(r["release_date"]) if r["release_date"] else None,
                        available_from=parse_ts(r["available_from"]),
                        available_until=parse_ts(r["available_until"]) if r["available_until"] else None,
                        premium=r["is_premium"].strip().lower() == "true", description=r["description"],
                        untrusted_text_flag=bool(INSTRUCTION_PATTERN.search(r["description"]) or
                                                 INSTRUCTION_PATTERN.search(r["title"])))
                except (KeyError, ValueError, TypeError) as exc:
                    report.add("catalogue_invalid_row", {"title_id": r.get("title_id"), "error": str(exc)})
                    continue
                if t.available_from is None:
                    report.add("catalogue_missing_available_from", t.title_id)
                    continue
                if t.title_id in titles:
                    report.add("catalogue_duplicate_title_id", t.title_id)
                    continue
                if t.untrusted_text_flag:
                    report.add("catalogue_instruction_like_text_ignored", t.title_id)
                titles[t.title_id] = t
        episodes: dict[str, list] = defaultdict(list)
        with open(pack.path("data", "episodes.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    episodes[r["content_slug"]].append(
                        (int(r["season"]), int(r["episode"]), r["episode_id"], int(r["minutes"])))
                except (KeyError, ValueError) as exc:
                    report.add("episode_invalid_row", {"episode_id": r.get("episode_id"), "error": str(exc)})
        for v in episodes.values():
            v.sort()
        return cls(titles, dict(episodes))

    def withdraw(self, title_id: str, at: datetime) -> None:
        """Apply a ``title_withdrawn`` control event: the title is unavailable from ``at`` on."""
        prev = self.withdrawn.get(title_id)
        self.withdrawn[title_id] = at if prev is None else min(prev, at)

    def available(self, title: Title | None, at: datetime) -> bool:
        if title is None or title.available_from > at:
            return False
        if title.available_until is not None and at >= title.available_until:
            return False
        w = self.withdrawn.get(title.title_id)
        return not (w is not None and at >= w)

    def available_through(self, title: Title, start: datetime, end: datetime) -> bool:
        """Available at ``start`` and not scheduled to end before ``end`` (known end dates only)."""
        if not self.available(title, start):
            return False
        ends = [d for d in (title.available_until, self.withdrawn.get(title.title_id)) if d is not None]
        return not any(d < end for d in ends)

    def episode_count(self, slug: str) -> int:
        return len(self.episodes.get(slug, ()))

    def episode_index(self, slug: str, episode_id: str) -> int | None:
        for i, ep in enumerate(self.episodes.get(slug, ())):
            if ep[2] == episode_id:
                return i
        return None

    def pick_variant(self, slug: str, dialect: str, language: str | None, at: datetime) -> Title | None:
        """The available title for ``slug`` in ``dialect``, preferring the viewer's app language."""
        options = [t for t in self.by_slug.get(slug, ()) if t.dialect == dialect and self.available(t, at)]
        if not options:
            return None
        options.sort(key=lambda t: (t.language != language, t.title_id))
        return options[0]
