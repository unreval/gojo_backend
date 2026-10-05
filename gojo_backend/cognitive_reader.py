"""Read-only bridge from durable Slow Loop state into character context."""
import json
import re
from datetime import datetime, timezone

from cognitive_config import (
    COGNITIVE_MAX_STICKY_NOTES_IN_CONTEXT,
    SHARED_RELATIONSHIP_FRAME_KEY,
    USER_FACING_STICKY_SOURCE,
)
from cognitive_output import sticky_emotion_tag
from cognitive_revision import current_belief_display, is_stable_reader_belief
from memory_authority import current_literal_belief_sql, current_answer_question_sql, answer_question_is_current
from relationship_semantics import (
    ENGAGEMENT_STYLE_KEY,
    INTERNAL_CONFLICT_KEY,
    ROMANTIC_LABEL_KEY,
    ROMANTIC_OPENNESS_KEY,
    is_nonrelationship_generated_source,
    normalize_romantic_label,
)
from role_view import PROJECTION_VERSION, render_role_view


def _json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback
    return value


def _projected_text(content, metadata, observer_id):
    metadata = metadata if isinstance(metadata, dict) else {}
    semantic = (metadata.get('semantic_payload')
                if metadata.get('projection_version') == PROJECTION_VERSION else None)
    return render_role_view(content, observer_id=observer_id,
                            source_character_id=observer_id, semantic=semantic)


def _json_list(value):
    parsed = _json_value(value, [])
    return parsed if isinstance(parsed, list) else []


def _safe_text(value, maximum):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text[:maximum]


def _source_event_ids_from_refs(user_id, character_id, refs, *, conn):
    """Resolve Cognitive event references back to their canonical Raw Events.

    Relationship panels fail closed when this provenance bridge is unavailable.
    A Cognitive row alone is not proof that its source is still active.
    """
    raw_ids = []
    cognitive_ids = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        source_id = str(ref.get('source_id') or '').strip()
        if source_id:
            raw_ids.append(source_id.removeprefix('memory_job:').removeprefix('raw_event:'))
            continue
        event_id = ref.get('event_id')
        if isinstance(event_id, int) or (isinstance(event_id, str) and event_id.isdigit()):
            cognitive_ids.append(int(event_id))
    if cognitive_ids:
        cur = conn.cursor()
        try:
            cur.execute(
                '''SELECT id, source_event_id
                   FROM cognitive_events
                   WHERE user_id = %s AND character_id = %s AND id = ANY(%s)''',
                (user_id, character_id, list(dict.fromkeys(cognitive_ids))),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
        found = {int(row[0]): str(row[1] or '').strip() for row in rows}
        if set(cognitive_ids) != set(found):
            return ()
        raw_ids.extend(found[event_id] for event_id in cognitive_ids)
    return tuple(dict.fromkeys(item for item in raw_ids if item))


def _active_relationship_source_ids(user_id, character_id, *, refs=(), raw_ids=(), conn):
    """Return source ids only when all are active and non-generated evidence."""
    ids = list(raw_ids or ())
    if not ids:
        try:
            ids = list(_source_event_ids_from_refs(
                user_id, character_id, refs, conn=conn))
        except Exception:
            return ()
    ids = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))
    if not ids or any(is_nonrelationship_generated_source(item) for item in ids):
        return ()
    try:
        import raw_events
        if not raw_events.sources_are_active(ids, user_id, character_id, conn=conn):
            return ()
    except Exception:
        return ()
    return tuple(ids)


def _relationship_item(kind, row, *, source_event_ids, value=None):
    return {
        'kind': kind,
        'status': row.get('status'),
        'statement': row.get('statement') or row.get('content') or '',
        'content': row.get('content') or row.get('statement') or '',
        'value': value,
        'confidence': row.get('confidence'),
        'source_event_ids': tuple(source_event_ids),
        'updated_at': row.get('updated_at'),
    }


