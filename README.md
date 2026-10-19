# One User, One Agent

I built an agent that owns one viewer's goal, plan, actions, evidence and outcomes. It replays the supplied trigger
day through the supplied simulator and measures its own impact against the randomized holdout. My priorities were
correctness, auditability, safe failure and honest measurement, ahead of feature count.

* **Runtime:** Python 3.11+, standard library only. No API key is needed: the default model provider is a
  deterministic mock.
* **State:** SQLite (one authoritative row per viewer) plus JSONL exports. Every decision is a JSONL record.
* **Inputs:** the challenge pack is read-only. I never modify `data/`, `config/`, `pilot/`, `replay/` or `simulator/`.

## Submission checklist

| Requirement | Where it is |
|---|---|
| Runnable CLI, setup instructions, one replay command | [Setup](#setup) and [Replay](#replay-the-trigger-script-one-command) below; `src/one_user_one_agent/cli.py` |
| Durable per-viewer state | `sample_run/viewer_state/` (`state.sqlite`, `viewers.jsonl`, `goals.jsonl`, `goal_revisions.jsonl`) |
| Evidence references | `evidence_refs` in every record of `sample_run/decisions.jsonl` (source, time, freshness, conflict) |
| Decision outputs | `sample_run/decisions.jsonl`: one record per trigger delivery, including duplicates and failures |
| Provider abstraction / mock mode | `src/one_user_one_agent/model_provider.py`: the `ModelProvider` interface and `MockModelProvider` (no key, reproduces `replay/model_faults.jsonl`) |
| Test: late or duplicate trigger | `tests/test_agent.py::test_duplicate_trigger_is_idempotent`, `::test_offline_buffered_trigger_gets_no_home_page_and_corrected_time`, `::test_late_event_not_visible_before_it_is_received` |
| Test: conflicting source | `tests/test_agent.py::test_dialect_conflict_uses_app_setting_and_is_recorded`, `::test_deeplink_dialect_is_recorded_but_not_used`, `tests/test_verification.py::test_opt_out_event_overrides_profile_consent` |
| Test: tool failure | `tests/test_agent.py::test_store_degradation_fails_safely_and_allows_retry`, `::test_model_outage_timeout_and_malformed_output_fall_back`, `::test_unsupported_model_claim_is_rejected` |
| Test: corrected goal rule | `tests/test_goals_outcomes_impact.py::test_rule_correction_updates_only_open_goals_and_keeps_history` |
| Sample replay: decisions, actions, no-action reasons, outcome judgments, impact report | `sample_run/decisions.jsonl` (`status`, `no_action_reason`), `actions.jsonl`, `outcomes.jsonl`, `impact_report.md`, `final_report.md`, `latency_report.json` |
| Architecture note | [ARCHITECTURE.md](ARCHITECTURE.md): ownership, planning, memory, verification, latency, failure recovery, trust boundaries, plus a worked viewer journey |
| AI collaboration log | [AI_COLLABORATION.md](AI_COLLABORATION.md) |
| Known limitations | [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) |
| Plain-language walkthrough (extra) | [HOW_IT_WORKS.md](HOW_IT_WORKS.md): every calculation with worked numbers |

## Layout

```
submission/
  README.md  ARCHITECTURE.md  AI_COLLABORATION.md  KNOWN_LIMITATIONS.md  HOW_IT_WORKS.md
  pyproject.toml  requirements-dev.txt  .gitignore
  src/one_user_one_agent/    config, data_loader, catalogue, evidence, goals, model_provider, planner,
                             verification, decision_engine, state_store, replay, outcome_tracker,
                             impact_analysis, reporting, cli
  tests/                     39 tests (unit tests and an end-to-end replay with the real simulator)
  sample_run/                artifacts of the documented run
```

The submission sits inside the challenge pack (`one_user_one_agent_pack/submission/`). The CLI finds the pack
automatically by walking up from the current folder. To point it elsewhere, use `--pack <path>` or set `OUA_PACK`.

## Setup

From the `submission/` folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt     # pytest only; runtime needs nothing
```

macOS/Linux: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`

On Windows, if `python` prints "Python was not found", the Microsoft Store alias is intercepting it. Either disable
the alias (Settings > Apps > Advanced app settings > App execution aliases) or call the interpreter by its full path,
for example `& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv`.

Optionally run `pip install -e .`, which gives you the `oua` command (`oua replay --out sample_run`).

## Replay the trigger script (one command)

```powershell
$env:PYTHONPATH = "src"; .\.venv\Scripts\python.exe -m one_user_one_agent replay --out sample_run
```

bash: `PYTHONPATH=src .venv/bin/python -m one_user_one_agent replay --out sample_run`

That single command:

1. Loads and validates the pack. Invalid, duplicate and conflicting rows are counted in `validation_report.json`.
2. Feeds all 2,769 trigger deliveries in `deliver_at` order, merged with live events and control events (title
   withdrawal, goal-rule correction, model outage, slow data store).
3. Writes `decisions.jsonl` and `actions.jsonl`.
4. Runs `simulator/simulate_outcomes.py` unmodified, as a subprocess.
5. Judges every goal, confirms message delivery from simulator events only, and updates each viewer's learning state.
6. Writes `impact_report.md`, `latency_report.json` and `final_report.md`.

It takes about 45-60 s on a laptop. Options:
* `--seed <s>`: the simulator seed.
* `--pack <path>`: a different pack or replay day.
* `--skip-sim`: decisions only.

Pilot analysis only: `python -m one_user_one_agent pilot`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Result: 39 passed (about 65-100 s). Two of them are integration tests that run the real CLI and simulator against the
supplied files, check holdout isolation, the call budgets and the withdrawn title, and check that two runs produce
identical decisions.

## Results of the sample run (simulator seed `oua-practice`)

| | |
|---|---|
| Trigger deliveries | 2,769: 1,006 PUBLISHED, 1,672 NO_ACTION (1,311 are holdout shadow decisions), 90 DUPLICATE, 1 FAILED, 0 REJECTED |
| Actions | 1,360: 906 home pages, 444 pushes, 10 in-app messages |
| Policy | 0 problems reported by the simulator; 0 actions on holdout viewers |
| Injected faults | All 21 handled: 3 unsupported model claims rejected, 16 decisions in the model outage window, 1 hang cut at 5 s, 1 malformed output. All fell back to the deterministic rules |
| Latency | Median 405 ms, p95 409 ms, max 5.0 s (the injected hang). 0 decisions over the 60 s budget. Real compute median 6 ms |
| Pilot impact (randomized ITT) | 18.2% vs 19.6%: **−1.3 pp [−3.8, +1.2]**. The internal report's "2.3x uplift" compares served users with everyone else and is selection bias |
| Replay impact (randomized ITT, simulated) | Goal success 8.5% vs 8.4%: **+0.1 pp [−2.6, +2.7]**. Any play within 7 days: +2.7 pp [−1.8, +7.1]. No detectable effect |

I report the null result as it is. The system measures honestly; it has not been shown to improve outcomes, and
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) explains why and what I would try next.

