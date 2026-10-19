# Impact report

All numbers below are computed by `one_user_one_agent.impact_analysis` from the supplied files and this run's simulator output. Reproduce from `submission/` with `python -m one_user_one_agent replay --out sample_run` (see README.md).

## 1. Historical pilot (days 1-14, `pilot/`)

**Design.** Users were individually randomized to `treatment` or `holdout` (unit = user; no cluster or stratum variable is supplied). Outcome = `succeeded` for the goal recorded in `pilot_outcomes.csv`. `served` is decided by the agent after randomization, so it is not a valid comparison group.

Users: 3891 ({'treatment': 1912, 'holdout': 1979}). Cross-file consistency issues: 0.

### Intention-to-treat (assigned arm)

| Group | n treat/agent | success treat | n control/holdout | success control | difference [95% CI] | relative |
|---|---|---|---|---|---|---|
| All goals (ITT) | 1912 | 18.2% (349) | 1979 | 19.6% (387) | -1.3 pp [-3.8 pp, +1.2 pp] | -7% |
| G02 | 177 | 19.2% (34) | 183 | 19.7% (36) | -0.5 pp [-8.6 pp, +7.7 pp] | -2% |
| G04 | 63 | 23.8% (15) | 66 | 22.7% (15) | +1.1 pp [-13.5 pp, +15.7 pp] | +5% |
| G05 | 1045 | 1.2% (13) | 1073 | 0.6% (6) | +0.7 pp [-0.1 pp, +1.5 pp] | +122% |
| G06 | 157 | 0.6% (1) | 174 | 2.9% (5) | -2.2 pp [-5.0 pp, +0.5 pp] | -78% |
| G09 | 470 | 60.9% (286) | 483 | 67.3% (325) | -6.4 pp [-12.5 pp, -0.4 pp] | -10% |

Goal-stratified ITT (weights = goal share): **-1.4 pp** [-3.2 pp, +0.4 pp]. Permutation test of the overall ITT difference (2,000 re-randomizations): p = 0.3053.

Goal mix by arm (balance check):

| Goal | treatment | holdout |
|---|---|---|
| G02 | 177 | 183 |
| G04 | 63 | 66 |
| G05 | 1045 | 1073 |
| G06 | 157 | 174 |
| G09 | 470 | 483 |

### Why the internal summary's 2.3x uplift is not a causal estimate

The summary compares *served* users (217, 40.1%) with *all other users* (3674, 17.7%): +22.4 pp. This reproduces their figures, but it is a selection comparison, not an experiment:

* Only 11.3% of the treatment arm was served. Who got served depended on behaviour after randomization (opening the app, being eligible for a page), which also predicts success.
* Served users' goal mix {'G09': 105, 'G05': 84, 'G02': 15, 'G06': 12, 'G04': 1} is heavily weighted to G09, the goal with a ~60-67% base rate, while the population mix is {'G05': 2118, 'G06': 331, 'G09': 953, 'G02': 360, 'G04': 129} (G05 base rate ~1%). The 'uplift' is mostly composition.
* 'All other users' mixes the holdout with unserved treatment users, so it is not a control group either.
* Served users in the holdout arm: 0 (expected 0).

**Conclusion.** The randomized comparison shows **no detectable benefit**: ITT -1.3 pp [-3.8 pp, +1.2 pp]. The data cannot support the claim that agent v0 raised goal success, and they do not justify a rollout. A Wald/IV estimate for served users (ITT / served share = -11.5 pp) is shown only for completeness. It assumes assignment affects outcomes only through being served, which is untested here, and its uncertainty is roughly 1/0.11 times the ITT interval.

Per-goal differences are exploratory (5 comparisons, no multiplicity correction). An individual-level causal claim ('the agent made viewer X convert') is not supported by any of this.

## 2. Replay day (simulated, seed `oua-practice`)

**Design.** `replay/arms.csv` randomizes every user to `agent` or `holdout`. Unit = triggered user. The agent runs the *same* pipeline for both arms up to the action (evidence, goal selection, model call), so the first goal is assigned arm-blind; holdout users receive no action. Outcome = success of the first committed goal, judged by the same rule code for both arms. Two goal-agnostic outcomes are also reported.

Triggered users: 1746 {'holdout': 883, 'agent': 863}. Users with no safely selectable goal: {'holdout': 14, 'agent': 13}.

