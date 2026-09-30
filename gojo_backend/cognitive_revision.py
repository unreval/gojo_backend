"""Deterministic Slow Loop revision helpers. No LLM. No rel_state writes."""
import re
import hashlib
import json

from cognitive_config import (
    COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE,
    COGNITIVE_CONFIDENCE_DELTA,
    COGNITIVE_SCOPE_CROSS_MIN_CONTEXTS,
    COGNITIVE_SCOPE_TENDENCY_MIN_CONTEXTS,
    SHARED_RELATIONSHIP_FRAME_KEY,
)


EVIDENCE_RELATIONS = frozenset({
    'support', 'contradiction', 'scope_limiter', 'irrelevant',
})
EVIDENCE_STRENGTHS = frozenset({'weak', 'normal', 'strong'})
BELIEF_SCOPES = frozenset({
    'single_event', 'topic', 'relationship_context',
    'cross_context', 'general_tendency', 'unknown', 'legacy',
})
FRAME_KINDS = frozenset({
    'friends', 'playful_ambiguous', 'probing',
    'serious_unconfirmed', 'committed_romantic', 'frame_shifting',
    'unknown', 'legacy',
})
REVIEW_STATUSES = frozenset({
    'stable', 'under_review', 'reopened',
})

_EPISODIC_RE = re.compile(
    r'(今晚|今天|刚才|刚刚|本次|这轮|这一次|那一次|昨晚|今早)'
    r'|(又喝酒|去睡觉|说自己很痛苦|要求用户)'
)
_QUOTED_UTTERANCE_RE = re.compile(r'[「『“"].{2,40}[」』”"]')
_GLOBAL_JUDGMENT_RE = re.compile(
    r'(缺乏独立|没有主见|不会自主|总是依赖|性格软弱|人格不成熟'
    r'|只有.{0,8}一个长期目标|永远不会)'
)
_REVISION_STICKY_RE = re.compile(
    r'(旧判断|可能不对|认识可能错|需要修正|不再只是'
    r'|好像开始有独立|原有.{0,12}判断)'
)


def is_episodic_statement(text) -> bool:
    """Single-event recap / utterance / instantaneous state cannot be a belief."""
    statement = str(text or '').strip()
    if not statement:
        return True
    if _EPISODIC_RE.search(statement) and len(statement) < 80:
        return True
    if _QUOTED_UTTERANCE_RE.search(statement) and len(statement) < 60:
        return True
    return False


def is_global_personality_judgment(text) -> bool:
    return bool(_GLOBAL_JUDGMENT_RE.search(str(text or '')))


def looks_like_belief_revision_sticky(content) -> bool:
    return bool(_REVISION_STICKY_RE.search(str(content or '')))


def normalize_scope(value, *, legacy=False) -> str:
    raw = str(value or '').strip()
    if raw in BELIEF_SCOPES:
        return raw
    return 'legacy' if legacy else 'unknown'


def allowed_scope_for_contexts(context_count) -> str:
    count = max(0, int(context_count or 0))
    if count >= COGNITIVE_SCOPE_TENDENCY_MIN_CONTEXTS:
        return 'general_tendency'
    if count >= COGNITIVE_SCOPE_CROSS_MIN_CONTEXTS:
        return 'cross_context'
    if count == 1:
        return 'topic'
    return 'single_event'


def scope_exceeds_evidence(scope, independent_contexts) -> bool:
    scope = normalize_scope(scope)
    if scope not in {'cross_context', 'general_tendency'}:
        return False
    allowed = allowed_scope_for_contexts(len(independent_contexts or []))
    rank = {
        'single_event': 0,
        'topic': 1,
        'relationship_context': 1,
        'unknown': 1,
        'legacy': 1,
        'cross_context': 2,
        'general_tendency': 3,
    }
    return rank.get(scope, 1) > rank.get(allowed, 1)


def confidence_delta(relation, strength) -> float:
    relation = str(relation or '').strip()
    strength = str(strength or 'normal').strip()
    if relation not in EVIDENCE_RELATIONS or relation == 'irrelevant':
        return 0.0
    if strength not in EVIDENCE_STRENGTHS:
        strength = 'normal'
    return float(COGNITIVE_CONFIDENCE_DELTA[(relation, strength)])


