"""Goal rules, rule corrections, outcome classification and randomized-holdout statistics."""
import csv
import os
from datetime import timedelta

from conftest import NOW, World, ev, profile

from one_user_one_agent.config import iso
from one_user_one_agent.goals import eligibility, judge_goal, new_goal_record, Eligibility
from one_user_one_agent.evidence import build_snapshot
from one_user_one_agent.impact_analysis import analyse_pilot, analyse_replay, prop_diff, stratified_diff
from one_user_one_agent.replay import apply_goal_rule_correction

CORRECTION = {"at": iso(NOW + timedelta(hours=2)), "type": "goal_rule_corrected", "goal_id": "G03",
              "new_success": {"event": "play_started", "condition": "on a later IST calendar day within the window"}}


def g(world, goal_id, uid="U-1", at=NOW, params=None, rid=None):
    return new_goal_record(rid or f"GR-{uid}-{goal_id}", uid, Eligibility(goal_id, True, [], params or {}),
                           world.rules, at, "D-1")


def test_outcome_failure_vs_insufficient_evidence(world):
    goal = g(world, "G01")  # 24 h window
    after = NOW + timedelta(days=2)
    assert judge_goal(goal, [], world.rules, world.catalogue, after)["status"] == "FAILED"
    early = NOW + timedelta(hours=10)
    assert judge_goal(goal, [], world.rules, world.catalogue, early)["status"] == "INCONCLUSIVE"
    assert judge_goal(goal, [], world.rules, world.catalogue, after, covered=False)["status"] == "INCONCLUSIVE"
    play = ev("U-1", "play_started", NOW + timedelta(hours=3), props={"title_id": "T101", "episode_id": "x"})
    r = judge_goal(goal, [play], world.rules, world.catalogue, after)
    assert r["status"] == "SUCCEEDED" and r["evidence"] == [play["event_id"]]
    late = ev("U-1", "play_started", NOW + timedelta(hours=30), props={"title_id": "T101"})
    assert judge_goal(goal, [late], world.rules, world.catalogue, after)["status"] == "FAILED"


def test_trial_purchase_does_not_count_as_paid(world):
    goal = g(world, "G05")
    trial = ev("U-1", "purchase_completed", NOW + timedelta(hours=1),
               props={"plan_id": "P_TRIAL", "price_inr": 1, "is_trial": True})
    end = NOW + timedelta(days=8)
    assert judge_goal(goal, [trial], world.rules, world.catalogue, end)["status"] == "FAILED"


def test_goal_without_success_signal_is_inconclusive_and_never_eligible(world):
    world.resumer()
    trig = ev("U-1", "app_open", NOW)
    snap = build_snapshot("U-1", trig, NOW, world.profiles["U-1"], world.history, world.catalogue, world.config)
    for gid in ("G10", "G11"):
        assert not eligibility(gid, snap, NOW, world.config).eligible
    goal = g(world, "G10")
    assert judge_goal(goal, [], world.rules, world.catalogue, NOW + timedelta(days=30))["status"] == "INCONCLUSIVE"


def test_rule_correction_updates_only_open_goals_and_keeps_history(world):
    open_g3 = g(world, "G03", uid="U-1")
    closed_g3 = dict(g(world, "G03", uid="U-2"), status="SUCCEEDED")
    other = g(world, "G01", uid="U-3")
    for x in (open_g3, closed_g3, other):
        world.store.save_goal(x, iso(NOW), "committed", {})
    decision_snapshot = dict(open_g3)  # what the decision record captured
    version, affected = apply_goal_rule_correction(world.store, world.rules, dict(CORRECTION))
    assert affected == [open_g3["goal_record_id"]]
    updated = world.store.get_goal(open_g3["goal_record_id"])
    assert updated["rule_version_current"] == version != updated["rule_version_at_commit"]
    assert updated["revisions"][0]["change"] == "rule_corrected"
    assert world.store.get_goal(closed_g3["goal_record_id"])["rule_version_current"] == closed_g3["rule_version_current"]
    assert world.store.get_goal(other["goal_record_id"])["rule_version_current"] == other["rule_version_current"]
    assert decision_snapshot["rule_version_current"] == open_g3["rule_version_at_commit"]  # history not rewritten
    # same evidence, different verdicts per version: an app_open alone no longer counts
    next_day_open = ev("U-1", "app_open", NOW + timedelta(days=1))
    end = NOW + timedelta(days=3)
    assert judge_goal(updated, [next_day_open], world.rules, world.catalogue, end)["status"] == "FAILED"
    assert judge_goal(updated, [next_day_open], world.rules, world.catalogue, end,
                      version=updated["rule_version_at_commit"])["status"] == "SUCCEEDED"
    # goals committed after the correction carry the new version from the start
    assert g(world, "G03", uid="U-4", at=NOW + timedelta(hours=3))["rule_version_at_commit"] == version


