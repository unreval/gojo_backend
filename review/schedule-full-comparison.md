# Schedule insert full regression comparison

Baseline: clean `git archive a091646`, with existing repository Git history available for read-only `git show` fixtures.

Both runs use `tests/run_offline.py` network/real psycopg2 guards, dummy provider keys and disposable local PGlite. No external model API or production database was used.

| Measure | Baseline | Final |
|---|---:|---:|
| collected | 1755 | 1796 |
| passed | 1701 | 1742 |
| passed_call_records | 1702 | 1743 |
| failure_records | 55 | 55 |
| failed_node_count | 54 | 54 |
| error_records | 0 | 0 |
| error_node_count | 0 | 0 |
| subtests_passed | 1190 | 1196 |
| exit_code | 1 | 1 |

Counts distinguish collected test nodes, parent call records and subtest records. One failing generation audit node emits two failing subtest records, while its parent call record passes; clean passed nodes exclude every node with any failing record.

Result: no new regression = **True**; added nodes = **41**, all passed = **True**. All 1701 previously passing nodes still pass. No collected nodes were removed. No existing failure exception/assertion features changed after source path normalization.

The task sheet mentions 32 existing failures. The actual `a091646` full-suite baseline has 54 failed nodes / 55 failing records. This report uses the measured baseline.

## Failure comparison by node

