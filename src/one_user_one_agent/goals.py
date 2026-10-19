"""Goal catalogue: versioned rules, eligibility, selection and success judgement.

The same ``judge_goal`` function is used during replay (to notice a goal that has already been reached) and after
the simulator run (final outcomes), so there is one definition of success per rule version.

Documented assumptions (also in submission/ARCHITECTURE.md):

* G06 "recently lapsed" is underspecified. Eligibility requires an observed ``subscription_lapsed`` event in the last 30 days.
  The event history covers 30 days, so a lapse older than that cannot be dated and is not treated as recent.
* G10 "engages meaningfully" has no success event or threshold. It is never selected; inventing a definition would
  produce unfalsifiable "successes".
* G11 depends on ``tv_app_open``, which no supplied stream contains. It is never selected.
* "Has never done X" (G01, G02) can only be proven inside the observable history window. A viewer qualifies only when
  their signup falls inside that window; otherwise eligibility is unverifiable and treated as not eligible.
* G12 "had stopped visiting" requires a signup at least 14 days before the trigger, so that the gap can be observed.
* Goal priority is not specified by the brief. The order puts time-sensitive, content-actionable goals first:
  ``AgentConfig.goal_priority``.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta

from .catalogue import Catalogue
from .config import AgentConfig, iso, ist_day, parse_ts
from .evidence import Snapshot

BASE_VERSION_PREFIX = "goals@"


@dataclass
class Eligibility:
    goal_id: str
    eligible: bool
    basis: list[str]
    params: dict


class GoalRules:
    """Goal definitions plus the corrections applied so far (in arrival order)."""

    def __init__(self, goals_json: dict):
        self.base_version = BASE_VERSION_PREFIX + goals_json["version"]
        self.goals = {g["goal_id"]: g for g in goals_json["goals"]}
        self.corrections: list[dict] = []

    def version(self, goal_id: str, at: datetime | None = None) -> str:
        """Rule version id for ``goal_id`` as of ``at`` (None = latest)."""
        v = self.base_version
        for c in self.corrections:
            if c["goal_id"] == goal_id and (at is None or c["_at"] <= at):
                v += f"+{goal_id}@{c['at']}"
        return v

    def success_rule(self, goal_id: str, version: str) -> dict:
        rule = copy.deepcopy(self.goals[goal_id]["success"])
        for c in self.corrections:
            if c["goal_id"] == goal_id and f"+{goal_id}@{c['at']}" in version:
                rule.update(c["new_success"])
        return rule

    def apply_correction(self, control: dict) -> str:
        c = dict(control)
        c.setdefault("_at", parse_ts(control["at"]))
        self.corrections.append(c)
        return self.version(c["goal_id"])


def _days_since(now: datetime, iso_ts: str | None) -> float | None:
    t = parse_ts(iso_ts) if iso_ts else None
    return None if t is None else (now - t).total_seconds() / 86400


def eligibility(goal_id: str, snap: Snapshot, now: datetime, config: AgentConfig) -> Eligibility:
    """Deterministic eligibility from the evidence snapshot. Never relies on model output."""
    f = snap.features
    status = snap.value("subscription_status")
    signup_days = _days_since(now, snap.value("signup_at"))
    hist_start = parse_ts(f.get("history_start")) if f.get("history_start") else None
    signup_t = parse_ts(snap.value("signup_at")) if snap.value("signup_at") else None
    observable_since_signup = bool(signup_t and hist_start and signup_t >= hist_start)
    basis: list[str] = []
    params: dict = {}

    def out(ok: bool) -> Eligibility:
        return Eligibility(goal_id, ok, basis, params)

    if goal_id in config.excluded_goals:
        basis.append(f"excluded: {config.excluded_goals[goal_id]}")
        return out(False)
    if goal_id == "G01":
        basis += [f"signup {snap.value('signup_at')} ({signup_days and round(signup_days, 2)} days ago)",
                  f"play_started observed: {f['ever_play_started']}"]
        return out(signup_days is not None and signup_days <= 7 and not f["ever_play_started"])
    if goal_id == "G02":
        basis += [f"episode_completed observed: {f['ever_episode_completed']}",
                  f"history observable since signup: {observable_since_signup}"]
        return out(not f["ever_episode_completed"] and observable_since_signup)
    if goal_id == "G03":
        basis.append(f"signup {snap.value('signup_at')}")
        return out(signup_days is not None and signup_days <= 2)
    if goal_id in ("G04", "G05", "G06", "G08", "G09"):
        allowed = {"G04": "trial_active", "G05": "never", "G06": "lapsed", "G08": "active", "G09": "active"}[goal_id]
        src = snap.facts["subscription_status"]
        basis.append(f"subscription_status={status} (source {src.source_type}:{src.source_ref})")
        if status != allowed:
            return out(False)
        if goal_id == "G05":
            basis.append(f"paid plan observed in history: {snap.value('paid_ever_observed')}")
            return out(not snap.value("paid_ever_observed"))
        if goal_id == "G06":
            lapse_days = _days_since(now, snap.value("lapsed_at"))
            basis.append(f"lapse event {snap.value('lapsed_at')} (assumption: recent = within "
                         f"{config.lapse_recent_days} days)")
            return out(lapse_days is not None and lapse_days <= config.lapse_recent_days)
        if goal_id == "G09":
            basis.append(f"play days in last 14: {len(f['play_days_last_14'])}")
            return out(len(f["play_days_last_14"]) <= 1)
        return out(True)
    if goal_id == "G07":
        cand = [s for s in f["unfinished_series"] if s["idle_days"] >= 3]
        if cand:
            params["target_slug"] = cand[0]["slug"]
            params["target_title_id"] = cand[0]["title_id"]
            params["next_episode_number"] = cand[0]["next_episode_number"]
            basis.append(f"unfinished {cand[0]['slug']}: next episode {cand[0]['next_episode_number']} of "
                         f"{cand[0]['episodes']}, idle {cand[0]['idle_days']} days")
        else:
            basis.append("no unfinished show/micro-drama idle for 3+ days")
        return out(bool(cand))
    if goal_id == "G12":
        basis += [f"app_open in 14 days before trigger: {f['app_opens_prev_14d']}",
                  f"signup {snap.value('signup_at')}"]
        return out(f["app_opens_prev_14d"] == 0 and signup_days is not None and signup_days >= 14)
    basis.append("unknown goal")
    return out(False)


def eligible_goals(snap: Snapshot, now: datetime, rules: GoalRules, config: AgentConfig) -> list[Eligibility]:
    """Eligible goals in priority order."""
    res = [eligibility(g, snap, now, config) for g in config.goal_priority if g in rules.goals]
    return [r for r in res if r.eligible]


def new_goal_record(goal_record_id: str, user_id: str, elig: Eligibility, rules: GoalRules, committed_at: datetime,
                    decision_id: str) -> dict:
    g = rules.goals[elig.goal_id]
    version = rules.version(elig.goal_id, committed_at)
    window = timedelta(hours=g["success"]["window_hours"])
    return {
        "goal_record_id": goal_record_id, "user_id": user_id, "goal_id": elig.goal_id, "name": g["name"],
        "committed_at": iso(committed_at), "deadline": iso(committed_at + window),
        "rule_version_at_commit": version, "rule_version_current": version,
        "success_signal": rules.success_rule(elig.goal_id, version), "eligibility_basis": elig.basis,
        "params": elig.params, "status": "ACTIVE", "decision_id": decision_id, "closed_at": None,
        "close_reason": None, "revisions": [],
    }


def judge_goal(goal: dict, events: list[dict], rules: GoalRules, catalogue: Catalogue, observed_until: datetime,
               covered: bool = True, version: str | None = None) -> dict:
    """Judge one goal against ``events`` (normalised, any order) under the given rule version.

    SUCCEEDED   - success condition met at or before the deadline.
    FAILED      - the whole window was observed (deadline <= observed_until) and the condition was not met.
    INCONCLUSIVE - window not fully observed, viewer not covered by the outcome feed, or no usable success signal.
    """
    version = version or goal["rule_version_current"]
    rule = rules.success_rule(goal["goal_id"], version)
    start, deadline = parse_ts(goal["committed_at"]), parse_ts(goal["deadline"])
    base = {"goal_record_id": goal["goal_record_id"], "user_id": goal["user_id"], "goal_id": goal["goal_id"],
            "rule_version": version, "committed_at": goal["committed_at"], "deadline": goal["deadline"],
            "observed_until": iso(observed_until)}
    if not covered:
        return {**base, "status": "INCONCLUSIVE", "reason": "viewer not covered by the outcome feed",
                "evidence": []}
    if not rule.get("event"):
        return {**base, "status": "INCONCLUSIVE", "reason": "goal has no observable success signal", "evidence": []}
    evs = sorted((e for e in events if start <= e["_event_time"] <= deadline and e["_recv"] <= observed_until),
                 key=lambda e: e["_event_time"])
    hit, evidence = None, []
    gid, cond = goal["goal_id"], rule.get("condition", "")
    if rule["event"] in ("play_started", "app_open") and "3 distinct IST days" in cond:
        days: list[str] = []
        for e in evs:
            if e["event"] == rule["event"] and ist_day(e["_event_time"]) not in days:
                days.append(ist_day(e["_event_time"]))
                evidence.append(e["event_id"])
                if len(days) >= 3:
                    hit = e
                    break
    else:
        for e in evs:
            if e["event"] != rule["event"]:
                continue
            p = e.get("properties") or {}
            if "is_trial = false" in cond or "paid plan" in cond:
                ok = p.get("is_trial") is not True
            elif "later IST calendar day" in cond:
                ok = ist_day(e["_event_time"]) > ist_day(start)
            elif "same content_slug" in cond:
                t = catalogue.titles.get(p.get("title_id"))
                ok = t is not None and t.slug == goal["params"].get("target_slug")
            elif "released in the 30 days" in cond:
                t = catalogue.titles.get(p.get("title_id"))
                ok = t is not None and t.release_date is not None and \
                    start.date() - timedelta(days=30) <= t.release_date <= start.date()
            else:
                ok = True
            if ok:
                hit = e
                evidence = [e["event_id"]]
                break
    if hit is not None:
        return {**base, "status": "SUCCEEDED", "reason": f"{rule['event']} ({cond}) at {iso(hit['_event_time'])}",
                "success_at": iso(hit["_event_time"]), "evidence": evidence}
    if deadline <= observed_until:
        return {**base, "status": "FAILED", "reason": f"no qualifying {rule['event']} ({cond}) within the window",
                "evidence": evidence}
    return {**base, "status": "INCONCLUSIVE",
            "reason": f"window ends {iso(deadline)} after observation ends {iso(observed_until)}",
            "evidence": evidence}
