# -*- coding: utf-8 -*-
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import context_budget  # noqa: E402
import context_layer  # noqa: E402


class ContextProfileTests(unittest.TestCase):
    def test_known_profiles_exist(self):
        for name in (
            'chat', 'text', 'voice', 'image', 'story', 'proactive',
            'group_chat', 'relationship_observer', 'memory_extractor',
            'diary_writer', 'cognitive_worker',
        ):
            self.assertIn(name, context_budget.PROFILES)
            cfg = context_budget.BudgetConfig.for_profile(name)
            self.assertGreater(cfg.total_token_budget, 0)

    def test_support_false_still_budgeted(self):
        huge = '日程说明' * 400
        trimmed = context_layer.budget_prompt_aux(
            {'schedule': huge, 'temporal': '现在下午', 'accounts': '账本'},
            profile='text',
        )
        self.assertTrue(trimmed.get('schedule') or trimmed.get('temporal'))
        used = sum(
            context_budget.estimate_tokens(text or '')
            for text in trimmed.values()
        )
        self.assertLess(used, context_budget.estimate_tokens(huge))

    def test_fallback_short_memory_goes_through_budget(self):
        rows = [('user', '很长的历史' * 80) for _ in range(40)]
        pack = context_layer.assemble_fallback_from_messages(
            rows, user_id='u', character_id='gojo', profile='voice')
        self.assertTrue(pack.messages)
        self.assertLessEqual(len(pack.messages), 40)
        self.assertFalse(pack.support_ready)
        used = sum(context_budget.estimate_tokens(m['content']) for m in pack.messages)
        voice = context_budget.BudgetConfig.for_profile('voice')
        self.assertLessEqual(used, voice.total_token_budget + 200)

    def test_prompt_without_support_still_mentions_budget(self):
        src = Path(os.path.join(BACKEND, 'prompt.py')).read_text(encoding='utf-8')
        self.assertIn('budget_prompt_aux', src)
        observer = Path(os.path.join(BACKEND, 'route_chat.py')).read_text(encoding='utf-8')
        self.assertIn('pack.messages', observer)
        diary = Path(os.path.join(BACKEND, 'diary_engine.py')).read_text(encoding='utf-8')
        self.assertIn("profile='diary_writer'", diary)

    def test_group_profile_caps_history_items(self):
        items = [
            context_budget.ContextItem(
                item_id=f'g{i}', item_type='hot_raw',
                text=('群消息内容。' * 20), priority=100, role='user',
            )
            for i in range(80)
        ]
        cfg = context_budget.BudgetConfig.for_profile('group_chat')
        kept = context_budget.ContextBudgetManager(cfg).allocate(items)
        self.assertLess(len(kept), 80)
        self.assertLessEqual(
            sum(item.token_cost for item in kept),
            cfg.total_token_budget + kept[-1].token_cost,
        )


if __name__ == '__main__':
    unittest.main()
