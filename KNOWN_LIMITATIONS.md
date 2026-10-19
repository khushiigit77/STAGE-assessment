# Known limitations

## What I intentionally left incomplete

| Item | Why I left it | What it would take |
|---|---|---|
| **WhatsApp is never proposed** | No approved template can be filled truthfully from the pack. `wa_new_episode_v2` claims a *new* episode (episodes have no release dates), `wa_recommend_title_v1` needs a dialect *name* (only codes exist), and `wa_trial_ending_v1` implies an end date nobody knows. I preferred no message to a dishonest one. The verifier still fully supports WhatsApp, and this is tested | Episode release dates, dialect display names, trial end dates |
| **No real LLM provider** | The brief asks that evaluators need no personal key. The mock returns the deterministic first choice, so in this version the model adds no decision value; the interface, schema validation, fallbacks and fault reproduction are real | A provider implementing `ModelProvider` with the same validation, plus a prompt-injection test set |
| **Learning state is recorded but not used** | Each viewer's outcome history is written back to their state, but goal choice uses a fixed priority. With one replay day there is no second decision cycle that could use it honestly | Use per-goal success by segment (measured against the holdout) to rank goals |
| **Pushes are not re-verified at send time** | They are scheduled for the next day at 19:00 IST and fully verified at decision time. The simulator interface has no send-time hook | A send-time verifier for consent, caps and title availability |
| **Published pages cannot be recalled** | The action interface has no recall. When T0001 was withdrawn, the 33 affected viewers were marked `needs_refresh` and get a corrected page on their next trigger | A recall or replace operation on the page store |
| **In-app messages** are used only for G04/G06 (10 sent) | The simulator gives them no effect and no delivery event, so their delivery is reported as `UNCONFIRMED` | A delivery or impression event |
| **Single-process replay** | Correctness (idempotency, compare-and-set) was the priority. Stale-write protection is unit-tested but never contended in the replay | Partitioned workers per viewer; keep the CAS and the idempotency ledger |

## What this submission does not prove

* **That the agent helps.** Pilot ITT: −1.3 pp [−3.8, +1.2]. Replay ITT on goal success: +0.1 pp [−2.6, +2.7]. I built
  the system to measure honestly, and the honest answer is "no detectable effect".
* **Anything about real viewers.** Replay outcomes come from the supplied simulator with one seed. A different seed or
  replay day will give different numbers.
* **Per-goal effects.** These are exploratory: many comparisons, small cells (flagged when n < 30), no multiplicity
  correction, and approximate intervals for rare outcomes (G05, G06).
* **Individual causality.** No claim that "the agent made viewer X convert" is possible from arm-level evidence.

## Assumptions I had to make

* **Goal priority.** The brief doesn't specify it. I put time-sensitive, content-actionable goals first: G04, G01, G03,
  G07, G12, G02, G06, G09, G08, G05. G07 ends up chosen for 61% of viewers and has about 2-3% success in the
  simulator, because simulated organic plays rarely pick the specific series. Priority is arm-blind, so the ITT stays
  valid, but it determines which outcome is measured.
* **G06** "recently lapsed" = a lapse event within 30 days. **G10** (no success definition) and **G11** (no
  `tv_app_open` event exists) are never selected.
* **"Never did X"** (G01/G02) is trusted only when the signup falls inside the 30-day history. **G12** needs a signup
  at least 14 days before the trigger.
* An observed **opt-out** is permanent, because re-opt-in is not observable.
* **Duplicate profile rows**: the latest `updated_at` wins, and the conflict stays visible.
* **Message dialect** = the last observed app dialect.

## Other simplifications

* **Latency is partly simulated.** Compute time is measured; dependency latency (model 0.4 s, store degradation, the
  5 s timeout) is injected deterministically from the control events.
* **Confidence** is an evidence-coverage score (six named items), not a calibrated probability.
* **Popularity** comes from the 14 days of history before the replay and is not updated during the day.
* **Large decision log.** `decisions.jsonl` is about 20 MB per day, because every check and evidence reference is
  inlined for auditability. Production would store references and sample the full traces.

## What I would do next, in order

1. **Re-rank goals by measured incremental effect** (agent vs holdout per goal) rather than a fixed priority, starting
   with G01 and G02, where the replay shows the largest positive point estimates (both small samples, so this is
   a hypothesis to test, not a finding).
2. **Add send-time re-verification and page recall**, then run the experiment across several simulator seeds,
   reporting the spread of estimates.
3. **Pre-register the next experiment**: one primary metric, a power calculation for the minimum detectable effect,
   and guardrails (opt-outs, delivery failures).
4. **Plug in a real model** behind the existing validation, and only where it adds value: ranking titles for the page,
   never eligibility or policy.
5. **Production hardening**: retention limits and access control for the logs, which contain personal data, plus
   monitoring of fallback rate, rejection rate and p95 latency.
