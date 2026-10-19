"""The per-viewer decision loop.

For one trigger: idempotency -> read durable state -> evidence snapshot -> own/revise one goal -> (model ranks goals,
deterministic check) -> plan -> independent verification -> atomic commit -> decision record.

Decision generation (this module + planner) is separate from publication: the engine returns verified actions; the
replay harness writes them to the actions file that the simulator/executor consumes.

Timing: the replay runs faster than real time. ``processing`` = measured wall-clock compute + simulated dependency time
(model latency, store latency/timeouts injected from control events). ``published_at = deliver_at + processing``.

Statuses (each has one meaning):
  PUBLISHED  at least one verified action was published.
  NO_ACTION  the agent decided not to act; ``no_action_reason`` says why (includes holdout shadow decisions).
  REJECTED   actions were proposed but every one failed verification; nothing published.
  FAILED     a dependency/budget failure prevented a safe decision; nothing published, state unchanged.
  DUPLICATE  re-delivery of an already-decided trigger; points to the original decision, nothing re-published.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .catalogue import Catalogue
from .config import AgentConfig, iso, ist_day, parse_ts
from .data_loader import ProfileRecord
from .evidence import ViewerHistory, build_snapshot, FIELD_POLICY
from .goals import GoalRules, eligibility, eligible_goals, judge_goal, new_goal_record
from .model_provider import ModelProvider, parse_model_output
from .planner import plan_home_page, plan_in_app, plan_push
from .state_store import StaleStateError, StateStore
from .verification import VerificationContext, verify, verify_goal_proposal

log = logging.getLogger("oua.engine")
STATUS_GOALS = {"G04": "trial_active", "G05": "never", "G06": "lapsed", "G08": "active", "G09": "active"}


class BudgetExceeded(RuntimeError):
    pass


class DependencyFailure(RuntimeError):
    pass


def h01(*parts: object) -> float:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


@dataclass
class DependencyFaults:
    """Windows taken from control events. Deterministic so repeated replays give identical results."""

    windows: list[dict] = field(default_factory=list)

    def register(self, control: dict) -> None:
        self.windows.append(control)

    def active(self, dependency: str, at: datetime) -> dict | None:
        for w in self.windows:
            if w["dependency"] == dependency and w["_at"] <= at < (w["_until"] or w["_at"]):
                return w
        return None

    def model_outages(self) -> list[tuple[datetime, datetime]]:
        return [(w["_at"], w["_until"]) for w in self.windows
                if w["dependency"] == "model_provider" and w["type"] == "dependency_unavailable"]

    def store_read(self, key: str, attempt: int, at: datetime, timeout_s: float) -> tuple[bool, float]:
        """(ok, simulated latency). Degraded window: 20% of reads time out, the rest have a long-tailed latency
        (p95 ~8 s); reads slower than the configured timeout are abandoned."""
        w = self.active("user_data_store", at)
        if w is None:
            return True, 0.0
        if w["type"] == "dependency_unavailable" or h01("timeout", key, attempt) < 0.20:
            return False, timeout_s
        lat = 0.05 + 7.95 * h01("lat", key, attempt) ** 4
        return (True, lat) if lat <= timeout_s else (False, timeout_s)


class Budget:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.model = 0
        self.tool = 0
        self.virtual_s = 0.0
        self.t0 = time.perf_counter()

    def tool_call(self, n: int = 1) -> None:
        self.tool += n
        if self.tool > self.cfg.max_tool_calls:
            raise BudgetExceeded(f"tool calls {self.tool} > {self.cfg.max_tool_calls}")

    def model_call(self) -> None:
        self.model += 1
        if self.model > self.cfg.max_model_calls:
            raise BudgetExceeded(f"model calls {self.model} > {self.cfg.max_model_calls}")

    @property
    def real_s(self) -> float:
        return time.perf_counter() - self.t0

    @property
    def elapsed_s(self) -> float:
        return self.real_s + self.virtual_s


class Agent:
    def __init__(self, *, catalogue: Catalogue, history: ViewerHistory, profiles: dict[str, ProfileRecord],
                 arms: dict[str, str], rules: GoalRules, interventions: dict, pricing: dict, store: StateStore,
                 provider: ModelProvider, faults: DependencyFaults, config: AgentConfig = AgentConfig()):
        self.catalogue, self.history, self.profiles, self.arms = catalogue, history, profiles, arms
        self.rules, self.interventions, self.pricing = rules, interventions, pricing
        self.store, self.provider, self.faults, self.config = store, provider, faults, config
        self.seq = {"D": 0, "A": 0, "GR": 0}

    def _id(self, kind: str) -> str:
        self.seq[kind] += 1
        return f"{kind}-{self.seq[kind]:06d}"

    # ------------------------------------------------------------------ main entry
    def handle_trigger(self, trigger: dict) -> tuple[dict, list[dict]]:
        budget = Budget(self.config)
        deliver = trigger["_deliver"]
        uid = trigger["user_id"]
        decision_id = self._id("D")
        base = self._base_record(decision_id, trigger)

        budget.tool_call()
        original = self.store.decision_for_trigger(trigger["event_id"])
        if original:
            return self._finish(base, budget, status="DUPLICATE", no_action_reason=
                                f"trigger already decided in {original}; nothing re-published",
                                extra={"duplicate_of": original}), []
        try:
            state, version = self._read_state(uid, trigger, budget)
        except (DependencyFailure, BudgetExceeded) as exc:
            return self._finish(base, budget, status="FAILED", failure_reason=str(exc),
                                fallback_reason="no action: viewer state (consent, caps, goal ownership) unavailable; "
                                                "trigger left unprocessed so a re-delivery can retry"), []
        try:
            return self._decide(base, trigger, state, version, budget, deliver)
        except BudgetExceeded as exc:
            return self._finish(base, budget, status="FAILED", failure_reason=str(exc),
                                fallback_reason="no action: call budget exceeded"), []
        except StaleStateError as exc:
            return self._finish(base, budget, status="FAILED", failure_reason=f"stale state: {exc}",
                                fallback_reason="decision discarded; newer viewer state wins"), []

    # ------------------------------------------------------------------ steps
    def _read_state(self, uid: str, trigger: dict, budget: Budget) -> tuple[dict, int]:
        errors = []
        for attempt in range(1, self.config.store_read_attempts + 1):
            budget.tool_call()
            at = trigger["_deliver"] + timedelta(seconds=budget.elapsed_s)
            ok, lat = self.faults.store_read(trigger["event_id"], attempt, at, self.config.store_read_timeout_s)
            budget.virtual_s += lat
            if ok:
                return self.store.read_viewer(uid)
            errors.append(f"attempt {attempt}: user_data_store read timed out after {lat:.1f}s")
        raise DependencyFailure("; ".join(errors))

    def _decide(self, base: dict, trigger: dict, state: dict, version: int, budget: Budget,
                deliver: datetime) -> tuple[dict, list[dict]]:
        uid, cfg = trigger["user_id"], self.config
        now = deliver
        budget.tool_call()  # evidence read
        snap = build_snapshot(uid, trigger, now, self.profiles.get(uid), self.history, self.catalogue, cfg)
        goal_changes: list[tuple[dict, str, dict]] = []

        # 1. own one goal: keep, close or revise the active goal
        budget.tool_call()
        goal = self.store.get_goal(state.get("active_goal_record_id"))
        if goal is not None:
            goal = self._review_active_goal(goal, uid, now, snap, goal_changes)
        model_info = {"provider": self.provider.name, "calls": 0, "status": "not_called"}
        committed = False
        candidates = eligible_goals(snap, now, self.rules, cfg) if goal is None else []
        if goal is None and candidates:
            chosen, model_info = self._choose_goal(candidates, snap, trigger, budget)
            goal = new_goal_record(self._id("GR"), uid, chosen, self.rules, now, base["decision_id"])
            goal_changes.append((goal, "committed", {"decision_id": base["decision_id"],
                                                     "rule_version": goal["rule_version_at_commit"]}))
            state["active_goal_record_id"] = goal["goal_record_id"]
            state["learning"]["goals_committed"] += 1
            committed = True
        elif goal is None:
            state["active_goal_record_id"] = None

        # 2. plan actions (arm-blind up to here; the holdout gets a shadow goal and no action)
        arm = self.arms.get(uid)
        proposals, notes = [], []
        no_action_reason = None
        lag = trigger.get("_buffer_lag_s") or 0
        if goal is None:
            no_action_reason = "no goal can be safely selected: " + self._no_goal_reason(snap, now)
        elif arm != "agent":
            no_action_reason = f"holdout arm ({arm!r}): shadow goal recorded for measurement, no agent action"
        else:
            if lag > cfg.live_visit_max_lag_s:
                notes.append(f"home page skipped: trigger was buffered offline for {lag:.0f}s; not a live visit")
            elif self._page_still_live(state, snap, goal, now):
                notes.append("home page skipped: an identical-goal page for this dialect is still live")
            else:
                budget.tool_call()  # catalogue query
                page, pnotes = plan_home_page(snap, goal, self.catalogue, self.history.popularity,
                                              deliver + timedelta(seconds=budget.elapsed_s), cfg, self.interventions)
                notes += pnotes
                if page:
                    proposals.append(page)
            target_day = None
            push, why = plan_push(snap, goal, self.catalogue, self.history.popularity, now, cfg)
            if push:
                target_day = ist_day(parse_ts(push["send_at"]))
                if state["message_days"].get(target_day):
                    notes.append(f"push skipped: one already scheduled for IST day {target_day}")
                else:
                    proposals.append(push)
            elif why:
                notes.append(f"push not proposed: {why}")
            inapp, why = plan_in_app(snap, goal, self.pricing, cfg)
            if inapp and not state["in_app_days"].get(ist_day(now)):
                proposals.append(inapp)
            if not proposals:
                no_action_reason = "; ".join(notes) or "nothing proportionate to do"

        # 3. independent verification of every proposal
        budget.tool_call()
        existing = {f"{a['trigger_event_id']}:{a['type']}" for a in self.store.actions_for_user(uid)}
        publish_at = deliver + timedelta(seconds=budget.elapsed_s)
        verifications, published, rejected = [], [], []
        for p in proposals:
            action = {"action_id": None, "decision_id": base["decision_id"], "user_id": uid,
                      "trigger_event_id": trigger["event_id"], "goal_record_id": goal["goal_record_id"],
                      "published_at": iso(publish_at), **p}
            ctx = VerificationContext(uid, arm, trigger, publish_at, snap, state, goal, existing, self.catalogue,
                                      self.interventions, self.pricing, cfg)
            res = verify(ctx, action)
            verifications.append(res.as_dict())
            if res.passed:
                action["action_id"] = self._id("A")
                published.append(action)
            else:
                rejected.append({"type": p["type"], "failures": res.failures})

        # 4. update durable state and commit atomically
        for a in published:
            if a["type"] == "home_page":
                state["last_page"] = {"action_id": a["action_id"], "published_at": a["published_at"],
                                      "dialect": a["dialect"], "goal_record_id": goal["goal_record_id"],
                                      "title_ids": sorted({t for r in a["rows"] for t in r["title_ids"]} |
                                                          ({a["hero_title_id"]} if a.get("hero_title_id") else set())),
                                      "needs_refresh": False}
                state["learning"]["pages_published"] += 1
            elif a["type"] in ("push", "whatsapp"):
                d = ist_day(parse_ts(a["send_at"]))
                state["message_days"][d] = state["message_days"].get(d, 0) + 1
                state["learning"]["messages_published"] += 1
            elif a["type"] == "in_app_message":
                d = ist_day(publish_at)
                state["in_app_days"][d] = state["in_app_days"].get(d, 0) + 1
        state["last_decision_id"] = base["decision_id"]
        state["last_trigger_received_at"] = iso(trigger["_recv"])
        budget.tool_call()
        if budget.elapsed_s > cfg.decision_budget_s:
            raise BudgetExceeded(f"decision took {budget.elapsed_s:.1f}s > {cfg.decision_budget_s}s")
        self.store.commit_decision(user_id=uid, expected_version=version, state=state, decision_id=base["decision_id"],
                                   trigger_event_id=trigger["event_id"], now_iso=iso(now), goals=goal_changes,
                                   actions=published)

        if published:
            status = "PUBLISHED"
        elif rejected:
            status = "REJECTED"
            no_action_reason = "all proposed actions failed verification: " + \
                               "; ".join(f"{r['type']} ({', '.join(r['failures'])})" for r in rejected)
        else:
            status = "NO_ACTION"
        rec = self._finish(base, budget, status=status, no_action_reason=no_action_reason, extra={
            "arm": arm,
            "goal": self._goal_view(goal, committed, goal_changes),
            "dialect": self._dialect_view(snap),
            "content": [{"action_type": a["type"], "dialect": a.get("dialect"), "hero_title_id": a.get("hero_title_id"),
                         "rows": a.get("rows"), "title_id": a.get("title_id"), "send_at": a.get("send_at")}
                        for a in published],
            "actions": [{"action_id": a["action_id"], "type": a["type"]} for a in published],
            "rejected_actions": rejected,
            "planning_notes": notes,
            "confidence": self._confidence(snap, goal, model_info),
            "evidence_refs": snap.refs(),
            "assumptions": snap.assumptions + self._goal_assumptions(goal),
            "unresolved_conflicts": snap.conflicts,
            "verification": verifications,
            "model": model_info,
            "fallback_reason": model_info.get("fallback_reason"),
            "published_at": iso(publish_at) if published else None,
        })
        return rec, published

    def _review_active_goal(self, goal: dict, uid: str, now: datetime, snap, changes: list) -> dict | None:
        """Close the active goal if it was reached, expired, or its eligibility basis no longer holds."""
        evs = self.history.visible(uid, now)
        verdict = judge_goal(goal, evs, self.rules, self.catalogue, observed_until=now)
        reason = None
        if verdict["status"] == "SUCCEEDED":
            goal.update(status="SUCCEEDED", closed_at=iso(now), close_reason=verdict["reason"])
            reason = "succeeded"
        elif now >= parse_ts(goal["deadline"]):
            goal.update(status="EXPIRED", closed_at=iso(now), close_reason="deadline passed")
            reason = "expired"
        elif goal["goal_id"] in STATUS_GOALS and snap.value("subscription_status") != STATUS_GOALS[goal["goal_id"]]:
            goal.update(status="SUPERSEDED", closed_at=iso(now),
                        close_reason=f"subscription_status is now {snap.value('subscription_status')!r} "
                                     f"(source {snap.facts['subscription_status'].source_ref})")
            reason = "superseded"
        if reason is None:
            return goal
        changes.append((goal, f"closed_{reason}", {"reason": goal["close_reason"], "verdict": verdict["status"]}))
        return None

    def _choose_goal(self, candidates, snap, trigger, budget: Budget):
        """Ask the model to rank deterministic candidates; verify; fall back to deterministic priority."""
        ids = [c.goal_id for c in candidates]
        info = {"provider": self.provider.name, "calls": 0, "status": None, "proposal": None, "error": None,
                "verification": None, "fallback_reason": None}
        context = {"candidate_goals": ids, "subscription_status": snap.value("subscription_status"),
                   "signup_at": snap.value("signup_at")}  # structured facts only; never catalogue text
        proposal, error = None, None
        for attempt in range(1 + self.config.model_retries_on_unavailable):
            budget.model_call()
            info["calls"] += 1
            at = trigger["_deliver"] + timedelta(seconds=budget.elapsed_s)
            resp = self.provider.propose_goal(context, trigger["event_id"], at)
            if resp.latency_s > self.config.model_timeout_s or resp.error == "timeout":
                budget.virtual_s += min(resp.latency_s, self.config.model_timeout_s)
                error = f"model call timed out after {self.config.model_timeout_s:.1f}s"
                break  # do not retry a hang: the latency budget matters more
            budget.virtual_s += resp.latency_s
            if not resp.ok:
                error = resp.error
                continue
            proposal, error = parse_model_output(resp.raw)
            info["raw_output"] = resp.raw if isinstance(resp.raw, (dict, str)) else None
            break
        info["proposal"], info["error"] = proposal, error
        ver = verify_goal_proposal(proposal, ids, snap)
        info["verification"] = ver.as_dict()
        by_id = {c.goal_id: c for c in candidates}
        if proposal and ver.passed:
            info["status"] = "accepted"
            return by_id[proposal["goal_id"]], info
        info["status"] = "rejected" if proposal else "failed"
        info["fallback_reason"] = ("model proposal rejected: " + "; ".join(ver.failures)) if proposal else \
            f"model unavailable ({error}); deterministic priority used"
        return candidates[0], info

    def _page_still_live(self, state: dict, snap, goal: dict, now: datetime) -> bool:
        last = state.get("last_page")
        if not last or last.get("needs_refresh"):
            return False
        age_min = (now - parse_ts(last["published_at"])).total_seconds() / 60
        return (age_min < self.config.page_refresh_after_min and last["dialect"] == snap.value("page_dialect")
                and last.get("goal_record_id") == goal["goal_record_id"])

    def _no_goal_reason(self, snap, now: datetime) -> str:
        parts = []
        for gid in self.config.goal_priority:
            e = eligibility(gid, snap, now, self.config)
            parts.append(f"{gid}: {'; '.join(e.basis)}")
        return " | ".join(parts[:4]) + (" | ..." if len(parts) > 4 else "")

    # ------------------------------------------------------------------ record helpers
    def _base_record(self, decision_id: str, t: dict) -> dict:
        dl = (t.get("properties") or {}).get("deeplink")
        return {
            "decision_id": decision_id, "viewer_id": t["user_id"],
            "trigger": {"event_id": t["event_id"], "event": t["event"], "session_id": t.get("session_id"),
                        "occurred_at": t.get("occurred_at"), "sent_at": t.get("sent_at"),
                        "received_at": t.get("received_at"), "deliver_at": iso(t["_deliver"]),
                        "app_dialect": t.get("app_dialect"), "app_language": t.get("app_language"),
                        "deeplink": dl, "buffer_lag_s": round(t.get("_buffer_lag_s") or 0, 3),
                        "phone_clock_skew_s": None if t.get("_clock_skew_s") is None else round(t["_clock_skew_s"], 3)},
            "received_time": t.get("received_at"),
            "event_age_s": round((t["_deliver"] - t["_event_time"]).total_seconds(), 3),
        }

    def _finish(self, base: dict, budget: Budget, *, status: str, no_action_reason: str | None = None,
                failure_reason: str | None = None, fallback_reason: str | None = None,
                extra: dict | None = None) -> dict:
        rec = dict(base)
        rec.update(extra or {})
        rec["status"] = status
        rec["no_action_reason"] = no_action_reason if status != "PUBLISHED" else None
        rec["failure_reason"] = failure_reason
        rec["fallback_reason"] = fallback_reason or rec.get("fallback_reason")
        rec["latency"] = {"processing_ms": round(budget.elapsed_s * 1000, 3),
                          "real_compute_ms": round(budget.real_s * 1000, 3),
                          "simulated_dependency_ms": round(budget.virtual_s * 1000, 3),
                          "within_budget": budget.elapsed_s <= self.config.decision_budget_s}
        rec["calls"] = {"model": budget.model, "tool": budget.tool,
                        "limits": {"model": self.config.max_model_calls, "tool": self.config.max_tool_calls}}
        rec["created_at"] = iso(parse_ts(rec["trigger"]["deliver_at"]) + timedelta(seconds=budget.elapsed_s))
        rec["record_schema"] = "decision/v1"
        return rec

    def _goal_view(self, goal: dict | None, committed: bool, changes: list) -> dict | None:
        closed = [{"goal_record_id": g["goal_record_id"], "goal_id": g["goal_id"], "change": c,
                   "reason": d.get("reason")} for g, c, d in changes if c.startswith("closed")]
        if goal is None:
            return {"active": None, "changes": closed}
        return {"goal_record_id": goal["goal_record_id"], "goal_id": goal["goal_id"], "name": goal["name"],
                "committed_now": committed, "committed_at": goal["committed_at"], "deadline": goal["deadline"],
                "success_signal": goal["success_signal"], "rule_version": goal["rule_version_current"],
                "eligibility_basis": goal["eligibility_basis"], "params": goal["params"], "changes": closed}

    def _dialect_view(self, snap) -> dict:
        f = snap.facts
        return {"page_dialect": f["page_dialect"].value, "page_dialect_source": f["page_dialect"].source_type,
                "page_dialect_note": f["page_dialect"].note, "message_dialect": f["message_dialect"].value,
                "message_dialect_source": f"{f['message_dialect'].source_type}:{f['message_dialect'].source_ref}",
                "profile_preferred_dialect": f["preferred_dialect"].value,
                "policy": {k: FIELD_POLICY[k] for k in ("page_dialect", "message_dialect")},
                "conflicts": [c for c in snap.conflicts if "dialect" in c]}

    def _goal_assumptions(self, goal: dict | None) -> list[str]:
        out = []
        if goal and goal["goal_id"] == "G06":
            out.append(f"'recently lapsed' interpreted as a lapse event within {self.config.lapse_recent_days} days")
        if goal and goal["goal_id"] in ("G01", "G02"):
            out.append("'never' proven only within the observable history window (signup inside the window)")
        if goal and goal["goal_id"] == "G12":
            out.append("'stopped visiting' requires signup >= 14 days before the trigger")
        return out

    def _confidence(self, snap, goal: dict | None, model_info: dict) -> dict:
        """Evidence-coverage score in [0, 1]. Not a probability that the goal will succeed."""
        f = snap.facts
        items = {
            "profile_present_and_fresh": bool(f["profile"].value and f["profile"].fresh),
            "subscription_status_known": snap.value("subscription_status") is not None,
            "current_dialect_known_and_catalogued": snap.value("page_dialect") is not None,
            "no_unresolved_conflicts": not snap.conflicts,
            "history_depth_ge_20_events": (snap.value("history_depth") or 0) >= 20,
            "goal_from_verified_proposal_or_rules": goal is not None,
        }
        score = round(sum(items.values()) / len(items), 2)
        label = "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
        missing = [k for k, v in items.items() if not v]
        return {"score": score, "label": label, "basis": items,
                "reason": ("all evidence items present" if not missing else "missing: " + ", ".join(missing)) +
                          "; model confidence is not used",
                "model_confidence_ignored": (model_info.get("proposal") or {}).get("confidence")}
