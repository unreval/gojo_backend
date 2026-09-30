"""Current canonical provenance for long/bond projections, shared by readers."""
import json

AUTHORITY = 'canonical_evidence_v1'
MEMORY_AUTHORITY_DDL = tuple(
    statement
    for table in ('long_memory', 'bond_memory')
    for statement in (
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS authority TEXT NOT NULL DEFAULT 'generated_recollection'",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS authority_event_id BIGINT",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS authority_belief_key TEXT",
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_authority_event ON {table}(authority_event_id) WHERE authority_event_id IS NOT NULL",
    )
)


def authoritative_memory_sql(table):
    """SQL is correlated to a fixed table name, never supplied by a request.

    A badge or source id alone proves nothing: compare the canonical text,
    adjudication, projected content, owner, scope, and effective belief too.
    This also rechecks cached/vector candidates and late legacy writes.
    """
    if table not in ('long_memory', 'bond_memory'):
        raise ValueError('unsupported_memory_table')
    return f"""{table}.authority = '{AUTHORITY}'
        AND COALESCE({table}.recall_status, 'active') = 'active'
        AND ({table}.expires_at IS NULL OR {table}.expires_at > CURRENT_TIMESTAMP)
        AND EXISTS (
            SELECT 1 FROM cognitive_events ce
            JOIN chat_log raw ON raw.user_id=ce.user_id
                AND raw.chat_id=COALESCE(ce.payload->>'source_chat_id', ce.character_id)
                AND COALESCE(NULLIF(raw.event_id, ''), raw.client_msg_id)=ce.source_event_id
            WHERE ce.id={table}.authority_event_id
                AND ce.user_id={table}.user_id AND ce.character_id={table}.character_id
                AND COALESCE(raw.status, 'active')='active'
                AND raw.text=ce.payload->>'content'
                AND ce.adjudication->>'status'='applied'
                AND ce.adjudication->>'memory_content'={table}.content
                AND ce.adjudication->>'memory_table'='{table}'
                AND (
                    (ce.source_event_type='canonical_assistant_turn'
                     AND raw.role IN ('assistant', 'gojo', 'char', 'character')
                     AND {table}.authority_belief_key IS NULL)
                    OR
                    (ce.source_event_type='canonical_user_turn' AND raw.role='user'
                     AND EXISTS (
                        SELECT 1 FROM cognitive_beliefs b
                        WHERE b.user_id=ce.user_id AND b.character_id=ce.character_id
                          AND b.belief_key={table}.authority_belief_key
                          AND b.status='active' AND b.metadata->>'review_status'='stable'
                          AND b.statement={table}.content))
                ))"""


def current_literal_belief_sql(alias):
    """Current source for the same literal report shown by cognitive readers."""
    if alias not in ('cognitive_beliefs', 'b'):
        raise ValueError('unsupported_belief_alias')
    return f"""EXISTS (
        SELECT 1 FROM cognitive_events ce
        JOIN chat_log raw ON raw.user_id=ce.user_id
            AND raw.chat_id=COALESCE(ce.payload->>'source_chat_id', ce.character_id)
            AND COALESCE(NULLIF(raw.event_id, ''), raw.client_msg_id)=ce.source_event_id
        WHERE ce.user_id={alias}.user_id AND ce.character_id={alias}.character_id
            AND ce.source_event_type='canonical_user_turn'
            AND ce.adjudication->>'status'='applied'
            AND ce.adjudication->>'belief_key'={alias}.belief_key
            AND raw.role='user' AND COALESCE(raw.status, 'active')='active'
            AND raw.text=ce.payload->>'content'
            AND COALESCE(ce.adjudication->>'memory_content',
                '用户明确自述：' || (ce.payload->'claim'->>'text') ||
                '（仅限这次自述，不推断隐含心理）')={alias}.statement)"""