## Artifacts (`sample_run/`)

| File | Content |
|---|---|
| `decisions.jsonl` | One record per trigger delivery: the trigger and its event age, the goal (eligibility basis, deadline, success signal, rule version), dialect resolution and conflicts, selected content, confidence and its basis, evidence references and freshness, assumptions, every verification check, the model call outcome, latency, call counts, status, and the no-action or failure reason |
| `actions.jsonl` | The verified, published actions, in the simulator's input schema |
| `outcomes.jsonl` | `goal_outcome` records (SUCCEEDED/FAILED/INCONCLUSIVE with evidence, rule version, judgment time) and `action_delivery` records (DELIVERED/FAILED only when the simulator confirmed it, otherwise UNCONFIRMED) |
| `viewer_state/` | The SQLite store and its exports: viewers (active goal, live page, message counters, learning state), goals, goal revisions |
| `latency_report.json` | Median, p95 and max processing time, failures, fallbacks, call counts |
| `impact_report.md`, `impact_analysis.json` | The pilot and replay intention-to-treat analyses |
| `final_report.md` | Statuses, no-action reasons, faults, control events, outcomes, simulator counts |
| `control_log.jsonl`, `validation_report.json` | How each control event was applied; input data-quality counts |
| `sim/` | Simulator output (`outcome_events.jsonl`, `simulator_report.json`) |
