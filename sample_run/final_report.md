# Final report: sample replay

Pack replay day starting 2026-09-30T18:32:59.458Z; simulator seed `oua-practice`. Timings: {'decisions': 2769, 'actions': 1360, 'load_seconds': 1.77, 'decision_loop_seconds': 21.78, 'total_seconds': 49.05}.

## Decisions

* Trigger deliveries processed: 2769
* Status counts: {'PUBLISHED': 1006, 'NO_ACTION': 1672, 'DUPLICATE': 90, 'FAILED': 1}
* Actions published: {'home_page': 906, 'push': 444, 'in_app_message': 10}
* Viewers with an action: 768; holdout viewers with an action: 0
* Goals committed (first goal per viewer and revisions): {'G07': 1055, 'G06': 19, 'G02': 42, 'G05': 265, 'G12': 115, 'G08': 72, 'G09': 36, 'G01': 114, 'G04': 2, 'G03': 1}

Top no-action / rejection reasons:

* 1311 x holdout arm ('holdout')
* 256 x home page skipped
* 38 x trigger app_dialect is missing/invalid (None)
* 31 x app dialect 'd7' has no titles in the catalogue
* 30 x no goal can be safely selected
* 6 x trigger app_dialect is missing/invalid ('NA')

Verification failures by check (actions blocked): {}

## Injected model faults and outages

* D-000438 U-04860 (arm agent): model rejected () -> model proposal rejected: proposed_goal_is_eligible: model proposed 'G04'; deterministic eligible set ['G07', 'G05']; subscription_status='never'
* D-000576 U-02509 (arm agent): model rejected () -> model proposal rejected: proposed_goal_is_eligible: model proposed 'G04'; deterministic eligible set ['G05']; subscription_status='never'
* D-000900 U-01991 (arm agent): model rejected () -> model proposal rejected: proposed_goal_is_eligible: model proposed 'G04'; deterministic eligible set ['G07', 'G05']; subscription_status='never'
* D-001314 U-03051 (arm agent): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001315 U-03702 (arm holdout): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001316 U-04976 (arm agent): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001317 U-02136 (arm holdout): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001318 U-03156 (arm holdout): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001319 U-01327 (arm agent): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001320 U-03185 (arm agent): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001321 U-04771 (arm agent): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001322 U-03417 (arm holdout): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001324 U-02006 (arm agent): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001325 U-00192 (arm holdout): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001327 U-02617 (arm agent): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001330 U-02360 (arm holdout): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001333 U-03022 (arm holdout): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001334 U-04102 (arm agent): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001335 U-02722 (arm agent): model failed (HTTP 503 model_provider unavailable) -> model unavailable (HTTP 503 model_provider unavailable); deterministic priority used
* D-001385 U-01090 (arm holdout): model failed (model call timed out after 5.0s) -> model unavailable (model call timed out after 5.0s); deterministic priority used
* D-001404 U-02563 (arm agent): model failed (malformed model output: Expecting value) -> model unavailable (malformed model output: Expecting value); deterministic priority used

## Control events

* {'at': '2026-10-01T07:00:00.000Z', 'type': 'title_withdrawn', 'title_id': 'T0001', 'live_pages_marked_for_refresh': "33 items: ['U-04720', 'U-02825', 'U-00050', 'U-04094', 'U-00347']", 'note': 'already-published pages cannot be recalled by the agent; the next trigger republishes'}
* {'at': '2026-10-01T08:30:00.000Z', 'type': 'goal_rule_corrected', 'goal_id': 'G03', 'new_rule_version': 'goals@2026-09-30+G03@2026-10-01T08:30:00.000Z', 'open_goals_reversioned': '0 items: []', 'note': 'only ACTIVE goals of this goal_id were touched; decisions.jsonl is append-only and keeps the version used at decision time'}
* {'at': '2026-10-01T10:35:00.000Z', 'type': 'dependency_unavailable', 'dependency': 'model_provider', 'until': '2026-10-01T10:42:00.000Z'}
* {'at': '2026-10-01T13:10:00.000Z', 'type': 'dependency_degraded', 'dependency': 'user_data_store', 'until': '2026-10-01T13:17:00.000Z'}

## Outcomes

* Goal outcomes: {'FAILED': 1576, 'SUCCEEDED': 145}
* By goal: {"G01": {"FAILED": 79, "SUCCEEDED": 35}, "G02": {"FAILED": 36, "SUCCEEDED": 6}, "G03": {"FAILED": 1}, "G04": {"FAILED": 2}, "G05": {"FAILED": 254, "SUCCEEDED": 11}, "G06": {"FAILED": 18, "SUCCEEDED": 1}, "G07": {"FAILED": 1027, "SUCCEEDED": 28}, "G08": {"SUCCEEDED": 20, "FAILED": 52}, "G09": {"FAILED": 31, "SUCCEEDED": 5}, "G12": {"SUCCEEDED": 39, "FAILED": 76}}
* Goals whose rule version changed while open (G03 correction): 0
* Message delivery (from simulator events only): {'DELIVERED': 387, 'UNCONFIRMED': 10, 'FAILED': 57}
* Simulator counts: {'actions_total': 1360, 'home_page_accepted': 906, 'home_page_published_but_not_shown': 15, 'home_page_shown_personalised': 826, 'in_app_message_queued': 10, 'in_app_message_requested': 10, 'organic_purchases_scripted': 36, 'push_clicked': 3, 'push_delivered': 387, 'push_requested': 444, 'push_undeliverable_token': 57}
* Simulator policy problems: 0 []

## Latency

```
{
  "definition": "processing_ms = measured wall-clock compute + simulated dependency latency (model calls, injected store latency/timeouts). published_at = deliver_at + processing_ms.",
  "decisions_total": 2769,
  "decisions_excluding_duplicates": 2679,
  "status_counts": {
    "PUBLISHED": 1006,
    "NO_ACTION": 1672,
    "DUPLICATE": 90,
    "FAILED": 1
  },
  "processing_ms": {
    "median": 405.166,
    "p95": 408.889,
    "max": 5006.916
  },
  "real_compute_ms": {
    "median": 5.783,
    "p95": 8.969,
    "max": 839.939
  },
  "published_processing_ms": {
    "median": 406.487,
    "p95": 409.648,
    "max": 2512.614
  },
  "budget_s": 60.0,
  "over_budget": 0,
  "failures": 1,
  "failure_reasons": {
    "attempt 1: user_data_store read timed out after Ns": 1
  },
  "model_fallbacks": 21,
  "model_calls": {
    "total": 1737,
    "max_per_decision": 2,
    "limit": 8
  },
  "tool_calls": {
    "total": 17163,
    "max_per_decision": 8,
    "limit": 20
  },
  "peak_triggers_per_minute": [
    [
      "2026-10-01T04:45",
      79
    ]
  ]
}
```

## Input validation

```
{
  "catalogue_instruction_like_text_ignored": 2,
  "events_duplicate_event_ids_dropped": 2176,
  "live_duplicate_event_ids_dropped": 7,
  "profile_duplicate_row": 30
}
```

See `impact_report.md` for the randomized-holdout analysis.
