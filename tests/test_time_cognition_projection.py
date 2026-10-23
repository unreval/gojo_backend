"""Permanent time-projection regressions: frozen clocks, synthetic active events."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import unittest

import context_budget
import context_layer
import config
import prompt
import raw_events
import route_chat
import temporal_awareness as temporal
import user_memory

ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 14, 1, 41, tzinfo=ZONE)


def event(event_id, role, text, timestamp):
    return dict(event_id=event_id, role=role, content=text, timestamp=timestamp,
                metadata={}, status='active', subtitle='原字幕')


def history(hour=1):
    return [event('u1', 'user', '我今天快十一点了才回到家',
                  datetime(2026, 10, 13, hour, 37, tzinfo=ZONE)),
            event('a1', 'assistant', '十一時に帰ってきたんだ。もう寝る？',
                  datetime(2026, 10, 13, hour, 38, tzinfo=ZONE)),
            event('current', 'user', '🦖', NOW)]


def snapshot(now=NOW):
    # latest_user_at intentionally points to this turn, as it does after save.
    return dict(now_utc=now.astimezone(timezone.utc), now_local=now,
                latest_user_at=now, has_history=True)


class TimeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(context_layer, 'maybe_store_spill_summary'))
        self.stack.enter_context(patch.object(context_layer, 'list_rolling_summaries', return_value=[]))
        self.stack.enter_context(patch.object(context_layer, 'list_pins', return_value=[]))
        self.stack.enter_context(patch('builtins.print'))

    def pack(self, rows, now=NOW, cfg=None):
        return context_layer.assemble_from_events(
            rows, user_id='u', character_id='gojo', user_message='🦖',
            config=cfg, temporal_snapshot=snapshot(now), current_event_id='current',
            include_recall=False, include_support=False, auto_pin_enabled=False)

    def messages(self, rows, text='🦖', now=NOW):
        pack = self.pack(rows, now)
        return pack, route_chat._history_plus_current(
            pack.messages, text, temporal_snapshot=pack.temporal_snapshot, context_pack=pack)

    def test_report_scenario_2_24_hours_history_and_adjacent_current_facts(self):
        rows = history()
        original = deepcopy(rows)
        pack, messages = self.messages(rows)
        self.assertIn('发送=2026-10-13T01:37:00+08:00', messages[0]['content'])
        self.assertIn('日期关系=昨天；距本轮=24小时04分', messages[0]['content'])
        facts, current = messages[-1]['content']
        self.assertEqual(current, {'type': 'text', 'text': '🦖'})
        self.assertEqual(len(messages[-1]['content']), 2)
        self.assertTrue(facts['text'].startswith('【后端当前时间事实】'))
        self.assertIn('previous_user=2026-10-13T01:37:00+08:00', facts['text'])
        self.assertIn('previous_assistant=2026-10-13T01:38:00+08:00', facts['text'])
        self.assertIn('elapsed_since_previous_assistant=86580秒（24小时03分）', facts['text'])
        self.assertIn('calendar_relation=cross_calendar_day', facts['text'])
        self.assertIn('retained_history_state=historical', facts['text'])
        self.assertIn('【对话间隔；距上一条=24小时03分；跨日=是】', facts['text'])
        self.assertEqual(pack.raw_messages[0]['content'], original[0]['content'])
        self.assertEqual(rows, original)

    def test_report_scenario_3_independent_16_hours(self):
        _, messages = self.messages(history(hour=9))
        self.assertIn('距本轮=16小时04分', messages[0]['content'])
        self.assertIn('elapsed_since_previous_assistant=57780秒（16小时03分）',
                      messages[-1]['content'][0]['text'])

    def test_report_scenario_5_one_minute_is_continuing(self):
        now = datetime(2026, 10, 14, 10, tzinfo=ZONE)
        rows = [event('plan', 'assistant', '十一点再聊', now - timedelta(minutes=1))]
        _, messages = self.messages(rows, text='那就这样', now=now)
        facts = messages[-1]['content'][0]['text']
        self.assertIn('60秒（1分）', facts)
        self.assertIn('retained_history_state=continuing', facts)
        self.assertNotIn('【对话间隔', facts)

    def test_report_scenario_6_two_minutes_cross_midnight(self):
        now = datetime(2026, 10, 14, 0, 1, tzinfo=ZONE)
        rows = [event('old', 'assistant', '继续', now - timedelta(minutes=2))]
        _, messages = self.messages(rows, text='继续', now=now)
        self.assertIn('日期关系=昨天；距本轮=2分', messages[0]['content'])
        self.assertIn('【对话间隔；距上一条=2分；跨日=是】',
                      messages[-1]['content'][0]['text'])

    def test_report_scenarios_9_and_10_keep_current_statement_and_history_reference(self):
        for text in ('昨天十一点到家，今天也没睡', '你记得昨天我几点回家吗'):
            with self.subTest(text=text):
                _, messages = self.messages(history(), text=text)
                self.assertEqual(messages[-1]['content'][1]['text'], text)
                self.assertIn('不证明用户睡过、没睡', messages[-1]['content'][0]['text'])
        candidate = {'messages': [{'jp': '昨日は十一時近くに帰ったね。',
                                   'zh': '昨天快十一点回家。'}]}
        self.assertIsNone(route_chat._find_bilingual_numeric_conflict(candidate))
        self.assertIsNone(route_chat._find_reply_clock_phase_conflict(
            '昨日の夜は遅かったね。昨天很晚了。', snapshot()))

    def test_main_and_fallback_are_identical_with_frozen_clock(self):
        rows = history()
        rows.insert(1, event('unknown', 'user', '时间缺失的原话', None))
        primary = self.pack(rows)
        with patch.object(user_memory, '_merge_recent_context', return_value=deepcopy(rows)):
            fallback_rows = user_memory.get_short_memory_for_prompt(
                'u', n=20, character_id='gojo', exclude_event_ids=['current'],
                now=NOW, return_events=True)
        fallback = context_layer.assemble_fallback_from_messages(
            fallback_rows, temporal_snapshot=snapshot())
        self.assertEqual(primary.messages, fallback.messages)
        self.assertEqual(primary.raw_messages, fallback.raw_messages)
        self.assertEqual(primary.history_time_facts, fallback.history_time_facts)

    def test_unknown_time_never_invents_date_or_elapsed(self):
        rows = [event('unknown', 'user', '没时间的原话', None)]
        _, messages = self.messages(rows)
        self.assertEqual(messages[0]['content'], '【历史消息；发送时间未知】\n没时间的原话')
        facts = messages[-1]['content'][0]['text']
        self.assertIn('previous_user=发送时间未知', facts)
        self.assertIn('elapsed_since_previous_user=未知', facts)
        self.assertIn('calendar_relation=unknown', facts)

    def test_latest_unknown_time_does_not_reuse_older_known_timestamp(self):
        rows = [event('known', 'user', '先前已知', NOW - timedelta(hours=1)),
                event('unknown', 'user', '上一条未知', None)]
        pack, messages = self.messages(rows)
        self.assertIsNone(pack.history_time_facts['previous_user'])
        self.assertIsNone(pack.history_time_facts['last_history_at'])
        facts = messages[-1]['content'][0]['text']
        self.assertIn('previous_user=发送时间未知', facts)
        self.assertIn('calendar_relation=unknown', facts)
        self.assertNotIn('距上一条=1小时', facts)

    def test_interval_boundary_and_cross_day_with_adjacent_history(self):
        for minutes, has_gap in ((24, False), (25, True), (26, True)):
            rows = [event('first', 'user', '先说', NOW - timedelta(minutes=minutes)),
                    event('second', 'assistant', '再说', NOW)]
            rendered = context_layer.format_history_messages(rows, now=NOW)
            self.assertEqual('【对话间隔' in rendered[1]['prompt_text'], has_gap)
        now = datetime(2026, 10, 14, 0, 1, tzinfo=ZONE)
        rows = [event('first', 'user', '先说', now - timedelta(minutes=2)),
                event('second', 'assistant', '再说', now)]
        self.assertIn('距上一条=2分；跨日=是',
                      context_layer.format_history_messages(rows, now=now)[1]['prompt_text'])

    def test_markers_are_budgeted_and_no_gap_targets_discarded_history(self):
        rows = [event(str(i), 'user', '完整原文' * 20, NOW - timedelta(minutes=90 - i))
                for i in range(50)]
        cfg = context_budget.BudgetConfig(total_token_budget=400, hot_token_budget=250)
        pack = self.pack(rows, cfg=cfg)
        expected = sum(context_budget.estimate_tokens(m['content']) for m in pack.messages)
        raw_cost = sum(context_budget.estimate_tokens(m['content']) for m in pack.raw_messages)
        self.assertEqual(pack.allocation['hot'], expected)
        self.assertGreater(expected, raw_cost)
        self.assertLessEqual(expected, cfg.total_token_budget)
        self.assertNotIn('【对话间隔', pack.messages[0]['content'])
        self.assertTrue(all(m['content'].endswith('完整原文' * 20) for m in pack.messages))
        self.assertEqual(rows[0]['subtitle'], '原字幕')

    def test_current_user_only_once_and_assembly_is_idempotent(self):
        pack, messages = self.messages(history())
        again = route_chat._history_plus_current(
            messages, '🦖', temporal_snapshot=pack.temporal_snapshot, context_pack=pack)
        self.assertEqual(messages, again)
        self.assertEqual(sum(m['content'][1]['text'] == '🦖'
                             for m in again if isinstance(m['content'], list)), 1)
        self.assertEqual(len(pack.messages), 2)

    def test_fallback_route_excludes_current_and_reuses_clock(self):
        snap = snapshot()
        with patch.object(context_layer, 'build_chat_context', return_value=context_layer.ChatContextPack()), \
                patch.object(user_memory, '_merge_recent_context', return_value=history()), \
                patch.object(route_chat, 'get_temporal_snapshot', side_effect=AssertionError('second clock')):
            pack, messages = route_chat._turn_context(
                'u', 'gojo', '🦖', current_event_id='current', temporal_snapshot=snap)
        self.assertIs(pack.temporal_snapshot, snap)
        self.assertEqual(len(messages), 2)
        self.assertEqual(pack.history_time_facts['previous_user'], history()[0]['timestamp'])
        self.assertNotIn('🦖', str(messages))

    def missing_id_pack(self, rows, path, text='🦖'):
        if path == 'main':
            with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
                    patch.object(raw_events, 'get_hot_candidate_events',
                                 return_value=deepcopy(rows)), \
                    patch.object(context_layer, '_support_items', return_value=([], '')):
                return context_layer.build_chat_context(
                    'u', 'gojo', user_message=text, current_event_id=None,
                    include_recall=False, temporal_snapshot=snapshot(), auto_pin_enabled=False)
        with patch.object(user_memory, '_merge_recent_context', return_value=deepcopy(rows)):
            rendered = user_memory.get_short_memory_for_prompt(
                'u', n=20, now=NOW, return_events=True, user_message=text)
        return context_layer.assemble_fallback_from_messages(
            rendered, temporal_snapshot=snapshot())

    def test_missing_id_excludes_current_before_labels_and_previous_user_facts(self):
        rows = history()
        original = deepcopy(rows)
        formatter = context_layer.format_history_messages
        for path in ('main', 'fallback'):
            with self.subTest(path=path), patch.object(
                    context_layer, 'format_history_messages', wraps=formatter) as render:
                pack = self.missing_id_pack(rows, path)
            self.assertEqual(pack.recent_event_ids, ['u1', 'a1'])
            self.assertEqual(pack.history_time_facts['previous_user'], rows[0]['timestamp'])
            self.assertEqual(pack.history_time_facts['previous_assistant'], rows[1]['timestamp'])
            self.assertEqual(pack.history_time_facts['last_history_at'], rows[1]['timestamp'])
            render.assert_called()
            for call in render.call_args_list:
                self.assertNotIn('current', [row.get('event_id') for row in call.args[0]])
            messages = route_chat._history_plus_current(
                pack.messages, '🦖', temporal_snapshot=pack.temporal_snapshot, context_pack=pack)
            facts = messages[-1]['content'][0]['text']
            self.assertIn('previous_user=2026-10-13T01:37:00+08:00', facts)
            self.assertNotIn('previous_user=' + NOW.isoformat(), facts)
        self.assertEqual(rows, original)

    def test_missing_id_removes_only_a_matching_latest_user_event(self):
        old = event('old', 'user', '🦖', NOW - timedelta(minutes=2))
        latest_user = event('current', 'user', '🦖', NOW)
        cases = (
            ([old, latest_user], '🦖', ['old']),
            ([old, event('latest', 'assistant', '🦖', NOW)], '🦖', ['old', 'latest']),
            ([old, event('latest', 'user', '其他原文', NOW)], '🦖', ['old', 'latest']),
            ([old, latest_user], '', ['old', 'current']),
        )
        for path in ('main', 'fallback'):
            for rows, text, expected_ids in cases:
                with self.subTest(path=path, text=text, expected_ids=expected_ids):
                    original = deepcopy(rows)
                    pack = self.missing_id_pack(rows, path, text)
                    self.assertEqual(pack.recent_event_ids, expected_ids)
                    self.assertEqual(rows, original)

    def test_fallback_preserves_keyed_previous_same_text_and_removes_unkeyed_current(self):
        old = event('previous-emoji', 'user', '🦖', NOW - timedelta(hours=1))
        unkeyed = event(None, 'user', '🦖', NOW)
        for rows in ([old], [old, unkeyed]):
            with self.subTest(rows=rows), patch.object(
                    user_memory, '_merge_recent_context', return_value=deepcopy(rows)):
                rendered = user_memory.get_short_memory_for_prompt(
                    'u', exclude_event_ids=['current'], exclude_messages=[('user', '🦖')],
                    now=NOW, return_events=True)
            self.assertEqual([m['event_id'] for m in rendered], ['previous-emoji'])

    def test_fallback_preserves_main_pack_support_and_consistent_history(self):
        main = context_layer.ChatContextPack(
            recall_ready=True, support_ready=True, memory_text='existing-recall',
            summary_prompt_text='existing-summary')
        with patch.object(context_layer, 'build_chat_context', return_value=main), \
                patch.object(user_memory, '_merge_recent_context', return_value=history()):
            pack, messages = route_chat._turn_context(
                'u', 'gojo', '🦖', current_event_id='current', temporal_snapshot=snapshot())
        self.assertEqual(pack.memory_text, 'existing-recall')
        self.assertEqual(pack.summary_prompt_text, 'existing-summary')
        self.assertTrue(pack.recall_ready)
        self.assertEqual(pack.messages, messages)
        self.assertEqual(len(pack.raw_messages), len(messages))

    def test_retries_keep_identical_facts_and_current_original(self):
        pack, messages = self.messages(history())
        captured = []
        raw = json.dumps({'emotion': '平静', 'messages': [{'jp': 'そうだね', 'zh': '是啊'}]},
                         ensure_ascii=False)
        def fake_create(_model, _tokens, _system, sent):
            captured.append(deepcopy(sent))
            return raw, SimpleNamespace(stop_reason='end_turn', usage=None)
        with patch.object(route_chat, '_create_json', side_effect=fake_create), \
                patch.object(temporal, 'get_temporal_snapshot', side_effect=AssertionError('second clock')):
            result, _ = route_chat._generate_or_none(
                'offline', 100, [], messages, attempts=2, log_tag='test', cache_tag='test',
                reject_fn=Mock(side_effect=['synthetic_existing_gate_failure', None]))
        self.assertIsNotNone(result)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0], captured[1])
        self.assertEqual(captured[0], messages)

    def test_facts_use_the_same_utc_frozen_clock_as_history(self):
        pack = self.pack(history())
        pack.temporal_snapshot['now_local'] = NOW - timedelta(hours=12)
        messages = route_chat._history_plus_current(
            pack.messages, '🦖', temporal_snapshot=pack.temporal_snapshot, context_pack=pack)
        self.assertIn('now=2026-10-14T01:41:00+08:00', messages[-1]['content'][0]['text'])
        self.assertIn('86580秒（24小时03分）', messages[-1]['content'][0]['text'])


class ObservationOnlyTests(unittest.TestCase):
    def test_hits_log_minimal_fields_without_retry_or_candidate_mutation(self):
        snap = snapshot(datetime(2026, 10, 14, 14, tzinfo=ZONE))
        pack = context_layer.ChatContextPack(history_time_facts={
            'previous_user': NOW, 'previous_assistant': NOW})
        candidate = {'emotion': '平静', 'messages': [
            {'jp': '今は深夜だ。あと三分だよ。', 'zh': '现在是深夜了。还有五分钟。'}]}
        before = deepcopy(candidate)
        raw = json.dumps(candidate, ensure_ascii=False)
        with patch.object(route_chat, '_create_json', return_value=(
                raw, SimpleNamespace(stop_reason='end_turn', usage=None))) as adapter, \
                patch('builtins.print') as logged:
            result, _ = route_chat._generate_or_none(
                'offline', 100, [], [], attempts=2, log_tag='test', cache_tag='test',
                observe_fn=lambda parsed: route_chat._observe_temporal_candidate(
                    parsed, snap, pack, 'event-123'))
        self.assertIsNotNone(result)
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(candidate, before)
        logs = [json.loads(call.args[0].split(' ', 1)[1]) for call in logged.call_args_list
                if call.args and str(call.args[0]).startswith('[temporal_check] ')]
        self.assertEqual({row['check_type'] for row in logs}, {'clock_phase'})
        for row in logs:
            self.assertEqual(row['event_id'], 'event-123')
            self.assertEqual(row['now'], snap['now_local'].isoformat())
            self.assertIn('elapsed_since_previous_assistant', row['intervals'])
            self.assertLessEqual(len(row['candidate_prefix']), 40)
            self.assertEqual(set(row), {'check_type', 'error_code', 'event_id', 'now',
                                        'intervals', 'candidate_prefix'})

    def test_clock_exemptions_and_separate_explicit_assertion(self):
        snap = snapshot(datetime(2026, 10, 14, 14, tzinfo=ZONE))
        for text in ('现在并不晚', '如果半夜醒来再说', '昨日は深夜だった。',
                     '「今は深夜だ」と昨日言った', '夜班刚下，准备睡。晚安。'):
            self.assertIsNone(route_chat._find_reply_clock_phase_conflict(text, snap))
        self.assertEqual(route_chat._find_reply_clock_phase_conflict(
            '昨天是深夜。现在是深夜了。', snap), 'current_clock_phase_conflict')

    def test_numeric_equivalence_ambiguity_and_mismatch(self):
        for jp, zh, mismatch in (
                ('午後十一時に帰る', '23:00回家', False),
                ('午前十一時に帰る', '23:00回家', True),
                ('十一時に帰る', '十一点回家', False),
                ('十一時に帰る', '十二点回家', True),
                ('あと３分間', '还有三分钟', False),
                ('あと３分間', '还有五分钟', True),
                ('一緒に休もう', '一起休息吧', False)):
            with self.subTest(jp=jp, zh=zh):
                code = route_chat._find_bilingual_numeric_conflict(
                    {'messages': [{'jp': jp, 'zh': zh}]})
                self.assertEqual(code is not None, mismatch)


class CurrentSaveBoundaryTests(unittest.TestCase):
    def test_image_profile_does_not_feed_new_markers_to_generic_cognitive_ingress(self):
        import generation_effects
        from temporal_projection_offline import module
        with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
                patch.object(raw_events, 'get_hot_candidate_events', return_value=history()), \
                patch.object(context_layer, 'maybe_store_spill_summary'), \
                patch.object(context_layer, 'list_rolling_summaries', return_value=[]), \
                patch.object(context_layer, 'list_pins', return_value=[]), \
                patch.object(context_layer, '_support_items', return_value=([], '')):
            pack = context_layer.build_chat_context(
                'u', 'gojo', profile='image', current_event_id='current',
                include_recall=False, temporal_snapshot=snapshot(), auto_pin_enabled=False)
        self.assertEqual(pack.messages, pack.raw_messages)
        process = Mock(return_value={'signals_extracted': 0, 'signals_applied': 0})
        with patch.dict('sys.modules', {'relationship_engine': module('relationship_engine', process_turn=process)}):
            generation_effects.apply_relationship_update({
                'user_id': 'u', 'character_id': 'gojo', 'source_event_id': 'image-current',
                'user_text': '[image]', 'full_jp': 'そうだね', 'pack': pack,
                'temporal_snapshot': snapshot()})
        recent = process.call_args.kwargs['recent_context']
        self.assertEqual(recent, pack.raw_messages)
        self.assertNotIn('【历史消息', str(recent))

    def test_background_profiles_keep_original_history_budget_and_text(self):
        with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
                patch.object(raw_events, 'get_hot_candidate_events', return_value=history()), \
                patch.object(context_layer, 'maybe_store_spill_summary'), \
                patch.object(context_layer, 'list_rolling_summaries', return_value=[]), \
                patch.object(context_layer, 'list_pins', return_value=[]), \
                patch.object(context_layer, '_support_items', return_value=([], '')):
            for profile in ('relationship_observer', 'memory_extractor', 'diary_writer', 'cognitive_worker'):
                with self.subTest(profile=profile):
                    pack = context_layer.build_chat_context(
                        'u', 'gojo', profile=profile, current_event_id='current',
                        include_recall=False, temporal_snapshot=snapshot(), auto_pin_enabled=False)
                    self.assertEqual(pack.messages, pack.raw_messages)
                    self.assertEqual(pack.allocation['hot'], sum(
                        context_budget.estimate_tokens(m['content']) for m in pack.raw_messages))

    def test_legacy_fallback_consumers_keep_existing_projection(self):
        row = event('recent', 'user', '原文', NOW - timedelta(minutes=2))
        with patch.object(user_memory, '_merge_recent_context', return_value=[row]):
            legacy = user_memory.get_short_memory_for_prompt('u', now=NOW)
        self.assertEqual(legacy, [{'role': 'user', 'content': '原文'}])

    def test_handler_saves_only_original_and_uses_raw_recall_query(self):
        from test_chat_commit_gate import ChatCommitGateTests
        fixture = ChatCommitGateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with ExitStack() as stack:
            stack.enter_context(patch.dict('sys.modules', {
                'context_layer': context_layer, 'temporal_awareness': temporal, 'config': config}))
            stack.enter_context(patch.object(context_layer, 'maybe_store_spill_summary'))
            stack.enter_context(patch.object(context_layer, 'list_rolling_summaries', return_value=[]))
            stack.enter_context(patch.object(context_layer, 'list_pins', return_value=[]))
            pack = context_layer.assemble_from_events(
                history(), user_id='u', character_id='gojo', current_event_id='current',
                temporal_snapshot=snapshot(), include_recall=False, auto_pin_enabled=False)
            fixture.route.get_temporal_snapshot = Mock(return_value=snapshot())
            fixture.route._turn_context = Mock(return_value=(pack, pack.messages))
            fixture.route._reject_schedule_candidate = Mock(return_value=None)
            fixture.client.messages.create = Mock(side_effect=AssertionError('real LLM forbidden'))
            fixture.tts.side_effect = AssertionError('TTS forbidden')
            stack.enter_context(patch('reply_availability.check_reply_availability', return_value={'can_reply': True}))
            stack.enter_context(patch('db_read_receipt.mark_immediate_seen'))
            response, _body = fixture.send([''], text='🦖', source_event_id='current')
            self.assertEqual(response.status_code, 502)
            self.assertEqual(fixture.short_rows[0]['content'], '🦖')
            sent = fixture.route._create_json.call_args.args[3]
            self.assertEqual(sent[-1]['content'][1]['text'], '🦖')
            self.assertEqual(len(sent), 3)
            query = fixture.route.build_system_blocks.call_args.args[2]
            self.assertEqual(query, '🦖 ' + ' '.join(m['content'] for m in pack.raw_messages[-2:]))
            self.assertNotIn('【历史消息', query)
            self.assertNotIn('【后端当前时间事实', str(fixture.save_user_once.call_args))
            fixture.client.messages.create.assert_not_called()
            fixture.tts.assert_not_called()

    def test_cognitive_context_and_background_transcript_use_originals(self):
        rows = history()[:2]
        pack = context_layer.ChatContextPack(
            raw_messages=[{'role': r['role'], 'content': r['content']} for r in rows],
            messages=[{'role': r['role'], 'content': '【历史消息】' + r['content']} for r in rows])
        with patch.object(route_chat, '_fire_relationship_update') as ingress:
            route_chat._start_relationship_update(
                'u', 'gojo', '🦖', '😒', {'core_prompt': 'core'}, [], snapshot(), 'current', pack)
        self.assertEqual(ingress.call_args.args[5], pack.raw_messages)
        self.assertNotIn('【历史消息', context_layer.compact_transcript(pack))

    def test_priority_rule_is_single_and_static_prefix_has_no_current_clock(self):
        from test_prompt_cache_efficiency import CurrentTurnGroundingTests
        fixture = CurrentTurnGroundingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        blocks = prompt.build_system_blocks('u', 'gojo', '🦖', temporal_snapshot=snapshot(),
                                            context_pack=context_layer.ChatContextPack(
                                                recall_ready=True, support_ready=True))
        complete = '\n'.join(block['text'] for block in blocks)
        self.assertEqual(complete.count('话题优先级：'), 1)
        self.assertEqual(complete.count('【当前消息优先】'), 1)
        self.assertIn('后端当前时间事实', blocks[-1]['text'])
        self.assertNotIn('2026-10-14', blocks[0]['text'] + blocks[1]['text'])


if __name__ == '__main__':
    unittest.main()
