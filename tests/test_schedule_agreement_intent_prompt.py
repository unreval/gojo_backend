"""Agreeing to clear a span must emit an intent; cancel or insert is the character's call."""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import context_layer
import db_schedule
import prompt
from schedule_contract import change_claim_conflict


sqlite3.register_converter(
    'ISO_TIMESTAMP', lambda value: datetime.fromisoformat(value.decode()))

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 25, 14, 48, tzinfo=TZ)
DIALOGUE = (
    '你是不是要去忙了，晚上再继续吧，晚上几点？',
    '三点开始打算出去一趟。晚上六点半以后有空。',
    '把你的日程表空出来给我哦　六点半到一点！',
    '六个半小时？你打算把我时间全占了啊。',
    '行吧，你中途别睡着就行。',
)
CASUAL_AGREEMENT = DIALOGUE[4]
USER_REQUEST = DIALOGUE[2]
PLANNED = [{'id': 47, 'status': 'planned'}]


def _sql_value(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


class ReadCursor:
    def __init__(self, connection):
        self.cursor = connection.cursor()
        self.close = Mock()

    def execute(self, sql, params):
        if not sql.lstrip().upper().startswith('SELECT '):
            raise AssertionError('prompt assembly must only read')
        self.cursor.execute(sql.replace('%s', '?'), tuple(map(_sql_value, params)))

    def fetchall(self):
        return self.cursor.fetchall()


class AgreementRequiresCancelPromptTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', detect_types=sqlite3.PARSE_DECLTYPES)
        self.addCleanup(self.db.close)
        self.db.execute('''CREATE TABLE char_schedule (
            id INTEGER PRIMARY KEY, revision INTEGER, character_id TEXT, user_id TEXT,
            status TEXT, sched_date TEXT,
            planned_start_at ISO_TIMESTAMP, planned_end_at ISO_TIMESTAMP,
            title TEXT, location TEXT, fixedness TEXT, category TEXT, reply_state TEXT)''')
        self.db.execute('''CREATE TABLE char_schedule_transition (
            schedule_id INTEGER, character_id TEXT, user_id TEXT, status TEXT,
            source_event_id TEXT, intent TEXT, created_at TEXT)''')
        cursor = ReadCursor(self.db)
        conn = Mock()
        conn.cursor.return_value = cursor
        self.patches = [
            patch.object(db_schedule, '_canonical_tz', return_value=TZ),
            patch.object(db_schedule, 'get_conn', return_value=conn),
            patch.object(db_schedule, '_advance_world_tx',
                         side_effect=AssertionError('clock advance is not part of this prompt')),
            patch.object(db_schedule, 'get_current_world_state', return_value={
                'event': None, 'activity': None, 'phase': None,
                'availability': {'reply_state': 'free', 'can_reply': True},
                'now': NOW,
            }),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stack_targets = (
            ('prompt.get_character', {'core_prompt': 'CORE-PERSONA'}),
            ('prompt.load_canon_lock', 'CANON-LOCK'),
            ('prompt.get_first_interaction_days', 20),
            ('prompt._accounts_block', ''),
            ('memory_authority.filter_recall_authority', {}),
            ('smart_recall.format_recall_for_prompt', ('', '', '')),
            ('context_layer.revalidate_pack_blocks', dict.fromkeys((
                'cognitive_prompt_text', 'pinned_prompt_text',
                'summary_prompt_text', 'episode_prompt_text'), '')),
            ('temporal_awareness.build_prompt_context', '现在是下午两点四十八。'),
        )
        for target, value in self.stack_targets:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def event(self, event_id, start, end, **changes):
        row = dict(
            id=event_id, revision=3, character_id='gojo', user_id='u',
            status='planned', sched_date=start.date().isoformat(),
            planned_start_at=start, planned_end_at=end,
            title='深夜事务处理', location='', fixedness='flexible',
            category='routine', reply_state='soft_busy')
        row.update(changes)
        columns = ', '.join(row)
        marks = ', '.join('?' for _ in row)
        self.db.execute(
            f'INSERT INTO char_schedule ({columns}) VALUES ({marks})',
            tuple(_sql_value(value) for value in row.values()))

    def rendered(self):
        pack = context_layer.ChatContextPack(
            support_ready=True, recall_ready=True, recall_result={},
            temporal_snapshot=dict(now_utc=NOW, has_history=False))
        blocks = prompt.build_system_blocks(
            'u', 'gojo', USER_REQUEST, context_pack=pack)
        self.assertEqual(len(blocks), 3)
        self.assertNotIn('cache_control', blocks[-1])
        return '\n'.join(block['text'] for block in blocks)

    def section(self, text):
        return text.split('【日程事实与结构化行动意图】', 1)[1].split('【提醒功能', 1)[0]

    def test_real_dialogue_agreement_requires_cancel_for_flexible_night(self):
        start = NOW.replace(hour=22, minute=0)
        end = (NOW + timedelta(days=1)).replace(hour=4, minute=0)
        self.event(47, start, end)
        text = self.rendered()
        rules = self.section(text)
        assistant_lines = (DIALOGUE[1], DIALOGUE[3], CASUAL_AGREEMENT)
        for line in assistant_lines:
            with self.subTest(line=line):
                self.assertIsNone(change_claim_conflict(
                    [{'zh': line}], {'event': None, 'now': NOW}, PLANNED, None))
        self.assertIn(CASUAL_AGREEMENT, rules)
        self.assertIn('就必须同时输出 schedule_action_intent', rules)
        self.assertIn('用 cancel 还是 insert，看挡路这件事本身', rules)
        self.assertIn('不重要、可做可不做、本来也没什么要做的，用 cancel', rules)
        self.assertIn('重要、但什么时候做都行、只要有时间就能做，用 insert', rules)
        self.assertIn('例如「陪她学习」', rules)
        self.assertIn('挡路的事会整体往后挪，不会消失', rules)
        self.assertIn('插入点之后还有 fixedness=fixed 的安排，不要用 insert', rules)
        self.assertIn('整段会被拒绝', rules)
        self.assertIn('只插入真正冲突的时段', rules)
        self.assertIn('18:30–22:00 本来没有安排', rules)
        self.assertIn('推过午夜', rules)
        self.assertNotIn('首选 type=cancel', rules)
        self.assertNotIn('不要用 insert 空出已有安排', rules)
        self.assertIn('答应为她空出一段时间，就是明确行动变化', rules)
        self.assertIn('没有明确行动变化就完全不要输出该字段', rules)
        self.assertIn('event_id=47 revision=3', text)
        self.assertIn('22:00~04:00 深夜事务处理', text)
        self.assertIn('fixedness=flexible', text)
        self.assertIn('必须输出 schedule_action_intent', text)
        self.assertIn('插入点之后若还有 fixedness=fixed 的安排', text)
        self.assertIn('只插入真正冲突的时段', text)
        self.assertNotIn('不要用 insert 把后面的安排往后推来假装把时间空出来', text)
        self.assertIsNone(change_claim_conflict(
            [{'jp': '途中で寝るなよ。', 'zh': CASUAL_AGREEMENT}],
            {'event': None, 'now': NOW}, PLANNED, None))

    def test_fixed_blocker_does_not_require_intent_or_block_the_reply(self):
        start = NOW.replace(hour=22, minute=0)
        end = (NOW + timedelta(days=1)).replace(hour=4, minute=0)
        self.event(47, start, end, fixedness='fixed')
        text = self.rendered()
        rules = self.section(text)
        self.assertIn('fixedness=fixed', text)
        self.assertIn('fixedness=fixed 的安排取消不了', rules)
        self.assertIn('不要假装答应，也不要输出该字段', rules)
        reply = '这档排死了，晚上没法让给你。'
        self.assertIsNone(change_claim_conflict(
            [{'zh': reply}], {'event': None, 'now': NOW}, PLANNED, None))

    def test_refusal_and_bargain_do_not_require_intent_or_block(self):
        text = self.rendered()
        rules = self.section(text)
        self.assertIn('明确拒绝或还价', rules)
        self.assertIn('这时不要输出该字段', rules)
        for reply in ('不行，一点太晚了，晚上我还得处理报告。',
                      '六点半到十点可以，再晚就不行。'):
            with self.subTest(reply=reply):
                self.assertIsNone(change_claim_conflict(
                    [{'zh': reply}], {'event': None, 'now': NOW}, PLANNED, None))

    def test_ordinary_chat_does_not_require_an_intent(self):
        text = self.rendered()
        rules = self.section(text)
        self.assertIn('没有明确行动变化就完全不要输出该字段', rules)
        self.assertNotIn('今天过得怎么样', rules)
        self.assertIsNone(change_claim_conflict(
            [{'jp': '今日はどうだった？', 'zh': '今天过得怎么样？'}],
            {'event': None, 'now': NOW}, PLANNED, None))
