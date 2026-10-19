"""Close the outcome loop from the simulator's ``outcome_events.jsonl``.

* Each goal record is judged with ``goals.judge_goal`` under its *current* rule version (corrections that applied to
  it while open). Goals closed before a correction keep their original version. Where the version changed, the
  verdict under the commit-time version is recorded too, so nothing is rewritten silently.
* Message delivery is reported only from ``message_delivered`` / ``message_failed`` events keyed by ``action_id``.
  Without one of those events the delivery status is ``UNCONFIRMED``.
* Viewer learning state (succeeded/failed/inconclusive by goal, delivery counts) is updated in the state store.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .catalogue import Catalogue
from .config import iso
from .data_loader import ValidationReport, load_events
from .goals import GoalRules, judge_goal
from .state_store import StateStore


def load_outcome_events(path: str, report: ValidationReport) -> dict[str, list[dict]]:
    evs, _ = load_events(path, report, kind="outcome")
    by_user: dict[str, list[dict]] = defaultdict(list)
    for e in evs:
        by_user[e["user_id"]].append(e)
    return by_user


def track_outcomes(*, store: StateStore, rules: GoalRules, catalogue: Catalogue, outcome_by_user: dict[str, list[dict]],
                   covered_users: set[str], observed_until: datetime, actions: list[dict], judged_at: datetime) -> list[dict]:
    records = []
    for g in store.all_goals():
        evs = outcome_by_user.get(g["user_id"], [])
        verdict = judge_goal(g, evs, rules, catalogue, observed_until, covered=g["user_id"] in covered_users)
        if g["status"] == "SUPERSEDED" and verdict["status"] != "SUCCEEDED":
            verdict.update(status="INCONCLUSIVE", reason=f"goal superseded before its deadline: {g['close_reason']}")
        verdict.update(record_type="goal_outcome", goal_name=g["name"], judged_at=iso(judged_at),
                       goal_status_at_end_of_replay=g["status"],
                       rule_version_at_commit=g["rule_version_at_commit"],
                       uncertainty=("simulated outcomes, single seed; attribution to the agent is only valid at arm "
                                    "level (see impact_report.md), never for an individual viewer"))
        if g["rule_version_current"] != g["rule_version_at_commit"]:
            old = judge_goal(g, evs, rules, catalogue, observed_until, covered=g["user_id"] in covered_users,
                             version=g["rule_version_at_commit"])
            verdict["verdict_under_commit_version"] = {"status": old["status"], "reason": old["reason"]}
        records.append(verdict)
        final = {"SUCCEEDED": "SUCCEEDED", "FAILED": "FAILED"}.get(verdict["status"], "INCONCLUSIVE")
        g2 = dict(g, final_outcome=final, final_reason=verdict["reason"])
        store.save_goal(g2, iso(judged_at), "outcome_judged", {"status": final, "rule_version": verdict["rule_version"]})

        def learn(state, v=verdict, gid=g["goal_id"]):
            key = {"SUCCEEDED": "succeeded", "FAILED": "failed"}.get(v["status"], "inconclusive")
            state["learning"][key] += 1
            by = state["learning"]["outcomes_by_goal"].setdefault(gid, {"succeeded": 0, "failed": 0, "inconclusive": 0})
            by[key] += 1
        store.update_viewer(g["user_id"], learn, iso(judged_at))

    delivery = {}
    for evs in outcome_by_user.values():
        for e in evs:
            aid = (e.get("properties") or {}).get("action_id")
            if aid and e["event"] in ("message_delivered", "message_failed"):
                delivery[aid] = (e["event"], e["properties"].get("reason"), iso(e["_event_time"]))
    for a in actions:
        if a["type"] not in ("push", "whatsapp", "in_app_message"):
            continue
        ev = delivery.get(a["action_id"])
        status = "UNCONFIRMED" if ev is None else ("DELIVERED" if ev[0] == "message_delivered" else "FAILED")
        records.append({"record_type": "action_delivery", "action_id": a["action_id"], "user_id": a["user_id"],
                        "type": a["type"], "delivery_status": status, "reason": ev and ev[1],
                        "confirmed_at": ev and ev[2],
                        "note": "in-app messages have no delivery event in the simulator" if a["type"] ==
                                "in_app_message" else None})
        if status != "UNCONFIRMED":
            def learn_msg(state, s=status):
                state["learning"]["messages_delivered" if s == "DELIVERED" else "messages_failed"] += 1
            store.update_viewer(a["user_id"], learn_msg, iso(judged_at))
    return records