| Group | n treat/agent | success treat | n control/holdout | success control | difference [95% CI] | relative |
|---|---|---|---|---|---|---|
| Goal success, strict (INCONCLUSIVE = not succeeded) | 850 | 8.5% (72) | 869 | 8.4% (73) | +0.1 pp [-2.6 pp, +2.7 pp] | +1% |
| Goal success, conclusive only | 850 | 8.5% (72) | 869 | 8.4% (73) | +0.1 pp [-2.6 pp, +2.7 pp] | +1% |
| Any play within 7 days of first trigger | 863 | 67.3% (581) | 883 | 64.7% (571) | +2.7 pp [-1.8 pp, +7.1 pp] | +4% |
| Paid purchase within the observation window | 863 | 4.2% (36) | 883 | 4.1% (36) | +0.1 pp [-1.8 pp, +2.0 pp] | +2% |
| Goal G01 | 61 | 32.8% (20) | 53 | 28.3% (15) | +4.5 pp [-12.4 pp, +21.4 pp] | +16% |
| Goal G02 (small n: interval unreliable) | 21 | 23.8% (5) | 21 | 4.8% (1) | +19.1 pp [-1.3 pp, +39.4 pp] | +400% |
| Goal G03 | 1 | – | 0 | – | not estimable | – |
| Goal G04 (small n: interval unreliable) | 1 | 0.0% (0) | 1 | 0.0% (0) | +0.0 pp [+0.0 pp, +0.0 pp] | n/a |
| Goal G05 | 140 | 3.6% (5) | 125 | 4.8% (6) | -1.2 pp [-6.1 pp, +3.6 pp] | -26% |
| Goal G06 (small n: interval unreliable) | 9 | 0.0% (0) | 10 | 10.0% (1) | -10.0 pp [-28.6 pp, +8.6 pp] | -100% |
| Goal G07 | 512 | 2.1% (11) | 543 | 3.1% (17) | -1.0 pp [-2.9 pp, +0.9 pp] | -31% |
| Goal G08 | 31 | 29.0% (9) | 41 | 26.8% (11) | +2.2 pp [-18.8 pp, +23.2 pp] | +8% |
| Goal G09 (small n: interval unreliable) | 19 | 15.8% (3) | 15 | 13.3% (2) | +2.5 pp [-21.3 pp, +26.2 pp] | +18% |
| Goal G12 | 55 | 34.5% (19) | 60 | 33.3% (20) | +1.2 pp [-16.1 pp, +18.5 pp] | +4% |

Goal-stratified difference: **+0.1 pp** [-2.3 pp, +2.5 pp]; permutation p = 1.0. INCONCLUSIVE goals by arm: {}.

Goal mix by arm:

```
{"G01": {"holdout": 53, "agent": 61}, "G02": {"agent": 21, "holdout": 21}, "G03": {"agent": 1}, "G04": {"agent": 1, "holdout": 1}, "G05": {"agent": 140, "holdout": 125}, "G06": {"agent": 9, "holdout": 10}, "G07": {"holdout": 543, "agent": 512}, "G08": {"holdout": 41, "agent": 31}, "G09": {"agent": 19, "holdout": 15}, "G12": {"holdout": 60, "agent": 55}}
```

Biased comparison, shown for contrast only: served vs unserved viewers *within* the agent arm. Being served depends on post-assignment conditions (a live visit, a catalogued app dialect), and viewers whose only trigger was an offline-buffered event are both unserved and inactive in the simulator, so this gap is selection, not effect:

| Group | n treat/agent | success treat | n control/holdout | success control | difference [95% CI] | relative |
|---|---|---|---|---|---|---|
| served vs unserved (agent arm) | 768 | 9.4% (72) | 82 | 0.0% (0) | +9.4 pp [+7.3 pp, +11.4 pp] | n/a |

## 3. Caveats

* Replay outcomes come from the supplied simulator with one seed. They show how this policy behaves *in that simulator*, not real-world uplift. Evaluators use a different seed and day; expect different numbers.
* Pilot and replay results are separate analyses and are not pooled.
* Within one replay day, effects can only act through the simulator's mechanisms (page shown in the right dialect before the home view, message clicks, a 3-day return boost).
* Simulator policy problems reported for the agent's actions: 0; holdout actions ignored: 0.
* Intervals are normal-approximation (Wald) intervals. They are reasonable for the pooled rows; rows marked 'small n' (fewer than 30 users in an arm) are descriptive only.
* Goal choice follows a fixed priority (see ARCHITECTURE.md). It is arm-blind, so it does not bias the ITT comparison, but it determines which outcome is measured: G07 dominates and has a low base rate.
