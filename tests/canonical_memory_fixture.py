"""Real PostgreSQL ingress for legacy memory behavior regressions."""
import json
from tests import test_cognitive_deterministic as hard
import user_memory


class CanonicalMemoryFixture:
    setUpClass = classmethod(hard.OfflineDatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(hard.OfflineDatabaseTests.tearDownClass.__func__)
    setUp = hard.OfflineDatabaseTests.setUp
    tearDown = hard.OfflineDatabaseTests.tearDown
    sql = hard.OfflineDatabaseTests.sql
    source = hard.OfflineDatabaseTests.source
    run_cycle = hard.OfflineDatabaseTests.run_cycle

    def ingest_sources(self, events, *, primary='u1', source_ids=None,
                       copied_user='', copied_assistant='', model_payload=None,
                       deleted=(), database_error=False, **ingress_options):
        for event in events:
            existing = self.sql(
                "SELECT text FROM chat_log WHERE event_id=%s", (event['event_id'],))
            if existing:
                self.assertEqual(existing[0][0], event['content'])
                continue
            self.source(event['event_id'], event['content'], role=event['role'])
            self.sql("UPDATE chat_log SET extra=%s,reply_to_event_id=%s WHERE event_id=%s",
                     (json.dumps(event.get('metadata') or {}),
                      event.get('reply_to_event_id'), event['event_id']))
        for source in deleted:
            self.sql("UPDATE chat_log SET status='deleted' WHERE event_id=%s", (source,))
        self.database.commit()
        if database_error:
            self.sql("ALTER TABLE chat_log RENAME COLUMN text TO unavailable_text")
            self.database.commit()
        try:
            ok = user_memory.extract_and_save_memory(
                'u', copied_user, copied_assistant, 'c', source_event_id=primary,
                source_event_ids=source_ids, parsed_override=model_payload,
                **ingress_options)
        finally:
            if database_error:
                self.database.rollback()
                self.sql("ALTER TABLE chat_log RENAME COLUMN unavailable_text TO text")
                self.database.commit()
        # The compatibility entry only enqueues. Exercise the real worker before
        # checking projections; no model, canonical reader or writer is mocked.
        while self.sql("SELECT count(*) FROM cognitive_event_triggers WHERE status IN ('pending','claimed')")[0][0]:
            self.run_cycle()
        return ok

    def assert_no_authority(self):
        for table in ('cognitive_beliefs', 'long_memory', 'bond_memory'):
            self.assertEqual(self.sql(f'SELECT count(*) FROM {table}')[0][0], 0, table)