| Test node | Baseline records | Final records | Same exception/assertion |
|---|---:|---:|---|
| `tests/test_cognitive_continuous_loop.py::SemanticPredictionTests::test_current_event_uses_signal_selectors_and_negative_evidence_wins` | 1 | 1 | True |
| `tests/test_cognitive_continuous_loop.py::SemanticPredictionTests::test_worker_accepts_historical_evidence_already_present_in_context` | 1 | 1 | True |
| `tests/test_cognitive_loop_v1.py::SchedulerTests::test_recent_success_suppresses_scheduled_occurrence` | 1 | 1 | True |
| `tests/test_cognitive_loop_v1.py::SchedulerTests::test_scheduler_creates_event_and_normal_trigger_but_no_cycle` | 1 | 1 | True |
| `tests/test_cognitive_loop_v1.py::TriggerIngressTests::test_cooldown_also_runs_after_fast_maintenance` | 1 | 1 | True |
| `tests/test_cognitive_loop_v1.py::TriggerIngressTests::test_duplicate_source_event_skips_all_side_effects` | 1 | 1 | True |
| `tests/test_cognitive_loop_v1.py::TriggerIngressTests::test_fast_maintenance_completes_before_slow_limit_result` | 1 | 1 | True |
| `tests/test_cognitive_spend_guard.py::RequestBudgetTests::test_oversized_current_event_rejected_before_api` | 1 | 1 | True |
| `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_at_output_ceiling_no_identical_truncation_retry` | 1 | 1 | True |
| `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_complete_json_with_truncation_still_fails_closed` | 1 | 1 | True |
| `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_configured_large_output_is_capped` | 1 | 1 | True |
| `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_default_slow_loop_call_disables_sdk_retries` | 1 | 1 | True |
| `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_provider_503_not_validation_retried` | 1 | 1 | True |
| `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_repeated_truncation_stops_after_two_calls` | 1 | 1 | True |
| `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_retry_does_not_add_failed_model_text_as_evidence` | 1 | 1 | True |
| `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_truncation_retry_changes_budget_once` | 1 | 1 | True |
| `tests/test_explicit_answer_sql.py::ExplicitAnswerSQLTests::test_same_event_independent_answers_and_retry` | 1 | 1 | True |
| `tests/test_flirt_response.py::DurableEngineUntouchedTests::test_handle_rejection_still_writes_flirt_friction` | 1 | 1 | True |
| `tests/test_flirt_response.py::DurableEngineUntouchedTests::test_rejection_still_routes_to_handle_rejection` | 1 | 1 | True |
| `tests/test_flirt_response.py::LogInteractionStatsTests::test_mixed_turn_not_last_write_wins` | 1 | 1 | True |
| `tests/test_flirt_response.py::LogInteractionStatsTests::test_null_source_event_id_skips_on_conflict` | 1 | 1 | True |
| `tests/test_flirt_response.py::LogInteractionStatsTests::test_offensive_character_writes_null_flirt_response` | 1 | 1 | True |
| `tests/test_flirt_response.py::LogInteractionStatsTests::test_offensive_third_party_writes_null_flirt_response` | 1 | 1 | True |
| `tests/test_flirt_response.py::LogInteractionStatsTests::test_retry_same_source_event_id_uses_on_conflict` | 1 | 1 | True |
| `tests/test_flirt_response.py::LogInteractionStatsTests::test_source_event_id_is_written` | 1 | 1 | True |
| `tests/test_flirt_response.py::ProcessTurnStatsOnceTests::test_apply_once_retry_does_not_log_stats_twice` | 1 | 1 | True |
| `tests/test_flirt_response.py::ProcessTurnStatsOnceTests::test_log_stats_receives_source_event_id` | 1 | 1 | True |
| `tests/test_generation_audit_trace.py::GenerationAuditTraceTests::test_assistant_turn_link_only_follows_durable_commit` | 2 | 2 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_assistant_denial_does_not_override_adjacent_canonical_evidence` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_compound_emoji_can_be_a_convention_payload` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_confirmed_nearby_emoji_reply_becomes_semantic_convention` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_convention_only_backfill_does_not_replace_active_rule_from_assistant_only_reassignment` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_convention_only_backfill_plans_then_applies_with_atomic_provenance` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_convention_only_backfill_supersedes_only_user_confirmed_target` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_convention_replace_and_revoke_keep_the_existing_bond_lifecycle` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_current_user_confirmed_durable_bond_still_saves` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_distinct_convention_slots_stay_independent` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_failed_merge_never_appends_same_event_bond` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_invalid_extractor_event_id_uses_primary_canonical_event` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_japanese_evidence_does_not_depend_on_chinese_keyword_gate` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_semantic_rewrite_is_rejected_but_faithful_summary_saves` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_short_confirmation_uses_multi_event_provenance` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_stable_self_claim_still_records_low_confidence_evidence` | 1 | 1 | True |
| `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_unique_convention_reference_inherits_its_verified_slot` | 1 | 1 | True |
| `tests/test_receipt_windows.py::ReceiptWindowTests::test_B_disconnect_after_commit_replays_same_response_without_generator` | 1 | 1 | True |
| `tests/test_receipt_windows.py::ReceiptWindowTests::test_C_repair_interrupted_assistant_effect_keeps_event_job_projection_once` | 1 | 1 | True |
| `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_current_facts_json_echo_is_accepted_and_written` | 1 | 1 | True |
| `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_current_facts_plaintext_echo_is_accepted_and_written` | 1 | 1 | True |
| `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_gap_json_echo_is_accepted_and_written` | 1 | 1 | True |
| `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_gap_plaintext_echo_is_accepted_and_written` | 1 | 1 | True |
| `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_history_json_echo_is_accepted_and_written` | 1 | 1 | True |
| `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_history_plaintext_echo_is_accepted_and_written` | 1 | 1 | True |
| `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_observer_exception_does_not_retry_reject_or_prevent_write` | 1 | 1 | True |
| `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_temporal_log_failure_does_not_retry_reject_or_prevent_write` | 1 | 1 | True |

## Failure exception and assertion details

### `tests/test_cognitive_continuous_loop.py::SemanticPredictionTests::test_current_event_uses_signal_selectors_and_negative_evidence_wins`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: None != -1.0
```

### `tests/test_cognitive_continuous_loop.py::SemanticPredictionTests::test_worker_accepts_historical_evidence_already_present_in_context`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AttributeError: module 'cognitive_worker' has no attribute '_event_ids'
```

### `tests/test_cognitive_loop_v1.py::SchedulerTests::test_recent_success_suppresses_scheduled_occurrence`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: 'SchedulerCursor' object has no attribute 'fetchall'
```

