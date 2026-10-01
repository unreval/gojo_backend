"""Deterministic Slow Loop revision helpers. No LLM. No rel_state writes."""
import re
import hashlib
import json
from datetime import datetime

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


# A compositional literal grammar, not a sentiment or irony classifier.
# Free-form values must be explicitly delimited. Only the witnessed report is
# committed; reported facts are not independently verified real-world facts.
_DATE_PREFIX = re.compile(r'在(?P<start>\d{4}-\d{2}-\d{2})(?:至(?P<end>\d{4}-\d{2}-\d{2}))?，(?P<body>.+)')
_REPORT = re.compile(r'我(?P<value>不喜欢|喜欢|讨厌)(?P<object>咖啡|茶|甜食|辣食|独处|聊天|你|「[^「」\n]{1,80}」)')
_ATTRIBUTE = re.compile(r'我的(?P<object>名字|职业|居住地|状态|「[^「」\n]{1,40}」)是「(?P<value>[^「」\n]{1,120})」')
_CHOICE = re.compile(r'我(?P<value>拒绝|接受)「(?P<object>[^「」\n]{1,80})」')
_BOUNDARY = re.compile(r'(?P<value>不要|可以)「(?P<object>[^「」\n]{1,80})」')
_PROMISE = re.compile(r'我承诺在(?P<deadline>\d{4}-\d{2}-\d{2})前完成「(?P<object>[^「」\n]{1,80})」')
_FULFILLMENT = re.compile(r'我已兑现截至(?P<deadline>\d{4}-\d{2}-\d{2})的承诺「(?P<object>[^「」\n]{1,80})」')
_CORRECTION = re.compile(
    r'更正(?:事件「(?P<source>[^「」\n]{1,200})」)?：'
    r'「(?P<old>.+)」不对，应为「(?P<new>.+)」[。.!！]?')


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def parse_explicit_report(text, user_id, character_id):
    """Bind a complete literal report to an exact subject, object and scope."""
    from datetime import date
    original = str(text or '').strip().rstrip('。.!！')
    body = original
    time_scope = 'unspecified'
    dated = _DATE_PREFIX.fullmatch(body)
    if dated:
        try:
            first = date.fromisoformat(dated['start'])
            last = date.fromisoformat(dated['end'] or dated['start'])
        except ValueError:
            return None
        if last < first:
            return None
        time_scope = dated['start'] + ('/' + dated['end'] if dated['end'] else '')
        body = dated['body']
    match = _REPORT.fullmatch(body)
    predicate = 'reported_preference'
    value = None
    deadline = None
    if not match:
        match = _ATTRIBUTE.fullmatch(body)
        if match:
            predicate = ('reported_state' if match['object'] == '状态' else
                         'reported_identity' if match['object'] in ('名字', '职业', '居住地')
                         else 'reported_fact')
    if not match:
        match = _CHOICE.fullmatch(body)
        predicate = 'explicit_refusal'
    if not match:
        match = _BOUNDARY.fullmatch(body)
        predicate = 'explicit_boundary'
    if not match:
        match = _PROMISE.fullmatch(body) or _FULFILLMENT.fullmatch(body)
        predicate = 'explicit_promise'
        if match:
            # A deadline already defines the promise's time scope; accepting a
            # second date prefix would leave the verification interval unclear.
            if dated:
                return None
            try:
                date.fromisoformat(match['deadline'])
            except ValueError:
                return None
            deadline = match['deadline']
            time_scope = 'deadline:' + deadline
            value = '承诺' if body.startswith('我承诺') else '已兑现'
    if not match:
        return None
    object_id = match['object'].strip('「」')
    if object_id == '你':
        if character_id == 'shared':
            return None
        object_id = 'character:' + character_id
    if character_id == 'shared' and predicate in ('explicit_boundary', 'explicit_refusal', 'explicit_promise'):
        return None  # An unaddressed group statement cannot invent a recipient.
    scope = {'subject': 'user:' + user_id, 'predicate': predicate,
             'object': object_id, 'time_scope': time_scope, 'source': 'canonical_user_turn'}
    key = 'report.' + _digest(json.dumps(scope, sort_keys=True, ensure_ascii=False))
    result = {**scope, 'value': value or match['value'], 'question_key': key, 'text': original}
    if deadline:
        result['deadline'] = deadline
    return result


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
    if not source_ids:
        return False
    cur.execute("""SELECT COALESCE(NULLIF(event_id, ''), client_msg_id)
                   FROM chat_log WHERE user_id=%s AND chat_id=%s
                   AND COALESCE(status, 'active')='active'
                   AND COALESCE(NULLIF(event_id, ''), client_msg_id)=ANY(%s)
                   FOR SHARE""", (user_id, character_id, list(source_ids)))
    if {row[0] for row in cur.fetchall()} != set(source_ids):
        return False
    from memory_authority import dependencies_current_sql
    cur.execute(f'''SELECT id FROM cognitive_events WHERE user_id=%s AND character_id=%s
                   AND source_event_id=ANY(%s) AND (adjudication->>'status'='superseded'
                     OR EXISTS (SELECT 1 FROM jsonb_each(COALESCE(adjudication->'operations','{{}}')) op
                       WHERE op.value->>'status'='superseded' OR
                         (op.value ? 'dependencies' AND NOT
                          {dependencies_current_sql("op.value->'dependencies'", 'cognitive_events.user_id', 'cognitive_events.character_id')})))
                   LIMIT 1''', (user_id, character_id, list(source_ids)))
    return cur.fetchone() is None