def project_canonical_memory(cur, *, user_id, character_id, event_id,
                             source_id, content, claim=None, belief_key=None,
                             occurred_at=None):
    """Project an adjudicated literal report/utterance in its cycle transaction.

    The caller has locked and reloaded the original message. Generic memory
    writers cannot select authority or pass model prose into this function.
    Readers independently verify the resulting row against its adjudication.
    """
    predicate = (claim or {}).get('predicate')
    is_bond = not claim or predicate in ('explicit_boundary', 'explicit_refusal', 'explicit_promise', 'reported_fulfillment')
    table = 'bond_memory' if is_bond else 'long_memory'
    from datetime import timedelta
    expires_at = occurred_at + timedelta(hours=48) if predicate == 'reported_state' and occurred_at else None
    category = {'reported_preference': '喜好', 'reported_state': '状态',
                'reported_identity': '身份'}.get(predicate, '其他')
    if table == 'long_memory':
        cur.execute("""INSERT INTO long_memory
            (user_id,character_id,content,category,timestamp,source_event_refs,
             authority,authority_event_id,authority_belief_key,expires_at)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
            ON CONFLICT (authority_event_id) WHERE authority_event_id IS NOT NULL
            DO NOTHING RETURNING id""",
            (user_id, character_id, content, category, occurred_at,
             json.dumps([{'source_id': source_id}]), AUTHORITY, event_id, belief_key, expires_at))
    else:
        cur.execute("""INSERT INTO bond_memory
            (user_id,character_id,kind,content,timestamp,authority,authority_event_id,authority_belief_key)
            VALUES (%s,%s,'between',%s,%s,%s,%s,%s)
            ON CONFLICT (authority_event_id) WHERE authority_event_id IS NOT NULL
            DO NOTHING RETURNING id""",
            (user_id, character_id, content, occurred_at, AUTHORITY, event_id, belief_key))
    row = cur.fetchone()
    if row:
        cur.execute("""INSERT INTO memory_source_events(memory_type,memory_id,source_event_id)
                       VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""", (table, row[0], source_id))
    return {'memory_table': table, 'memory_content': content}


def filter_recall_authority(result, user_id, character_id):
    """Revalidate factual candidates at prompt assembly, including cached packs.

    Recollections in the subjective diary/episode channels retain their own
    non-factual labels. No caller-provided authority marker can admit a fact.
    """
    if result is None:
        return None
    result = dict(result)
    facts = result.get('facts') or []
    bonds = list(result.get('loose_bonds') or []) + list(result.get('tolds') or [])
    bonds += [bond for fact in facts for bond in fact.get('bonds', [])]
    from db import get_conn
    allowed = {}
    database = None
    try:
        for table, candidates in (('long_memory', facts), ('bond_memory', bonds)):
            ids = list({row['id'] for row in candidates if isinstance(row.get('id'), int)})
            allowed[table] = set()
            if not ids:
                continue
            if database is None:
                database = get_conn()
            cur = database.cursor()
            try:
                cur.execute(f"""SELECT id, content FROM {table}
                    WHERE user_id=%s AND character_id IN (%s,%s) AND id=ANY(%s)
                      AND {authoritative_memory_sql(table)}""",
                    (user_id, character_id, 'shared' if table == 'long_memory' else character_id, ids))
                allowed[table] = set(cur.fetchall())
            finally:
                cur.close()
    except Exception:
        # A source-check failure must not expose a cached generated assertion.
        allowed = {'long_memory': set(), 'bond_memory': set()}
    finally:
        if database is not None:
            database.close()

    def keep(rows, table):
        return [dict(row, authority=AUTHORITY) for row in rows
                if (row.get('id'), row.get('content')) in allowed[table]]

    result['facts'] = keep(facts, 'long_memory')
    for fact in result['facts']:
        fact['bonds'] = keep(fact.get('bonds', []), 'bond_memory')
    for key in ('loose_bonds', 'tolds'):
        result[key] = keep(result.get(key) or [], 'bond_memory')
    return result