### `tests/test_cognitive_loop_v1.py::SchedulerTests::test_scheduler_creates_event_and_normal_trigger_but_no_cycle`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: 'SchedulerCursor' object has no attribute 'fetchall'
```

### `tests/test_cognitive_loop_v1.py::TriggerIngressTests::test_cooldown_also_runs_after_fast_maintenance`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'cognitive_events' from '<repo>/cognitive_events.py'> does not have the attribute 'high_weight_trigger_specs'
```

### `tests/test_cognitive_loop_v1.py::TriggerIngressTests::test_duplicate_source_event_skips_all_side_effects`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'cognitive_events' from '<repo>/cognitive_events.py'> does not have the attribute '_embed_v4_evidence'
```

### `tests/test_cognitive_loop_v1.py::TriggerIngressTests::test_fast_maintenance_completes_before_slow_limit_result`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'cognitive_events' from '<repo>/cognitive_events.py'> does not have the attribute 'high_weight_trigger_specs'
```

### `tests/test_cognitive_spend_guard.py::RequestBudgetTests::test_oversized_current_event_rejected_before_api`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: SlowLoopRequestBudgetError not raised
```

### `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_at_output_ceiling_no_identical_truncation_retry`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'cognitive_worker' from '<repo>/cognitive_worker.py'> does not have the attribute 'COGNITIVE_WORKER_MAX_TOKENS'
```

### `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_complete_json_with_truncation_still_fails_closed`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AttributeError: module 'cognitive_worker' has no attribute 'SlowLoopOutputError'
```

### `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_configured_large_output_is_capped`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'cognitive_worker' from '<repo>/cognitive_worker.py'> does not have the attribute 'COGNITIVE_WORKER_MAX_TOKENS'
```

### `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_default_slow_loop_call_disables_sdk_retries`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AttributeError: 'NoneType' object has no attribute 'kwargs'
```

### `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_provider_503_not_validation_retried`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: RuntimeError not raised
```

### `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_repeated_truncation_stops_after_two_calls`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'cognitive_worker' from '<repo>/cognitive_worker.py'> does not have the attribute 'COGNITIVE_WORKER_MODEL_ATTEMPTS'
```

### `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_retry_does_not_add_failed_model_text_as_evidence`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       IndexError: list index out of range
```

### `tests/test_cognitive_spend_guard.py::WorkerRecoveryTests::test_truncation_retry_changes_budget_once`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'cognitive_worker' from '<repo>/cognitive_worker.py'> does not have the attribute 'COGNITIVE_WORKER_MAX_TOKENS'
```

### `tests/test_explicit_answer_sql.py::ExplicitAnswerSQLTests::test_same_event_independent_answers_and_retry`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 2 != 3
```

### `tests/test_flirt_response.py::DurableEngineUntouchedTests::test_handle_rejection_still_writes_flirt_friction`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AttributeError: module 'relationship_engine' has no attribute '_handle_rejection'
```

### `tests/test_flirt_response.py::DurableEngineUntouchedTests::test_rejection_still_routes_to_handle_rejection`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AttributeError: module 'relationship_engine' has no attribute '_route_signal'
```

### `tests/test_flirt_response.py::LogInteractionStatsTests::test_mixed_turn_not_last_write_wins`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'relationship_engine' from '<repo>/relationship_engine.py'> does not have the attribute 'get_conn'
```

### `tests/test_flirt_response.py::LogInteractionStatsTests::test_null_source_event_id_skips_on_conflict`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'relationship_engine' from '<repo>/relationship_engine.py'> does not have the attribute 'get_conn'
```

### `tests/test_flirt_response.py::LogInteractionStatsTests::test_offensive_character_writes_null_flirt_response`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'relationship_engine' from '<repo>/relationship_engine.py'> does not have the attribute 'get_conn'
```

### `tests/test_flirt_response.py::LogInteractionStatsTests::test_offensive_third_party_writes_null_flirt_response`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'relationship_engine' from '<repo>/relationship_engine.py'> does not have the attribute 'get_conn'
```

