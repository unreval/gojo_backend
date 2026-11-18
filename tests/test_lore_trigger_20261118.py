"""Character lore must not fire on an everyday character."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'gojo_backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import characters
import context_layer
import fix_character_memory_20261118 as seed_fix
import prompt
from characters import CHARACTER_LORE_NOTE, CHARACTER_LORE_TITLE
from characters_data.gojo.memories import SEED_MEMORIES


DAILY_KEYWORDS = {
    '吃', '喝', '甜', '睡', '夜', '困', '休息', '眼睛', '强', '厉害', '能力',
    '战斗', '朋友', '过去', '学生', '老师', '学校', '同学', '同事', '关心',
    '照顾', '治愈', '受伤', '目标', '未来', '家族', '出生', '自信', '几号',
    '学姐', '学弟', '校长', '班主任',
}
REMOVED_SENTENCES = (
    '最喜欢黄油土豆，尤其是北海道产的',
    '爱喝百事可乐，比其他饮料都喜欢',
    '睡得很少，每天只睡3小时左右，是个慢性熬夜的人',
)
EMPTY_QUERIES = (
    '这个蛋糕好甜',
    '我今晚想吃火锅',
    '勉强吧',
    '这家店有优惠',
    '我困了想睡觉',
    '我眼睛好痛',
    '我生日快到了',
)
HIT_QUERIES = (
    '你喜欢喜久福吗',
    '夏油杰是谁',
    '你的生日是哪天',
    '七海是你的谁',
    '你每天睡几个小时',
)
LORE_SAMPLE = '喜久福是仙台伴手礼'
OLD_TITLE_MARK = '此刻' + '自然想起'


def _keywords():
    found = []
    for _content, _category, keywords, _importance in SEED_MEMORIES:
        found.extend(part.strip() for part in keywords.split(',') if part.strip())
    lore_path = BACKEND / 'characters_data' / 'gojo' / 'lore.json'
    data = json.loads(lore_path.read_text(encoding='utf-8'))
    for entry in data['条目']:
        found.extend(part.strip() for part in entry.get('关键词', []) if part and part.strip())
    return found


class LoreKeywordTests(unittest.TestCase):
    def test_gojo_keywords_are_not_single_characters_or_everyday_words(self):
        keywords = _keywords()
        self.assertTrue(keywords)
        singles = [word for word in keywords if len(word) == 1]
        everyday = [word for word in keywords if word in DAILY_KEYWORDS]
        self.assertEqual(singles, [])
        self.assertEqual(everyday, [])
        contents = [item[0] for item in SEED_MEMORIES]
        for sentence in REMOVED_SENTENCES:
            self.assertNotIn(sentence, contents)

    def test_server_startup_does_not_run_the_seed_correction(self):
        server = (BACKEND / 'gojo_server.py').read_text(encoding='utf-8')
        self.assertNotIn('fix_character_memory_20261118', server)
        self.assertNotIn('fix_character_memory_20261116', server)


class LoreTitleTests(unittest.TestCase):
    def test_support_items_use_the_background_title(self):
        with ExitStack() as stack:
            stack.enter_context(patch(
                'psycopg2.connect', side_effect=AssertionError('live DB forbidden')))
            stack.enter_context(patch(
                'ai_client.get_anthropic', side_effect=AssertionError('model forbidden')))
            stack.enter_context(patch.object(
                characters, 'retrieve_character_memory', return_value=[LORE_SAMPLE]))
            items, _rules = context_layer._support_items('u', 'gojo', '你喜欢喜久福吗', [])
        lore = [item for item in items if item.item_type == 'character_lore']
        self.assertEqual(len(lore), 1)
        self.assertEqual(lore[0].item_id, 'lore:hits')
        self.assertEqual(lore[0].priority, 28)
        self.assertEqual(
            lore[0].text,
            f'{CHARACTER_LORE_TITLE}\n{CHARACTER_LORE_NOTE}\n- {LORE_SAMPLE}',
        )
        self.assertNotIn(OLD_TITLE_MARK, lore[0].text)

    def test_old_prompt_path_uses_the_background_title(self):
        with ExitStack() as stack:
            stack.enter_context(patch(
                'psycopg2.connect', side_effect=AssertionError('live DB forbidden')))
            stack.enter_context(patch(
                'ai_client.get_anthropic', side_effect=AssertionError('model forbidden')))
            stack.enter_context(patch.object(
                prompt, 'get_character', return_value={'core_prompt': 'CORE'}))
            stack.enter_context(patch.object(
                prompt, 'retrieve_character_memory', return_value=[LORE_SAMPLE]))
            stack.enter_context(patch.object(
                prompt.memory_search, 'is_vector_ready', return_value=False))
            stack.enter_context(patch(
                'smart_recall.two_level_recall', return_value=None))
            stack.enter_context(patch.object(prompt, 'get_long_memory', return_value=[]))
            stack.enter_context(patch.object(prompt, 'get_bond_memories', return_value=[]))
            stack.enter_context(patch.object(
                prompt, 'get_first_interaction_days', return_value=None))
            stack.enter_context(patch.object(prompt, 'build_relation_rules', return_value=''))
            stack.enter_context(patch.object(prompt, 'get_period_context', return_value=''))
            stack.enter_context(patch.object(prompt, 'get_recent_openings', return_value=[]))
            stack.enter_context(patch.object(
                prompt, 'get_last_assistant_reply', return_value=''))
            stack.enter_context(patch('diary_engine.build_diary_hint', return_value=''))
            stack.enter_context(patch(
                'temporal_awareness.build_prompt_context',
                side_effect=RuntimeError('offline')))
            stack.enter_context(patch(
                'db_schedule.format_world_prompt', side_effect=RuntimeError('offline')))
            _head, _rules, tail = prompt._build_prompt_parts('u', 'gojo', '你喜欢喜久福吗')
        block = f'{CHARACTER_LORE_TITLE}\n{CHARACTER_LORE_NOTE}\n- {LORE_SAMPLE}'
        self.assertIn(block, tail)
        self.assertNotIn(OLD_TITLE_MARK, tail)
        self.assertNotIn('聊到相关话题时可以像突然想起一样自然带出', tail)


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE for disposable PostgreSQL')
class LoreRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def setUp(self):
        self.database.rollback()
        self.database.query('DROP SCHEMA public CASCADE')
        self.database.query('CREATE SCHEMA public')
        self.database.query('''CREATE TABLE character_memory (
            id SERIAL PRIMARY KEY,
            character_id TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT DEFAULT '其他',
            keywords TEXT DEFAULT '',
            importance REAL DEFAULT 0.5,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        for content, category, keywords, importance in SEED_MEMORIES:
            self.database.query(
                '''INSERT INTO character_memory
                   (character_id, content, category, keywords, importance)
                   VALUES (%s, %s, %s, %s, %s)''',
                ('gojo', content, category, keywords, importance))
        self.conn = patch.object(characters, 'get_conn', return_value=self.database)
        self.conn.start()
        self.addCleanup(self.conn.stop)

    def test_everyday_words_do_not_retrieve_gojo_lore(self):
        for query in EMPTY_QUERIES:
            with self.subTest(query=query):
                self.assertEqual(characters.retrieve_character_memory('gojo', query), [])

    def test_proper_names_still_retrieve_gojo_lore(self):
        for query in HIT_QUERIES:
            with self.subTest(query=query):
                self.assertGreaterEqual(
                    len(characters.retrieve_character_memory('gojo', query)), 1)


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE for disposable PostgreSQL')
class LoreSeedCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def setUp(self):
        self.database.rollback()
        self.database.query('DROP SCHEMA public CASCADE')
        self.database.query('CREATE SCHEMA public')
        self.database.query('''CREATE TABLE character_memory (
            id SERIAL PRIMARY KEY,
            character_id TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT DEFAULT '其他',
            keywords TEXT DEFAULT '',
            importance REAL DEFAULT 0.5,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        self.conn = patch.object(seed_fix, 'get_conn', return_value=self.database)
        self.conn.start()
        self.addCleanup(self.conn.stop)

    def insert(self, character_id, content, keywords='旧关键词',
               category='关系', importance=0.6):
        self.database.query(
            '''INSERT INTO character_memory
               (character_id, content, category, keywords, importance)
               VALUES (%s, %s, %s, %s, %s)''',
            (character_id, content, category, keywords, importance))

    def stored(self):
        result = self.database.query(
            '''SELECT id, character_id, content, category, keywords, importance
                 FROM character_memory ORDER BY id''')
        return [
            (row['id'], row['character_id'], row['content'], row['category'],
             row['keywords'], row['importance'])
            for row in result['rows']
        ]

    def seed_originals(self):
        for item in seed_fix.ITEMS:
            self.insert('gojo', item['old'], '旧关键词', '原分类', 1)

    def test_dry_run_leaves_the_table(self):
        self.seed_originals()
        before = self.stored()
        captured = io.StringIO()
        with redirect_stdout(captured):
            counts = seed_fix.run(apply=False)
        output = captured.getvalue()
        self.assertEqual(self.stored(), before)
        self.assertEqual(counts, {'updated': 16, 'deleted': 2, 'skipped': 0})
        self.assertIn('汇总：改了 16 条，删了 2 条，跳过 0 条（演练，未写库）', output)
        self.assertNotIn('已删除', output)
        self.assertNotIn('已更新', output)

    def test_dry_run_reports_already_applied_and_hand_edits(self):
        applied = seed_fix.ITEMS[0]
        duplicated = seed_fix.ITEMS[1]
        edited = seed_fix.ITEMS[6]
        self.insert('gojo', applied['new'], applied['keywords'])
        self.insert('gojo', duplicated['old'])
        self.insert('gojo', duplicated['old'])
        self.insert('gojo', '他会逗学生，但这不是原句', '别动')
        self.insert('geto', duplicated['old'], '别动夏油角色')
        before = self.stored()

        captured = io.StringIO()
        with redirect_stdout(captured):
            counts = seed_fix.run(apply=False)
        output = captured.getvalue()

        self.assertEqual(self.stored(), before)
        self.assertEqual(counts['updated'], 0)
        self.assertEqual(counts['deleted'], 0)
        self.assertIn('已应用', output)
        self.assertIn('匹配到 2 行，跳过，不动', output)
        self.assertIn('未找到，可能在 App 里改过', output)
        self.assertIn('他会逗学生，但这不是原句', output)
        self.assertNotIn('别动夏油角色', output)

    def test_apply_deletes_two_and_updates_sixteen(self):
        self.seed_originals()
        self.insert('gojo', '这条是用户自己加的，不要动', '用户关键词', '其他', 0.5)
        self.insert('geto', seed_fix.ITEMS[1]['old'], '别删', '关系', 1)

        captured = io.StringIO()
        with redirect_stdout(captured):
            counts = seed_fix.run(apply=True)
        output = captured.getvalue()
        rows = self.stored()
        by_character = {}
        for _row_id, character_id, content, category, keywords, importance in rows:
            by_character.setdefault(character_id, []).append(
                (content, category, keywords, importance))

        self.assertEqual(counts, {'updated': 16, 'deleted': 2, 'skipped': 0})
        self.assertIn('汇总：改了 16 条，删了 2 条，跳过 0 条（已写入）', output)
        self.assertEqual(output.count('已删除'), 2)
        self.assertEqual(output.count('已更新'), 16)
        gojo = by_character['gojo']
        self.assertIn(
            ('这条是用户自己加的，不要动', '其他', '用户关键词', 0.5), gojo)
        for item in seed_fix.ITEMS:
            if item['new'] is None:
                self.assertNotIn(item['old'], [row[0] for row in gojo])
                continue
            self.assertIn((item['new'], '原分类', item['keywords'], 1), gojo)
        self.assertEqual(by_character['geto'], [(seed_fix.ITEMS[1]['old'], '关系', '别删', 1)])

    def test_failed_apply_rolls_back_the_whole_transaction(self):
        self.seed_originals()
        before = self.stored()

        def refuse_commit():
            raise RuntimeError('commit refused')

        with patch.object(self.database, 'commit', side_effect=refuse_commit):
            captured = io.StringIO()
            with redirect_stdout(captured):
                with self.assertRaises(RuntimeError):
                    seed_fix.run(apply=True)
        self.assertIn('已回滚', captured.getvalue())
        self.assertEqual(self.stored(), before)
