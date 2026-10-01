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
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS authority_operation_id TEXT NOT NULL DEFAULT 'main'",
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_authority_operation ON {table}(authority_event_id, authority_operation_id) WHERE authority_event_id IS NOT NULL",
        f"DROP INDEX IF EXISTS idx_{table}_authority_event",
    )
)


def dependencies_current_sql(dependencies, user, character):
    """Check every required snapshot, including the question behind a bare yes.

    Arguments are internal SQL expressions, never caller-provided identifiers.
    New bindings are private-chat only; group scope retains its existing policy.
    """
    return f"""(jsonb_array_length(COALESCE({dependencies}, '[]'::jsonb)) > 0
        AND NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements({dependencies}) dep
            WHERE NOT EXISTS (
                SELECT 1 FROM chat_log src
                WHERE src.user_id={user} AND src.chat_id={character}
                  AND COALESCE(NULLIF(src.event_id,''),src.client_msg_id)=dep->>'source_id'
                  AND COALESCE(src.status,'active')='active'
                  AND src.text=dep->>'content' AND src.role=dep->>'raw_role'
                  AND src.id::text=dep->>'row_id'
                  AND src.created_at=(dep->>'timestamp')::timestamptz
                  AND COALESCE(src.reply_to_event_id,'')=COALESCE(dep->>'reply_to_event_id','')
                  AND COALESCE(NULLIF(src.extra,''),'{{}}')::jsonb=dep->'metadata'
            )))"""


def current_answer_question_sql(alias='cognitive_questions'):
    deps = f"{alias}.metadata->'dependencies'"
    return f"""({dependencies_current_sql(deps, alias + '.user_id', alias + '.character_id')}
        AND ({alias}.status <> 'resolved' OR EXISTS (
            SELECT 1 FROM cognitive_events ae
            CROSS JOIN LATERAL jsonb_each(COALESCE(ae.adjudication->'operations','{{}}'::jsonb)) op
            WHERE ae.user_id={alias}.user_id AND ae.character_id={alias}.character_id
              AND ae.adjudication->>'status'='applied'
              AND op.value->>'status'='applied'
              AND op.value->>'question_key'={alias}.question_key
              AND ae.id::text={alias}.metadata->'current_judgment'->>'event_id'
              AND op.key={alias}.metadata->'current_judgment'->>'operation_id'
              AND op.value->>'memory_content'={alias}.metadata->'current_judgment'->>'content'
        )))"""


def filter_derived_answer_sources(rows, user_id, character_id):
    """Reject a summary/episode/cache when an answer dependency is no longer current."""
    ids = list({sid for row in rows for sid in row.get('source_event_ids', ())})
    if not ids:
        return []
    from db import get_conn
    database = cur = None
    try:
        database = get_conn()
        cur = database.cursor()
        cur.execute(f'''SELECT DISTINCT ce.source_event_id FROM cognitive_events ce
            CROSS JOIN LATERAL jsonb_each(COALESCE(ce.adjudication->'operations','{{}}')) op
            WHERE ce.user_id=%s AND ce.character_id=%s AND ce.source_event_id=ANY(%s)
              AND op.value->>'authority'='explicit_answer_v1'
              AND (ce.adjudication->>'status'='superseded' OR op.value->>'status'='superseded'
                   OR (op.value ? 'dependencies' AND NOT
                       {dependencies_current_sql("op.value->'dependencies'", 'ce.user_id', 'ce.character_id')}))''',
            (user_id, character_id, ids))
        invalid = {row[0] for row in cur.fetchall()}
        from raw_events import get_active_events_by_ids
        active = {row['event_id'] for row in get_active_events_by_ids(user_id, character_id, ids, conn=database)}
        return [row for row in rows if row.get('source_event_ids')
                and set(row['source_event_ids']).issubset(active - invalid)]
    except Exception:
        return []
    finally:
        if cur is not None:
            cur.close()
        if database is not None:
            database.close()


