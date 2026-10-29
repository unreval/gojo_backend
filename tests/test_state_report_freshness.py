"""Reported-state cognition expires with the canonical event, not belief.updated_at."""
import json
import os
from pathlib import Path
import unittest

from tests import test_cognitive_deterministic as acceptance
from memory_authority import (
    STATE_REPORT_TTL_HOURS,
    current_literal_belief_sql,
    fresh_state_report_sql,
)


ROOT = Path(__file__).resolve().parents[1]


def _predicates(rows):
    found = set()
    for row in rows:
        scope = (row.get('metadata') or {}).get('claim_scope') or {}
        predicate = scope.get('predicate')
        if predicate:
            found.add(predicate)
    return found


class StateReportTtlLiteralTests(unittest.TestCase):
    def test_hour_count_is_defined_once_and_the_reader_does_not_repeat_it(self):
        authority = (ROOT / 'gojo_backend' / 'memory_authority.py').read_text(encoding='utf-8')
        reader = (ROOT / 'gojo_backend' / 'cognitive_reader.py').read_text(encoding='utf-8')
        recall = (ROOT / 'gojo_backend' / 'smart_recall.py').read_text(encoding='utf-8')
        self.assertEqual(authority.count('STATE_REPORT_TTL_HOURS = 48'), 1)
        self.assertNotIn('timedelta(hours=48)', authority)
        self.assertNotRegex(reader, r'\b48\b')
        self.assertNotIn('= 48', recall)
        self.assertIn('STATUS_EXPIRE_HOURS = STATE_REPORT_TTL_HOURS', recall)

    def test_freshness_sql_is_separate_from_current_source_sql(self):
        current = current_literal_belief_sql('cognitive_beliefs')
        self.assertNotIn('reported_state', current)
        self.assertNotIn('INTERVAL', current)
        belief = fresh_state_report_sql('cognitive_beliefs')
        hypothesis = fresh_state_report_sql('cognitive_hypotheses')
        for sql, key in (
                (belief, 'cognitive_beliefs.belief_key'),
                (hypothesis, 'cognitive_hypotheses.hypothesis_key')):
            self.assertIn("IS DISTINCT FROM 'reported_state'", sql)
            self.assertIn('ce.occurred_at >', sql)
            self.assertNotIn('updated_at', sql)
            self.assertIn(f"INTERVAL '{STATE_REPORT_TTL_HOURS} hours'", sql)
            self.assertIn(key, sql)
        with self.assertRaises(ValueError):
            fresh_state_report_sql('cognitive_questions')


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'set COGNITIVE_TEST_PGLITE to the local test-only package')
class StateReportReadTtlTests(unittest.TestCase):
    setUpClass = classmethod(acceptance.OfflineDatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(acceptance.OfflineDatabaseTests.tearDownClass.__func__)
    setUp = acceptance.OfflineDatabaseTests.setUp
    tearDown = acceptance.OfflineDatabaseTests.tearDown
    sql = acceptance.OfflineDatabaseTests.sql
    source = acceptance.OfflineDatabaseTests.source
    ingest = acceptance.OfflineDatabaseTests.ingest
    run_cycle = acceptance.OfflineDatabaseTests.run_cycle
    report = acceptance.OfflineDatabaseTests.report
    def age(self, predicate, hours):
        self.sql('''UPDATE cognitive_events ce
                    SET occurred_at = CURRENT_TIMESTAMP - (%s || ' hours')::interval
                    WHERE EXISTS (
                        SELECT 1
                        FROM jsonb_each(COALESCE(ce.adjudication->'operations',
                            jsonb_build_object('main', ce.adjudication))) op
                        JOIN cognitive_beliefs b
                          ON b.user_id=ce.user_id AND b.character_id=ce.character_id
                         AND b.belief_key=op.value->>'belief_key'
                        WHERE (b.metadata->'claim_scope')->>'predicate'=%s)''',
                 (str(hours), predicate))
        self.database.commit()

    def state(self):
        from cognitive_reader import fetch_cognitive_reader_state
        return fetch_cognitive_reader_state('u', 'c', conn=self.database)

    def test_reported_state_expires_on_event_time_for_beliefs_and_hypotheses(self):
        self.report('tired', '我的状态是「很累」')
        self.report('coffee', '我喜欢咖啡。')
        self.report('job', '我的职业是「教师」')
        inferred = json.dumps({'authority': 'model_inference'})
        self.sql('''INSERT INTO cognitive_beliefs
                    (user_id,character_id,belief_key,statement,confidence,status,
                     belief_type,metadata)
                    VALUES ('u','c','inferred-taste','她可能喜欢安静',0.4,'active',
                            'user_model',%s::jsonb)''', (inferred,))
        self.sql('''INSERT INTO cognitive_hypotheses
                    (user_id,character_id,hypothesis_key,statement,status,
                     hypothesis_type,confidence,metadata)
                    VALUES ('u','c','inferred-taste','她可能喜欢安静','supported',
                            'user_model',0.4,%s::jsonb)''', (inferred,))
        self.database.commit()
        paired = self.sql('''SELECT (b.metadata->'claim_scope')->>'predicate' AS belief_predicate,
                                    (h.metadata->'claim_scope')->>'predicate' AS hypothesis_predicate,
                                    b.metadata->>'authority' AS belief_authority,
                                    h.metadata->>'authority' AS hypothesis_authority
                             FROM cognitive_beliefs b
                             JOIN cognitive_hypotheses h
                               ON h.user_id=b.user_id AND h.character_id=b.character_id
                              AND h.hypothesis_key=b.belief_key
                             WHERE (b.metadata->'claim_scope')->>'predicate'='reported_state' ''')
        self.assertEqual(paired, [(
            'reported_state', 'reported_state',
            'literal_self_report_only', 'literal_self_report_only')])

        def visible(reader_state):
            belief_keys = {row['belief_key'] for row in reader_state['beliefs']}
            hypothesis_keys = {row['hypothesis_key'] for row in reader_state['hypotheses']}
            return (
                _predicates(reader_state['beliefs']),
                _predicates(reader_state['hypotheses']),
                'inferred-taste' in belief_keys,
                'inferred-taste' in hypothesis_keys,
            )

        fresh = visible(self.state())
        self.assertEqual(fresh[0], {'reported_state', 'reported_preference', 'reported_identity'})
        self.assertEqual(fresh[1], {'reported_state', 'reported_preference', 'reported_identity'})
        self.assertTrue(fresh[2] and fresh[3])

        self.age('reported_state', STATE_REPORT_TTL_HOURS - 1)
        self.assertEqual(visible(self.state()), fresh)

        self.age('reported_state', STATE_REPORT_TTL_HOURS + 1)
        aged = visible(self.state())
        self.assertNotIn('reported_state', aged[0])
        self.assertNotIn('reported_state', aged[1])
        self.assertEqual(aged[0], {'reported_preference', 'reported_identity'})
        self.assertEqual(aged[1], {'reported_preference', 'reported_identity'})
        self.assertTrue(aged[2] and aged[3])
        self.assertEqual(self.sql('''SELECT status FROM cognitive_beliefs
                                     WHERE metadata->'claim_scope'->>'predicate'='reported_state' '''),
                         [('active',)])
        self.assertEqual(self.sql('''SELECT status FROM cognitive_hypotheses
                                     WHERE metadata->'claim_scope'->>'predicate'='reported_state' '''),
                         [('supported',)])

        self.age('reported_preference', STATE_REPORT_TTL_HOURS + 1)
        self.age('reported_identity', STATE_REPORT_TTL_HOURS + 1)
        kept = visible(self.state())
        self.assertEqual(kept[0], {'reported_preference', 'reported_identity'})
        self.assertEqual(kept[1], {'reported_preference', 'reported_identity'})
        self.assertTrue(kept[2] and kept[3])