### `tests/test_flirt_response.py::LogInteractionStatsTests::test_retry_same_source_event_id_uses_on_conflict`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'relationship_engine' from '<repo>/relationship_engine.py'> does not have the attribute 'get_conn'
```

### `tests/test_flirt_response.py::LogInteractionStatsTests::test_source_event_id_is_written`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'relationship_engine' from '<repo>/relationship_engine.py'> does not have the attribute 'get_conn'
```

### `tests/test_flirt_response.py::ProcessTurnStatsOnceTests::test_apply_once_retry_does_not_log_stats_twice`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'relationship_engine' from '<repo>/relationship_engine.py'> does not have the attribute 'ensure_state_row'
```

### `tests/test_flirt_response.py::ProcessTurnStatsOnceTests::test_log_stats_receives_source_event_id`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E           AttributeError: <module 'relationship_engine' from '<repo>/relationship_engine.py'> does not have the attribute 'ensure_state_row'
```

### `tests/test_generation_audit_trace.py::GenerationAuditTraceTests::test_assistant_turn_link_only_follows_durable_commit`

Baseline 2 record(s); phase=call, subtest=True. Final characteristics identical: True.

```text
E               TypeError: GenerationAuditTraceTests._run_chat_with_commit_state.<locals>.<lambda>() got an unexpected keyword argument 'temporal_snapshot'
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_assistant_denial_does_not_override_adjacent_canonical_evidence`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_compound_emoji_can_be_a_convention_payload`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_confirmed_nearby_emoji_reply_becomes_semantic_convention`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_convention_only_backfill_does_not_replace_active_rule_from_assistant_only_reassignment`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       KeyError: 'decision'
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_convention_only_backfill_plans_then_applies_with_atomic_provenance`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       KeyError: 'candidate'
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_convention_only_backfill_supersedes_only_user_confirmed_target`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       IndexError: list index out of range
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_convention_replace_and_revoke_keep_the_existing_bond_lifecycle`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       IndexError: list index out of range
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_current_user_confirmed_durable_bond_still_saves`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_distinct_convention_slots_stay_independent`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       IndexError: list index out of range
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_failed_merge_never_appends_same_event_bond`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_invalid_extractor_event_id_uses_primary_canonical_event`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_japanese_evidence_does_not_depend_on_chinese_keyword_gate`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_semantic_rewrite_is_rejected_but_faithful_summary_saves`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       IndexError: list index out of range
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_short_confirmation_uses_multi_event_provenance`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_stable_self_claim_still_records_low_confidence_evidence`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_private_memory_fact_gate.py::PrivateMemoryFactGateTests::test_unique_convention_reference_inherits_its_verified_slot`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       IndexError: list index out of range
```

### `tests/test_receipt_windows.py::ReceiptWindowTests::test_B_disconnect_after_commit_replays_same_response_without_generator`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_receipt_windows.py::ReceiptWindowTests::test_C_repair_interrupted_assistant_effect_keeps_event_job_projection_once`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E       AssertionError: 0 != 1
```

### `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_current_facts_json_echo_is_accepted_and_written`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E                           IndexError: list index out of range
```

### `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_current_facts_plaintext_echo_is_accepted_and_written`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E                           IndexError: list index out of range
```

### `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_gap_json_echo_is_accepted_and_written`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E                           IndexError: list index out of range
```

### `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_gap_plaintext_echo_is_accepted_and_written`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E                           IndexError: list index out of range
```

### `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_history_json_echo_is_accepted_and_written`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E                           IndexError: list index out of range
```

### `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_history_plaintext_echo_is_accepted_and_written`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E                           IndexError: list index out of range
```

### `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_observer_exception_does_not_retry_reject_or_prevent_write`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E                           IndexError: list index out of range
```

### `tests/test_time_cognition_echo_audit.py::TimeLabelEchoObservationTests::test_temporal_log_failure_does_not_retry_reject_or_prevent_write`

Baseline 1 record(s); phase=call, subtest=False. Final characteristics identical: True.

