"""Chat renders view stickies as thoughts; diary keeps the default and skips them."""
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from smart_recall import format_recall_for_prompt  # noqa: E402


REMINDER = {
    'id': 11,
    'content': '记得带伞',
    'expires_at': '2026-10-25T22:42:00+08:00',
}
REMINDER_BLOCK = '''
【便利贴备忘——短期、可完成/可过期，不是长期记忆】
- [到期 2026-10-25 22:42] 记得带伞

使用规则：
1. 便利贴用于临近事项、未完成目标和短期提醒。
2. 到期或完成后不要继续当作普通回忆主动提起。
3. 它不绕过证据管线，也不能直接改变关系判断。'''
VIEW_TITLE = '【你自己记下的念头——你当时心里冒出来的话，不是她说的】'
VIEW_RULES = '''使用规则：
1. 这是你当时自己的想法。可以顺着它接话，也可以完全不提。
2. 绝不能说成“你说过”，也不能当成她说过的话或已确认的事实。
3. 它不能用来反驳她，也不改变任何关系判断。
4. 想法会变。它只代表当时，不代表你现在还这么想。'''


def _view(content, emotion=None):
    metadata = {'projection_version': 'sticky_view_v1'}
    if emotion is not None:
        metadata['emotion'] = emotion
    return {'id': 21, 'content': content, 'expires_at': '2026-10-25T22:42:00+08:00',
            'metadata': metadata}


class ViewStickyPromptTests(unittest.TestCase):
    def test_empty_sticky_list_emits_no_block(self):
        memory_text, bond_text, told_text = format_recall_for_prompt(
            {'sticky_notes': []})
        self.assertEqual(memory_text, '')
        self.assertEqual(bond_text, '')
        self.assertEqual(told_text, '')
        self.assertNotIn('便利贴备忘', memory_text)
        self.assertNotIn(VIEW_TITLE, memory_text)

    def test_reminder_only_block_is_byte_identical(self):
        default_text, default_bond, default_told = format_recall_for_prompt(
            {'sticky_notes': [REMINDER]})
        opted_text, opted_bond, opted_told = format_recall_for_prompt(
            {'sticky_notes': [REMINDER]}, include_view_sticky=True)
        self.assertEqual(default_text, opted_text)
        self.assertEqual((default_bond, default_told, opted_bond, opted_told),
                         ('', '', '', ''))
        self.assertIn(REMINDER_BLOCK, default_text)
        self.assertNotIn(VIEW_TITLE, default_text)
        self.assertEqual(default_text.count('便利贴备忘'), 1)

    def test_default_skips_view_notes_and_keeps_the_reminder_block(self):
        reminder_only, _, _ = format_recall_for_prompt({'sticky_notes': [REMINDER]})
        mixed, bond_text, told_text = format_recall_for_prompt({
            'sticky_notes': [REMINDER, _view('这账记得比谁都清楚，我可没那记性。', '在意')],
        })
        self.assertEqual(mixed, reminder_only)
        self.assertEqual((bond_text, told_text), ('', ''))
        self.assertNotIn(VIEW_TITLE, mixed)
        self.assertNotIn('这账记得比谁都清楚', mixed)
        self.assertNotIn('绝不能说成“你说过”', mixed)

    def test_view_only_renders_the_thought_block(self):
        memory_text, bond_text, told_text = format_recall_for_prompt({
            'sticky_notes': [_view('这账记得比谁都清楚，我可没那记性。', '在意')],
        }, include_view_sticky=True)
        self.assertIn(VIEW_TITLE, memory_text)
        self.assertIn(VIEW_RULES, memory_text)
        self.assertIn('- [在意] 这账记得比谁都清楚，我可没那记性。', memory_text)
        self.assertIn('绝不能说成“你说过”', memory_text)
        self.assertNotIn('便利贴备忘', memory_text)
        self.assertNotIn('[到期', memory_text)
        self.assertEqual((bond_text, told_text), ('', ''))

    def test_both_groups_put_the_reminder_block_first(self):
        memory_text, _, _ = format_recall_for_prompt({
            'sticky_notes': [
                _view('顺着杆子就往上爬，我倒觉得有意思。', '有意思'),
                REMINDER,
            ],
        }, include_view_sticky=True)
        reminder_at = memory_text.index('便利贴备忘')
        view_at = memory_text.index(VIEW_TITLE)
        self.assertLess(reminder_at, view_at)
        self.assertIn(REMINDER_BLOCK, memory_text)
        self.assertIn('- [有意思] 顺着杆子就往上爬，我倒觉得有意思。', memory_text)
        self.assertIn('绝不能说成“你说过”', memory_text)

    def test_missing_emotion_omits_the_brackets(self):
        missing, _, _ = format_recall_for_prompt({
            'sticky_notes': [_view('这账记得比谁都清楚，我可没那记性。')],
        }, include_view_sticky=True)
        blank, _, _ = format_recall_for_prompt({
            'sticky_notes': [_view('这账记得比谁都清楚，我可没那记性。', '  ')],
        }, include_view_sticky=True)
        self.assertIn('- 这账记得比谁都清楚，我可没那记性。', missing)
        self.assertIn('- 这账记得比谁都清楚，我可没那记性。', blank)
        for text in (missing, blank):
            view = text.split(VIEW_TITLE, 1)[1]
            self.assertNotIn('[]', view)
            self.assertNotIn('[到期', view)

    def test_diary_call_does_not_pass_include_view_sticky(self):
        prompt = (ROOT / 'gojo_backend' / 'prompt.py').read_text(encoding='utf-8')
        layer = (ROOT / 'gojo_backend' / 'context_layer.py').read_text(encoding='utf-8')
        diary = (ROOT / 'gojo_backend' / 'diary_engine.py').read_text(encoding='utf-8')
        self.assertEqual(prompt.count('include_view_sticky=True'), 2)
        self.assertEqual(layer.count('include_view_sticky=True'), 1)
        self.assertNotIn('include_view_sticky', diary)
        self.assertIn('format_recall_for_prompt(verified)', diary)


if __name__ == '__main__':
    unittest.main()