def test_g06_requires_recent_lapse_event(world):
    uid = "U-7"
    world.viewer(uid, "agent", profile(uid, status="lapsed"))
    trig = ev(uid, "app_open", NOW)
    snap = build_snapshot(uid, trig, NOW, world.profiles[uid], world.history, world.catalogue, world.config)
    assert not eligibility("G06", snap, NOW, world.config).eligible  # lapse date unknown
    world.history.add(ev(uid, "subscription_lapsed", NOW - timedelta(days=10)))
    snap = build_snapshot(uid, trig, NOW, world.profiles[uid], world.history, world.catalogue, world.config)
    assert eligibility("G06", snap, NOW, world.config).eligible


def test_never_completed_requires_observable_history(world):
    old = "U-8"
    world.viewer(old, "agent", profile(old, signup="2024-01-01T00:00:00.000Z"))
    new = "U-9"
    world.viewer(new, "agent", profile(new, signup="2026-09-20T00:00:00.000Z"))
    for uid, expected in ((old, False), (new, True)):
        snap = build_snapshot(uid, ev(uid, "app_open", NOW), NOW, world.profiles[uid], world.history,
                              world.catalogue, world.config)
        assert eligibility("G02", snap, NOW, world.config).eligible is expected


def _write_pilot(tmp_path, rows):
    a, o = tmp_path / "a.csv", tmp_path / "o.csv"
    with open(a, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["user_id", "arm", "served"])
        w.writerows([(u, arm, s) for u, arm, s, _, _ in rows])
    with open(o, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["user_id", "arm", "served", "goal_id", "goal_started_at", "succeeded"])
        w.writerows([(u, arm, s, goal, "2026-09-02T00:00:00.000Z", y) for u, arm, s, goal, y in rows])
    return str(a), str(o)


def test_itt_uses_assignment_not_served_status(tmp_path):
    # identical success rates per goal in both arms; agent "serves" only the high-base-rate goal users
    rows = []
    n = 0
    for arm in ("treatment", "holdout"):
        for goal, k, rate in (("G09", 100, 0.6), ("G05", 300, 0.02)):
            for i in range(k):
                n += 1
                served = "true" if arm == "treatment" and goal == "G09" and i < 50 else "false"
                rows.append((f"U{n}", arm, served, goal, "true" if i < k * rate else "false"))
    res = analyse_pilot(*_write_pilot(tmp_path, rows))
    assert res["itt"]["abs_diff"] == 0.0
    assert abs(res["itt_stratified_by_goal"]["abs_diff"]) < 1e-9
    assert res["naive_served_vs_all_others"]["abs_diff"] > 0.5  # selection, not effect
    assert res["itt_permutation_p"] > 0.5


def test_prop_diff_and_stratified_math():
    d = prop_diff(30, 100, 20, 100)
    assert d["abs_diff"] == 0.1 and d["ci95"][0] < 0.1 < d["ci95"][1] and d["rel_diff"] == 0.5
    s = stratified_diff([("a", True, True)] * 5 + [("a", True, False)] * 5 + [("a", False, False)] * 10 +
                        [("b", True, True)])
    assert s["strata_skipped_one_arm_only"] == ["b"] and s["abs_diff"] > 0


def test_replay_impact_compares_arms_not_served():
    units = ([{"user_id": f"a{i}", "arm": "agent", "goal_id": "G01", "goal_status": "SUCCEEDED" if i < 5 else "FAILED",
               "played_7d": True, "purchased_14d": False, "served": i < 5} for i in range(10)] +
             [{"user_id": f"h{i}", "arm": "holdout", "goal_id": "G01",
               "goal_status": "SUCCEEDED" if i < 5 else "FAILED", "played_7d": True, "purchased_14d": False,
               "served": False} for i in range(10)])
    res = analyse_replay(units)
    assert res["goal_success_strict"]["abs_diff"] == 0.0
    assert res["naive_served_vs_unserved_within_agent_arm"]["abs_diff"] == 1.0
