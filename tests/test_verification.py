"""The independent verifier blocks policy violations regardless of what the planner proposes."""
from datetime import datetime, timedelta

from conftest import NOW, World, ev, profile

from one_user_one_agent.config import UTC, iso
from one_user_one_agent.evidence import build_snapshot
from one_user_one_agent.goals import eligible_goals, new_goal_record
from one_user_one_agent.verification import VerificationContext, verify


def ctx_for(w: World, uid="U-1", arm="agent", state_extra=None, publish_at=NOW, trig=None):
    trig = trig or ev(uid, "app_open", NOW)
    snap = build_snapshot(uid, trig, NOW, w.profiles.get(uid), w.history, w.catalogue, w.config)
    goal = new_goal_record("GR-1", uid, eligible_goals(snap, NOW, w.rules, w.config)[0], w.rules, NOW, "D-1")
    state = {"active_goal_record_id": "GR-1", "message_days": {}, "in_app_days": {}, "last_page": None}
    state.update(state_extra or {})
    return VerificationContext(uid, arm, trig, publish_at, snap, state, goal, set(), w.catalogue, w.interventions,
                               w.pricing, w.config), goal


def push(goal, send_at, **kw):
    a = {"type": "push", "action_id": None, "user_id": "U-1", "trigger_event_id": "x", "goal_record_id":
         goal["goal_record_id"], "send_at": iso(send_at), "title_id": "T100",
         "text": {"title": "Continue watching", "body": "Episode 2 of Name T100 is waiting for you."},
         "deeplink": "ott://title/T100"}
    a.update(kw)
    return a


def page(goal, rows, dialect="d1", hero=None):
    return {"type": "home_page", "user_id": "U-1", "trigger_event_id": "x", "goal_record_id": goal["goal_record_id"],
            "dialect": dialect, "hero_title_id": hero, "rows": rows}


def failed(res):
    return {c["check"] for c in res.checks if not c["passed"]}


DAYTIME = datetime(2026, 10, 2, 13, 30, tzinfo=UTC)  # 19:00 IST


def test_push_allowed_when_all_rules_hold(world):
    world.resumer()
    ctx, goal = ctx_for(world)
    assert verify(ctx, push(goal, DAYTIME)).passed


def test_push_blocked_without_consent(world):
    world.resumer(push="false")
    ctx, goal = ctx_for(world)
    assert "consent" in failed(verify(ctx, push(goal, DAYTIME)))


def test_opt_out_event_overrides_profile_consent(world):
    world.resumer(push="true")
    world.history.add(ev("U-1", "notification_opt_out", NOW - timedelta(days=1), props={"channel": "push"}))
    ctx, goal = ctx_for(world)
    assert "consent" in failed(verify(ctx, push(goal, DAYTIME)))


def test_push_blocked_in_quiet_hours(world):
    world.resumer()
    ctx, goal = ctx_for(world)
    night = datetime(2026, 10, 2, 17, 0, tzinfo=UTC)  # 22:30 IST
    early = datetime(2026, 10, 2, 2, 0, tzinfo=UTC)   # 07:30 IST
    assert "quiet_hours" in failed(verify(ctx, push(goal, night)))
    assert "quiet_hours" in failed(verify(ctx, push(goal, early)))


def test_push_blocked_by_daily_frequency_cap_across_channels(world):
    world.resumer()
    ctx, goal = ctx_for(world, state_extra={"message_days": {"2026-10-02": 1}})
    assert "frequency_cap" in failed(verify(ctx, push(goal, DAYTIME)))
    # a WhatsApp delivered earlier that IST day (from history) also counts
    w2 = World()
    w2.resumer()
    w2.history.add(ev("U-1", "message_delivered", NOW - timedelta(hours=1),
                      props={"channel": "whatsapp", "campaign_id": "c", "title_id": "T100"}))
    ctx2, goal2 = ctx_for(w2)
    same_day = NOW + timedelta(hours=2)
    assert "frequency_cap" in failed(verify(ctx2, push(goal2, same_day)))


