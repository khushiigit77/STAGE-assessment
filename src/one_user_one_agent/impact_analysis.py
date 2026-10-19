"""Randomized-holdout impact analysis (intention to treat).

The randomization unit is the user (``pilot/pilot_assignment.csv``, ``replay/arms.csv``); no cluster or stratum
variable is supplied, so the analysis uses independent two-proportion comparisons:

* ITT difference = success rate(assigned treatment/agent) - success rate(assigned holdout), Wald 95% CI.
* A goal-stratified (post-stratified) difference, because success base rates differ ~100x across goals. Goals were
  assigned from pre-treatment status, so stratifying on them does not condition on a post-treatment variable.
* A permutation test of the overall ITT difference (re-randomizing arm labels, fixed seed).

``served`` is a post-randomization outcome of the agent's own behaviour, so served-vs-others comparisons are shown
only to explain why they are biased.
"""
from __future__ import annotations

import csv
import math
import random
from collections import Counter, defaultdict

Z95 = 1.959964


def prop_diff(s1: int, n1: int, s0: int, n0: int) -> dict:
    if n1 == 0 or n0 == 0:
        return {"n_treat": n1, "n_control": n0, "estimable": False}
    p1, p0 = s1 / n1, s0 / n0
    se = math.sqrt(p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
    d = p1 - p0
    return {"n_treat": n1, "succ_treat": s1, "rate_treat": round(p1, 4), "n_control": n0, "succ_control": s0,
            "rate_control": round(p0, 4), "abs_diff": round(d, 4), "se": round(se, 4),
            "ci95": [round(d - Z95 * se, 4), round(d + Z95 * se, 4)],
            "rel_diff": round(d / p0, 4) if p0 > 0 else None, "estimable": True}


def stratified_diff(rows: list[tuple[str, bool, bool]]) -> dict:
    """rows = (stratum, treated, success). Weights = stratum share of all units."""
    cell = defaultdict(lambda: [0, 0, 0, 0])  # n1, s1, n0, s0
    for s, t, y in rows:
        c = cell[s]
        if t:
            c[0] += 1
            c[1] += y
        else:
            c[2] += 1
            c[3] += y
    N = sum(c[0] + c[2] for c in cell.values())
    est, var, used, skipped = 0.0, 0.0, [], []
    for s, (n1, s1, n0, s0) in sorted(cell.items()):
        if n1 == 0 or n0 == 0:
            skipped.append(s)
            continue
        w = (n1 + n0) / N
        p1, p0 = s1 / n1, s0 / n0
        est += w * (p1 - p0)
        var += w * w * (p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
        used.append(s)
    se = math.sqrt(var)
    return {"abs_diff": round(est, 4), "se": round(se, 4), "ci95": [round(est - Z95 * se, 4), round(est + Z95 * se, 4)],
            "strata_used": used, "strata_skipped_one_arm_only": skipped}


def permutation_p(treated: list[bool], success: list[bool], n_perm: int = 2000, seed: int = 20261001) -> float:
    n1 = sum(treated)
    n0 = len(treated) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    tot = sum(success)

    def diff(t):
        s1 = sum(y for x, y in zip(t, success) if x)
        return s1 / n1 - (tot - s1) / n0
    obs = abs(diff(treated))
    rng = random.Random(seed)
    labels = list(treated)
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(labels)
        hits += abs(diff(labels)) >= obs - 1e-12
    return round((hits + 1) / (n_perm + 1), 4)


def analyse_pilot(assign_path: str, outcomes_path: str) -> dict:
    with open(assign_path, encoding="utf-8") as f:
        assign = {r["user_id"]: r for r in csv.DictReader(f)}
    with open(outcomes_path, encoding="utf-8") as f:
        outs = list(csv.DictReader(f))
    issues = []
    rows = []
    for r in outs:
        a = assign.get(r["user_id"])
        if a is None:
            issues.append(f"{r['user_id']} has an outcome but no assignment")
            continue
        if a["arm"] != r["arm"] or a["served"] != r["served"]:
            issues.append(f"{r['user_id']} arm/served differs between files")
        rows.append((r["goal_id"], a["arm"] == "treatment", r["succeeded"] == "true", a["served"] == "true"))
    treated = [t for _, t, _, _ in rows]
    success = [y for _, _, y, _ in rows]
    itt = prop_diff(sum(y for t, y in zip(treated, success) if t), sum(treated),
                    sum(y for t, y in zip(treated, success) if not t), len(treated) - sum(treated))
    by_goal = {}
    for g in sorted({r[0] for r in rows}):
        sub = [r for r in rows if r[0] == g]
        by_goal[g] = prop_diff(sum(1 for r in sub if r[1] and r[2]), sum(1 for r in sub if r[1]),
                               sum(1 for r in sub if not r[1] and r[2]), sum(1 for r in sub if not r[1]))
    served = [r for r in rows if r[3]]
    others = [r for r in rows if not r[3]]
    naive = prop_diff(sum(r[2] for r in served), len(served), sum(r[2] for r in others), len(others))
    n_t = sum(treated)
    compliance = len(served) / n_t if n_t else 0
    mix_served = Counter(r[0] for r in served)
    mix_all = Counter(r[0] for r in rows)
    return {
        "n_users": len(rows), "arms": Counter("treatment" if t else "holdout" for t in treated),
        "data_issues": issues[:20], "data_issue_count": len(issues),
        "itt": itt, "itt_stratified_by_goal": stratified_diff([(r[0], r[1], r[2]) for r in rows]),
        "itt_permutation_p": permutation_p(treated, success),
        "itt_by_goal": by_goal,
        "goal_mix_by_arm": {g: {"treatment": sum(1 for r in rows if r[0] == g and r[1]),
                                "holdout": sum(1 for r in rows if r[0] == g and not r[1])} for g in sorted(mix_all)},
        "naive_served_vs_all_others": naive,
        "served_share_of_treatment": round(compliance, 4),
        "served_goal_mix": dict(mix_served), "all_goal_mix": dict(mix_all),
        "served_in_holdout": sum(1 for r in rows if r[3] and not r[1]),
        "cace_wald_estimate": (round(itt["abs_diff"] / compliance, 4) if compliance else None),
    }


def analyse_replay(units: list[dict]) -> dict:
    """units: one per triggered viewer: {user_id, arm, goal_id, goal_status, played_7d, purchased_14d, served}."""
    out = {"n_triggered_users": len(units), "arms": dict(Counter(u["arm"] for u in units))}
    with_goal = [u for u in units if u.get("goal_id")]
    strict = [(u["goal_id"], u["arm"] == "agent", u["goal_status"] == "SUCCEEDED") for u in with_goal]
    out["goal_success_strict"] = prop_diff(*_counts(strict))
    out["goal_success_strict_note"] = "INCONCLUSIVE counted as not succeeded"
    conclusive = [x for x, u in zip(strict, with_goal) if u["goal_status"] in ("SUCCEEDED", "FAILED")]
    out["goal_success_conclusive_only"] = prop_diff(*_counts(conclusive))
    out["inconclusive_by_arm"] = dict(Counter(u["arm"] for u in with_goal if u["goal_status"] == "INCONCLUSIVE"))
    out["goal_success_stratified_by_goal"] = stratified_diff(strict)
    out["goal_success_permutation_p"] = permutation_p([x[1] for x in strict], [x[2] for x in strict])
    out["goal_success_by_goal"] = {g: prop_diff(*_counts([x for x in strict if x[0] == g]))
                                   for g in sorted({x[0] for x in strict})}
    out["goal_mix_by_arm"] = {g: dict(Counter(u["arm"] for u in with_goal if u["goal_id"] == g))
                              for g in sorted({u["goal_id"] for u in with_goal})}
    out["no_goal_by_arm"] = dict(Counter(u["arm"] for u in units if not u.get("goal_id")))
    for k in ("played_7d", "purchased_14d"):
        rows = [("all", u["arm"] == "agent", bool(u[k])) for u in units]
        out[k] = prop_diff(*_counts(rows))
    agent = [u for u in units if u["arm"] == "agent" and u.get("goal_id")]
    srv = [u for u in agent if u["served"]]
    uns = [u for u in agent if not u["served"]]
    out["naive_served_vs_unserved_within_agent_arm"] = prop_diff(
        sum(u["goal_status"] == "SUCCEEDED" for u in srv), len(srv),
        sum(u["goal_status"] == "SUCCEEDED" for u in uns), len(uns))
    return out


def _counts(rows):
    n1 = sum(1 for r in rows if r[1])
    s1 = sum(1 for r in rows if r[1] and r[2])
    n0 = len(rows) - n1
    s0 = sum(1 for r in rows if not r[1] and r[2])
    return s1, n1, s0, n0
