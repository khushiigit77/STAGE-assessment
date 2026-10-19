"""Replay harness: feeds triggers, live events and control events to the agent in delivery order, writes artifacts,
runs the supplied simulator (unmodified, as a subprocess) and closes the outcome loop.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

from .catalogue import Catalogue
from .config import AgentConfig, IST, Pack, UTC, iso
from .data_loader import (ValidationReport, load_arms, load_controls, load_events, load_faults, load_profiles,
                          load_triggers)
from .decision_engine import Agent, DependencyFaults
from .evidence import ViewerHistory
from .goals import GoalRules
from .impact_analysis import analyse_pilot, analyse_replay
from .model_provider import MockModelProvider
from .outcome_tracker import load_outcome_events, track_outcomes
from .reporting import write_reports
from .state_store import StateStore

log = logging.getLogger("oua.replay")
KIND_ORDER = {"control": 0, "live": 1, "trigger": 2}


def write_jsonl(path: str, rows) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def apply_goal_rule_correction(store: StateStore, rules: GoalRules, control: dict) -> tuple[str, list[str]]:
    """Re-version only the still-ACTIVE goals of the corrected goal_id. Closed goals and past decision records are
    untouched; each change is logged as a goal revision."""
    new_version = rules.apply_correction(control)
    affected = []
    for g in store.open_goals(control["goal_id"]):
        old_version = g["rule_version_current"]
        g["rule_version_current"] = new_version
        g["success_signal_current"] = rules.success_rule(control["goal_id"], new_version)
        g["revisions"].append({"at": control["at"], "change": "rule_corrected", "from": old_version,
                               "to": new_version, "note": control.get("note")})
        store.save_goal(g, control["at"], "rule_corrected", {"from": old_version, "to": new_version})
        affected.append(g["goal_record_id"])
    return new_version, affected


def replay_start_of(triggers: list[dict]) -> datetime:
    first = min(t["_deliver"] for t in triggers)
    d = first.astimezone(IST).date()
    return datetime(d.year, d.month, d.day, tzinfo=IST).astimezone(UTC)


class Replay:
    def __init__(self, pack_root: str, out_dir: str, config: AgentConfig = AgentConfig()):
        self.pack, self.out, self.config = Pack(pack_root), out_dir, config
        self.report = ValidationReport()
        self.control_log: list[dict] = []

    def load(self) -> None:
        p, r = self.pack, self.report
        t0 = time.perf_counter()
        self.catalogue = Catalogue.load(p, r)
        self.profiles = load_profiles(p, r)
        hist_events, _ = load_events(p.path("data", "events.jsonl"), r, "events")
        self.triggers = load_triggers(p.path("replay", "triggers.jsonl"), r)
        self.live, _ = load_events(p.path("replay", "live_events.jsonl"), r, "live")
        self.controls = load_controls(p.path("replay", "control_events.jsonl"), r)
        self.faults_by_trigger = load_faults(p.path("replay", "model_faults.jsonl"), r)
        self.arms = load_arms(p.path("replay", "arms.csv"), r)
        self.rules = GoalRules(p.load_json("config", "goals.json"))
        self.interventions = p.load_json("config", "interventions.json")
        self.pricing = p.load_json("config", "pricing.json")
        self.replay_start = replay_start_of(self.triggers)
        self.history = ViewerHistory()
        self.history.finalise_history(hist_events, self.catalogue, self.replay_start, self.config)
        first_deliver = min(t["_deliver"] for t in self.triggers)
        late = [e for e in hist_events if e["_recv"] > first_deliver]
        if late:
            r.add("history_event_received_after_first_trigger", len(late))
        self.load_seconds = round(time.perf_counter() - t0, 2)
        log.info("loaded pack in %.1fs: %d history events, %d triggers", self.load_seconds, len(hist_events),
                 len(self.triggers))

    def build_agent(self, state_path: str) -> Agent:
        if os.path.exists(state_path):
            os.remove(state_path)  # this tool's own output from a previous run; inputs are never touched
        self.store = StateStore(state_path)
        self.faults = DependencyFaults()
        self.provider = MockModelProvider(self.faults_by_trigger, [], self.config.model_nominal_latency_s)
        return Agent(catalogue=self.catalogue, history=self.history, profiles=self.profiles, arms=self.arms,
                     rules=self.rules, interventions=self.interventions, pricing=self.pricing, store=self.store,
                     provider=self.provider, faults=self.faults, config=self.config)

    # ------------------------------------------------------------------ control events
    def apply_control(self, c: dict) -> None:
        entry = {"at": c["at"], "type": c["type"]}
        if c["type"] == "title_withdrawn":
            self.catalogue.withdraw(c["title_id"], c["_at"])
            affected = []
            rows = self.store.db.execute("SELECT user_id FROM viewers WHERE state LIKE ?", (f'%"{c["title_id"]}"%',))
            for (uid,) in rows.fetchall():
                def mark(s, tid=c["title_id"]):
                    if s.get("last_page") and tid in s["last_page"]["title_ids"]:
                        s["last_page"]["needs_refresh"] = True
                self.store.update_viewer(uid, mark, c["at"])
                affected.append(uid)
            entry.update(title_id=c["title_id"], live_pages_marked_for_refresh=affected,
                         note="already-published pages cannot be recalled by the agent; the next trigger republishes")
        elif c["type"] == "goal_rule_corrected":
            new_version, affected = apply_goal_rule_correction(self.store, self.rules, c)
            entry.update(goal_id=c["goal_id"], new_rule_version=new_version, open_goals_reversioned=affected,
                         note="only ACTIVE goals of this goal_id were touched; decisions.jsonl is append-only and "
                              "keeps the version used at decision time")
        elif c["type"] in ("dependency_unavailable", "dependency_degraded"):
            self.faults.register(c)
            self.provider.outages = self.faults.model_outages()
            entry.update(dependency=c["dependency"], until=c.get("until"))
        else:
            self.report.add("control_unknown_type", c["type"])
            entry["note"] = "unknown control type ignored"
        self.control_log.append(entry)

    # ------------------------------------------------------------------ main
    def run_decisions(self) -> tuple[list[dict], list[dict]]:
        os.makedirs(self.out, exist_ok=True)
        agent = self.build_agent(os.path.join(self.out, "viewer_state", "state.sqlite"))
        stream = [(c["_at"], KIND_ORDER["control"], i, "control", c) for i, c in enumerate(self.controls)]
        stream += [(e["_deliver"], KIND_ORDER["live"], i, "live", e) for i, e in enumerate(self.live)]
        stream += [(t["_deliver"], KIND_ORDER["trigger"], i, "trigger", t) for i, t in enumerate(self.triggers)]
        stream.sort(key=lambda x: x[:3])
        seen = {e["event_id"] for evs in self.history.events.values() for e in evs}
        decisions, actions = [], []
        for _, _, _, kind, item in stream:
            if kind == "control":
                self.apply_control(item)
            elif kind == "live":
                if item["event_id"] not in seen:
                    seen.add(item["event_id"])
                    self.history.add(item)
            else:
                rec, acts = agent.handle_trigger(item)
                decisions.append(rec)
                actions.extend(acts)
                if item["event_id"] not in seen:
                    seen.add(item["event_id"])
                    self.history.add(item)
        self.decisions, self.actions = decisions, actions
        write_jsonl(os.path.join(self.out, "decisions.jsonl"), decisions)
        write_jsonl(os.path.join(self.out, "actions.jsonl"), actions)
        write_jsonl(os.path.join(self.out, "control_log.jsonl"), self.control_log)
        return decisions, actions

    def run_simulator(self, seed: str) -> str:
        sim_out = os.path.join(self.out, "sim")
        cmd = [sys.executable, self.pack.path("simulator", "simulate_outcomes.py"), "--pack", self.pack.root,
               "--actions", os.path.join(self.out, "actions.jsonl"), "--out", sim_out, "--seed", seed]
        log.info("running simulator: %s", " ".join(cmd))
        subprocess.run(cmd, check=True)
        return sim_out

    def close_loop(self, sim_out: str) -> dict:
        rep = ValidationReport()
        by_user = load_outcome_events(os.path.join(sim_out, "outcome_events.jsonl"), rep)
        covered = {t["user_id"] for t in self.triggers}  # simulator contract: every triggered user is simulated
        observed_until = self.replay_start + timedelta(days=15)  # replay day + 14 simulated days
        judged_at = observed_until
        outcomes = track_outcomes(store=self.store, rules=self.rules, catalogue=self.catalogue,
                                  outcome_by_user=by_user, covered_users=covered, observed_until=observed_until,
                                  actions=self.actions, judged_at=judged_at)
        write_jsonl(os.path.join(self.out, "outcomes.jsonl"), outcomes)
        self.store.export(os.path.join(self.out, "viewer_state"))
        units = self.replay_units(outcomes, by_user, observed_until)
        with open(os.path.join(sim_out, "simulator_report.json"), encoding="utf-8") as f:
            sim_report = json.load(f)
        return {"outcomes": outcomes, "units": units, "sim_report": sim_report, "outcome_validation": rep.as_dict(),
                "observed_until": iso(observed_until)}

    def replay_units(self, outcomes: list[dict], by_user: dict, observed_until: datetime) -> list[dict]:
        goal_out = {o["goal_record_id"]: o for o in outcomes if o["record_type"] == "goal_outcome"}
        goals_by_user = defaultdict(list)
        for g in self.store.all_goals():
            goals_by_user[g["user_id"]].append(g)
        first_trig = {}
        for t in self.triggers:
            if t["user_id"] not in first_trig or t["_deliver"] < first_trig[t["user_id"]]:
                first_trig[t["user_id"]] = t["_deliver"]
        served = {a["user_id"] for a in self.actions}
        units = []
        for uid, t0 in sorted(first_trig.items()):
            gs = sorted(goals_by_user.get(uid, []), key=lambda g: g["committed_at"])
            g = gs[0] if gs else None
            evs = by_user.get(uid, [])
            units.append({
                "user_id": uid, "arm": self.arms.get(uid, "unknown"),
                "goal_id": g and g["goal_id"], "goal_record_id": g and g["goal_record_id"],
                "goal_status": goal_out[g["goal_record_id"]]["status"] if g else None,
                "played_7d": any(e["event"] == "play_started" and t0 <= e["_event_time"] < t0 + timedelta(days=7)
                                 for e in evs),
                "purchased_14d": any(e["event"] == "purchase_completed" and e["properties"].get("is_trial") is not True
                                     and t0 <= e["_event_time"] < observed_until for e in evs),
                "served": uid in served})
        return units


def run(pack_root: str, out_dir: str, seed: str = "oua-practice", skip_sim: bool = False,
        config: AgentConfig = AgentConfig()) -> dict:
    t0 = time.perf_counter()
    rp = Replay(pack_root, out_dir, config)
    rp.load()
    decisions, actions = rp.run_decisions()
    t_dec = time.perf_counter()
    result = {"decisions": len(decisions), "actions": len(actions), "load_seconds": rp.load_seconds,
              "decision_loop_seconds": round(t_dec - t0 - rp.load_seconds, 2)}
    with open(os.path.join(out_dir, "validation_report.json"), "w", encoding="utf-8") as f:
        json.dump(rp.report.as_dict(), f, indent=2, default=str)
    if skip_sim:
        rp.store.export(os.path.join(out_dir, "viewer_state"))
        return result
    sim_out = rp.run_simulator(seed)
    loop = rp.close_loop(sim_out)
    pilot = analyse_pilot(rp.pack.path("pilot", "pilot_assignment.csv"), rp.pack.path("pilot", "pilot_outcomes.csv"))
    replay_impact = analyse_replay(loop["units"])
    write_reports(out_dir=out_dir, decisions=decisions, actions=actions, loop=loop, pilot=pilot,
                  replay_impact=replay_impact, control_log=rp.control_log, validation=rp.report.as_dict(),
                  config=config, seed=seed, timings={**result, "total_seconds": round(time.perf_counter() - t0, 2)})
    rp.store.close()
    result.update(outcomes=len(loop["outcomes"]), sim_policy_problems=loop["sim_report"].get("policy_problem_count"))
    return result