def apply_confidence_delta(old_confidence, relation, strength) -> float:
    old = float(old_confidence or 0.0)
    updated = old + confidence_delta(relation, strength)
    return max(0.0, min(1.0, round(updated, 4)))


def review_status_after_confidence(new_confidence, *, was_committed=False) -> str:
    if was_committed and new_confidence < COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE:
        return 'under_review'
    return 'stable'


def append_revision_history(metadata, *, old_statement, old_confidence,
                            new_statement, new_confidence, relation,
                            reason, evidence_refs, cycle_id):
    meta = dict(metadata or {})
    history = list(meta.get('revision_history') or [])
    history.append({
        'old_statement': old_statement,
        'old_confidence': old_confidence,
        'new_statement': new_statement,
        'new_confidence': new_confidence,
        'evidence_relation': relation,
        'revision_reason': reason,
        'evidence_refs': list(evidence_refs or []),
        'cycle_id': cycle_id,
        'superseded': relation in {'contradiction', 'scope_limiter'},
    })
    meta['revision_history'] = history[-20:]
    return meta


def current_belief_display(belief) -> str:
    """Prompt injection shows the live cognition, not simultaneous old+new facts."""
    statement = str((belief or {}).get('statement') or '').strip()
    meta = belief.get('metadata') if isinstance(belief, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    review = meta.get('review_status') or 'stable'
    history = meta.get('revision_history') or []
    if review == 'under_review' and history:
        previous = history[-1].get('old_statement') or ''
        if previous and previous != statement:
            return (
                f'过去曾判断「{previous}」；近期证据使其正在被重新评估，'
                f'当前更准确的说法是「{statement}」。'
            )
        return f'{statement}（该认识正在被新证据重新评估，还不是稳定事实）'
    return statement


def is_stable_reader_belief(belief) -> bool:
    if not isinstance(belief, dict):
        return False
    if belief.get('status') not in (None, 'active'):
        return False
    meta = belief.get('metadata') if isinstance(belief.get('metadata'), dict) else {}
    review = meta.get('review_status') or 'stable'
    if review in {'under_review', 'reopened'}:
        return False
    return float(belief.get('confidence') or 0) >= 0.65


def infer_hypothesis_relation(supporting_refs, contradicting_refs) -> str:
    support_n = len(supporting_refs or [])
    contra_n = len(contradicting_refs or [])
    if contra_n and not support_n:
        return 'contradiction'
    if support_n and not contra_n:
        return 'support'
    if contra_n > support_n:
        return 'contradiction'
    if support_n:
        return 'support'
    return 'irrelevant'


SHARED_FRAME_KEY = SHARED_RELATIONSHIP_FRAME_KEY


# This is a deliberately closed grammar of literal self-reports, not a
# sentiment/sarcasm classifier. Quoted objects extend the vocabulary without
# allowing arbitrary trailing prose to be interpreted as part of an object.
_REPORT = re.compile(
    r'(?:在(?P<day>\d{4}-\d{2}-\d{2})，)?我'
    r'(?P<value>不喜欢|喜欢|讨厌)'
    r'(?P<object>咖啡|茶|甜食|辣食|独处|聊天|你|「[^「」\n]{1,40}」)[。.!！]?')
_CORRECTION = re.compile(
    r'更正(?:事件「(?P<source>[^「」\n]{1,200})」)?：'
    r'「(?P<old>.+)」不对，应为「(?P<new>.+)」[。.!！]?')


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def parse_explicit_report(text, user_id, character_id):
    """Return only what the speaker explicitly reported, with exact scope."""
    match = _REPORT.fullmatch(str(text or '').strip())
    if not match:
        return None
    day = match['day']
    if day:
        from datetime import date
        try:
            date.fromisoformat(day)
        except ValueError:
            return None
    object_id = match['object'].strip('「」')
    if object_id == '你':
        object_id = 'character:' + character_id
    scope = {
        'subject': 'user:' + user_id, 'predicate': 'reported_preference',
        'object': object_id, 'time_scope': day or 'unspecified',
        'source': 'canonical_user_turn',
    }
    key = 'report.' + _digest(json.dumps(scope, sort_keys=True, ensure_ascii=False))
    return {**scope, 'value': match['value'], 'question_key': key,
            'text': str(text).strip().rstrip('。.!！')}


def parse_cognitive_evidence(text, user_id, character_id):
    if len(str(text or '')) > 2000:
        return {'operation': 'pending', 'reason': 'outside_supported_grammar_size'}
    correction = _CORRECTION.fullmatch(str(text or '').strip())
    if correction:
        old = parse_explicit_report(correction['old'], user_id, character_id)
        new = parse_explicit_report(correction['new'], user_id, character_id)
        if old and new and old['question_key'] == new['question_key']:
            return {'operation': 'correction', 'claim': new, 'old_claim': old,
                    'target_source_id': correction['source']}
        return {'operation': 'pending', 'reason': 'correction_scope_mismatch_or_unsupported'}
    claim = parse_explicit_report(text, user_id, character_id)
    if claim:
        return {'operation': 'report', 'claim': claim}
    return {'operation': 'pending', 'reason': 'unsupported_or_implicit_semantics'}


def evidence_question_key(parsed, source_event_id):
    return (parsed.get('claim') or {}).get('question_key') or (
        'pending.' + _digest(source_event_id))


def deterministic_cycle_output(context):
    """Plan only evidence processing; all state decisions are made under lock."""
    refs = [{'event_id': event['event_id'], 'reason': 'process_source_evidence_once'}
            for event in context.get('events', [])]
    return {
        'cycle_summary': {'summary': '按原始证据核对当前判断。',
                          'salient_change': '', 'uncertainty': '', 'confidence': 'low'},
        'question_updates': [], 'belief_updates': [], 'hypothesis_updates': [],
        'new_predictions': [], 'evidence_refs': refs,
        'reflection_note': {'content': '', 'evidence_refs': []},
        'sticky_note_updates': [], 'diary_entries': [],
    }


def _json(value, fallback):
    return json.loads(value) if isinstance(value, str) else (value if value is not None else fallback)


def sources_current_for_derivation(cur, user_id, character_id, source_ids):
    """Serialize derived writes with revision so a late summary cannot revive it."""
    from cognitive_queue import stable_advisory_lock_key
    cur.execute('SELECT pg_advisory_xact_lock(%s)',
                (stable_advisory_lock_key(user_id, character_id),))
    cur.execute('''SELECT id FROM cognitive_events WHERE user_id=%s AND character_id=%s
                   AND source_event_id=ANY(%s) AND adjudication->>'status'='superseded'
                   LIMIT 1''', (user_id, character_id, list(source_ids)))
    return cur.fetchone() is None


def _invalidate_dependents(cur, user_id, character_id, target_id, event_id, cycle_id, now):
    """Withdraw effective state; preserve statements, evidence and past outcomes."""
    ref = json.dumps([{'event_id': target_id}])
    cur.execute(
        '''WITH RECURSIVE affected AS (
               SELECT b.id, b.belief_key FROM cognitive_beliefs b
               LEFT JOIN cognitive_hypotheses h ON h.id=b.committed_from_hypothesis_id
               WHERE b.user_id=%s AND b.character_id=%s AND
                 (b.evidence_refs @> %s::jsonb OR h.supporting_evidence_refs @> %s::jsonb)
               UNION
               SELECT b.id, b.belief_key FROM cognitive_beliefs b
               LEFT JOIN cognitive_hypotheses h ON h.id=b.committed_from_hypothesis_id
               JOIN affected a
                 ON b.metadata->'basis_belief_keys' @> to_jsonb(ARRAY[a.belief_key])
                 OR h.metadata->'basis_belief_keys' @> to_jsonb(ARRAY[a.belief_key])
               WHERE b.user_id=%s AND b.character_id=%s
           ) SELECT id, belief_key FROM affected''',
        (user_id, character_id, ref, ref, user_id, character_id))
    affected = cur.fetchall()
    keys = [row[1] for row in affected]
    invalidation = {'review_status': 'under_review', 'invalidated_by_event_id': event_id,
                    'review_reason': 'explicit_source_correction',
                    'action_basis_valid': False}
    cur.execute(
        '''UPDATE cognitive_beliefs SET status='retracted',
               metadata=metadata || %s::jsonb, updated_at=%s
           WHERE user_id=%s AND character_id=%s AND id=ANY(%s)''',
        (json.dumps(invalidation), now, user_id, character_id, [r[0] for r in affected]))
    cur.execute(
        '''UPDATE cognitive_hypotheses SET status='rejected',
               metadata=metadata || %s::jsonb, updated_at=%s
           WHERE user_id=%s AND character_id=%s AND (
               supporting_evidence_refs @> %s::jsonb OR
               metadata->'basis_belief_keys' ?| %s OR id IN (
                   SELECT committed_from_hypothesis_id FROM cognitive_beliefs
                   WHERE user_id=%s AND character_id=%s AND belief_key=ANY(%s)))
           RETURNING id, question_id''',
        (json.dumps(invalidation), now, user_id, character_id, ref, keys,
         user_id, character_id, keys))
    hypotheses = cur.fetchall()
    question_ids = list({row[1] for row in hypotheses if row[1] is not None})
    cur.execute(
        '''UPDATE cognitive_questions SET status='active',
               metadata=(metadata - 'resolution' - 'current_judgment') || %s::jsonb ||
                 jsonb_build_object('prior_judgment', metadata->'current_judgment',
                                    'prior_resolution', metadata->'resolution'),
               updated_at=%s
           WHERE user_id=%s AND character_id=%s AND (id=ANY(%s)
               OR metadata->'basis_belief_keys' ?| %s
               OR metadata->'current_judgment'->>'belief_key'=ANY(%s))''',
        (json.dumps(invalidation), now, user_id, character_id, question_ids, keys, keys))
    cur.execute(
        '''UPDATE cognitive_predictions SET status='expired', settled_at=%s,
               last_error_code='basis_retracted',
               metadata=metadata || %s::jsonb || jsonb_build_object(
                   'prior_status', status, 'prior_observed_value', observed_value,
                   'prior_settled_by_event_id', settled_by_event_id)
           WHERE user_id=%s AND character_id=%s AND (
               hypothesis_id=ANY(%s) OR metadata->'basis_belief_keys' ?| %s
               OR metadata->'evidence_refs' @> %s::jsonb)''',
        (now, json.dumps(invalidation), user_id, character_id,
         [row[0] for row in hypotheses], keys, ref))
    cur.execute(
        '''UPDATE cognitive_sticky_notes SET status='archived', updated_at=%s,
               metadata=metadata || %s::jsonb
           WHERE user_id=%s AND character_id=%s AND (
               source_event_refs @> %s::jsonb OR metadata->'basis_belief_keys' ?| %s
               OR metadata->'action_basis'->'basis_belief_keys' ?| %s)''',
        (now, json.dumps(invalidation), user_id, character_id, ref, keys, keys))
    cur.execute(
        '''UPDATE cognitive_cycles SET invalidated_by_event_id=%s
           WHERE user_id=%s AND character_id=%s AND id<>%s AND (
               evidence_refs @> %s::jsonb OR id IN (
                   SELECT created_by_cycle_id FROM cognitive_beliefs
                   WHERE user_id=%s AND character_id=%s AND belief_key=ANY(%s)))''',
        (event_id, user_id, character_id, cycle_id, ref, user_id, character_id, keys))
    cur.execute(
        '''UPDATE cognitive_diary_entries SET invalidated_by_event_id=%s
           WHERE user_id=%s AND character_id=%s AND (source_event_refs @> %s::jsonb
               OR created_by_cycle_id IN (SELECT id FROM cognitive_cycles
                   WHERE user_id=%s AND character_id=%s AND invalidated_by_event_id=%s))''',
        (event_id, user_id, character_id, ref, user_id, character_id, event_id))
    cur.execute('SELECT source_event_id FROM cognitive_events WHERE id=%s', (target_id,))
    raw_ref = json.dumps([cur.fetchone()[0]])
    # These are existing optional context tables, not a parallel memory store.
    for table in ('rolling_summaries', 'pinned_context', 'episodic_memory_index'):
        cur.execute('SELECT to_regclass(%s)', (table,))
        if cur.fetchone()[0] is not None:
            cur.execute(f'''UPDATE {table} SET status='superseded'
                            WHERE user_id=%s AND character_id=%s
                              AND source_event_ids::jsonb @> %s::jsonb''',
                        (user_id, character_id, raw_ref))
    return keys


def apply_rule_evidence(cur, *, cycle_id, user_id, character_id, output, now):
    """The existing cycle transaction's deterministic policy and revision step.

    A caller's proposed judgments are never authority. Reload both the evidence
    and its canonical source; process in source order, exactly once per event.
    """
    from raw_events import get_active_events_by_ids
    from cognitive_predictions import create_prediction, settle_pending_predictions

    ids = [r['event_id'] for r in output['evidence_refs']]
    cur.execute(
        '''SELECT id, source_event_type, source_event_id, payload, occurred_at, adjudication
           FROM cognitive_events WHERE user_id=%s AND character_id=%s AND id=ANY(%s)
           ORDER BY occurred_at, id FOR UPDATE''', (user_id, character_id, ids))
    events = cur.fetchall()
    decisions = []
    for event_id, event_type, source_id, stored, occurred_at, adjudication in events:
        if _json(adjudication, {}).get('processed_by_cycle_id'):
            decisions.append({'event_id': event_id, 'action': 'already_processed'})
            continue
        payload = _json(stored, {})
        decision = {'event_id': event_id, 'processed_by_cycle_id': cycle_id}
        if event_type == 'scheduled_reflection':
            settled = settle_pending_predictions(cur.connection, user_id=user_id,
                character_id=character_id, event_id=event_id, occurred_at=now)
            decision.update(status='applied', action='deadline_check', settled=settled)
        elif event_type != 'canonical_user_turn':
            # Historical model signals, self-claims and extracted labels have
            # no authority, even if their old payload claimed high confidence.
            decision.update(status='pending', action='pending', reason='untrusted_derived_evidence')
        else:
            raw = get_active_events_by_ids(user_id, character_id, [source_id], conn=cur.connection, lock=True)
            if (len(raw) != 1 or raw[0]['role'] != 'user'
                    or raw[0]['content'] != payload.get('content')):
                decision.update(status='pending', action='pending', reason='source_not_active_or_changed')
                parsed = {'operation': 'pending'}
            else:
                parsed = parse_cognitive_evidence(raw[0]['content'], user_id, character_id)
            claim = parsed.get('claim')
            qkey = evidence_question_key(parsed, source_id)
            cur.execute('''SELECT id, metadata FROM cognitive_questions
                           WHERE user_id=%s AND character_id=%s AND question_key=%s FOR UPDATE''',
                        (user_id, character_id, qkey))
            previous = cur.fetchone()
            meta = dict(_json(previous[1], {}) if previous else {})
            old = meta.get('current_judgment') or {}
            if parsed['operation'] == 'correction':
                cur.execute(
                    '''SELECT id, source_event_id, payload FROM cognitive_events
                       WHERE user_id=%s AND character_id=%s
                         AND source_event_type='canonical_user_turn'
                         AND payload->'claim'->>'question_key'=%s
                         AND payload->'claim'->>'text'=%s
                         AND adjudication->>'status'='applied' AND occurred_at<=%s
                         AND (CAST(%s AS text) IS NULL OR source_event_id=%s)
                       ORDER BY occurred_at, id LIMIT 2 FOR UPDATE''',
                    (user_id, character_id, qkey, parsed['old_claim']['text'], occurred_at,
                     parsed['target_source_id'], parsed['target_source_id']))
                targets = cur.fetchall()
                if len(targets) == 1:
                    target_id, target_source, target_payload = targets[0]
                    target_raw = get_active_events_by_ids(user_id, character_id, [target_source], conn=cur.connection, lock=True)
                    target_content = _json(target_payload, {}).get('content')
                    target_claim = parse_cognitive_evidence(target_content, user_id, character_id).get('claim')
                    if (not target_raw or target_raw[0]['role'] != 'user'
                            or target_raw[0]['content'] != target_content
                            or target_claim != parsed['old_claim']):
                        targets = []
                if len(targets) != 1:
                    parsed = {'operation': 'pending', 'reason': 'correction_source_not_unique_or_active'}
                    # An invalid correction gets its own pending issue and
                    # cannot erase a valid answer to a different scoped issue.
                    qkey = evidence_question_key(parsed, source_id)
                    meta, old = {}, {}
                else:
                    withdrawn = _invalidate_dependents(cur, user_id, character_id, target_id, event_id, cycle_id, now)
                    cur.execute('''UPDATE cognitive_events SET adjudication=adjudication || %s::jsonb
                                   WHERE id=%s''',
                                (json.dumps({'status': 'superseded', 'superseded_by_event_id': event_id}), target_id))
                    history = list(meta.get('revision_history') or [])
                    history.append({'old_judgment': old, 'corrected_source_event_id': target_source,
                                    'correction_event_id': event_id, 'withdrawn_beliefs': withdrawn})
                    meta['revision_history'] = history
                    old = {}
                    decision['withdrawn_beliefs'] = withdrawn
            if parsed['operation'] == 'report' and old and old.get('value') != claim['value']:
                parsed = {'operation': 'pending', 'reason': 'conflicting_report_requires_confirmation'}
                cur.execute('''UPDATE cognitive_beliefs SET metadata=metadata || %s::jsonb
                               WHERE user_id=%s AND character_id=%s AND belief_key=%s''',
                            (json.dumps({'review_status': 'under_review'}), user_id, character_id,
                             old.get('belief_key')))
                cur.execute('''UPDATE cognitive_hypotheses SET status='open',
                                   confidence=GREATEST(0, confidence + %s),
                                   contradicting_evidence_refs=contradicting_evidence_refs || %s::jsonb
                               WHERE user_id=%s AND character_id=%s AND question_id=%s''',
                            (confidence_delta('contradiction', 'normal'), json.dumps([{'event_id': event_id}]),
                             user_id, character_id, previous[0]))
            if parsed['operation'] == 'pending':
                if old:
                    meta['prior_judgment'] = old
                meta.update(pending={'event_id': event_id, 'reason': decision.get('reason') or parsed.get('reason')},
                            current_judgment=None)
                question_status = 'active'
                question_text = '待确认原始陈述的含义或纠正范围。'
                decision.update(status='pending', action='pending', reason=meta['pending']['reason'])
            else:
                question_status = 'resolved'
                question_text = f'用户对{claim["object"]}的明确自述（{claim["time_scope"]}）是什么？'
                statement = f'用户明确自述：{claim["text"]}（仅限这次自述，不推断隐含心理）'
                bkey = old.get('belief_key') or qkey + f'.e{event_id}'
                meta.pop('pending', None)
                meta['current_judgment'] = {'value': claim['value'], 'content': statement,
                    'status': 'committed', 'belief_key': bkey, 'evidence_event_ids': [source_id]}
                meta['claim_scope'] = {k: claim[k] for k in ('subject', 'predicate', 'object', 'time_scope', 'source')}
                decision.update(status='applied', action='corrected' if parsed['operation'] == 'correction' else 'reported', belief_key=bkey)
            meta['updated_by'] = 'deterministic_evidence_policy_v1'
            refs = [{'event_id': event_id, 'source_id': source_id}]
            cur.execute(
                '''INSERT INTO cognitive_questions (user_id, character_id, question_key, question_text,
                       status, metadata, source_event_refs, created_by_cycle_id, updated_by_cycle_id, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)
                   ON CONFLICT (user_id,character_id,question_key) DO UPDATE SET
                       status=EXCLUDED.status, metadata=EXCLUDED.metadata,
                       source_event_refs=cognitive_questions.source_event_refs || EXCLUDED.source_event_refs,
                       updated_by_cycle_id=EXCLUDED.updated_by_cycle_id, updated_at=EXCLUDED.updated_at
                   RETURNING id''',
                (user_id, character_id, qkey, question_text, question_status, json.dumps(meta, ensure_ascii=False),
                 json.dumps(refs), cycle_id, cycle_id, now))
            question_id = cur.fetchone()[0]
            if decision['status'] == 'applied' and old:
                cur.execute('''UPDATE cognitive_beliefs SET evidence_refs=evidence_refs || %s::jsonb
                               WHERE user_id=%s AND character_id=%s AND belief_key=%s''',
                            (json.dumps(refs), user_id, character_id, bkey))
            if decision['status'] == 'applied' and not old:
                provenance = {'claim_scope': meta['claim_scope'], 'scope': 'single_event',
                    'question_key': qkey, 'review_status': 'stable',
                    'authority': 'literal_self_report_only', 'basis_belief_keys': []}
                # Confidence is confidence in the witnessed report, never in
                # a hidden feeling. Repeated reports do not increase it.
                cur.execute(
                    '''INSERT INTO cognitive_hypotheses (user_id,character_id,question_id,hypothesis_key,
                           statement,status,hypothesis_type,confidence,supporting_evidence_refs,metadata,
                           created_by_cycle_id,updated_by_cycle_id)
                       VALUES (%s,%s,%s,%s,%s,'supported','user_model',0.8,%s::jsonb,%s::jsonb,%s,%s)
                       RETURNING id''',
                    (user_id, character_id, question_id, bkey, statement, json.dumps(refs),
                     json.dumps(provenance), cycle_id, cycle_id))
                hypothesis_id = cur.fetchone()[0]
                cur.execute(
                    '''INSERT INTO cognitive_beliefs (user_id,character_id,belief_key,statement,
                           confidence,status,belief_type,evidence_refs,committed_from_hypothesis_id,
                           metadata,created_by_cycle_id,updated_by_cycle_id)
                       VALUES (%s,%s,%s,%s,0.8,'active','user_model',%s::jsonb,%s,%s::jsonb,%s,%s)''',
                    (user_id, character_id, bkey, statement, json.dumps(refs), hypothesis_id,
                     json.dumps(provenance), cycle_id, cycle_id))
                from datetime import timedelta
                create_prediction(user_id=user_id, character_id=character_id,
                    prediction_key=bkey + '.next', resolver_name='explicit_report_outcome',
                    fulfillment_operator='>=', fulfillment_value=1,
                    violation_operator='<=', violation_value=-1,
                    expires_at=now + timedelta(days=30), question_id=question_id,
                    hypothesis_id=hypothesis_id, created_by_cycle_id=cycle_id,
                    metadata={'claim_scope': meta['claim_scope'], 'expected_value': claim['value'],
                              'basis_belief_keys': [bkey], 'evidence_refs': refs,
                              'description': '相同范围内下一次明确自述是否一致；不预测隐含心理'},
                    conn=cur.connection)
            if parsed['operation'] == 'report' or decision.get('reason') == 'conflicting_report_requires_confirmation':
                decision['settled'] = settle_pending_predictions(cur.connection, user_id=user_id,
                    character_id=character_id, event_id=event_id, occurred_at=occurred_at)
        cur.execute('''UPDATE cognitive_events SET adjudication=%s::jsonb WHERE id=%s''',
                    (json.dumps(decision, ensure_ascii=False, default=str), event_id))
        # Prediction outcomes already handled in this transaction must not
        # schedule another empty pass over the very same evidence.
        cur.execute('''UPDATE cognitive_event_triggers SET status='suppressed',
                           slow_cycle_suppressed_reason='processed_in_current_cycle', suppressed_at=%s
                       WHERE event_id=%s AND user_id=%s AND character_id=%s AND status='pending'
                         AND trigger_class IN ('prediction_error','prediction_confirmation')''',
                    (now, event_id, user_id, character_id))
        decisions.append(decision)
    applied = sum(d.get('action') in {'reported', 'corrected'} for d in decisions)
    pending = sum(d.get('status') == 'pending' for d in decisions)
    output['cycle_summary'] = {
        'summary': f'已处理{applied}条明确自述或纠正；{pending}条证据待确认。',
        'salient_change': '；'.join(f'{d["event_id"]}:{d["action"]}' for d in decisions),
        'uncertainty': '待确认内容不形成有效判断。' if pending else '',
        'confidence': 'high' if applied else 'low',
    }
    return decisions
