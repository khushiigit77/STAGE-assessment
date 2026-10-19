"""Durable viewer state in SQLite.

* ``viewers``: one authoritative row per viewer (active goal, live page, message counters, learning state) with a
  ``version`` column used for compare-and-set. Work computed from an old version cannot overwrite newer state.
* ``processed_triggers``: idempotency ledger. A re-delivered trigger maps to the original decision.
* ``goals`` / ``goal_revisions``: every goal commitment and every later change (rule corrections, closure), so history
  is never rewritten silently.
* ``actions``: published actions with a unique idempotency key (trigger, type).

Each decision is committed in a single transaction.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS viewers (user_id TEXT PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL,
                                    updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processed_triggers (event_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
                                               user_id TEXT NOT NULL, processed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS goals (goal_record_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, goal_id TEXT NOT NULL,
                                  status TEXT NOT NULL, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS goals_by_goal_status ON goals(goal_id, status);
CREATE INDEX IF NOT EXISTS goals_by_user ON goals(user_id);
CREATE TABLE IF NOT EXISTS goal_revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, goal_record_id TEXT NOT NULL,
                                           at TEXT NOT NULL, change TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions (action_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, user_id TEXT NOT NULL,
                                    type TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS actions_by_user ON actions(user_id);
"""


class StaleStateError(RuntimeError):
    """The viewer row changed after it was read; the decision must not be committed."""


def empty_state(user_id: str) -> dict:
    return {"user_id": user_id, "active_goal_record_id": None, "last_page": None, "message_days": {},
            "in_app_days": {}, "last_decision_id": None, "last_trigger_received_at": None,
            "learning": {"goals_committed": 0, "succeeded": 0, "failed": 0, "inconclusive": 0, "pages_published": 0,
                         "messages_published": 0, "messages_delivered": 0, "messages_failed": 0,
                         "outcomes_by_goal": {}}}


class StateStore:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)  # explicit BEGIN/COMMIT in tx()
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self):
        try:
            self.db.execute("BEGIN")
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # --- viewers
    def read_viewer(self, user_id: str) -> tuple[dict, int]:
        row = self.db.execute("SELECT state, version FROM viewers WHERE user_id=?", (user_id,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (empty_state(user_id), 0)

    def _write_viewer(self, db, user_id: str, state: dict, expected_version: int, now_iso: str) -> int:
        if expected_version == 0:
            try:
                db.execute("INSERT INTO viewers VALUES (?,?,?,?)", (user_id, 1, json.dumps(state), now_iso))
            except sqlite3.IntegrityError as exc:
                raise StaleStateError(f"viewer {user_id} was created concurrently") from exc
            return 1
        cur = db.execute("UPDATE viewers SET state=?, version=version+1, updated_at=? WHERE user_id=? AND version=?",
                         (json.dumps(state), now_iso, user_id, expected_version))
        if cur.rowcount != 1:
            raise StaleStateError(f"viewer {user_id} changed since version {expected_version}")
        return expected_version + 1

    # --- idempotency
    def decision_for_trigger(self, event_id: str) -> str | None:
        row = self.db.execute("SELECT decision_id FROM processed_triggers WHERE event_id=?", (event_id,)).fetchone()
        return row[0] if row else None

    # --- goals
    def get_goal(self, goal_record_id: str | None) -> dict | None:
        if not goal_record_id:
            return None
        row = self.db.execute("SELECT record FROM goals WHERE goal_record_id=?", (goal_record_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def open_goals(self, goal_id: str) -> list[dict]:
        rows = self.db.execute("SELECT record FROM goals WHERE goal_id=? AND status='ACTIVE'", (goal_id,))
        return [json.loads(r[0]) for r in rows]

    def all_goals(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT record FROM goals ORDER BY rowid")]

    def upsert_goal(self, db, goal: dict) -> None:
        db.execute("INSERT INTO goals VALUES (?,?,?,?,?) ON CONFLICT(goal_record_id) DO UPDATE SET status=excluded.status,"
                   " record=excluded.record", (goal["goal_record_id"], goal["user_id"], goal["goal_id"],
                                               goal["status"], json.dumps(goal)))

    def add_revision(self, db, goal_record_id: str, at: str, change: str, detail: dict) -> None:
        db.execute("INSERT INTO goal_revisions (goal_record_id, at, change, detail) VALUES (?,?,?,?)",
                   (goal_record_id, at, change, json.dumps(detail)))

    def save_goal(self, goal: dict, at: str, change: str, detail: dict) -> None:
        """Update one goal outside a decision (rule corrections, final outcome) and log the revision."""
        with self.tx() as db:
            self.upsert_goal(db, goal)
            self.add_revision(db, goal["goal_record_id"], at, change, detail)

    # --- actions
    def actions_for_user(self, user_id: str) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT record FROM actions WHERE user_id=? ORDER BY rowid",
                                                          (user_id,))]

    def commit_decision(self, *, user_id: str, expected_version: int, state: dict, decision_id: str,
                        trigger_event_id: str, now_iso: str, goals: list[tuple[dict, str, dict]],
                        actions: list[dict]) -> int:
        """Atomically record the trigger, goal changes, actions and the new viewer state."""
        with self.tx() as db:
            try:
                db.execute("INSERT INTO processed_triggers VALUES (?,?,?,?)",
                           (trigger_event_id, decision_id, user_id, now_iso))
            except sqlite3.IntegrityError as exc:
                raise StaleStateError(f"trigger {trigger_event_id} already processed") from exc
            for goal, change, detail in goals:
                self.upsert_goal(db, goal)
                self.add_revision(db, goal["goal_record_id"], now_iso, change, detail)
            for a in actions:
                db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?)",
                           (a["action_id"], a["decision_id"], a["user_id"], a["type"],
                            f"{a['trigger_event_id']}:{a['type']}", json.dumps(a)))
            return self._write_viewer(db, user_id, state, expected_version, now_iso)

    def record_trigger_only(self, *, decision_id: str, trigger_event_id: str, user_id: str, now_iso: str) -> None:
        """Mark a trigger as handled when no viewer state change is allowed (e.g. failed dependency)."""
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO processed_triggers VALUES (?,?,?,?)",
                       (trigger_event_id, decision_id, user_id, now_iso))

    def update_viewer(self, user_id: str, mutate, now_iso: str) -> None:
        """Read-modify-write with compare-and-set, used by the outcome tracker."""
        for _ in range(3):
            state, v = self.read_viewer(user_id)
            mutate(state)
            try:
                with self.tx() as db:
                    self._write_viewer(db, user_id, state, v, now_iso)
                return
            except StaleStateError:
                continue
        raise StaleStateError(f"could not update viewer {user_id}")

    # --- export
    def export(self, out_dir: str) -> None:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "viewers.jsonl"), "w", encoding="utf-8") as f:
            for uid, ver, state, upd in self.db.execute("SELECT * FROM viewers ORDER BY user_id"):
                f.write(json.dumps({"user_id": uid, "version": ver, "updated_at": upd, "state": json.loads(state)}) + "\n")
        with open(os.path.join(out_dir, "goals.jsonl"), "w", encoding="utf-8") as f:
            for g in self.all_goals():
                f.write(json.dumps(g) + "\n")
        with open(os.path.join(out_dir, "goal_revisions.jsonl"), "w", encoding="utf-8") as f:
            for gid, at, change, detail in self.db.execute(
                    "SELECT goal_record_id, at, change, detail FROM goal_revisions ORDER BY id"):
                f.write(json.dumps({"goal_record_id": gid, "at": at, "change": change, "detail": json.loads(detail)}) + "\n")
