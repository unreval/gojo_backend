"""Read-only inventory of old memory projections; never grants authority.

Pass an explicitly chosen database connection. This module does not connect to a
database, update rows, or treat a matching legacy phrase as evidence.
"""
from collections import Counter
import json

from memory_authority import AUTHORITY, authoritative_memory_sql
from role_view import PROJECTION_VERSION


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _classify(memory, event, sources, belief):
    if memory['authority'] != AUTHORITY:
        return 'historical_only'
    if memory['recall_status'] != 'active':
        return 'projection_inactive'
    if memory['expired']:
        return 'projection_expired'
    if not memory['authority_event_id'] or not event:
        return 'missing_canonical_event'
    if (event['user_id'], event['character_id']) != (memory['user_id'], memory['character_id']):
        return 'event_scope_mismatch'

    adjudication = _object(event['adjudication'])
    if adjudication.get('status') != 'applied':
        return 'event_not_applied'
    operations = adjudication.get('operations')
    decision = _object(operations.get(memory['authority_operation_id'] or 'main')) if isinstance(operations, dict) else adjudication
    if not decision:
        return 'operation_missing'
    if decision.get('status') != 'applied':
        return 'operation_not_applied'

    payload = _object(event['payload'])
    if not sources:
        return 'raw_source_missing'
    expected_chat = payload.get('source_chat_id') or event['character_id']
    source = next((s for s in sources if s['chat_id'] == expected_chat), None)
    if source is None:
        return 'raw_source_scope_mismatch'
    if source['status'] not in (None, 'active'):
        return 'raw_source_inactive'
    if source['text'] != payload.get('content'):
        return 'raw_source_text_mismatch'
    roles = ({'user'} if event['source_event_type'] == 'canonical_user_turn'
             else {'assistant', 'gojo', 'char', 'character'} if event['source_event_type'] == 'canonical_assistant_turn'
             else set())
    if source['role'] not in roles:
        return 'raw_source_role_mismatch'
    if decision.get('memory_table') != memory['table']:
        return 'verdict_table_mismatch'

    is_semantic = memory['projection_version'] == PROJECTION_VERSION
    if memory['authority_belief_key']:
        if not belief or belief['status'] != 'active' or _object(belief['metadata']).get('review_status') != 'stable':
            return 'belief_not_current'
        if is_semantic and memory['authority_belief_key'] != decision.get('belief_key'):
            return 'belief_verdict_mismatch'
        if not is_semantic and belief['statement'] != decision.get('memory_content'):
            return 'belief_verdict_mismatch'
    elif event['source_event_type'] == 'canonical_user_turn' and decision.get('authority') != 'explicit_answer_v1':
        return 'belief_missing'

    expected = decision.get('memory_content')
    actual = memory['content']
    if is_semantic:
        semantic = _object(memory['semantic_payload'])
        if (decision.get('projection_version') != PROJECTION_VERSION
                or semantic != _object(decision.get('semantic_payload'))
                or semantic.get('source_event_id') != event['source_event_id']
                or semantic.get('source_scope') != event['character_id']):
            return 'semantic_identity_mismatch'
        return ('current_semantic_projection' if actual == expected
                else 'current_semantic_display_changed')
    if actual == expected:
        if actual.startswith('用户'):
            return 'current_legacy_user'
        if actual.startswith('她'):
            return 'current_legacy_she'
        return 'current_projection'
    # The removed startup SQL only touched rows beginning with 用户, then
    # replaced every occurrence. This exact signature is an audit hint, not
    # sufficient proof to repair or admit the row for recall.
    if expected and expected.startswith('用户') and actual == expected.replace('用户', '她'):
        return 'legacy_startup_rewrite_candidate'
    return 'projection_mismatch_needs_review'


def audit_memory_projections(conn, user_id, character_id, *, limit=1000):
    """Classify up to ``limit`` rows per table using SELECT queries only.

    A rewrite candidate remains untrusted until an independently approved
    recovery verifies all provenance, scope, and current authority conditions.
    No production connection is opened here.
    """
    if limit < 1:
        raise ValueError('limit must be positive')
    cur = conn.cursor()
    records = []
    truncated = False
    try:
        for table in ('long_memory', 'bond_memory'):
            scope = "character_id IN (%s,'shared')" if table == 'long_memory' else 'character_id=%s'
            cur.execute(f'''SELECT id,user_id,character_id,content,authority,
                       authority_event_id,authority_operation_id,authority_belief_key,
                       projection_version,semantic_payload,
                       COALESCE(recall_status,'active'),
                       (expires_at IS NOT NULL AND expires_at <= CURRENT_TIMESTAMP)
                       FROM {table} WHERE user_id=%s AND {scope}
                       ORDER BY id LIMIT %s''', (user_id, character_id, limit + 1))
            rows = cur.fetchall()
            truncated |= len(rows) > limit
            for values in rows[:limit]:
                memory = dict(zip(('id', 'user_id', 'character_id', 'content', 'authority',
                                   'authority_event_id', 'authority_operation_id',
                                   'authority_belief_key', 'projection_version',
                                   'semantic_payload', 'recall_status', 'expired'), values))
                memory['table'] = table
                event = belief = None
                sources = []
                if memory['authority'] == AUTHORITY and memory['authority_event_id']:
                    cur.execute('''SELECT user_id,character_id,source_event_type,
                               source_event_id,payload,adjudication FROM cognitive_events
                               WHERE id=%s''', (memory['authority_event_id'],))
                    event_row = cur.fetchone()
                    if event_row:
                        event = dict(zip(('user_id', 'character_id', 'source_event_type',
                                          'source_event_id', 'payload', 'adjudication'), event_row))
                        cur.execute('''SELECT chat_id,role,text,status FROM chat_log
                                   WHERE user_id=%s AND COALESCE(NULLIF(event_id,''),client_msg_id)=%s''',
                                    (event['user_id'], event['source_event_id']))
                        sources = [dict(zip(('chat_id', 'role', 'text', 'status'), row))
                                   for row in cur.fetchall()]
                        if memory['authority_belief_key']:
                            cur.execute('''SELECT status,statement,metadata FROM cognitive_beliefs
                                       WHERE user_id=%s AND character_id=%s AND belief_key=%s''',
                                        (event['user_id'], event['character_id'], memory['authority_belief_key']))
                            belief_row = cur.fetchone()
                            if belief_row:
                                belief = dict(zip(('status', 'statement', 'metadata'), belief_row))
                recall_eligible = False
                if memory['authority'] == AUTHORITY:
                    cur.execute(f'''SELECT 1 FROM {table} WHERE id=%s AND user_id=%s
                               AND {authoritative_memory_sql(table)}''',
                                (memory['id'], memory['user_id']))
                    recall_eligible = cur.fetchone() is not None
                records.append({'table': table, 'id': memory['id'],
                                'authority_event_id': memory['authority_event_id'],
                                'status': _classify(memory, event, sources, belief),
                                'recall_eligible': recall_eligible})
    finally:
        cur.close()
    return {'records': records, 'counts': dict(Counter(r['status'] for r in records)),
            'truncated': truncated}
