"""Catch percent literals that break psycopg2 parameterized SQL execution."""
import unittest

from memory_authority import (
    authoritative_memory_sql,
    current_answer_question_sql,
    current_literal_belief_sql,
    dependencies_current_sql,
)


def _bare_percents(sql):
    """Find '%' characters that are neither escaped nor named placeholders."""
    found = []
    i = 0
    while i < len(sql):
        if sql[i] == '%':
            next_char = sql[i + 1] if i + 1 < len(sql) else ''
            if next_char in ('%', '('):
                i += 2
                continue
            found.append(sql[max(0, i - 25):i + 10].replace('\n', ' '))
        i += 1
    return found


class SqlPercentLiteralTests(unittest.TestCase):
    def test_authority_sql_has_no_bare_percent(self):
        for table in ('long_memory', 'bond_memory'):
            with self.subTest(table=table):
                self.assertEqual(_bare_percents(authoritative_memory_sql(table)), [])

    def test_other_authority_fragments_have_no_bare_percent(self):
        fragments = {
            'dependencies_current_sql': dependencies_current_sql(
                "op.value->'dependencies'", 'ce.user_id', 'ce.character_id'),
            'current_answer_question_sql': current_answer_question_sql(),
            'current_literal_belief_sql': current_literal_belief_sql('b'),
        }
        for name, sql in fragments.items():
            with self.subTest(fragment=name):
                self.assertEqual(_bare_percents(sql), [])

    def test_helper_detects_the_original_bug(self):
        self.assertTrue(_bare_percents("x LIKE '用户%'"))
        self.assertEqual(_bare_percents("x LIKE '用户%%'"), [])
        self.assertEqual(_bare_percents('x = %(name)s'), [])


if __name__ == '__main__':
    unittest.main()
