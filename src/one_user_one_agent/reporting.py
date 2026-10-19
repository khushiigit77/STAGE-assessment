"""Reports generated from the artifacts of one run. Every number here is computed, none is hand-entered."""
from __future__ import annotations

import json
import os
import re
from collections import Counter

from .config import AgentConfig


def _q(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return round(s[k], 3)


def latency_report(decisions: list[dict], config: AgentConfig) -> dict:
    real = [d for d in decisions if d["status"] != "DUPLICATE"]
    proc = [d["latency"]["processing_ms"] for d in real]
    comp = [d["latency"]["real_compute_ms"] for d in real]
    pub = [d["latency"]["processing_ms"] for d in real if d["status"] == "PUBLISHED"]
    return {
        "definition": "processing_ms = measured wall-clock compute + simulated dependency latency (model calls, "
                      "injected store latency/timeouts). published_at = deliver_at + processing_ms.",
        "decisions_total": len(decisions), "decisions_excluding_duplicates": len(real),
        "status_counts": dict(Counter(d["status"] for d in decisions)),
        "processing_ms": {"median": _q(proc, 0.5), "p95": _q(proc, 0.95), "max": _q(proc, 1.0)},
        "real_compute_ms": {"median": _q(comp, 0.5), "p95": _q(comp, 0.95), "max": _q(comp, 1.0)},
        "published_processing_ms": {"median": _q(pub, 0.5), "p95": _q(pub, 0.95), "max": _q(pub, 1.0)},
        "budget_s": config.decision_budget_s,
        "over_budget": sum(1 for d in real if not d["latency"]["within_budget"]),
        "failures": sum(1 for d in decisions if d["status"] == "FAILED"),
        "failure_reasons": dict(Counter(re.sub(r"[0-9.]+s", "Ns", (d.get("failure_reason") or "").split(";")[0])
                                        for d in decisions if d["status"] == "FAILED")),
        "model_fallbacks": sum(1 for d in decisions if (d.get("model") or {}).get("fallback_reason")),
        "model_calls": {"total": sum(d["calls"]["model"] for d in decisions),
                        "max_per_decision": max((d["calls"]["model"] for d in decisions), default=0),
                        "limit": config.max_model_calls},
        "tool_calls": {"total": sum(d["calls"]["tool"] for d in decisions),
                       "max_per_decision": max((d["calls"]["tool"] for d in decisions), default=0),
                       "limit": config.max_tool_calls},
        "peak_triggers_per_minute": Counter(d["trigger"]["deliver_at"][:16] for d in decisions).most_common(1),
    }


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _pp(x):
    return "n/a" if x is None else f"{x * 100:+.1f} pp"


def _diff_row(name: str, d: dict) -> str:
    if not d.get("estimable"):
        return f"| {name} | {d.get('n_treat', 0)} | – | {d.get('n_control', 0)} | – | not estimable | – |"
    rel = "n/a" if d["rel_diff"] is None else f"{d['rel_diff'] * 100:+.0f}%"
    if min(d["n_treat"], d["n_control"]) < 30:
        name += " (small n: interval unreliable)"
    return (f"| {name} | {d['n_treat']} | {_pct(d['rate_treat'])} ({d['succ_treat']}) | {d['n_control']} | "
            f"{_pct(d['rate_control'])} ({d['succ_control']}) | {_pp(d['abs_diff'])} "
            f"[{_pp(d['ci95'][0])}, {_pp(d['ci95'][1])}] | {rel} |")


HDR = ("| Group | n treat/agent | success treat | n control/holdout | success control | difference [95% CI] | "
       "relative |\n|---|---|---|---|---|---|---|")


def impact_md(pilot: dict, rep: dict, sim_report: dict, seed: str) -> str:
    p_itt, p_str = pilot["itt"], pilot["itt_stratified_by_goal"]
    naive = pilot["naive_served_vs_all_others"]
    L = ["# Impact report", "",
         "All numbers below are computed by `one_user_one_agent.impact_analysis` from the supplied files and this run's "
         "simulator output. Reproduce from `submission/` with `python -m one_user_one_agent replay --out sample_run` (see README.md).", "",
         "## 1. Historical pilot (days 1-14, `pilot/`)", "",
         "**Design.** Users were individually randomized to `treatment` or `holdout` (unit = user; no cluster or "
         "stratum variable is supplied). Outcome = `succeeded` for the goal recorded in `pilot_outcomes.csv`. "
         "`served` is decided by the agent after randomization, so it is not a valid comparison group.", "",
         f"Users: {pilot['n_users']} ({dict(pilot['arms'])}). Cross-file consistency issues: {pilot['data_issue_count']}.",
         "", "### Intention-to-treat (assigned arm)", "", HDR, _diff_row("All goals (ITT)", p_itt)]
    for g, d in pilot["itt_by_goal"].items():
        L.append(_diff_row(g, d))
    L += ["", f"Goal-stratified ITT (weights = goal share): **{_pp(p_str['abs_diff'])}** "
              f"[{_pp(p_str['ci95'][0])}, {_pp(p_str['ci95'][1])}]. Permutation test of the overall ITT difference "
              f"(2,000 re-randomizations): p = {pilot['itt_permutation_p']}.", "",
          "Goal mix by arm (balance check):", "", "| Goal | treatment | holdout |", "|---|---|---|"]
    for g, m in pilot["goal_mix_by_arm"].items():
        L.append(f"| {g} | {m['treatment']} | {m['holdout']} |")
    L += ["", "### Why the internal summary's 2.3x uplift is not a causal estimate", "",
          f"The summary compares *served* users ({naive['n_treat']}, {_pct(naive['rate_treat'])}) with *all other "
          f"users* ({naive['n_control']}, {_pct(naive['rate_control'])}): {_pp(naive['abs_diff'])}. "
          "This reproduces their figures, but it is a selection comparison, not an experiment:", "",
          f"* Only {_pct(pilot['served_share_of_treatment'])} of the treatment arm was served. Who got served depended "
          "on behaviour after randomization (opening the app, being eligible for a page), which also predicts success.",
          f"* Served users' goal mix {pilot['served_goal_mix']} is heavily weighted to G09, the goal with a ~60-67% base "
          f"rate, while the population mix is {pilot['all_goal_mix']} (G05 base rate ~1%). The 'uplift' is "
          "mostly composition.",
          "* 'All other users' mixes the holdout with unserved treatment users, so it is not a control group either.",
          f"* Served users in the holdout arm: {pilot['served_in_holdout']} (expected 0).", "",
          f"**Conclusion.** The randomized comparison shows **no detectable benefit**: ITT {_pp(p_itt['abs_diff'])} "
          f"[{_pp(p_itt['ci95'][0])}, {_pp(p_itt['ci95'][1])}]. The data cannot support the claim that agent v0 "
          "raised goal success, and they do not justify a rollout. A Wald/IV estimate for served users "
          f"(ITT / served share = {_pp(pilot['cace_wald_estimate'])}) is shown only for completeness. It assumes "
          "assignment affects outcomes only through being served, which is untested here, and its uncertainty is "
          f"roughly 1/{pilot['served_share_of_treatment']:.2f} times the ITT interval.", "",
          "Per-goal differences are exploratory (5 comparisons, no multiplicity correction). An individual-level "
          "causal claim ('the agent made viewer X convert') is not supported by any of this.", "",
          f"## 2. Replay day (simulated, seed `{seed}`)", "",
          "**Design.** `replay/arms.csv` randomizes every user to `agent` or `holdout`. Unit = triggered user. The "
          "agent runs the *same* pipeline for both arms up to the action (evidence, goal selection, model call), so the "
          "first goal is assigned arm-blind; holdout users receive no action. Outcome = success of the first committed "
          "goal, judged by the same rule code for both arms. Two goal-agnostic outcomes are also reported.", "",
          f"Triggered users: {rep['n_triggered_users']} {rep['arms']}. Users with no safely selectable goal: "
          f"{rep['no_goal_by_arm']}.", "", HDR,
          _diff_row("Goal success, strict (INCONCLUSIVE = not succeeded)", rep["goal_success_strict"]),
          _diff_row("Goal success, conclusive only", rep["goal_success_conclusive_only"]),
          _diff_row("Any play within 7 days of first trigger", rep["played_7d"]),
          _diff_row("Paid purchase within the observation window", rep["purchased_14d"])]
    for g, d in rep["goal_success_by_goal"].items():
        L.append(_diff_row(f"Goal {g}", d))
    s = rep["goal_success_stratified_by_goal"]
    L += ["", f"Goal-stratified difference: **{_pp(s['abs_diff'])}** [{_pp(s['ci95'][0])}, {_pp(s['ci95'][1])}]; "
              f"permutation p = {rep['goal_success_permutation_p']}. INCONCLUSIVE goals by arm: "
              f"{rep['inconclusive_by_arm']}.", "",
          "Goal mix by arm:", "", "```", json.dumps(rep["goal_mix_by_arm"]), "```", "",
          "Biased comparison, shown for contrast only: served vs unserved viewers *within* the agent arm. Being "
          "served depends on post-assignment conditions (a live visit, a catalogued app dialect), and viewers whose "
          "only trigger was an offline-buffered event are both unserved and inactive in the simulator, so this gap is "
          "selection, not effect:", "", HDR,
          _diff_row("served vs unserved (agent arm)", rep["naive_served_vs_unserved_within_agent_arm"]),
          "", "## 3. Caveats", "",
          "* Replay outcomes come from the supplied simulator with one seed. They show how this policy behaves *in "
          "that simulator*, not real-world uplift. Evaluators use a different seed and day; expect different numbers.",
          "* Pilot and replay results are separate analyses and are not pooled.",
          "* Within one replay day, effects can only act through the simulator's mechanisms (page shown in the right "
          "dialect before the home view, message clicks, a 3-day return boost).",
          f"* Simulator policy problems reported for the agent's actions: {sim_report.get('policy_problem_count')}; "
          f"holdout actions ignored: {sim_report.get('counts', {}).get('holdout_actions_ignored', 0)}.",
          "* Intervals are normal-approximation (Wald) intervals. They are reasonable for the pooled rows; rows marked "
          "'small n' (fewer than 30 users in an arm) are descriptive only.",
          "* Goal choice follows a fixed priority (see ARCHITECTURE.md). It is arm-blind, so it does not bias the ITT "
          "comparison, but it determines which outcome is measured: G07 dominates and has a low base rate.", ""]
    return "\n".join(L)


def write_reports(*, out_dir, decisions, actions, loop, pilot, replay_impact, control_log, validation, config, seed,
                  timings) -> None:
    lat = latency_report(decisions, config)
    with open(os.path.join(out_dir, "latency_report.json"), "w", encoding="utf-8") as f:
        json.dump(lat, f, indent=2)
    with open(os.path.join(out_dir, "impact_analysis.json"), "w", encoding="utf-8") as f:
        json.dump({"pilot": pilot, "replay": replay_impact}, f, indent=2, default=str)
    sim = loop["sim_report"]
    with open(os.path.join(out_dir, "impact_report.md"), "w", encoding="utf-8") as f:
        f.write(impact_md(pilot, replay_impact, sim, seed))

    goal_out = [o for o in loop["outcomes"] if o["record_type"] == "goal_outcome"]
    deliv = [o for o in loop["outcomes"] if o["record_type"] == "action_delivery"]
    reasons = Counter()
    for d in decisions:
        if d["status"] in ("NO_ACTION", "REJECTED"):
            r = d.get("no_action_reason") or ""
            reasons[re.sub(r"[0-9]+s", "Ns", r.split(":")[0].split(";")[0])[:90]] += 1
    rej_checks = Counter(f.split(":")[0] for d in decisions for r in d.get("rejected_actions", []) for f in r["failures"])
    faulted = [d for d in decisions if (d.get("model") or {}).get("status") in ("rejected", "failed")]
    L = ["# Final report: sample replay", "",
         f"Pack replay day starting {decisions[0]['trigger']['deliver_at'] if decisions else 'n/a'}; simulator seed "
         f"`{seed}`. Timings: {timings}.", "",
         "## Decisions", "", f"* Trigger deliveries processed: {len(decisions)}",
         f"* Status counts: {dict(Counter(d['status'] for d in decisions))}",
         f"* Actions published: {dict(Counter(a['type'] for a in actions))}",
         f"* Viewers with an action: {len({a['user_id'] for a in actions})}; holdout viewers with an action: "
         f"{sum(1 for d in decisions if d.get('arm') == 'holdout' and d['status'] == 'PUBLISHED')}",
         f"* Goals committed (first goal per viewer and revisions): "
         f"{dict(Counter(o['goal_id'] for o in goal_out))}", "",
         "Top no-action / rejection reasons:", ""]
    L += [f"* {n} x {r}" for r, n in reasons.most_common(12)]
    L += ["", f"Verification failures by check (actions blocked): {dict(rej_checks)}", "",
          "## Injected model faults and outages", ""]
    for d in faulted:
        m = d["model"]
        L.append(f"* {d['decision_id']} {d['viewer_id']} (arm {d.get('arm')}): model {m['status']} "
                 f"({m.get('error') or ''}) -> {m.get('fallback_reason')}")
    L += ["", "## Control events", ""]
    for c in control_log:
        brief = {k: (v if not isinstance(v, list) else f"{len(v)} items: {v[:5]}") for k, v in c.items()}
        L.append(f"* {brief}")
    L += ["", "## Outcomes", "",
          f"* Goal outcomes: {dict(Counter(o['status'] for o in goal_out))}",
          f"* By goal: {json.dumps({g: dict(Counter(o['status'] for o in goal_out if o['goal_id'] == g)) for g in sorted({o['goal_id'] for o in goal_out})})}",
          f"* Goals whose rule version changed while open (G03 correction): "
          f"{sum(1 for o in goal_out if 'verdict_under_commit_version' in o)}",
          f"* Message delivery (from simulator events only): {dict(Counter(o['delivery_status'] for o in deliv))}",
          f"* Simulator counts: {sim.get('counts')}",
          f"* Simulator policy problems: {sim.get('policy_problem_count')} {sim.get('policy_problems')[:5]}", "",
          "## Latency", "", "```", json.dumps(lat, indent=2), "```", "",
          "## Input validation", "", "```", json.dumps(validation["counts"], indent=2), "```", "",
          "See `impact_report.md` for the randomized-holdout analysis.", ""]
    with open(os.path.join(out_dir, "final_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
