"""Inventory and guarded recovery of old canonical memory projections.

Pass an explicitly chosen database connection. Audit and the default recovery
mode use SELECT only; the explicit apply mode rechecks and commits one transaction.
"""
from collections import Counter
import json

from memory_authority import (AUTHORITY, authoritative_memory_sql,
                              canonical_semantic_payload)
from role_view import PROJECTION_VERSION, render_role_view


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _array(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _decision(event, operation_id):
    adjudication = _object(event['adjudication'])
    operations = adjudication.get('operations')
    return _object(operations.get(operation_id or 'main')) if isinstance(operations, dict) else adjudication


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
    decision = _decision(event, memory['authority_operation_id'])
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


def _recovery_plan(cur, memory, event, sources, belief):
    """Only a parsed raw claim and complete linked provenance may be upgraded."""
    if _classify(memory, event, sources, belief) != 'legacy_startup_rewrite_candidate':
        return 'not_a_startup_rewrite', None
    if (memory['projection_version'] != 'legacy_v1'
            or memory['semantic_payload'] is not None
            or event['source_event_type'] != 'canonical_user_turn'):
        return 'unsupported_legacy_origin', None
    decision = _decision(event, memory['authority_operation_id'])
    if (decision.get('action') not in ('reported', 'corrected')
            or decision.get('projection_version') not in (None, 'legacy_v1')
            or not memory['authority_belief_key']
            or decision.get('belief_key') != memory['authority_belief_key']):
        return 'verdict_identity_incomplete', None
    payload = _object(event['payload'])
    claim = _object(payload.get('claim'))
    if len(sources) != 1 or sources[0]['chat_id'] != event['character_id']:
        return 'raw_source_not_unique_or_private', None
    from cognitive_revision import parse_cognitive_evidence
    parsed = parse_cognitive_evidence(sources[0]['text'], event['user_id'], event['character_id'])
    if parsed.get('operation') not in ('report', 'correction') or parsed.get('claim') != claim:
        return 'claim_not_verified_from_raw', None
    if (not belief or belief['statement'] != decision['memory_content']
            or _object(belief['metadata']).get('authority') != 'literal_self_report_only'):
        return 'belief_provenance_incomplete', None
    refs = _array(belief['evidence_refs'])
    if not any(ref.get('event_id') == event['id']
               and ref.get('source_id') == event['source_event_id']
               and ref.get('operation_id') == memory['authority_operation_id']
               for ref in refs if isinstance(ref, dict)):
        return 'belief_evidence_unlinked', None
    cur.execute('''SELECT source_event_id FROM memory_source_events
                   WHERE memory_type=%s AND memory_id=%s''',
                (memory['table'], memory['id']))
    links = [row[0] for row in cur.fetchall()]
    if set(links) != {event['source_event_id']}:
        return 'memory_source_links_incomplete', None
    if memory['table'] == 'long_memory':
        refs = _array(memory['source_event_refs'])
        if len(refs) != 1 or not isinstance(refs[0], dict) or refs[0].get('source_id') != event['source_event_id']:
            return 'memory_source_refs_incomplete', None
    question_key = claim.get('question_key')
    if not question_key:
        return 'question_key_missing', None
    cur.execute('''SELECT status,metadata FROM cognitive_questions
                   WHERE user_id=%s AND character_id=%s AND question_key=%s''',
                (event['user_id'], event['character_id'], question_key))
    question = cur.fetchone()
    if not question or question[0] != 'resolved':
        return 'question_not_current', None
    qmeta = _object(question[1])
    judgment = _object(qmeta.get('current_judgment'))
    if (judgment.get('belief_key') != memory['authority_belief_key']
            or event['source_event_id'] not in _array(judgment.get('evidence_event_ids'))
            or qmeta.get('claim_scope') != {key: claim.get(key) for key in
                                           ('subject', 'predicate', 'object', 'time_scope', 'source')}):
        return 'question_verdict_not_current', None
    semantic = canonical_semantic_payload(user_id=event['user_id'],
        character_id=event['character_id'], source_id=event['source_event_id'], claim=claim)
    content = render_role_view('', observer_id=event['character_id'],
        source_character_id=event['character_id'], semantic=semantic)
    return 'recoverable', {'table': memory['table'], 'id': memory['id'],
        'event_id': event['id'], 'operation_id': memory['authority_operation_id'],
        'belief_key': memory['authority_belief_key'], 'question_key': question_key,
        'semantic': semantic, 'content': content}


def audit_memory_projections(conn, user_id, character_id, *, limit=1000,
                             _plans=False, _lock=False):
    """Classify up to ``limit`` rows per table using SELECT queries only.

    A rewrite candidate remains untrusted until an independently approved
    recovery verifies all provenance, scope, and current authority conditions.
    No production connection is opened here.
    """
    if limit < 1:
        raise ValueError('limit must be positive')
    cur = conn.cursor()
    records = []
    plans = []
    truncated = False
    try:
        for table in ('long_memory', 'bond_memory'):
            scope = "character_id IN (%s,'shared')" if table == 'long_memory' else 'character_id=%s'
            cur.execute(f'''SELECT id,user_id,character_id,content,authority,
                       authority_event_id,authority_operation_id,authority_belief_key,
                       projection_version,semantic_payload,
                       {"source_event_refs" if table == "long_memory" else "'[]'::jsonb"},
                       COALESCE(recall_status,'active'),
                       (expires_at IS NOT NULL AND expires_at <= CURRENT_TIMESTAMP)
                       FROM {table} WHERE user_id=%s AND {scope}
                       ORDER BY id LIMIT %s {"FOR UPDATE" if _lock else ""}''',
                        (user_id, character_id, limit + 1))
            rows = cur.fetchall()
            truncated |= len(rows) > limit
            for values in rows[:limit]:
                memory = dict(zip(('id', 'user_id', 'character_id', 'content', 'authority',
                                   'authority_event_id', 'authority_operation_id',
                                   'authority_belief_key', 'projection_version',
                                   'semantic_payload', 'source_event_refs',
                                   'recall_status', 'expired'), values))
                memory['table'] = table
                event = belief = None
                sources = []
                if memory['authority'] == AUTHORITY and memory['authority_event_id']:
                    cur.execute(f'''SELECT id,user_id,character_id,source_event_type,
                               source_event_id,payload,adjudication FROM cognitive_events
                               WHERE id=%s {"FOR UPDATE" if _lock else ""}''',
                                (memory['authority_event_id'],))
                    event_row = cur.fetchone()
                    if event_row:
                        event = dict(zip(('id', 'user_id', 'character_id', 'source_event_type',
                                          'source_event_id', 'payload', 'adjudication'), event_row))
                        cur.execute(f'''SELECT chat_id,role,text,status FROM chat_log
                                   WHERE user_id=%s AND COALESCE(NULLIF(event_id,''),client_msg_id)=%s
                                   {"FOR UPDATE" if _lock else ""}''',
                                    (event['user_id'], event['source_event_id']))
                        sources = [dict(zip(('chat_id', 'role', 'text', 'status'), row))
                                   for row in cur.fetchall()]
                        if memory['authority_belief_key']:
                            cur.execute(f'''SELECT status,statement,metadata,evidence_refs FROM cognitive_beliefs
                                       WHERE user_id=%s AND character_id=%s AND belief_key=%s
                                       {"FOR UPDATE" if _lock else ""}''',
                                        (event['user_id'], event['character_id'], memory['authority_belief_key']))
                            belief_row = cur.fetchone()
                            if belief_row:
                                belief = dict(zip(('status', 'statement', 'metadata', 'evidence_refs'), belief_row))
                recall_eligible = False
                if memory['authority'] == AUTHORITY:
                    cur.execute(f'''SELECT 1 FROM {table} WHERE id=%s AND user_id=%s
                               AND {authoritative_memory_sql(table)}''',
                                (memory['id'], memory['user_id']))
                    recall_eligible = cur.fetchone() is not None
                status = _classify(memory, event, sources, belief)
                recovery_status = None
                if status == 'legacy_startup_rewrite_candidate':
                    recovery_status, plan = _recovery_plan(cur, memory, event, sources, belief)
                    if plan:
                        plans.append(plan)
                records.append({'table': table, 'id': memory['id'],
                                'authority_event_id': memory['authority_event_id'],
                                'status': status, 'recovery_status': recovery_status,
                                'recall_eligible': recall_eligible})
    finally:
        cur.close()
    result = {'records': records, 'counts': dict(Counter(r['status'] for r in records)),
              'recovery_counts': dict(Counter(r['recovery_status'] for r in records
                                              if r['recovery_status'])),
              'truncated': truncated}
    if _plans:
        result['_plans'] = plans
    return result


def recover_memory_projections(conn, user_id, character_id, *, dry_run=True, limit=1000):
    """Default to SELECT-only. Explicit apply atomically upgrades verified rows.

    The caller chooses the connection; this module never discovers credentials.
    Only IDs and status counts are returned, never source or memory text.
    """
    audit = audit_memory_projections(conn, user_id, character_id, limit=limit,
                                     _plans=True, _lock=not dry_run)
    plans = audit.pop('_plans')
    result = {'dry_run': dry_run, 'eligible_ids': [
        {'table': p['table'], 'id': p['id']} for p in plans],
        'counts': audit['counts'], 'recovery_counts': audit['recovery_counts'],
        'truncated': audit['truncated'], 'applied': 0}
    if dry_run or not plans:
        return result
    cur = conn.cursor()
    try:
        for plan in plans:
            payload = json.dumps(plan['semantic'], ensure_ascii=False)
            cur.execute('''UPDATE cognitive_events SET adjudication=jsonb_set(
                adjudication, ARRAY['operations',%s],
                (adjudication->'operations'->%s) || %s::jsonb)
                WHERE id=%s AND adjudication->>'status'='applied'
                  AND adjudication->'operations'->%s->>'status'='applied' ''',
                (plan['operation_id'], plan['operation_id'],
                 json.dumps({'projection_version': PROJECTION_VERSION,
                             'semantic_payload': plan['semantic']}, ensure_ascii=False),
                 plan['event_id'], plan['operation_id']))
            if cur.rowcount != 1:
                raise RuntimeError('adjudication_changed_during_recovery')
            cur.execute('''UPDATE cognitive_beliefs SET metadata=metadata || %s::jsonb
                           WHERE user_id=%s AND character_id=%s AND belief_key=%s
                             AND status='active' AND metadata->>'review_status'='stable' ''',
                        (json.dumps({'projection_version': PROJECTION_VERSION,
                                     'semantic_payload': plan['semantic']}, ensure_ascii=False),
                         user_id, character_id, plan['belief_key']))
            if cur.rowcount != 1:
                raise RuntimeError('belief_changed_during_recovery')
            cur.execute('''UPDATE cognitive_questions SET metadata=jsonb_set(metadata,
                '{current_judgment}',
                (metadata->'current_judgment') || %s::jsonb)
                WHERE user_id=%s AND character_id=%s AND question_key=%s
                  AND status='resolved' ''',
                (json.dumps({'projection_version': PROJECTION_VERSION,
                             'semantic_payload': plan['semantic']}, ensure_ascii=False),
                 user_id, character_id, plan['question_key']))
            if cur.rowcount != 1:
                raise RuntimeError('question_changed_during_recovery')
            table = plan['table']
            cur.execute(f'''UPDATE {table} SET content=%s,projection_version=%s,
                           semantic_payload=%s::jsonb
                           WHERE id=%s AND user_id=%s AND character_id=%s
                             AND authority=%s AND projection_version='legacy_v1' ''',
                        (plan['content'], PROJECTION_VERSION, payload, plan['id'],
                         user_id, character_id, AUTHORITY))
            if cur.rowcount != 1:
                raise RuntimeError('projection_changed_during_recovery')
            cur.execute(f'''SELECT 1 FROM {table} WHERE id=%s AND user_id=%s
                           AND {authoritative_memory_sql(table)}''',
                        (plan['id'], user_id))
            if cur.fetchone() is None:
                raise RuntimeError('recovered_projection_failed_authority_gate')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    result['applied'] = len(plans)
    return result