def _invalidate_dependents(cur, user_id, character_id, target_id, event_id, cycle_id, now,
                           operation_id=None):
    """Withdraw effective state; preserve statements, evidence and past outcomes."""
    ref = json.dumps([{'event_id': target_id, **({'operation_id': operation_id} if operation_id else {})}])
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
               OR metadata->'current_judgment'->>'belief_key'=ANY(%s)
               OR (metadata->'current_judgment'->>'event_id'=%s::text
                   AND (%s::text IS NULL OR metadata->'current_judgment'->>'operation_id'=%s)))''',
        (json.dumps(invalidation), now, user_id, character_id, question_ids, keys, keys,
         target_id, operation_id, operation_id))
    cur.execute(
        '''UPDATE cognitive_predictions SET status='expired', settled_at=%s,
               last_error_code='basis_retracted',
               metadata=metadata || %s::jsonb || jsonb_build_object(
                   'prior_status', status, 'prior_observed_value', observed_value,
                   'prior_settled_by_event_id', settled_by_event_id)
           WHERE user_id=%s AND character_id=%s AND (
               hypothesis_id=ANY(%s) OR metadata->'basis_belief_keys' ?| %s
               OR metadata->'evidence_refs' @> %s::jsonb
               OR EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(metadata->'evidence_refs','[]')) r
                   WHERE r->>'event_id'=%s::text AND NOT (r ? 'operation_id')))''',
        (now, json.dumps(invalidation), user_id, character_id,
         [row[0] for row in hypotheses], keys, ref, target_id))
    cur.execute(
        '''UPDATE cognitive_sticky_notes SET status='archived', updated_at=%s,
               metadata=metadata || %s::jsonb
           WHERE user_id=%s AND character_id=%s AND (
               source_event_refs @> %s::jsonb
               OR EXISTS (SELECT 1 FROM jsonb_array_elements(source_event_refs) r
                   WHERE r->>'event_id'=%s::text AND NOT (r ? 'operation_id'))
               OR metadata->'basis_belief_keys' ?| %s
               OR metadata->'action_basis'->'basis_belief_keys' ?| %s)''',
        (now, json.dumps(invalidation), user_id, character_id, ref, target_id, keys, keys))
    cur.execute(
        '''UPDATE cognitive_cycles SET invalidated_by_event_id=%s
           WHERE user_id=%s AND character_id=%s AND id<>%s AND (
               evidence_refs @> %s::jsonb
               OR EXISTS (SELECT 1 FROM jsonb_array_elements(evidence_refs) r
                   WHERE r->>'event_id'=%s::text AND NOT (r ? 'operation_id'))
               OR id IN (
                   SELECT created_by_cycle_id FROM cognitive_beliefs
                   WHERE user_id=%s AND character_id=%s AND belief_key=ANY(%s)))''',
        (event_id, user_id, character_id, cycle_id, ref, target_id, user_id, character_id, keys))
    cur.execute(
        '''UPDATE cognitive_diary_entries SET invalidated_by_event_id=%s
           WHERE user_id=%s AND character_id=%s AND (source_event_refs @> %s::jsonb
               OR EXISTS (SELECT 1 FROM jsonb_array_elements(source_event_refs) r
                   WHERE r->>'event_id'=%s::text AND NOT (r ? 'operation_id'))
               OR created_by_cycle_id IN (SELECT id FROM cognitive_cycles
                   WHERE user_id=%s AND character_id=%s AND invalidated_by_event_id=%s))''',
        (event_id, user_id, character_id, ref, target_id, user_id, character_id, event_id))
    cur.execute('SELECT source_event_id FROM cognitive_events WHERE id=%s', (target_id,))
    raw_ref = json.dumps([cur.fetchone()[0]])
    for table in ('long_memory', 'bond_memory'):
        cur.execute(f"""UPDATE {table} SET recall_status='superseded'
                        WHERE user_id=%s AND character_id=%s
                          AND ((authority_event_id=%s AND (%s::text IS NULL OR authority_operation_id=%s))
                               OR authority_belief_key=ANY(%s))""",
                    (user_id, character_id, target_id, operation_id, operation_id, keys))
    # These are existing optional context tables, not a parallel memory store.
    for table in ('rolling_summaries', 'pinned_context', 'episodic_memory_index'):
        cur.execute('SELECT to_regclass(%s)', (table,))
        if cur.fetchone()[0] is not None:
            cur.execute(f'''UPDATE {table} SET status='superseded'
                            WHERE user_id=%s AND character_id=%s
                              AND source_event_ids::jsonb @> %s::jsonb''',
                        (user_id, character_id, raw_ref))
    return keys


# Explicit exchanges stay in the same adjudicator as literal self-reports.
_NICKNAME = r'(?:宝宝|「[^「」\n]{1,40}」|“[^“”\n]{1,40}”)'
_NICK_QUESTION = re.compile(
    rf'(?:你(?:接受|接不接受)我叫你(?P<a>{_NICKNAME})(?:吗)?'
    rf'|我(?:可以|能)叫你(?P<b>{_NICKNAME})吗)[？?]')
_NICK_ANTECEDENT = re.compile(rf'我(?:还是)?叫你(?P<name>{_NICKNAME})')
_NICK_ANSWER = re.compile(
    rf'(?:我)?(?:明确)?(?P<choice>不接受|接受|不同意|同意)你叫我(?P<name>{_NICKNAME})')
_DEFERRED_ANSWER = re.compile(r'(?:我)?(?:明天|稍后|晚点)(?:再|给你)?回答')
_WITHDRAW_ANSWER = re.compile(
    rf'更正事件「(?P<source>[^「」\n]{{1,200}})」：我刚才回答的不是(?P<name>{_NICKNAME})这个称呼')


def _exchange_clauses(text, separators='。.!！;；\n？?'):
    """Split only outside balanced quotations; unknown syntax remains intact."""
    from structured_output import _skip_quoted_text
    parts, stack, start, index = [], [], 0, 0
    pairs = {'「': '」', '『': '』', '“': '”'}
    while index < len(text):
        char = text[index]
        if char == '"' and not stack:
            end = _skip_quoted_text(text, index)
            if end == len(text) and not text.endswith('"'):
                return [text]
            index = end
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char in separators:
            part = text[start:index + 1].strip()
            if part:
                parts.append(part)
            start = index + 1
        index += 1
    if stack:
        return [text]
    if text[start:].strip():
        parts.append(text[start:].strip())
    return parts or [text]


def _nickname(value):
    return value.strip('「」“”')


def _explicit_answer(text):
    from cognitive_events import _LITERAL_POLARITY
    parts = _exchange_clauses(text.strip().rstrip('。.!！;；'), ',，')
    answers, names = set(), set()
    for part in parts:
        part = part.strip().rstrip(',，').strip().casefold()
        literal = re.fullmatch(r'(?:我)?(?:明确)?(不接受|接受|不是|是|不要|要|不愿意|愿意|不可以|可以|不同意|同意|yes|no)', part)
        token = literal[1] if literal else part
        if token in _LITERAL_POLARITY:
            answers.add(_LITERAL_POLARITY[token])
            continue
        declared = re.fullmatch(r'我明确说了(yes|no)', part)
        match = _NICK_ANSWER.fullmatch(part)
        if declared:
            answers.add(declared[1])
        elif match:
            answers.add(_LITERAL_POLARITY[match['choice']])
            names.add(_nickname(match['name']))
        else:
            return None
    if len(answers) != 1 or len(names) > 1:
        return None
    return {'value': answers.pop(), 'object': next(iter(names), None)}


def _exchange_parts(text):
    parts = _exchange_clauses(text)
    antecedents = [_NICK_ANTECEDENT.fullmatch(p.strip().rstrip('。.!！;；')) for p in parts]
    names = {m['name'] for m in antecedents if m}
    result = []
    for index, part in enumerate(parts):
        body = part.strip().rstrip('。.!！;；')
        question = _NICK_QUESTION.fullmatch(body)
        if question:
            kind, value = 'question', _nickname(question['a'] or question['b'])
        elif body in ('你接不接受这个称呼？', '你接受这个称呼吗？') and len(names) == 1:
            kind, value = 'question', _nickname(next(iter(names)))
        elif antecedents[index]:
            kind, value = 'antecedent', None
        elif _explicit_answer(body):
            kind, value = 'answer', _explicit_answer(body)
        elif _DEFERRED_ANSWER.fullmatch(body):
            kind, value = 'deferred', None
        elif _WITHDRAW_ANSWER.fullmatch(body):
            kind, value = 'withdrawal', _WITHDRAW_ANSWER.fullmatch(body).groupdict()
        else:
            kind, value = 'literal', None
        result.append({'operation_id': 'main' if len(parts) == 1 else f'clause.{index}',
                       'kind': kind, 'value': value, 'text': part})
    return result


def _exchange_source(cur, user_id, character_id, source_id):
    """Read the actual persisted reference; copied transcripts never enter here."""
    cur.execute('''SELECT id,COALESCE(NULLIF(event_id,''),client_msg_id),role,text,
                         created_at::text,reply_to_event_id,extra
                   FROM chat_log WHERE user_id=%s AND chat_id=%s
                     AND COALESCE(NULLIF(event_id,''),client_msg_id)=%s
                     AND COALESCE(status,'active')='active' FOR SHARE''',
                (user_id, character_id, source_id))
    rows = cur.fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    return dict(row_id=row[0], source_id=row[1], raw_role=row[2], content=row[3],
                timestamp=row[4], reply_to_event_id=row[5], metadata=_json(row[6], {}))


def _exchange_role(source):
    return 'user' if source['raw_role'] == 'user' else (
        'character' if source['raw_role'] in ('assistant', 'gojo', 'char', 'character') else None)


def _question_spec(source, part):
    speaker = _exchange_role(source)
    return {'question_key': 'answer.' + _digest(source['source_id'] + ':' + part['operation_id']),
            'question_text': part['text'], 'object': part['value'],
            'speaker': speaker, 'respondent': 'character' if speaker == 'user' else 'user',
            'predicate': 'nickname_permission', 'time_scope': 'this_exchange',
            'dependencies': [dict(source, evidence_role='question')]}


def _answer_candidates(cur, user_id, character_id, source, name=None):
    """An explicit reference or a contiguous bounded question window, never recency alone."""
    ref = source.get('reply_to_event_id') or source['metadata'].get('reply_to_event_id')
    if (source.get('reply_to_event_id') and source['metadata'].get('reply_to_event_id')
            and source['reply_to_event_id'] != source['metadata']['reply_to_event_id']):
        return []
    if ref:
        prior = _exchange_source(cur, user_id, character_id, ref)
        sources = [prior] if prior and (datetime.fromisoformat(prior['timestamp']), prior['row_id']) < (
            datetime.fromisoformat(source['timestamp']), source['row_id']) else []
    else:
        cur.execute('''SELECT COALESCE(NULLIF(event_id,''),client_msg_id) FROM chat_log
            WHERE user_id=%s AND chat_id=%s AND (created_at,id)<(%s,%s)
              AND created_at >= %s::timestamptz - INTERVAL '24 hours'
            ORDER BY created_at DESC,id DESC LIMIT 6''',
            (user_id, character_id, source['timestamp'], source['row_id'], source['timestamp']))
        prior_ids = [row[0] for row in cur.fetchall()]
        sources = [_exchange_source(cur, user_id, character_id, sid) for sid in prior_ids]
    candidates, bridges = [], []
    for prior in sources:
        if not prior:
            break  # A deleted intervening turn must not create a new adjacency.
        parts = _exchange_parts(prior['content'])
        questions = [p for p in parts if p['kind'] == 'question']
        if not questions or any(p['kind'] not in ('question', 'antecedent') for p in parts):
            if not ref and (all(p['kind'] == 'deferred' for p in parts)
                            or prior['content'].strip() in ('答案呢？', '答案呢?', '你的回答呢？')):
                bridges.append(dict(prior, evidence_role='context'))
                continue
            if not name and any(p['text'].endswith(('？','?')) for p in parts):
                return []
            break
        cur.execute('''SELECT payload->>'content' AS content,adjudication->>'status' AS status FROM cognitive_events
            WHERE user_id=%s AND character_id=%s AND source_event_id=%s''',
            (user_id, character_id, prior['source_id']))
        if any(text != prior['content'] or status == 'superseded' for text, status in cur.fetchall()):
            break
        for part in questions:
            spec = _question_spec(prior, part)
            spec['dependencies'].extend(bridges)
            if spec['respondent'] != _exchange_role(source) or (name and spec['object'] != name):
                continue
            cur.execute('''SELECT status FROM cognitive_questions
                WHERE user_id=%s AND character_id=%s AND question_key=%s FOR UPDATE''',
                (user_id, character_id, spec['question_key']))
            old = cur.fetchone()
            if not old or old[0] == 'active' or (old[0] == 'resolved' and (ref or name)):
                candidates.append(spec)
    return candidates


def _apply_exchange_operation(cur, *, cycle_id, user_id, character_id, event_id,
                              source, part, now):
    from memory_authority import project_canonical_memory
    op = part['operation_id']
    decision = {'status': 'pending', 'action': 'pending', 'authority': 'explicit_answer_v1',
                'operation_id': op, 'reason': 'question_not_unique_or_active'}
    if part['kind'] == 'question':
        spec = _question_spec(source, part)
    else:
        answer = part['value'] or {}
        candidates = _answer_candidates(cur, user_id, character_id, source, answer.get('object'))
        if len(candidates) != 1:
            # A complete first-person choice supplies its own object, even without
            # a preceding question. A bad explicit reference never falls back.
            if (not candidates and answer.get('object') and not source.get('reply_to_event_id')
                    and not source['metadata'].get('reply_to_event_id')):
                spec = _question_spec(source, dict(part, value=answer['object']))
                spec.update(speaker='character' if _exchange_role(source) == 'user' else 'user',
                            respondent=_exchange_role(source), question_text='是否接受对方叫自己' + answer['object'],
                            dependencies=[], source_kind='self_contained_choice')
            else:
                return decision
        else:
            spec = candidates[0]
    qkey = spec['question_key']
    cur.execute('''SELECT metadata,source_event_refs,status FROM cognitive_questions
        WHERE user_id=%s AND character_id=%s AND question_key=%s FOR UPDATE''',
        (user_id, character_id, qkey))
    previous = cur.fetchone()
    if part['kind'] == 'deferred' and previous and previous[2] == 'resolved':
        return dict(decision, status='applied', action='late_pending_answer_ignored', question_key=qkey)
    meta = dict(_json(previous[0], {}) if previous else {})
    refs = list(_json(previous[1], []) if previous else [])
    ref = {'event_id': event_id, 'operation_id': op, 'source_id': source['source_id']}
    if ref not in refs:
        refs.append(ref)
    deps = list(spec['dependencies'])
    status = previous[2] if previous else 'active'
    if part['kind'] == 'question':
        # Answer-first processing has already materialized this same question.
        if not previous:
            meta.update(proposition=spec, dependencies=deps)
        decision.update(status='applied', action='question', question_key=qkey, dependencies=deps)
    else:
        deps.append(dict(source, evidence_role='answer'))
        actor = '角色' if spec['respondent'] == 'character' else '用户'
        caller = '用户' if spec['speaker'] == 'user' else '角色'
        scope = f'{actor}被{caller}称作「{spec["object"]}」'
        value = (part['value'] or {}).get('value')
        pending = part['kind'] == 'deferred'
        content = (f'{actor}在该次对话中表示「{part["text"]}」，关于{scope}的问题仍待回答。'
                   if pending else f'{actor}在该次对话中明确答复：{"接受" if value == "yes" else "不接受"}{scope}（仅记录该答复，不推断感情或其他许可）。')
        judgment = dict(value=value, content=content, status='pending' if pending else 'committed',
                        event_id=event_id, operation_id=op, actor=spec['respondent'],
                        evidence_event_ids=list(dict.fromkeys(d['source_id'] for d in deps)))
        history = list(meta.get('lifecycle_history') or [])
        if meta.get('current_judgment') or meta.get('pending_answer'):
            history.append({'current_judgment': meta.get('current_judgment'),
                            'pending_answer': meta.get('pending_answer')})
        meta.update(proposition=spec, dependencies=deps, lifecycle_history=history)
        if pending:
            # No new wall-clock deadline: the raw promise and source timestamp
            # are retained. This grammar does not assert a resolved deadline.
            if status != 'resolved':
                meta['pending_answer'] = judgment
                status = 'active'
        else:
            old = meta.get('current_judgment') or {}
            retired = [old] if old else []
            if (meta.get('pending_answer') or {}).get('status') == 'pending':
                retired.append(meta['pending_answer'])
            for prior in retired:
                _invalidate_dependents(cur, user_id, character_id, prior['event_id'],
                    event_id, cycle_id, now, operation_id=prior['operation_id'])
                cur.execute('''UPDATE cognitive_events SET adjudication=jsonb_set(adjudication,
                    ARRAY['operations',%s], (adjudication->'operations'->%s) || %s::jsonb)
                    WHERE id=%s''', (prior['operation_id'], prior['operation_id'],
                    json.dumps({'status':'superseded','superseded_by_event_id':event_id}), prior['event_id']))
            if meta.get('pending_answer'):
                meta['pending_answer'] = dict(meta['pending_answer'], status='fulfilled')
            meta.update(current_judgment=judgment, resolution=judgment)
            status = 'resolved'
        decision.update(status='applied', action='pending_answer' if pending else 'answered',
                        question_key=qkey, value=value, actor=spec['respondent'],
                        object=spec['object'], dependencies=deps)
        decision.update(project_canonical_memory(cur, user_id=user_id, character_id=character_id,
            event_id=event_id, source_id=source['source_id'], content=content, operation_id=op,
            dependencies=deps, occurred_at=datetime.fromisoformat(source['timestamp'])))
    meta['updated_by'] = 'explicit_answer_v1'
    cur.execute('''INSERT INTO cognitive_questions(user_id,character_id,question_key,question_text,
        status,metadata,source_event_refs,created_by_cycle_id,updated_by_cycle_id,updated_at)
        VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)
        ON CONFLICT (user_id,character_id,question_key) DO UPDATE SET
          status=EXCLUDED.status,metadata=EXCLUDED.metadata,source_event_refs=EXCLUDED.source_event_refs,
          updated_by_cycle_id=EXCLUDED.updated_by_cycle_id,updated_at=EXCLUDED.updated_at''',
        (user_id, character_id, qkey, spec['question_text'], status,
         json.dumps(meta, ensure_ascii=False), json.dumps(refs), cycle_id, cycle_id, now))
    return decision



def _withdraw_answer(cur, *, cycle_id, user_id, character_id, event_id, source, part, now, output):
    """A user's scoped correction can withdraw their own answer, never character speech."""
    target_source = _exchange_source(cur, user_id, character_id, part['value']['source'])
    pending = dict(status='pending', action='pending', reason='correction_source_not_unique_or_owned')
    if (not target_source or _exchange_role(source) != 'user'
            or _exchange_role(target_source) != 'user'
            or (datetime.fromisoformat(target_source['timestamp']), target_source['row_id']) >= (
                datetime.fromisoformat(source['timestamp']), source['row_id'])):
        return pending
    cur.execute('''SELECT id,adjudication FROM cognitive_events WHERE user_id=%s
        AND character_id=%s AND source_event_id=%s AND source_event_type='canonical_user_turn'
        FOR UPDATE''', (user_id, character_id, target_source['source_id']))
    row = cur.fetchone()
    if not row or not _json(row[1], {}).get('processed_by_cycle_id'):
        # A late answer job must not revive an already submitted correction.
        # Materialize the earlier source through this same ingress/adjudicator
        # inside the existing cycle transaction; never fabricate a raw message.
        if not any(p['kind'] == 'answer' for p in _exchange_parts(target_source['content'])):
            return pending
        from cognitive_events import ingest_canonical_turn
        ingress = ingest_canonical_turn(user_id=user_id, character_id=character_id,
            source_event_id=target_source['source_id'], conn=cur.connection, aggregate=False)
        target_id = row[0] if row else ingress['event_id']
        ref = {'event_id': target_id, 'reason': 'canonical_answer_before_scoped_correction'}
        if not any(r['event_id'] == target_id for r in output['evidence_refs']):
            output['evidence_refs'].append(ref)
        apply_rule_evidence(cur, cycle_id=cycle_id, user_id=user_id, character_id=character_id,
                            output={'evidence_refs': [ref]}, now=now)
        cur.execute("""UPDATE cognitive_event_triggers SET status='suppressed',
            slow_cycle_suppressed_reason='processed_in_current_cycle',suppressed_at=%s
            WHERE event_id=%s AND status='pending'""", (now,target_id))
        cur.execute('SELECT id,adjudication FROM cognitive_events WHERE id=%s', (target_id,))
        row = cur.fetchone()
    prior = _json(row[1], {})
    candidates = [(op, value) for op, value in prior.get('operations', {}).items()
                  if value.get('action') == 'answered' and value.get('status') == 'applied'
                  and value.get('object') == _nickname(part['value']['name'])]
    if len(candidates) != 1:
        return pending
    op, value = candidates[0]
    _invalidate_dependents(cur, user_id, character_id, row[0], event_id, cycle_id, now, operation_id=op)
    prior['operations'][op] = dict(value, status='superseded', superseded_by_event_id=event_id)
    cur.execute('UPDATE cognitive_events SET adjudication=%s::jsonb WHERE id=%s',
                (json.dumps(prior, ensure_ascii=False), row[0]))
    return dict(status='applied', action='answer_withdrawn', target_event_id=row[0],
                target_operation_id=op, question_key=value['question_key'])


def _apply_literal_operation(cur, *, cycle_id, user_id, character_id, event_id,
                             source_id, payload, parsed, occurred_at, now, operation_id,
                             decision):
    from raw_events import get_active_events_by_ids
    from cognitive_predictions import create_prediction, settle_pending_predictions
    claim = parsed.get('claim')
    qkey = evidence_question_key(parsed, source_id if operation_id == 'main' else source_id + ':' + operation_id)
    cur.execute('''SELECT id, metadata FROM cognitive_questions
                   WHERE user_id=%s AND character_id=%s AND question_key=%s FOR UPDATE''',
                (user_id, character_id, qkey))
    previous = cur.fetchone()
    meta = dict(_json(previous[1], {}) if previous else {})
    old = meta.get('current_judgment') or (
        meta.get('prior_judgment') if (meta.get('pending') or {}).get('reason') == 'conflicting_report_requires_confirmation' else None) or {}
    if parsed['operation'] == 'correction':
        cur.execute(
            '''SELECT id, source_event_id, payload FROM cognitive_events
               WHERE user_id=%s AND character_id=%s
                 AND source_event_type='canonical_user_turn'
                 AND COALESCE(payload->>'source_chat_id', character_id)=%s
                 AND payload->'claim'->>'question_key'=%s
                 AND payload->'claim'->>'text'=%s
                 AND adjudication->>'status'='applied' AND occurred_at<=%s
                 AND (CAST(%s AS text) IS NULL OR source_event_id=%s)
               ORDER BY occurred_at, id LIMIT 2 FOR UPDATE''',
            (user_id, character_id, payload.get('source_chat_id') or character_id, qkey, parsed['old_claim']['text'], occurred_at,
             parsed['target_source_id'], parsed['target_source_id']))
        targets = cur.fetchall()
        if len(targets) == 1:
            target_id, target_source, target_payload = targets[0]
            target_raw = get_active_events_by_ids(user_id, _json(target_payload, {}).get('source_chat_id') or character_id, [target_source], conn=cur.connection, lock=True)
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
            qkey = evidence_question_key(parsed, source_id if operation_id == 'main' else source_id + ':' + operation_id)
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
    if parsed['operation'] == 'report' and claim['predicate'] == 'explicit_promise' and claim['value'] == '已兑现':
        if old and (old.get('source_chat_id') or character_id) != (payload.get('source_chat_id') or character_id):
            parsed = {'operation': 'pending', 'reason': 'fulfillment_source_scope_mismatch'}
            qkey = evidence_question_key(parsed, source_id if operation_id == 'main' else source_id + ':' + operation_id)
            meta, old = {}, {}
        elif not old or old.get('value') not in {'承诺', '已兑现'}:
            parsed = {'operation': 'pending', 'reason': 'fulfillment_requires_matching_promise'}
        elif old.get('value') == '承诺':
            # Keep the original commitment as historical evidence; its
            # prediction can now settle from this independent report.
            cur.execute("""UPDATE bond_memory SET recall_status='completed'
                           WHERE user_id=%s AND character_id=%s AND authority_belief_key=%s""",
                        (user_id, character_id, old.get('belief_key')))
            meta['prior_commitment'] = old
            old = {}
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
            'status': 'committed', 'belief_key': bkey, 'evidence_event_ids': [source_id],
            'source_chat_id': payload.get('source_chat_id') or character_id}
        meta['claim_scope'] = {k: claim[k] for k in ('subject', 'predicate', 'object', 'time_scope', 'source')}
        decision.update(status='applied', action='corrected' if parsed['operation'] == 'correction' else 'reported', belief_key=bkey)
    meta['updated_by'] = 'deterministic_evidence_policy_v1'
    refs = [{'event_id': event_id, 'source_id': source_id, 'operation_id': operation_id}]
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
        if claim['predicate'] == 'reported_preference' or (claim['predicate'] == 'explicit_promise' and claim['value'] == '承诺'):
            from datetime import datetime, timedelta, timezone
            is_promise = claim['predicate'] == 'explicit_promise'
            deadline = (datetime.fromisoformat(claim['deadline']).replace(tzinfo=timezone(timedelta(hours=8)))
                        + timedelta(days=1)) if is_promise else now + timedelta(days=30)
            create_prediction(user_id=user_id, character_id=character_id,
                prediction_key=bkey + '.next', resolver_name='explicit_report_outcome',
                fulfillment_operator='>=', fulfillment_value=1,
                violation_operator='<=', violation_value=-1,
                expires_at=deadline, question_id=question_id,
                hypothesis_id=hypothesis_id, created_by_cycle_id=cycle_id,
                metadata={'claim_scope': meta['claim_scope'], 'expected_value': '已兑现' if is_promise else claim['value'],
                          'source_chat_id': payload.get('source_chat_id') or character_id,
                          'basis_belief_keys': [bkey], 'evidence_refs': refs,
                          'description': '承诺在截止日期前是否有明确兑现报告；不证明现实完成' if is_promise else '相同范围内下一次明确自述是否一致；不预测隐含心理'},
                conn=cur.connection)
    if decision['status'] == 'applied':
        from memory_authority import project_canonical_memory
        decision.update(project_canonical_memory(cur, user_id=user_id, character_id=character_id,
            event_id=event_id, source_id=source_id, content=statement, claim=claim,
            belief_key=bkey, occurred_at=occurred_at, operation_id=operation_id))
    if parsed['operation'] == 'report' or decision.get('reason') == 'conflicting_report_requires_confirmation':
        decision['settled'] = settle_pending_predictions(cur.connection, user_id=user_id,
            character_id=character_id, event_id=event_id, occurred_at=occurred_at)
    return decision


def apply_rule_evidence(cur, *, cycle_id, user_id, character_id, output, now):
    """Reload and lock canonical sources; adjudicate each bounded operation once."""
    from raw_events import get_active_events_by_ids, cognitive_source_matches_scope
    from cognitive_predictions import settle_pending_predictions
    from memory_authority import project_canonical_memory

    ids = [r['event_id'] for r in output['evidence_refs']]
    cur.execute(
        """SELECT id, source_event_type, source_event_id, payload, occurred_at, adjudication
           FROM cognitive_events WHERE user_id=%s AND character_id=%s AND id=ANY(%s)
           ORDER BY occurred_at, id FOR UPDATE""", (user_id, character_id, ids))
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
        elif event_type not in ('canonical_user_turn', 'canonical_assistant_turn'):
            decision.update(status='pending', action='pending', reason='untrusted_derived_evidence')
        else:
            chat_id = payload.get('source_chat_id') or character_id
            raw = get_active_events_by_ids(user_id, chat_id, [source_id], conn=cur.connection, lock=True)
            role = 'user' if event_type == 'canonical_user_turn' else 'assistant'
            if (len(raw) != 1 or raw[0]['role'] != role
                    or raw[0]['content'] != payload.get('content')
                    or not cognitive_source_matches_scope(raw[0], character_id, chat_id)):
                decision.update(status='pending', action='pending', reason='source_not_active_or_changed')
            else:
                parts = _exchange_parts(raw[0]['content']) if chat_id == character_id else []
                supported = parts and any(p['kind'] != 'antecedent' for p in parts) and all(p['kind'] != 'literal' or
                    parse_cognitive_evidence(p['text'], user_id, character_id)['operation'] != 'pending'
                    for p in parts)
                if not supported:
                    parts = [dict(kind='literal', text=raw[0]['content'], operation_id='main')]
                source = (_exchange_source(cur, user_id, character_id, source_id)
                          if any(p['kind'] not in ('literal', 'antecedent') for p in parts) else None)
                operations = {}
                if role == 'assistant':
                    quote_op = 'utterance' if supported else 'main'
                    content = '角色实际说过：' + json.dumps(raw[0]['content'], ensure_ascii=False) + '（仅为话语记录，不证明话中内容或关系）'
                    operations[quote_op] = dict(status='applied', action='quoted',
                        **project_canonical_memory(cur, user_id=user_id, character_id=character_id,
                            event_id=event_id, source_id=source_id, content=content,
                            occurred_at=occurred_at, operation_id=quote_op))
                for part in parts:
                    op = part['operation_id']
                    if part['kind'] in ('question', 'answer', 'deferred'):
                        operations[op] = _apply_exchange_operation(cur, cycle_id=cycle_id,
                            user_id=user_id, character_id=character_id, event_id=event_id,
                            source=source, part=part, now=now)
                    elif part['kind'] == 'withdrawal':
                        operations[op] = _withdraw_answer(cur, cycle_id=cycle_id,
                            user_id=user_id, character_id=character_id, event_id=event_id,
                            source=source, part=part, now=now, output=output)
                    elif part['kind'] == 'literal' and role == 'user':
                        operations[op] = _apply_literal_operation(cur, cycle_id=cycle_id,
                            user_id=user_id, character_id=character_id, event_id=event_id,
                            source_id=source_id, payload=payload,
                            parsed=parse_cognitive_evidence(part['text'], user_id, character_id),
                            occurred_at=occurred_at, now=now, operation_id=op, decision={})
                main = operations.get('main') or operations.get('utterance') or next(iter(operations.values()))
                decision.update(main)
                decision.update(operations=operations,
                    status='applied' if any(op['status'] == 'applied' for op in operations.values()) else 'pending')
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
    operations = [op for d in decisions for op in (d.get('operations') or {'main':d}).values()]
    applied = sum(d.get('action') in {'reported', 'corrected', 'answered', 'answer_withdrawn'} for d in operations)
    pending = sum(d.get('status') == 'pending' or d.get('action') == 'pending_answer' for d in operations)
    output['cycle_summary'] = {
        'summary': f'已处理{applied}条明确自述、答复或纠正；{pending}条证据待确认。',
        'salient_change': '；'.join(f'{d["event_id"]}:{d["action"]}' for d in decisions),
        'uncertainty': '待确认内容不形成有效判断。' if pending else '',
        'confidence': 'high' if applied else 'low',
    }
    return decisions
