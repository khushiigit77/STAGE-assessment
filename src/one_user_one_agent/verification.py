"""Independent verification of every proposed action.

This module deliberately does not import the planner or the decision engine. It re-derives each check from the
evidence snapshot, the catalogue, the intervention/messaging policy, pricing and durable viewer state. A model
assertion, a planner note or catalogue text is never accepted as approval.

Every check is recorded with its result, detail and supporting evidence. Any failed check blocks the action.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .catalogue import Catalogue
from .config import AgentConfig, in_quiet_hours, ist_day, parse_ts

EXTRA_FORBIDDEN = ("discount", "% off", "offer", "free", "expires", "ends tonight")
PRICE_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*)", re.IGNORECASE)
NUMBER_PRICE_RE = re.compile(r"([0-9][0-9,]*)\s*(?:₹|rs\.?|inr|rupees)", re.IGNORECASE)


@dataclass
class VerificationResult:
    action_type: str
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "", evidence: object = None) -> None:
        self.checks.append({"check": name, "passed": bool(passed), "detail": detail, "evidence": evidence})

    @property
    def passed(self) -> bool:
        return all(c["passed"] for c in self.checks)

    @property
    def failures(self) -> list[str]:
        return [f"{c['check']}: {c['detail']}" for c in self.checks if not c["passed"]]

    def as_dict(self) -> dict:
        return {"action_type": self.action_type, "passed": self.passed, "checks": self.checks}


@dataclass
class VerificationContext:
    user_id: str
    arm: str | None
    trigger: dict
    publish_at: datetime
    snapshot: object                 # evidence.Snapshot
    viewer_state: dict
    goal: dict | None
    existing_action_keys: set[str]
    catalogue: Catalogue
    interventions: dict
    pricing: dict
    config: AgentConfig


def _prices(pricing: dict) -> set[int]:
    return {int(p["price_inr"]) for p in pricing.get("plans", [])}


def check_copy(res: VerificationResult, texts: list[str], forbidden: list[str], pricing: dict) -> None:
    joined = " ".join(t for t in texts if t)
    low = joined.lower()
    bad = [p for p in list(forbidden) + list(EXTRA_FORBIDDEN)
           if re.search(r"(?<![a-z])" + re.escape(p.lower()) + r"(?![a-z])", low)]
    res.add("copy_forbidden_phrases", not bad, f"found {bad}" if bad else "none found")
    claimed = {int(m.replace(",", "")) for m in PRICE_RE.findall(joined) + NUMBER_PRICE_RE.findall(joined)}
    unknown = sorted(claimed - _prices(pricing))
    res.add("copy_price_claims", not unknown,
            f"prices {sorted(claimed)} vs pricing.json {sorted(_prices(pricing))}", sorted(claimed))


def common_checks(res: VerificationResult, ctx: VerificationContext, action: dict) -> None:
    cfg = ctx.config
    res.add("holdout_excluded", ctx.arm == "agent", f"arm={ctx.arm!r}", "replay/arms.csv")
    g = ctx.goal
    owned = bool(g and g["status"] == "ACTIVE" and ctx.viewer_state.get("active_goal_record_id") == g["goal_record_id"]
                 and action.get("goal_record_id") == g["goal_record_id"])
    res.add("active_goal_ownership", owned,
            f"action goal {action.get('goal_record_id')} vs viewer active goal "
            f"{ctx.viewer_state.get('active_goal_record_id')}")
    if g:
        res.add("goal_not_excluded", g["goal_id"] not in cfg.excluded_goals, g["goal_id"])
        dl = parse_ts(g["deadline"])
        res.add("goal_deadline", ctx.publish_at < dl, f"publish {ctx.publish_at.isoformat()} < deadline {g['deadline']}")
    elapsed = (ctx.publish_at - ctx.trigger["_deliver"]).total_seconds()
    res.add("publish_within_budget", elapsed <= cfg.decision_budget_s, f"{elapsed:.3f}s <= {cfg.decision_budget_s}s")
    key = f"{ctx.trigger['event_id']}:{action['type']}"
    res.add("idempotency", key not in ctx.existing_action_keys, key)


def verify_home_page(ctx: VerificationContext, action: dict) -> VerificationResult:
    res = VerificationResult("home_page")
    common_checks(res, ctx, action)
    cat, hp = ctx.catalogue, ctx.interventions["home_page"]
    lag = ctx.trigger.get("_buffer_lag_s") or 0
    res.add("trigger_is_live_visit", lag <= ctx.config.live_visit_max_lag_s,
            f"phone buffered the trigger {lag:.0f}s (limit {ctx.config.live_visit_max_lag_s:.0f}s)")
    d = action.get("dialect")
    app_d = ctx.trigger.get("_dialect")
    res.add("dialect_matches_app_setting", d is not None and d == app_d,
            f"page dialect {d!r} vs app dialect at trigger {app_d!r}", ctx.trigger["event_id"])
    res.add("dialect_in_catalogue", d in cat.dialects, f"{d!r}")
    rows = action.get("rows") or []
    rcfg = hp["rows"]
    res.add("max_rows", len(rows) <= rcfg["max_rows"], f"{len(rows)} <= {rcfg['max_rows']}")
    seen_slugs: set[str] = set()
    hero_id = action.get("hero_title_id")
    if hero_id:
        t = cat.titles.get(hero_id)
        ok = t is not None and t.dialect == d and cat.available(t, ctx.publish_at)
        res.add("hero_valid", ok, f"{hero_id}: exists={t is not None}, dialect={t and t.dialect}, "
                                  f"available={t is not None and cat.available(t, ctx.publish_at)}")
        if t:
            seen_slugs.add(t.slug)
    for i, row in enumerate(rows):
        ids = row.get("title_ids") or []
        res.add(f"row{i}_type_allowed", row.get("row_type") in rcfg["row_types"], str(row.get("row_type")))
        invalid = []
        dup = []
        for tid in ids:
            t = cat.titles.get(tid)
            if t is None:
                invalid.append(f"{tid}:unknown")
            elif t.dialect != d:
                invalid.append(f"{tid}:dialect {t.dialect}")
            elif not cat.available(t, ctx.publish_at):
                invalid.append(f"{tid}:unavailable")
            elif t.slug in seen_slugs:
                dup.append(tid)
            else:
                seen_slugs.add(t.slug)
        valid_n = len(ids) - len(invalid) - len(dup)
        res.add(f"row{i}_titles_valid", not invalid, f"invalid {invalid}" if invalid else "all valid", ids)
        res.add(f"row{i}_unique_content", not dup, f"repeated content {dup}" if dup else "unique")
        res.add(f"row{i}_size", rcfg["min_titles_per_row"] <= valid_n <= rcfg["max_titles_per_row"],
                f"{valid_n} valid titles (allowed {rcfg['min_titles_per_row']}-{rcfg['max_titles_per_row']})")
    res.add("page_has_content", bool(rows) or bool(hero_id), f"{len(rows)} rows, hero={hero_id}")
    last = ctx.viewer_state.get("last_page")
    if last:
        age_min = (ctx.publish_at - parse_ts(last["published_at"])).total_seconds() / 60
        same = last["dialect"] == d and set(last["title_ids"]) == {x for r in rows for x in r["title_ids"]} | (
            {hero_id} if hero_id else set())
        repeat = same and age_min < ctx.config.page_refresh_after_min and not last.get("needs_refresh")
        res.add("not_repeat_of_live_page", not repeat, f"last page {last['action_id']} {age_min:.1f} min ago, "
                                                       f"identical={same}")
    return res


def _message_common(res: VerificationResult, ctx: VerificationContext, action: dict, channel: str) -> datetime | None:
    snap, cat = ctx.snapshot, ctx.catalogue
    consent_fact = snap.facts.get(f"{channel}_consent")
    res.add("consent", bool(consent_fact and consent_fact.value is True),
            f"{channel}_consent={consent_fact and consent_fact.value} "
            f"(source {consent_fact and consent_fact.source_type}:{consent_fact and consent_fact.source_ref})")
    send_at = parse_ts(action.get("send_at")) if action.get("send_at") else None
    res.add("send_at_present", send_at is not None and send_at >= ctx.publish_at, str(action.get("send_at")))
    if send_at is None:
        return None
    qh = ctx.interventions[channel]["quiet_hours_ist"]
    res.add("quiet_hours", not in_quiet_hours(send_at, qh[0], qh[1]), f"send_at IST {send_at.isoformat()} vs {qh}")
    day = ist_day(send_at)
    prior = int(ctx.viewer_state.get("message_days", {}).get(day, 0)) + \
        int(snap.features.get("message_days", {}).get(day, 0))
    res.add("frequency_cap", prior < 1, f"{prior} push/WhatsApp already on IST day {day} (max 1, both channels)")
    tid = action.get("title_id")
    if tid is not None:
        t = cat.titles.get(tid)
        md = snap.value("message_dialect")
        res.add("title_exists", t is not None, tid)
        res.add("title_available_at_send", t is not None and cat.available(t, send_at), f"{tid} at {action['send_at']}")
        res.add("title_in_viewer_dialect", t is not None and md is not None and t.dialect == md,
                f"title dialect {t and t.dialect} vs viewer dialect {md}")
    return send_at


def verify_push(ctx: VerificationContext, action: dict) -> VerificationResult:
    res = VerificationResult("push")
    common_checks(res, ctx, action)
    _message_common(res, ctx, action, "push")
    copy = ctx.interventions["push"]["copy"]
    text = action.get("text") or {}
    res.add("copy_title_length", len(text.get("title", "")) <= copy["title_max_chars"],
            f"{len(text.get('title', ''))} <= {copy['title_max_chars']}")
    res.add("copy_body_length", len(text.get("body", "")) <= copy["body_max_chars"],
            f"{len(text.get('body', ''))} <= {copy['body_max_chars']}")
    check_copy(res, [text.get("title", ""), text.get("body", "")], copy["forbidden_phrases"], ctx.pricing)
    expected = ctx.interventions["push"]["deeplink_format"].replace("<title_id>", str(action.get("title_id")))
    res.add("deeplink_format", action.get("deeplink") == expected, f"{action.get('deeplink')} vs {expected}")
    return res


def verify_whatsapp(ctx: VerificationContext, action: dict) -> VerificationResult:
    res = VerificationResult("whatsapp")
    common_checks(res, ctx, action)
    _message_common(res, ctx, action, "whatsapp")
    templates = {t["template_id"]: t for t in ctx.interventions["whatsapp"]["approved_templates"]}
    tpl = templates.get(action.get("template_id"))
    res.add("template_approved", tpl is not None, str(action.get("template_id")))
    if tpl is None:
        return res
    vars_ = action.get("variables") or {}
    res.add("template_variables_exact", set(vars_) == set(tpl["variables"]), f"{sorted(vars_)} vs {tpl['variables']}")
    t = ctx.catalogue.titles.get(action.get("title_id"))
    prices = {f"Rs {p}" for p in _prices(ctx.pricing)} | {f"₹{p}" for p in _prices(ctx.pricing)}
    for k, v in vars_.items():
        if k == "title_name":
            ok, src = t is not None and v == t.name, "catalogue title"
        elif k == "plan_price":
            ok, src = v in prices, "pricing.json"
        elif k == "episode_number":
            eps = ctx.catalogue.episodes.get(t.slug, []) if t else []
            ok, src = str(v).isdigit() and any(e[1] == int(v) for e in eps), "episodes.csv"
        elif k == "dialect_name":
            ok, src = t is not None and v == t.dialect, "catalogue dialect (no dialect names are supplied)"
        else:
            ok, src = False, "unknown variable"
        res.add(f"variable_{k}_from_source", ok, f"{v!r} must come from {src}")
    return res


def verify_in_app(ctx: VerificationContext, action: dict) -> VerificationResult:
    res = VerificationResult("in_app_message")
    common_checks(res, ctx, action)
    day = ist_day(ctx.publish_at)
    n = int(ctx.viewer_state.get("in_app_days", {}).get(day, 0))
    cap = ctx.interventions["in_app_message"]["max_per_day"]
    res.add("in_app_daily_cap", n < cap, f"{n} already on {day} (max {cap})")
    text = action.get("text") or {}
    check_copy(res, [text.get("title", ""), text.get("body", "")], ctx.interventions["push"]["copy"]["forbidden_phrases"],
               ctx.pricing)
    return res


VERIFIERS = {"home_page": verify_home_page, "push": verify_push, "whatsapp": verify_whatsapp,
             "in_app_message": verify_in_app}


def verify(ctx: VerificationContext, action: dict) -> VerificationResult:
    fn = VERIFIERS.get(action.get("type"))
    if fn is None:
        res = VerificationResult(str(action.get("type")))
        res.add("action_type_supported", False, f"unknown action type {action.get('type')!r}")
        return res
    return fn(ctx, action)


def verify_goal_proposal(proposal: dict | None, candidates: list[str], snapshot) -> VerificationResult:
    """Check a model goal proposal against deterministic eligibility. The model's reason text is not evidence."""
    res = VerificationResult("goal_proposal")
    if proposal is None:
        res.add("proposal_present", False, "no valid proposal")
        return res
    gid = proposal.get("goal_id")
    res.add("proposed_goal_is_eligible", gid in candidates,
            f"model proposed {gid!r}; deterministic eligible set {candidates}; subscription_status="
            f"{snapshot.value('subscription_status')!r}", {"model_reason_ignored_as_evidence": proposal.get("reason")})
    return res