```text
E                           IndexError: list index out of range
```

## Added passing nodes

- `tests/test_generation_temporal_hotfix.py::GenerationEnvelopeTransportTests::test_provider_schema_allows_optional_timed_insert_and_round_trips_it`
- `tests/test_insert_intent_prompt.py::InsertIntentPromptTests::test_rendered_insert_rules_limit_booking_to_later_today`
- `tests/test_schedule_insert_time.py::test_free_text_cannot_become_an_insert`
- `tests/test_schedule_insert_time.py::test_insert_keeps_valid_start_time[00:00-00:00]`
- `tests/test_schedule_insert_time.py::test_insert_keeps_valid_start_time[21:00-21:00]`
- `tests/test_schedule_insert_time.py::test_insert_keeps_valid_start_time[23:59-23:59]`
- `tests/test_schedule_insert_time.py::test_insert_keeps_valid_start_time[9:00-09:00]`
- `tests/test_schedule_insert_time.py::test_insert_keeps_valid_start_time[9:30-09:30]`
- `tests/test_schedule_insert_time.py::test_insert_rejects_unsupported_date_fields[event-date]`
- `tests/test_schedule_insert_time.py::test_insert_rejects_unsupported_date_fields[event-sched_date]`
- `tests/test_schedule_insert_time.py::test_insert_rejects_unsupported_date_fields[event-start_at]`
- `tests/test_schedule_insert_time.py::test_insert_rejects_unsupported_date_fields[intent-date]`
- `tests/test_schedule_insert_time.py::test_insert_rejects_unsupported_date_fields[intent-sched_date]`
- `tests/test_schedule_insert_time.py::test_insert_rejects_unsupported_date_fields[intent-start_at]`
- `tests/test_schedule_insert_time.py::test_insert_without_start_time_preserves_complete_legacy_shape`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[ 21:00]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[-1:00]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[2100]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[21:00:00]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[21:00\n]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[21:60]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[24:00]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[25:00]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[9:0]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[None]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[True]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[\u665a\u4e0a\u4e5d\u70b9]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[clock6]`
- `tests/test_schedule_insert_time.py::test_invalid_insert_start_time_rejects_entire_intent[clock7]`
- `tests/test_schedule_reflow_transition.py::test_insert_without_start_keeps_active_event_rejection`
- `tests/test_schedule_reflow_transition.py::test_timed_insert_anchors_to_local_today_and_preserves_overnight_end[now0-23:30-120-expected_start0]`
- `tests/test_schedule_reflow_transition.py::test_timed_insert_anchors_to_local_today_and_preserves_overnight_end[now1-01:00-60-expected_start1]`
- `tests/test_schedule_reflow_transition.py::test_timed_insert_checks_active_actual_or_planned_half_open_window[15:45-None-True]`
- `tests/test_schedule_reflow_transition.py::test_timed_insert_checks_active_actual_or_planned_half_open_window[16:00-16:30-True]`
- `tests/test_schedule_reflow_transition.py::test_timed_insert_checks_active_actual_or_planned_half_open_window[16:00-None-False]`
- `tests/test_schedule_reflow_transition.py::test_timed_insert_checks_active_actual_or_planned_half_open_window[16:30-16:30-False]`
- `tests/test_schedule_reflow_transition.py::test_timed_insert_fixed_conflict_rolls_back_all_schedule_tables`
- `tests/test_schedule_reflow_transition.py::test_timed_insert_rejects_past_or_equal_start_without_writes[09:00]`
- `tests/test_schedule_reflow_transition.py::test_timed_insert_rejects_past_or_equal_start_without_writes[15:30]`
- `tests/test_schedule_reflow_transition.py::test_timed_insert_reserves_nine_pm_during_active_event_and_reflows_overnight`

## Evidence

- `review/run_schedule_validation.py`
- `review/schedule-baseline-full-tests.json`
- `review/schedule-baseline-full-tests.log`
- `review/schedule-final-full-tests.json`
- `review/schedule-final-full-tests.log`
- `review/schedule-full-comparison.json`
