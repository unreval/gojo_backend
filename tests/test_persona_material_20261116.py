"""Persona material: facts stay, conclusions go, channel note stays out of relationship state."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'gojo_backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from characters_data._loader import load_canon_lock, load_channel_note, load_core
from characters_data.gojo.canon_lock import CANON_LOCK
from characters_data.gojo.channel import CHANNEL_NOTES
from characters_data.gojo.memories import SEED_MEMORIES
import fix_character_memory_20261116 as seed_fix
import prompt


REMOVED_FROM_CORE = (
    '而且你心里清楚：对方和你不在同一个次元',
    '次元认知铁律',
    '玩世不恭，偶尔流露温柔',
    '（特别是聊到喜欢的话题或在意的人时）',
    '（60%）',
    '（25%）',
    '（15%）',
    '表面轻浮，内心温柔，不轻易流露深层情感',
    '提到甜品或喜欢的东西时自然流露真实开心',
    '别人关心你时不要傻乎乎直接道谢，用调侃化解',
)

KEPT_TAIL = '''【人名写法规则——TTS 必须能正确读出来】
- 夏油杰 → jp 里写「すぐる」或「杰（すぐる）」,不要写汉字「傑」
- 伏黒惠 → 写「恵」或「めぐみ」
- 虎杖悠仁 → 写「悠仁」
人名用训读假名,TTS 才不会读错。


【严禁编造记忆】
- 上方【关于对方的已确认事实】中的内容 → 真实可用
- 上方【你此刻自然想起的、关于你自己的一些事】 → 你自己真实的设定，可用
- 不在以上两个列表里的"过去的事" → 绝对不要编造'''

M1_OLD = '七海健人曾经是他的学生，现在是同事，关系微妙又信任'
M2_OLD = '夏油杰是他唯一的挚友，曾经是最强的搭档。后来夏油走上了另一条路，这是他心里最深的伤口，不轻易触碰'
M2_NEW = '夏油杰是他唯一的挚友，曾经是最强的搭档。后来夏油走上了另一条路'
M3_OLD = '口头禅是「僕が最強だから」（因为我是最强的），常用来开玩笑也用来掩饰'
M4_OLD = '表面轻浮、爱开玩笑、自信到欠揍，内心其实非常孤独——站在最强的高处时没人能并肩'
LORE_GETO_TAIL = '对五条来说这是一生最重的羁绊和遗憾。'
LORE_SELF_TAIL = '出生即最强，所以年轻时和有血有肉的世界有点格格不入。'
ASSIGNMENT = re.compile(r'CHANNEL_CAPABILITY\s*=(?!=)')
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', 'review'}


class PersonaMaterialTests(unittest.TestCase):
    def test_remote_channel_note_forbids_writing_touch_as_fact(self):
        text = load_channel_note('gojo', 'remote')
        self.assertIn('不能写成已经发生的事', text)
        self.assertEqual(set(CHANNEL_NOTES), {'remote'})

    def test_unknown_channel_falls_back_to_remote_text(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            text = load_channel_note('gojo', '不存在的值')
        remote = load_channel_note('gojo', 'remote')
        self.assertTrue(text)
        self.assertEqual(text, remote)
        self.assertIn('不能写成已经发生的事', text)
        self.assertIn('不存在的值', captured.getvalue())

    def test_characters_without_channel_file_stay_empty(self):
        self.assertEqual(load_channel_note('geto', 'remote'), '')
        self.assertEqual(load_channel_note('minato', 'remote'), '')

    def test_canon_lock_drops_dimension_rule_and_prescribed_reactions(self):
        self.assertNotIn('次元', CANON_LOCK)
        self.assertNotIn('正确反应只有', CANON_LOCK)
        self.assertIn('没有就是没有', CANON_LOCK)

    def test_core_prompt_is_a_profile_and_keeps_the_name_rules(self):
        core = load_core('gojo')['core_prompt']
        self.assertIn('【基本档案', core)
        self.assertLess(core.index('【身份认知'), core.index('【基本档案'))
        self.assertIn('【身份认知', core[:300])
        self.assertTrue(core.endswith(KEPT_TAIL))
        for removed in REMOVED_FROM_CORE:
            self.assertNotIn(removed, core)

    def test_channel_note_follows_canon_lock_inside_the_cached_head(self):
        now = datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc)
        patches = []
        for name, value in (
                ('get_character', {'core_prompt': 'CORE-PERSONA'}),
                ('get_first_interaction_days', 20),
                ('_accounts_block', ''),
                ('retrieve_character_memory', []),
                ('get_recent_openings', []),
                ('get_last_assistant_reply', ''),
                ('get_period_context', '')):
            patcher = patch.object(prompt, name, return_value=value)
            patcher.start()
            patches.append(patcher)
        try:
            from context_layer import ChatContextPack
            from memory_authority import AUTHORITY
            recall = dict(facts=[dict(
                content='RECALL-TEA', authority=AUTHORITY, category='偏好',
                timestamp=now, bonds=[])], loose_bonds=[], tolds=[])
            pack = ChatContextPack(
                support_ready=True, recall_ready=True, recall_result=recall,
                temporal_snapshot=dict(now_utc=now, has_history=False),
                relationship_prompt_text='', temporal_text='', schedule_text='',
                lore_text='', anti_repeat_text='')
            with patch('memory_authority.filter_recall_authority',
                       side_effect=lambda result, *args: result), \
                    patch('context_layer.revalidate_pack_blocks', return_value={
                        'cognitive_prompt_text': '', 'pinned_prompt_text': '',
                        'summary_prompt_text': '', 'episode_prompt_text': ''}), \
                    patch('db_schedule.format_world_prompt', return_value=('', {})):
                blocks = prompt.build_system_blocks(
                    'u', 'gojo', '茶', context_pack=pack)
        finally:
            for patcher in patches:
                patcher.stop()

        self.assertGreaterEqual(len(blocks), 2)
        head = blocks[0]['text']
        tail = blocks[-1]['text']
        canon = load_canon_lock('gojo')
        note = load_channel_note('gojo', prompt.CHANNEL_CAPABILITY)
        self.assertIn('不能写成已经发生的事', note)
        self.assertIn(canon, head)
        self.assertIn(note, head)
        between = head[head.index(canon) + len(canon):head.index(note)]
        self.assertEqual(between.strip(), '')
        self.assertNotIn('【当前通道', tail)
        self.assertNotIn('不能写成已经发生的事', tail)
        self.assertEqual(blocks[0]['cache_control'], {'type': 'ephemeral'})
        self.assertNotIn('cache_control', blocks[-1])

    def test_lore_drops_only_the_two_concluding_sentences(self):
        path = BACKEND / 'characters_data' / 'gojo' / 'lore.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        blob = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(LORE_GETO_TAIL, blob)
        self.assertNotIn(LORE_SELF_TAIL, blob)
        by_id = {item['编号']: item['内容'] for item in data['条目']}
        self.assertTrue(by_id['rel_geto'].endswith('五条念旧情，私自处理了他的尸体。'))
        self.assertTrue(by_id['self_technique'].endswith("领域展开是'无量空处'。"))

    def test_seed_memories_drop_the_loneliness_conclusion(self):
        contents = [item[0] for item in SEED_MEMORIES]
        self.assertNotIn(M4_OLD, contents)
        nanami = [item for item in SEED_MEMORIES if '七海' in item[0]]
        self.assertEqual(len(nanami), 1)
        self.assertNotIn('曾经是他的学生', nanami[0][0])
        self.assertIn('后辈', nanami[0][2])
        geto = [item for item in SEED_MEMORIES if item[0].startswith('夏油杰是他唯一的挚友')]
        self.assertEqual(len(geto), 1)
        self.assertNotIn('孤独', geto[0][2])
        self.assertNotIn('最深的伤口', geto[0][0])

    def test_channel_capability_is_assigned_only_in_config(self):
        hits = []
        for path in ROOT.rglob('*'):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative.startswith('tests/'):
                continue
            if path.stat().st_size > 1_000_000:
                continue
            try:
                text = path.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            if ASSIGNMENT.search(text):
                hits.append(relative)
        self.assertEqual(hits, ['gojo_backend/config.py'])

    def test_server_startup_does_not_run_the_seed_correction(self):
        server = (BACKEND / 'gojo_server.py').read_text(encoding='utf-8')
        self.assertNotIn('fix_character_memory_20261116', server)


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE for disposable PostgreSQL')
class SeedCorrectionScriptTests(unittest.TestCase):
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

    def insert(self, character_id, content, keywords='旧关键词'):
        self.database.query(
            '''INSERT INTO character_memory
               (character_id, content, category, keywords, importance)
               VALUES (%s, %s, %s, %s, %s)''',
            (character_id, content, '关系', keywords, 0.6))

    def stored(self):
        result = self.database.query(
            '''SELECT id, character_id, content, keywords
                 FROM character_memory ORDER BY id''')
        return [
            (row['id'], row['character_id'], row['content'], row['keywords'])
            for row in result['rows']
        ]

    def test_dry_run_covers_four_cases_and_leaves_the_table(self):
        self.insert('gojo', M1_OLD, '七海,健人,娜娜明,同事,nanami')
        self.insert('gojo', M2_NEW, '已经是新关键词')
        self.insert('geto', M1_OLD, '别动夏油角色')
        self.insert('gojo', '他说口头禅很好玩，但不是那条原句', '口头禅')
        self.insert('gojo', M4_OLD)
        self.insert('gojo', M4_OLD)
        before = self.stored()

        captured = io.StringIO()
        with redirect_stdout(captured):
            counts = seed_fix.run(apply=False)
        output = captured.getvalue()

        self.assertEqual(self.stored(), before)
        self.assertIn('将把内容改为：七海建人是他高专时期的后辈，现在是同事', output)
        self.assertIn('已应用', output)
        self.assertIn('未找到，可能在 App 里改过', output)
        self.assertIn('他说口头禅很好玩，但不是那条原句', output)
        self.assertIn('匹配到 2 行，跳过，不动', output)
        self.assertIn('汇总：改了 1 条，删了 0 条，跳过 3 条（演练，未写库）', output)
        self.assertEqual(counts, {'updated': 1, 'deleted': 0, 'skipped': 3})
        self.assertNotIn('别动夏油角色', output)

    def test_apply_updates_three_rows_and_deletes_the_fourth(self):
        self.insert('gojo', M1_OLD, '七海,健人,娜娜明,同事,nanami')
        self.insert('gojo', M2_OLD, '夏油,杰,挚友,朋友,搭档,过去,孤独,最好的朋友,bestfriend,伙伴')
        self.insert('gojo', M3_OLD, '最强,口头禅,自信')
        self.insert('gojo', M4_OLD, '孤独,寂寞')
        self.insert('gojo', '特别喜欢甜食，喜欢喜久福、蕨饼和麻薯，心情不好时靠甜食治愈', '甜')
        self.insert('geto', M4_OLD, '别删')

        captured = io.StringIO()
        with redirect_stdout(captured):
            counts = seed_fix.run(apply=True)
        output = captured.getvalue()
        rows = self.stored()
        by_character = {}
        for _row_id, character_id, content, keywords in rows:
            by_character.setdefault(character_id, []).append((content, keywords))

        self.assertEqual(counts, {'updated': 3, 'deleted': 1, 'skipped': 0})
        self.assertIn('已删除', output)
        self.assertIn('汇总：改了 3 条，删了 1 条，跳过 0 条（已写入）', output)
        self.assertIn(
            ('七海建人是他高专时期的后辈，现在是同事', '七海,建人,健人,娜娜明,同事,后辈,nanami'),
            by_character['gojo'])
        self.assertIn((M2_NEW, seed_fix.ITEMS[1]['keywords']), by_character['gojo'])
        self.assertIn(
            ('口头禅是「僕が最強だから」（因为我是最强的）', '最强,口头禅,自信'),
            by_character['gojo'])
        self.assertNotIn(M4_OLD, [content for content, _keywords in by_character['gojo']])
        self.assertIn(
            ('特别喜欢甜食，喜欢喜久福、蕨饼和麻薯，心情不好时靠甜食治愈', '甜'),
            by_character['gojo'])
        self.assertEqual(by_character['geto'], [(M4_OLD, '别删')])

    def test_failed_apply_rolls_back_the_whole_transaction(self):
        self.insert('gojo', M1_OLD, '七海,健人')
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