def test_push_copy_rules(world):
    world.resumer()
    ctx, goal = ctx_for(world)
    bad = push(goal, DAYTIME, text={"title": "Hurry! Last chance", "body": "Only Rs 99 today"})
    f = failed(verify(ctx, bad))
    assert {"copy_forbidden_phrases", "copy_price_claims"} <= f
    ok_price = push(goal, DAYTIME, text={"title": "Plans", "body": "Monthly plan Rs 149."})
    assert verify(ctx, ok_price).passed


def test_whatsapp_requires_approved_template_and_sourced_variables(world):
    world.resumer(wa="true")
    ctx, goal = ctx_for(world)
    base = {"type": "whatsapp", "user_id": "U-1", "trigger_event_id": "x", "goal_record_id": "GR-1",
            "send_at": iso(DAYTIME), "title_id": "T100"}
    good = dict(base, template_id="wa_new_episode_v2", variables={"title_name": "Name T100", "episode_number": "2"})
    assert verify(ctx, good).passed
    free_text = dict(base, template_id="wa_custom", variables={"text": "anything"})
    assert "template_approved" in failed(verify(ctx, free_text))
    invented = dict(base, template_id="wa_new_episode_v2", variables={"title_name": "Name T100", "episode_number": "99"})
    assert "variable_episode_number_from_source" in failed(verify(ctx, invented))


def test_wrong_dialect_unknown_and_unavailable_titles_rejected(world):
    world.resumer()
    world.catalogue.withdraw("T103", NOW - timedelta(minutes=1))
    ctx, goal = ctx_for(world)
    res = verify(ctx, page(goal, [{"row_type": "popular_in_dialect",
                                   "title_ids": ["T101", "T102", "T201", "T103", "T404"]}]))
    detail = next(c for c in res.checks if c["check"] == "row0_titles_valid")["detail"]
    assert not res.passed
    assert "T201:dialect d2" in detail and "T103:unavailable" in detail and "T404:unknown" in detail


def test_page_in_dialect_other_than_app_setting_rejected(world):
    world.resumer()
    ctx, goal = ctx_for(world)
    res = verify(ctx, page(goal, [{"row_type": "popular_in_dialect", "title_ids": ["T201", "T202"]}], dialect="d2"))
    assert "dialect_matches_app_setting" in failed(res)


def test_row_with_fewer_than_three_valid_titles_rejected(world):
    world.resumer()
    ctx, goal = ctx_for(world)
    res = verify(ctx, page(goal, [{"row_type": "popular_in_dialect", "title_ids": ["T101", "T102"]}]))
    assert "row0_size" in failed(res)
    ok = verify(ctx, page(goal, [{"row_type": "popular_in_dialect", "title_ids": ["T101", "T102", "T104"]}]))
    assert ok.passed


def test_duplicate_content_in_page_rejected(world):
    world.resumer()
    ctx, goal = ctx_for(world)
    res = verify(ctx, page(goal, [{"row_type": "popular_in_dialect", "title_ids": ["T101", "T102", "T104"]}],
                           hero="T101"))
    assert "row0_unique_content" in failed(res)


def test_planner_abstains_when_dialect_has_too_few_titles(world):
    world.resumer()
    rec, acts = world.agent.handle_trigger(ev("U-1", "app_open", NOW, dialect="d2"))  # d2 has only 2 titles
    pages = [a for a in acts if a["type"] == "home_page"]
    assert all(len(r["title_ids"]) >= 3 for p in pages for r in p["rows"])


def test_holdout_and_goal_ownership_block_any_action(world):
    world.resumer()
    ctx, goal = ctx_for(world, arm="holdout")
    assert "holdout_excluded" in failed(verify(ctx, push(goal, DAYTIME)))
    ctx2, goal2 = ctx_for(world, state_extra={"active_goal_record_id": "GR-other"})
    assert "active_goal_ownership" in failed(verify(ctx2, push(goal2, DAYTIME)))


def test_publish_after_budget_rejected(world):
    world.resumer()
    trig = ev("U-1", "app_open", NOW)
    ctx, goal = ctx_for(world, trig=trig, publish_at=trig["_deliver"] + timedelta(seconds=61))
    assert "publish_within_budget" in failed(verify(ctx, push(goal, DAYTIME)))
