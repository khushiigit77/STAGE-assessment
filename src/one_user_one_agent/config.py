"""Configuration, pack paths and time helpers.

All tunable limits live in ``AgentConfig`` so they are explicit, documented and testable.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

log = logging.getLogger("oua")


def parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are rejected (the pack is always zoned)."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value!r}")
    return dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    """Format as the pack does: millisecond UTC with a trailing Z."""
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ist_day(dt: datetime) -> str:
    return dt.astimezone(IST).date().isoformat()


def in_quiet_hours(dt: datetime, start: str = "22:00", end: str = "08:00") -> bool:
    """True when ``dt`` falls inside the IST quiet window [start, end) that wraps midnight."""
    t = dt.astimezone(IST).time()
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    s, e = sh * 60 + sm, eh * 60 + em
    m = t.hour * 60 + t.minute
    return m >= s or m < e if s > e else s <= m < e


@dataclass(frozen=True)
class AgentConfig:
    """Explicit operating limits. Defaults are the values used for the sample run."""

    decision_budget_s: float = 60.0          # publish within 60 s of the trigger
    max_model_calls: int = 8
    max_tool_calls: int = 20
    model_timeout_s: float = 5.0             # a 45 s model hang is cut here, then the agent falls back
    model_nominal_latency_s: float = 0.4     # simulated latency of a healthy mock model call
    model_retries_on_unavailable: int = 1
    store_read_timeout_s: float = 2.0
    store_read_attempts: int = 2
    live_visit_max_lag_s: float = 600.0      # corrected lag above this = offline-buffered, not a live visit
    profile_stale_days: float = 14.0         # profile facts older than this lower confidence
    lapse_recent_days: int = 30              # G06 "recently lapsed" assumption
    page_refresh_after_min: float = 100.0    # republish a live page only when it is about to expire
    push_local_hour: int = 19                # next-day push send time (IST), outside quiet hours
    history_days_popularity: int = 14
    new_release_days: int = 30
    supported_message_goals: tuple = ("G07", "G12")
    in_app_goals: tuple = ("G04", "G06")
    goal_priority: tuple = ("G04", "G01", "G03", "G07", "G12", "G02", "G06", "G09", "G08", "G05")
    excluded_goals: dict = field(default_factory=lambda: {
        "G10": "success condition 'engages meaningfully' has no event or threshold (underspecified)",
        "G11": "success event 'tv_app_open' does not exist in any supplied stream (unavailable signal)",
    })


def find_pack(explicit: str | None = None, start: str | None = None) -> str:
    """Locate the challenge pack root (the folder holding ``simulator/simulate_outcomes.py``).

    Order: explicit ``--pack``, the ``OUA_PACK`` environment variable, then ``start`` (default: current directory)
    and its parents. The submission normally sits inside the pack, so the parent is found automatically.
    """
    marker = os.path.join("simulator", "simulate_outcomes.py")
    for cand in (explicit, os.environ.get("OUA_PACK")):
        if cand:
            if not os.path.exists(os.path.join(cand, marker)):
                raise FileNotFoundError(f"{cand!r} is not a challenge pack (missing {marker})")
            return os.path.abspath(cand)
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, marker)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise FileNotFoundError("challenge pack not found; pass --pack <path> or set OUA_PACK")
        d = parent


@dataclass
class Pack:
    """Locations of the supplied challenge files (never written to)."""

    root: str

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def load_json(self, *parts: str) -> dict:
        with open(self.path(*parts), encoding="utf-8") as f:
            return json.load(f)