def read_relationship_semantic_state(user_id, character_id, *, conn=None):
    """Read only the canonical Slow Loop items used by ``relationship_panel``.

    This is a narrow reader, not a relationship judge: item lifecycles,
    confidence, and semantic values are all decided by existing Cognitive
    writers.  Every item is dropped if its linked Raw Event is unavailable.
    """
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    result = {
        'engagement_style': None,
        'romantic_label': None,
        'romantic_openness': None,
        'internal_conflict': None,
    }
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT question_key, question_text, status, metadata,
                      source_event_refs, updated_at
               FROM cognitive_questions
               WHERE user_id = %s AND character_id = %s AND question_key = %s
               LIMIT 1''',
            (user_id, character_id, ROMANTIC_LABEL_KEY),
        )
        question = cur.fetchone()
        if question:
            metadata = _json_value(question[3], {})
            metadata = metadata if isinstance(metadata, dict) else {}
            resolution = metadata.get('resolution') or {}
            judgment = metadata.get('current_judgment') or {}
            candidate = resolution if question[2] == 'resolved' and resolution else judgment
            if metadata.get('updated_by') == 'explicit_answer_v1' and not answer_question_is_current(
                    user_id, character_id,
                    {'question_key': question[0], 'status': question[2], 'metadata': metadata},
                    conn=database):
                candidate = None
            if isinstance(candidate, dict):
                value = normalize_romantic_label(candidate.get('value'))
                raw_ids = candidate.get('evidence_event_ids') or ()
                refs = _json_list(candidate.get('evidence_refs')) or _json_list(question[4])
                source_ids = _active_relationship_source_ids(
                    user_id, character_id, refs=refs, raw_ids=raw_ids, conn=database,
                )
                if value and source_ids:
                    result['romantic_label'] = _relationship_item(
                        'question',
                        {
                            'status': question[2],
                            'content': _projected_text(candidate.get('content') or question[1],
                                                       candidate, character_id),
                            'updated_at': question[5],
                        },
                        source_event_ids=source_ids,
                        value=value,
                    )

        cur.execute(
            '''SELECT belief_key, statement, confidence, belief_type, status,
                      evidence_refs, metadata, updated_at
               FROM cognitive_beliefs
               WHERE user_id = %s AND character_id = %s
                 AND status = 'active' AND belief_key = ANY(%s)''',
            (user_id, character_id, [ENGAGEMENT_STYLE_KEY, INTERNAL_CONFLICT_KEY]),
        )
        for row in cur.fetchall():
            key = row[0]
            belief = {
                'belief_key': key,
                'statement': row[1],
                'confidence': float(row[2] or 0),
                'belief_type': row[3],
                'status': row[4],
                'evidence_refs': _json_list(row[5]),
                'metadata': _json_value(row[6], {}),
                'updated_at': row[7],
            }
            if not is_stable_reader_belief(belief):
                continue
            source_ids = _active_relationship_source_ids(
                user_id, character_id, refs=belief['evidence_refs'], conn=database,
            )
            if not source_ids:
                continue
            if key == ENGAGEMENT_STYLE_KEY and belief['belief_type'] == 'relationship_observation':
                result['engagement_style'] = _relationship_item(
                    'belief', belief, source_event_ids=source_ids,
                )
            elif key == INTERNAL_CONFLICT_KEY:
                result['internal_conflict'] = _relationship_item(
                    'belief', belief, source_event_ids=source_ids,
                )

        cur.execute(
            '''SELECT hypothesis_key, statement, status, hypothesis_type,
                      confidence, supporting_evidence_refs,
                      contradicting_evidence_refs, updated_at
               FROM cognitive_hypotheses
               WHERE user_id = %s AND character_id = %s
                 AND status IN ('open', 'supported')
                 AND hypothesis_key = ANY(%s)''',
            (
                user_id, character_id,
                [ENGAGEMENT_STYLE_KEY, ROMANTIC_OPENNESS_KEY, INTERNAL_CONFLICT_KEY],
            ),
        )
        for row in cur.fetchall():
            key = row[0]
            hypothesis = {
                'hypothesis_key': key,
                'statement': row[1],
                'status': row[2],
                'hypothesis_type': row[3],
                'confidence': float(row[4] or 0),
                'evidence_refs': (
                    _json_list(row[5]) + _json_list(row[6])
                ),
                'updated_at': row[7],
            }
            source_ids = _active_relationship_source_ids(
                user_id, character_id, refs=hypothesis['evidence_refs'], conn=database,
            )
            if not source_ids:
                continue
            item = _relationship_item('hypothesis', hypothesis, source_event_ids=source_ids)
            if key == ENGAGEMENT_STYLE_KEY and hypothesis['hypothesis_type'] == 'relationship':
                if result['engagement_style'] is None:
                    result['engagement_style'] = item
            elif key == ROMANTIC_OPENNESS_KEY and hypothesis['hypothesis_type'] == 'relationship':
                result['romantic_openness'] = item
            elif key == INTERNAL_CONFLICT_KEY and result['internal_conflict'] is None:
                result['internal_conflict'] = item
        return result
    finally:
        cur.close()
        if owns_connection:
            database.close()


def fetch_cognitive_reader_state(user_id, character_id, *, conn=None):
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT cycle_summary, reflection_note, completed_at
               FROM cognitive_cycles
               WHERE user_id = %s AND character_id = %s
                 AND status = 'succeeded' AND cycle_summary IS NOT NULL
                 AND invalidated_by_event_id IS NULL
               ORDER BY completed_at DESC, id DESC
               LIMIT 1''',
            (user_id, character_id),
        )
        cycle_row = cur.fetchone()

        cur.execute(
            f'''SELECT question_key, question_text, status, updated_at,
                      metadata, source_event_refs
               FROM cognitive_questions
               WHERE user_id = %s AND character_id = %s
                 AND (status IN ('active', 'dormant')
                      OR (status = 'resolved' AND
                          (metadata ? 'resolution' OR metadata ? 'current_judgment')))
                 AND (metadata->>'updated_by' IS DISTINCT FROM 'deterministic_evidence_policy_v1'
                      OR status <> 'resolved'
                      OR EXISTS (SELECT 1 FROM cognitive_beliefs b
                          WHERE b.user_id=cognitive_questions.user_id
                            AND b.character_id=cognitive_questions.character_id
                            AND b.belief_key=cognitive_questions.metadata->'current_judgment'->>'belief_key'
                            AND b.status='active' AND {current_literal_belief_sql('b')}))
                 AND (metadata->>'updated_by' IS DISTINCT FROM 'explicit_answer_v1'
                      OR {current_answer_question_sql()})
               ORDER BY updated_at DESC, id DESC
               LIMIT 6''',
            (user_id, character_id),
        )
        questions = [
            {
                'question_key': row[0],
                'question_text': row[1],
                'status': row[2],
                'updated_at': row[3],
                'metadata': _json_value(row[4] if len(row) > 4 else {}, {}),
                'source_event_refs': _json_value(row[5] if len(row) > 5 else [], []),
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            f'''SELECT belief_key, statement, confidence, belief_type,
                      updated_at, metadata, evidence_refs
               FROM cognitive_beliefs
               WHERE user_id = %s AND character_id = %s
                 AND status = 'active'
                 AND (metadata->>'authority' IS DISTINCT FROM 'literal_self_report_only'
                      OR {current_literal_belief_sql('cognitive_beliefs')})
               ORDER BY confidence DESC, updated_at DESC, id DESC
               LIMIT 12''',
            (user_id, character_id),
        )
        beliefs = [
            {
                'belief_key': row[0],
                'statement': row[1],
                'confidence': float(row[2]),
                'belief_type': row[3],
                'updated_at': row[4],
                'metadata': _json_value(row[5] if len(row) > 5 else {}, {}),
                'status': 'active',
                'evidence_refs': _json_value(row[6] if len(row) > 6 else [], []),
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            f'''SELECT hypothesis_key, statement, status, hypothesis_type,
                       confidence, updated_at, supporting_evidence_refs, metadata
               FROM cognitive_hypotheses
               WHERE user_id = %s AND character_id = %s
                 AND status IN ('open', 'supported')
                 AND (metadata->>'authority' IS DISTINCT FROM 'literal_self_report_only'
                      OR EXISTS (SELECT 1 FROM cognitive_beliefs b
                          WHERE b.user_id=cognitive_hypotheses.user_id
                            AND b.character_id=cognitive_hypotheses.character_id
                            AND b.belief_key=cognitive_hypotheses.hypothesis_key
                            AND b.status='active' AND {current_literal_belief_sql('b')}))
               ORDER BY updated_at DESC, id DESC
               LIMIT 6''',
            (user_id, character_id),
        )
        hypotheses = [
            {
                'hypothesis_key': row[0],
                'statement': row[1],
                'status': row[2],
                'hypothesis_type': row[3],
                'confidence': float(row[4]),
                'updated_at': row[5],
                'evidence_refs': _json_value(row[6] if len(row) > 6 else [], []),
                'metadata': _json_value(row[7] if len(row) > 7 else {}, {}),
            }
            for row in cur.fetchall()
        ]

        sticky_notes = list_user_facing_sticky_notes(
            user_id, character_id,
            limit=COGNITIVE_MAX_STICKY_NOTES_IN_CONTEXT, conn=database)

        cur.execute(
            '''SELECT prediction_key, metadata, expires_at
               FROM cognitive_predictions
               WHERE user_id=%s AND character_id=%s AND status='pending'
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
               ORDER BY created_at DESC, id DESC LIMIT 4''',
            (user_id, character_id),
        )
        predictions = [
            {'prediction_key': row[0], 'metadata': _json_value(row[1], {}),
             'expires_at': row[2], 'status': 'pending'}
            for row in cur.fetchall()
        ]
        summary = _json_value(cycle_row[0], {}) if cycle_row else {}
        reflection_note = _json_value(cycle_row[1], {}) if cycle_row else {}
        return {
            'cycle_summary': summary if isinstance(summary, dict) else {},
            'reflection_note': (
                reflection_note if isinstance(reflection_note, dict) else {}
            ),
            'completed_at': cycle_row[2] if cycle_row else None,
            'questions': questions,
            'beliefs': beliefs,
            'hypotheses': hypotheses,
            'sticky_notes': sticky_notes,
            'predictions': predictions,
        }
    finally:
        cur.close()
        if owns_connection:
            database.close()


def fetch_shared_frame(user_id, character_id, *, conn=None):
    """Read the revisable shared relationship frame. Fast Loop may use this."""
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT statement, confidence, metadata, evidence_refs
               FROM cognitive_beliefs
               WHERE user_id = %s AND character_id = %s
                 AND belief_key = %s AND status = 'active'
               ORDER BY updated_at DESC, id DESC
               LIMIT 1''',
            (user_id, character_id, SHARED_RELATIONSHIP_FRAME_KEY),
        )
        row = cur.fetchone()
        if not row:
            return {'frame_kind': 'unknown', 'confidence': 0.0}
        if not _active_relationship_source_ids(
                user_id, character_id, refs=_json_list(row[3]) if len(row) > 3 else (),
                conn=database):
            return {'frame_kind': 'unknown', 'confidence': 0.0}
        meta = _json_value(row[2], {})
        review = meta.get('review_status') or 'stable'
        confidence = float(row[1] or 0)
        lock_confidence = (
            min(confidence, 0.69)
            if review in {'under_review', 'reopened'} else confidence
        )
        return {
            'frame_kind': meta.get('frame_kind') or 'unknown',
            'confidence': lock_confidence,
            'raw_confidence': confidence,
            'statement': row[0],
            'review_status': review,
        }
    finally:
        cur.close()
        if owns_connection:
            database.close()


def build_cognitive_prompt_context(user_id, character_id, *, conn=None, query=None):
    """Format current cognition only; model reasoning and predictions stay private."""
    state = fetch_cognitive_reader_state(
        user_id, character_id, conn=conn,
    )
    items = iter_active_cognitive_items(
        user_id, character_id, conn=conn, query=query or '', _state=state,
    )
    if not items:
        return ''
    return '【当前认知——结论只表达，未决保持未决】\n' + '\n'.join(
        row['text'] for row in items)


def iter_active_cognitive_items(user_id, character_id, *, conn=None,
                                query='', now=None, _state=None):
    """Select the current, relevant working set from the existing Slow Loop.

    No second judgment engine: confidence/revision/lifecycle were decided by
    the writers. This reader only validates, matches the query, and deduplicates.
    """
    state = _state if _state is not None else fetch_cognitive_reader_state(
        user_id, character_id, conn=conn)
    items = []
    seen = set()
    now = now or datetime.now(timezone.utc)
    from context_layer import _summary_recall_terms
    query_terms = _summary_recall_terms(query)

    def add(kind, text, row, critical_kind=None, *, related_text='', raw_ids=(),
            authority_priority=0):
        text = _safe_text(text, 520)
        if not text:
            return
        relevance = len(query_terms & _summary_recall_terms(text + ' ' + related_text))
        if query_terms and not relevance:
            return
        expires = row.get('expires_at')
        if expires:
            if isinstance(expires, str):
                expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            if expires.replace(tzinfo=expires.tzinfo or timezone.utc) <= now:
                return
        raw_ids = list(raw_ids)
        refs = [] if raw_ids else (row.get('source_event_refs') or row.get('evidence_refs') or [])
        for ref in refs:
            if isinstance(ref, dict) and (ref.get('source') or ref.get('source_type')) in (
                    'memory_extraction', 'raw_event', 'chat_log', 'relationship_engine_v4'):
                source_id = str(ref.get('source_id') or '')
                source_id = source_id.removeprefix('memory_job:').removeprefix('raw_event:')
                if source_id:
                    raw_ids.append(source_id)
        if refs and not raw_ids:
            source_db = conn
            owns_source_db = source_db is None
            try:
                if source_db is None:
                    from db import get_conn
                    source_db = get_conn()
                raw_ids.extend(_source_event_ids_from_refs(
                    user_id, character_id, refs, conn=source_db))
            except Exception:
                return
            finally:
                if owns_source_db and source_db is not None:
                    source_db.close()
        raw_ids = list(dict.fromkeys(str(value) for value in raw_ids if value))
        if not raw_ids:
            return
        if raw_ids:
            try:
                from raw_events import get_active_events_by_ids
                active = get_active_events_by_ids(user_id, character_id, raw_ids, conn=conn)
                if {str(row.get('event_id')) for row in active} != set(raw_ids):
                    print('[cognitive_context] candidate_dropped reason=inactive_source')
                    return
            except Exception:
                print('[cognitive_context] candidate_dropped reason=source_validity_unavailable')
                return
        identity = (kind, row.get('belief_key') or row.get('question_key')
                    or row.get('hypothesis_key') or row.get('note_key')
                    or row.get('prediction_key') or row.get('id')
                    or tuple(raw_ids))
        if identity[1] and identity in seen:
            return
        if identity[1]:
            seen.add(identity)
        text = render_role_view(
            text, observer_id=character_id, source_character_id=character_id)
        items.append({
            'kind': kind, 'text': text, 'source_event_ids': tuple(raw_ids),
            'subjective': True, 'critical_kind': critical_kind,
            'relevance': relevance, 'authority_priority': authority_priority,
        })
        return True

    for belief in state.get('beliefs') or []:
        if not is_stable_reader_belief(belief):
            continue
        add('cognitive_judgment',
            '当前已形成的判断（只表达，不现场重判）：' +
            _projected_text(current_belief_display(belief), belief.get('metadata'), character_id),
            belief, 'judgment')
    for question in state.get('questions') or []:
        meta = question.get('metadata') or {}
        if meta.get('updated_by') == 'explicit_answer_v1' and not answer_question_is_current(
                user_id, character_id, question, conn=conn):
            continue
        resolution = meta.get('resolution') or {}
        text = _safe_text(question.get('question_text'), 420)
        pending = meta.get('pending_answer') or {}
        followup_text = ''
        if pending.get('status') == 'pending':
            # A short "答案呢" refers to the pending answer without repeating
            # the original question. Match only this same question's promise.
            followup_text = _projected_text(pending.get('content'), pending, character_id) + ' 待回答的问题答案'
        if question.get('status') == 'resolved' and resolution:
            add('cognitive_judgment',
                '当前已明确的结论：' + _projected_text(
                    resolution.get('content') or resolution.get('value'), resolution, character_id),
                question, 'judgment', related_text=text,
                raw_ids=resolution.get('evidence_event_ids') or (), authority_priority=2)
            continue
        if question.get('status') not in ('active', 'resolved'):
            continue
        judgment = meta.get('current_judgment') or {}
        judgment_added = False
        if judgment.get('status') in ('current', 'committed') and judgment.get('content'):
            judgment_added = add('cognitive_judgment',
                '当前问题的已形成判断（READ → EXPRESS）：' +
                _projected_text(judgment['content'], judgment, character_id),
                question, 'judgment', related_text=text + ' ' + followup_text,
                raw_ids=judgment.get('evidence_event_ids') or (), authority_priority=1)
        if not judgment_added and question.get('status') == 'active':
            add('cognitive_question', f'未解决问题（unresolved，不能现场决定 yes/no）：{text}',
                question, 'question', related_text=followup_text)
        if pending.get('status') == 'pending':
            add('cognitive_promise', '待履行的明确承诺：' + _projected_text(
                pending.get('content') or text, pending, character_id),
                question, 'promise', related_text=text + ' ' + followup_text,
                raw_ids=pending.get('evidence_event_ids') or ())
    for hypothesis in state.get('hypotheses') or []:
        if hypothesis.get('status') not in ('open', 'supported'):
            continue
        text = _safe_text(_projected_text(
            hypothesis.get('statement'), hypothesis.get('metadata'), character_id), 420)
        if text:
            add('cognitive_hypothesis', f'进行中的假设（主观，待验证）：{text}', hypothesis)
    for note in state.get('sticky_notes') or []:
        if note.get('status') != 'active':
            continue
        text = _safe_text(note.get('content'), 300)
        if text:
            add('cognitive_sticky', f'近期备忘：{text}', note, 'sticky')
    for prediction in state.get('predictions') or []:
        if prediction.get('status') != 'pending':
            continue
        meta = prediction.get('metadata') or {}
        text = meta.get('statement') or meta.get('description')
        if text:
            add('cognitive_prediction', f'待验证预测（不是事实）：{text}',
                {**prediction, 'evidence_refs': meta.get('evidence_refs') or []}, 'prediction')
    ranked = sorted(items, key=lambda row: (
        -row['authority_priority'], -row['relevance']))
    counts, selected = {}, []
    for row in ranked:
        kind = row['kind']
        if counts.get(kind, 0) >= 2:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        selected.append(row)
    return selected[:12]


def _iso(value):
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _sticky_where(user_id, character_id=None, *, include_inactive=False,
                  source=None, include_hidden=True, exclude_expired=False,
                  unviewed_only=False, user_visible_only=False):
    clauses = ['user_id = %s']
    params = [user_id]
    if character_id:
        clauses.append('character_id = %s')
        params.append(character_id)
    if not include_inactive:
        clauses.append("status = 'active'")
    if source:
        clauses.append('source = %s')
        params.append(source)
    if source == USER_FACING_STICKY_SOURCE:
        clauses.extend((
            "metadata->>'projection_version' = 'sticky_evidence_v1'",
            "(CASE WHEN jsonb_typeof(source_event_refs) = 'array' "
            "THEN jsonb_array_length(source_event_refs) ELSE 0 END) = 1",
            "source_event_refs->0->>'source_type' = 'raw_event'",
            "source_event_refs->0->>'user_id' = cognitive_sticky_notes.user_id",
            "source_event_refs->0->>'character_id' = cognitive_sticky_notes.character_id",
            '''EXISTS (
                SELECT 1 FROM chat_log raw
                WHERE raw.user_id = cognitive_sticky_notes.user_id
                  AND raw.chat_id = cognitive_sticky_notes.character_id
                  AND COALESCE(NULLIF(raw.event_id, ''), raw.client_msg_id)
                      = source_event_refs->0->>'source_id'
                  AND raw.role = 'user'
                  AND raw.text = metadata->>'source_text'
                  AND COALESCE(raw.status, 'active') = 'active')''',
        ))
    if not include_hidden:
        clauses.append('user_hidden_at IS NULL')
    if exclude_expired:
        clauses.append('(expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)')
    if unviewed_only:
        clauses.append('viewed = FALSE')
    if user_visible_only:
        clauses.append('user_visible = TRUE')
    return ' AND '.join(clauses), params


def _serialize_sticky_row(row):
    metadata = _json_value(row[17] if len(row) > 17 else {}, {})
    if not isinstance(metadata, dict):
        metadata = {}
    emotion = str(metadata.get('emotion') or '').strip()
    trigger_snippet = str(metadata.get('trigger_snippet') or '').strip()
    tone = str(metadata.get('tone') or '').strip()
    tag = str(metadata.get('tag') or sticky_emotion_tag(emotion)).strip() or '·'
    return {
        'id': row[0],
        'character_id': row[1],
        'note_key': row[2],
        'content': render_role_view(
            row[3], observer_id=row[1], source_character_id=row[1]),
        'status': row[4],
        'source': row[5],
        'source_event_refs': _json_value(row[6], []),
        'created_by_cycle_id': row[7],
        'updated_by_cycle_id': row[8],
        'expires_at': _iso(row[9]),
        'completed_at': _iso(row[10]),
        'created_at': _iso(row[11]),
        'updated_at': _iso(row[12]),
        'viewed': bool(row[13]),
        'viewed_at': _iso(row[14]),
        'user_hidden': row[15] is not None,
        'user_hidden_at': _iso(row[15]),
        'user_visible': bool(row[16]) if len(row) > 16 else True,
        'metadata': metadata,
        'emotion': emotion,
        'tone': tone,
        'trigger_snippet': trigger_snippet,
        'tag': tag,
        'emotion_tag': tag,
    }


def list_sticky_notes(user_id, character_id=None, *, include_inactive=False,
                      limit=50, conn=None, source=None, include_hidden=True,
                      exclude_expired=False, user_visible_only=False):
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        where_sql, params = _sticky_where(
            user_id, character_id,
            include_inactive=include_inactive,
            source=source,
            include_hidden=include_hidden,
            exclude_expired=exclude_expired,
            user_visible_only=user_visible_only,
        )
        params = list(params)
        params.append(max(1, min(int(limit), 100)))
        cur.execute(
            f'''SELECT id, character_id, note_key, content, status, source,
                      source_event_refs, created_by_cycle_id,
                      updated_by_cycle_id, expires_at, completed_at,
                      created_at, updated_at, viewed, viewed_at,
                      user_hidden_at, user_visible, metadata
               FROM cognitive_sticky_notes
               WHERE {where_sql}
               ORDER BY updated_at DESC, id DESC
               LIMIT %s''',
            params,
        )
        return [_serialize_sticky_row(row) for row in cur.fetchall()]
    finally:
        cur.close()
        if owns_connection:
            database.close()


def list_user_facing_sticky_notes(user_id, character_id=None, *, limit=50,
                                  conn=None):
    """Canonical visible sticky projection shared by UI and chat."""
    return list_sticky_notes(
        user_id,
        character_id,
        include_inactive=False,
        limit=limit,
        conn=conn,
        source=USER_FACING_STICKY_SOURCE,
        include_hidden=False,
        exclude_expired=True,
        user_visible_only=True,
    )


def count_unviewed_sticky_notes(user_id, character_id=None, *, source=None,
                                conn=None):
    """User-facing unread count. Does not change semantic status."""
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        where_sql, params = _sticky_where(
            user_id, character_id,
            include_inactive=False,
            source=source,
            include_hidden=False,
            exclude_expired=True,
            unviewed_only=True,
            user_visible_only=True,
        )
        cur.execute(
            f'''SELECT COUNT(*) FROM cognitive_sticky_notes
               WHERE {where_sql}''',
            params,
        )
        row = cur.fetchone()
        return int((row[0] if row else 0) or 0)
    finally:
        cur.close()
        if owns_connection:
            database.close()


def mark_sticky_notes_viewed(user_id, character_id=None, *, source=None,
                             conn=None):
    """Mark matching notes as read. Never changes status/completed_at."""
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        where_sql, params = _sticky_where(
            user_id, character_id,
            include_inactive=False,
            source=source,
            include_hidden=False,
            exclude_expired=True,
            unviewed_only=True,
            user_visible_only=True,
        )
        cur.execute(
            f'''UPDATE cognitive_sticky_notes
               SET viewed = TRUE,
                   viewed_at = COALESCE(viewed_at, CURRENT_TIMESTAMP)
               WHERE {where_sql}''',
            params,
        )
        n = cur.rowcount
        database.commit()
        return int(n or 0)
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def hide_sticky_note(user_id, note_id, *, character_id=None, conn=None):
    """User tear-off. Hides from UI without completing cognitive lifecycle."""
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        clauses = ['id = %s', 'user_id = %s', 'user_hidden_at IS NULL']
        params = [note_id, user_id]
        if character_id:
            clauses.append('character_id = %s')
            params.append(character_id)
        cur.execute(
            f'''UPDATE cognitive_sticky_notes
               SET user_hidden_at = CURRENT_TIMESTAMP
               WHERE {' AND '.join(clauses)}
               RETURNING id, status''',
            params,
        )
        row = cur.fetchone()
        database.commit()
        return bool(row)
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def complete_sticky_note(user_id, character_id, note_id, *, conn=None):
    """Semantic lifecycle complete. Not the user-facing 撕掉/viewed action."""
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        cur.execute(
            '''UPDATE cognitive_sticky_notes
               SET status = 'completed',
                   completed_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = %s AND user_id = %s AND character_id = %s
               RETURNING id''',
            (note_id, user_id, character_id),
        )
        row = cur.fetchone()
        database.commit()
        return bool(row)
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def list_day_cycle_notes(user_id, character_id, day_start, *, limit=8, conn=None):
    """Succeeded Slow Loop notes for one local day. Not a diary body."""
    from datetime import timedelta

    if not user_id or not character_id or day_start is None:
        return {'cycles': [], 'questions': [], 'settled_predictions': []}
    day_end = day_start + timedelta(days=1)
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT cycle_summary, completed_at
               FROM cognitive_cycles
               WHERE user_id = %s AND character_id = %s
                 AND status = 'succeeded' AND cycle_summary IS NOT NULL
                 AND invalidated_by_event_id IS NULL
                 AND completed_at >= %s AND completed_at < %s
               ORDER BY completed_at ASC, id ASC
               LIMIT %s''',
            (user_id, character_id, day_start, day_end,
             max(1, min(int(limit), 12))),
        )
        notes = []
        for summary, completed_at in cur.fetchall():
            data = _json_value(summary, {})
            if not isinstance(data, dict):
                data = {}
            notes.append({
                'summary': _safe_text(data.get('summary'), 400),
                'salient_change': _safe_text(data.get('salient_change'), 240),
                'uncertainty': _safe_text(data.get('uncertainty'), 240),
                'completed_at': completed_at,
            })
        cur.execute(
            '''SELECT question_text, status
               FROM cognitive_questions
               WHERE user_id = %s AND character_id = %s
                 AND updated_at >= %s AND updated_at < %s
               ORDER BY updated_at DESC
               LIMIT 6''',
            (user_id, character_id, day_start, day_end),
        )
        questions = [
            {'question_text': _safe_text(row[0], 240), 'status': row[1]}
            for row in cur.fetchall()
        ]
        cur.execute(
            '''SELECT prediction_key, status, metadata
               FROM cognitive_predictions
               WHERE user_id = %s AND character_id = %s
                 AND settled_at >= %s AND settled_at < %s
               ORDER BY settled_at DESC
               LIMIT 6''',
            (user_id, character_id, day_start, day_end),
        )
        predictions = []
        for key, status, metadata in cur.fetchall():
            meta = _json_value(metadata, {})
            predictions.append({
                'prediction_key': key,
                'status': status,
                'statement': _safe_text(
                    (meta or {}).get('statement') or key, 240),
            })
        return {
            'cycles': notes,
            'questions': questions,
            'settled_predictions': predictions,
        }
    except Exception:
        return {'cycles': [], 'questions': [], 'settled_predictions': []}
    finally:
        cur.close()
        if owns_connection:
            database.close()


def list_diary_entries(user_id, character_id, *, limit=30, conn=None):
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT id, diary_key, content, reflection_kind,
                      source_event_refs, occurred_at, created_at
               FROM cognitive_diary_entries
               WHERE user_id = %s AND character_id = %s
                 AND invalidated_by_event_id IS NULL
               ORDER BY occurred_at DESC, id DESC
               LIMIT %s''',
            (user_id, character_id, max(1, min(int(limit), 100))),
        )
        return [{
            'id': row[0],
            'diary_key': row[1],
            'content': row[2],
            'reflection_kind': row[3],
            'source_event_refs': _json_value(row[4], []),
            'occurred_at': row[5],
            'created_at': row[6],
        } for row in cur.fetchall()]
    finally:
        cur.close()
        if owns_connection:
            database.close()