def answer_question_is_current(user_id, character_id, question, conn=None):
    """Revalidate a cached cognitive working set against current adjudication."""
    from db import get_conn
    database, cur = conn, None
    try:
        database = conn or get_conn()
        cur = database.cursor()
        cur.execute(f'''SELECT 1 FROM cognitive_questions
            WHERE user_id=%s AND character_id=%s AND question_key=%s
              AND metadata=%s::jsonb AND {current_answer_question_sql()}''',
            (user_id, character_id, question['question_key'],
             json.dumps(question['metadata'], ensure_ascii=False)))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        if cur is not None:
            cur.close()
        if conn is None and database is not None:
            database.close()


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
            CROSS JOIN LATERAL (SELECT CASE WHEN ce.adjudication ? 'operations'
                THEN ce.adjudication->'operations'->{table}.authority_operation_id
                ELSE ce.adjudication END AS decision) adjudicated
            JOIN chat_log raw ON raw.user_id=ce.user_id
                AND raw.chat_id=COALESCE(ce.payload->>'source_chat_id', ce.character_id)
                AND COALESCE(NULLIF(raw.event_id, ''), raw.client_msg_id)=ce.source_event_id
            WHERE ce.id={table}.authority_event_id
                AND ce.user_id={table}.user_id AND ce.character_id={table}.character_id
                AND COALESCE(raw.status, 'active')='active'
                AND raw.text=ce.payload->>'content'
                AND ce.adjudication->>'status'='applied'
                AND decision->>'status'='applied'
                AND decision->>'memory_content'={table}.content
                AND decision->>'memory_table'='{table}'
                AND (NOT (decision ? 'dependencies') OR
                     {dependencies_current_sql("decision->'dependencies'", 'ce.user_id', 'ce.character_id')})
                AND (
                    (ce.source_event_type='canonical_assistant_turn'
                     AND raw.role IN ('assistant', 'gojo', 'char', 'character')
                     AND {table}.authority_belief_key IS NULL)
                    OR
                    (ce.source_event_type='canonical_user_turn' AND raw.role='user'
                     AND ((decision->>'authority'='explicit_answer_v1'
                           AND {table}.authority_belief_key IS NULL)
                       OR EXISTS (
                        SELECT 1 FROM cognitive_beliefs b
                        WHERE b.user_id=ce.user_id AND b.character_id=ce.character_id
                          AND b.belief_key={table}.authority_belief_key
                          AND b.status='active' AND b.metadata->>'review_status'='stable'
                          AND b.statement={table}.content)))
                ))"""


def current_literal_belief_sql(alias):
    """Current source for the same literal report shown by cognitive readers."""
    if alias not in ('cognitive_beliefs', 'b'):
        raise ValueError('unsupported_belief_alias')
    return f"""EXISTS (
        SELECT 1 FROM cognitive_events ce
        CROSS JOIN LATERAL jsonb_each(COALESCE(ce.adjudication->'operations',
            jsonb_build_object('main',ce.adjudication))) op
        JOIN chat_log raw ON raw.user_id=ce.user_id
            AND raw.chat_id=COALESCE(ce.payload->>'source_chat_id', ce.character_id)
            AND COALESCE(NULLIF(raw.event_id, ''), raw.client_msg_id)=ce.source_event_id
        WHERE ce.user_id={alias}.user_id AND ce.character_id={alias}.character_id
            AND ce.source_event_type='canonical_user_turn'
            AND ce.adjudication->>'status'='applied'
            AND op.value->>'status'='applied'
            AND op.value->>'belief_key'={alias}.belief_key
            AND raw.role='user' AND COALESCE(raw.status, 'active')='active'
            AND raw.text=ce.payload->>'content'
            AND COALESCE(op.value->>'memory_content',
                '用户明确自述：' || (ce.payload->'claim'->>'text') ||
                '（仅限这次自述，不推断隐含心理）')={alias}.statement)"""


def project_canonical_memory(cur, *, user_id, character_id, event_id,
                             source_id, content, claim=None, belief_key=None,
                             occurred_at=None, operation_id='main', dependencies=None):
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
             authority,authority_event_id,authority_belief_key,expires_at,authority_operation_id)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
            ON CONFLICT (authority_event_id,authority_operation_id) WHERE authority_event_id IS NOT NULL
            DO NOTHING RETURNING id""",
            (user_id, character_id, content, category, occurred_at,
             json.dumps(dependencies or [{'source_id': source_id}]), AUTHORITY, event_id, belief_key, expires_at, operation_id))
    else:
        cur.execute("""INSERT INTO bond_memory
            (user_id,character_id,kind,content,timestamp,authority,authority_event_id,authority_belief_key,authority_operation_id)
            VALUES (%s,%s,'between',%s,%s,%s,%s,%s,%s)
            ON CONFLICT (authority_event_id,authority_operation_id) WHERE authority_event_id IS NOT NULL
            DO NOTHING RETURNING id""",
            (user_id, character_id, content, occurred_at, AUTHORITY, event_id, belief_key, operation_id))
    row = cur.fetchone()
    if row:
        for dependency in dependencies or [{'source_id': source_id}]:
            cur.execute("""INSERT INTO memory_source_events(memory_type,memory_id,source_event_id)
                           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (table, row[0], dependency['source_id']))
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
