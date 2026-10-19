"""Small hand-built world that uses the pack's real goal, intervention and pricing configs."""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta

import pytest

from one_user_one_agent.catalogue import Catalogue, Title
from one_user_one_agent.config import AgentConfig, Pack, UTC, find_pack, iso
from one_user_one_agent.data_loader import ProfileRecord, ValidationReport, normalise_event
from one_user_one_agent.decision_engine import Agent, DependencyFaults
from one_user_one_agent.evidence import ViewerHistory
from one_user_one_agent.goals import GoalRules
from one_user_one_agent.model_provider import MockModelProvider
from one_user_one_agent.state_store import StateStore

PACK_ROOT = find_pack(start=os.path.dirname(__file__))  # OUA_PACK, or the pack that contains this submission
NOW = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)  # 11:30 IST, outside quiet hours


def title(tid, slug, dialect="d1", ctype="show", genres=("Drama",), premium=False, desc="A story.", until=None,
          avail_from=datetime(2025, 1, 1, tzinfo=UTC), release=date(2025, 1, 1), lang="hin"):
    from one_user_one_agent.catalogue import INSTRUCTION_PATTERN
    return Title(tid, slug, f"Name {tid}", dialect, lang, ctype, tuple(genres), release, avail_from, until, premium,
                 desc, bool(INSTRUCTION_PATTERN.search(desc)))


def make_catalogue(extra=()):
    ts = [title(f"T1{i:02d}", f"s1-{i}") for i in range(12)]
    ts += [title("T201", "s2-1", dialect="d2"), title("T202", "s2-2", dialect="d2")]
    ts += list(extra)
    eps = {t.slug: [(1, n, f"{t.slug}-s1e{n}", 10) for n in range(1, 6)] for t in ts}
    return Catalogue({t.title_id: t for t in ts}, eps)


def ev(uid, event, at, dialect="d1", props=None, lag_s=0.0, eid=None, session="s-1", deliver=True, lang="hin"):
    occ = at - timedelta(seconds=lag_s)
    raw = {"event_id": eid or str(uuid.uuid4()), "user_id": uid, "event": event, "occurred_at": iso(occ),
           "sent_at": iso(at), "received_at": iso(at + timedelta(seconds=1)), "session_id": session,
           "app_dialect": dialect, "app_language": lang, "platform": "android", "app_version": "6.1.4",
           "properties": props or {}}
    if deliver:
        raw["deliver_at"] = raw["received_at"]
    return normalise_event(raw, ValidationReport())


def profile(uid, status="never", dialect="d1", push="true", wa="false", signup="2025-01-01T00:00:00.000Z",
            updated=datetime(2026, 9, 29, tzinfo=UTC)):
    row = {"user_id": uid, "signup_at": signup, "subscription_status": status, "plan_started_at": "",
           "plan_ends_at": "", "preferred_dialect": dialect, "preferred_language": "hin",
           "notifications_opt_in": push, "whatsapp_opt_in": wa, "platform": "android", "updated_at": iso(updated),
           "snapshot_at": "2026-09-30T00:00:00.000Z", "_updated": updated, "_snapshot": None,
           "_signup": datetime.fromisoformat(signup.replace("Z", "+00:00"))}
    return ProfileRecord(row, updated, None, [])


class World:
    def __init__(self, catalogue=None, faults=None, config=AgentConfig()):
        pack = Pack(PACK_ROOT)
        self.catalogue = catalogue or make_catalogue()
        self.history = ViewerHistory()
        self.history.history_start = datetime(2026, 9, 1, tzinfo=UTC)
        for t in list(self.catalogue.titles.values()):
            self.history.popularity[t.dialect][t.slug] += 1
        self.profiles, self.arms = {}, {}
        self.rules = GoalRules(pack.load_json("config", "goals.json"))
        self.interventions = pack.load_json("config", "interventions.json")
        self.pricing = pack.load_json("config", "pricing.json")
        self.store = StateStore(":memory:")
        self.faults = DependencyFaults()
        self.provider = MockModelProvider(faults or {}, [], 0.4)
        self.config = config
        self.agent = Agent(catalogue=self.catalogue, history=self.history, profiles=self.profiles, arms=self.arms,
                           rules=self.rules, interventions=self.interventions, pricing=self.pricing, store=self.store,
                           provider=self.provider, faults=self.faults, config=config)

    def viewer(self, uid, arm="agent", prof=None, events=()):
        self.arms[uid] = arm
        if prof is not None:
            self.profiles[uid] = prof
        for e in events:
            self.history.add(e)

    def resumer(self, uid="U-1", arm="agent", **pkw):
        """A viewer with an unfinished d1 series last played 5 days ago -> G07."""
        played = NOW - timedelta(days=5)
        self.viewer(uid, arm, profile(uid, **pkw), [
            ev(uid, "app_open", played - timedelta(minutes=1)),
            ev(uid, "play_started", played, props={"title_id": "T100", "episode_id": "s1-0-s1e1"})])


@pytest.fixture
def world():
    return World()
