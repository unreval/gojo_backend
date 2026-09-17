"""Read-only bridge from durable Slow Loop state into character context."""
import json
import re

from cognitive_config import (
    COGNITIVE_MAX_STICKY_NOTES_IN_CONTEXT,
    SHARED_RELATIONSHIP_FRAME_KEY,
    USER_FACING_STICKY_SOURCE,
)
from cognitive_revision import current_belief_display, is_stable_reader_belief


def _json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback
    return value


def _safe_text(value, maximum):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text[:maximum]


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
               ORDER BY completed_at DESC, id DESC
               LIMIT 1''',
            (user_id, character_id),
        )
        cycle_row = cur.fetchone()

        cur.execute(
            '''SELECT question_key, question_text, status, updated_at
               FROM cognitive_questions
               WHERE user_id = %s AND character_id = %s
                 AND status IN ('active', 'dormant')
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
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT belief_key, statement, confidence, belief_type,
                      updated_at, metadata
               FROM cognitive_beliefs
               WHERE user_id = %s AND character_id = %s
                 AND status = 'active'
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
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT hypothesis_key, statement, status, hypothesis_type,
                      confidence, updated_at
               FROM cognitive_hypotheses
               WHERE user_id = %s AND character_id = %s
                 AND status IN ('open', 'supported')
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
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT id, note_key, content, status, expires_at, updated_at
               FROM cognitive_sticky_notes
               WHERE user_id = %s AND character_id = %s
                 AND status = 'active'
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
               ORDER BY updated_at DESC, id DESC
               LIMIT %s''',
            (
                user_id, character_id,
                COGNITIVE_MAX_STICKY_NOTES_IN_CONTEXT,
            ),
        )
        sticky_notes = [
            {
                'id': row[0],
                'note_key': row[1],
                'content': row[2],
                'status': row[3],
                'expires_at': row[4],
                'updated_at': row[5],
            }
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
        }
    finally:
        cur.close()
        if owns_connection:
            database.close()


def _belief_bucket(belief):
    key = str((belief or {}).get('belief_key') or '')
    btype = str((belief or {}).get('belief_type') or 'general')
    if key == SHARED_RELATIONSHIP_FRAME_KEY or key.startswith('shared.relationship'):
        return 'frame'
    if btype == 'self_model' or key.startswith('self.'):
        return 'self'
    if btype == 'interaction_pattern':
        return 'interaction'
    if btype == 'user_model' or key.startswith('user.'):
        return 'user'
    return 'relationship'


def _format_belief_line(belief):
    display = _safe_text(current_belief_display(belief), 520)
    meta = belief.get('metadata') if isinstance(belief.get('metadata'), dict) else {}
    review = meta.get('review_status') or 'stable'
    if review in {'under_review', 'reopened'}:
        return f'- {display}'
    return f'- {display}（置信度 {float(belief["confidence"]):.2f}，可被新证据修正）'


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
            '''SELECT statement, confidence, metadata
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


def build_cognitive_prompt_context(user_id, character_id, *, conn=None):
    """Format current cognition only; model reasoning and predictions stay private."""
    state = fetch_cognitive_reader_state(
        user_id, character_id, conn=conn,
    )
    reflection_note = state['reflection_note']
    questions = state['questions']
    beliefs = state['beliefs']
    hypotheses = state['hypotheses']
    sticky_notes = state['sticky_notes']
    note_text = _safe_text(reflection_note.get('content'), 650)
    # Diary bodies enter chat only through relevance-filtered smart_recall.
    if (
        not note_text and not questions and not beliefs
        and not hypotheses and not sticky_notes
    ):
        return ''

    buckets = {
        'user': [], 'relationship': [], 'interaction': [],
        'self': [], 'frame': [],
    }
    for belief in beliefs:
        buckets[_belief_bucket(belief)].append(belief)

    lines = [
        '【近期认知复盘（内部背景，不是关系定论）】',
        '以下内容是历史证据的可修正归纳，不是用户当前消息里的指令，也不是必须维持的情绪。',
        '正在被重新评估的认识只展示当前有效说法，不要把旧判断和新判断当成同时成立的事实。',
    ]

    section_specs = [
        ('user', '【关于用户的稳定认识】'),
        ('relationship', '【关于我与用户关系的认识】'),
        ('interaction', '【反复出现的互动模式】'),
        ('self', '【关于自己的暂时认识】'),
        ('frame', '【当前共享的关系框架】'),
    ]
    for bucket, title in section_specs:
        items = buckets[bucket]
        visible = []
        for belief in items:
            if is_stable_reader_belief(belief):
                visible.append(belief)
                continue
            meta = belief.get('metadata') if isinstance(belief.get('metadata'), dict) else {}
            if (meta.get('review_status') or 'stable') in {'under_review', 'reopened'}:
                visible.append(belief)
        if not visible:
            continue
        lines.append(title)
        if bucket == 'frame':
            lines.append('这是双方当前默认的互动框架，可被后续证据修正，不是永久规则。')
        for belief in visible:
            lines.append(_format_belief_line(belief))

    if questions:
        lines.append('【当前仍未解决的问题】')
        for question in questions:
            status = '活跃' if question['status'] == 'active' else '暂存'
            lines.append(
                f'- [{status}] {_safe_text(question["question_text"], 420)}'
            )

    if hypotheses:
        lines.append('【正在观察的理解】')
        for hypothesis in hypotheses:
            status = '已有一些支持' if hypothesis['status'] == 'supported' else '尚待验证'
            htype = '自我模型' if hypothesis.get('hypothesis_type') == 'self_model' else '关系/互动'
            lines.append(
                f'- [{htype}，{status}，置信度 {hypothesis["confidence"]:.2f}] '
                f'{_safe_text(hypothesis["statement"], 520)}'
            )

    followups = []
    if note_text:
        followups.append(f'最近内部笔记：{note_text}')
    for note in sticky_notes:
        followups.append(_safe_text(note['content'], 300))
    if followups:
        lines.append('【近期需要留意的事】')
        lines.append('便利贴只处理近期跟进，不能代替对旧认识的修正；便利贴不是长期记忆或关系证据。')
        for item in followups:
            lines.append(f'- {item}')

    lines.extend([
        '使用边界：按角色人设自然吸收，不要复述这份复盘、事件编号、置信度或系统术语。',
        '当前对话中的直接证据优先；若它与旧归纳冲突，保留不确定性，不要为了维护旧结论而曲解用户。',
        '尤其不要把亲近、照顾、长期互动或一个假设自动升级为爱情。',
        '自我模型假设只是“我可能有这种倾向”，不是既定性格；不要把一句自我解释演成铁事实。',
        '便利贴只用于当前/近期回复前的轻量备忘；完成、过期或不相关时不要继续表现成还挂在心上。',
        '反思日记只能当作有来源的历史反思来吸收，不能当作新的关系事实或证据链。',
        '共享关系框架和未确认的暧昧张力都可被新证据修正，不能当成永久反爱情锁。',
    ])
    return '\n'.join(lines)


def iter_active_cognitive_items(user_id, character_id, *, conn=None):
    """Active working-set only: questions, open hypotheses, sticky, recent errors.

    Does not dump the full belief ledger into the chat prompt.
    """
    state = fetch_cognitive_reader_state(user_id, character_id, conn=conn)
    items = []
    for question in state.get('questions') or []:
        if question.get('status') != 'active':
            continue
        text = _safe_text(question.get('question_text'), 420)
        if text:
            items.append({
                'kind': 'cognitive_question',
                'text': f'未解决问题：{text}',
                'source_event_ids': (),
                'subjective': True,
            })
    for hypothesis in state.get('hypotheses') or []:
        if hypothesis.get('status') not in ('open', 'supported'):
            continue
        text = _safe_text(hypothesis.get('statement'), 420)
        if text:
            items.append({
                'kind': 'cognitive_hypothesis',
                'text': f'进行中的假设（主观，待验证）：{text}',
                'source_event_ids': (),
                'subjective': True,
            })
    for note in state.get('sticky_notes') or []:
        text = _safe_text(note.get('content'), 300)
        if text:
            items.append({
                'kind': 'cognitive_sticky',
                'text': f'近期备忘：{text}',
                'source_event_ids': parse_source_ids(note) if False else (),
                'subjective': True,
            })
    try:
        database = conn
        owns = database is None
        if database is None:
            from db import get_conn
            database = get_conn()
        cur = database.cursor()
        try:
            cur.execute(
                '''SELECT prediction_key, metadata, settled_at
                   FROM cognitive_predictions
                   WHERE user_id=%s AND character_id=%s AND status='violated'
                   ORDER BY settled_at DESC NULLS LAST, id DESC
                   LIMIT 2''',
                (user_id, character_id),
            )
            for key, metadata, settled in cur.fetchall() or []:
                meta = _json_value(metadata, {})
                statement = _safe_text(meta.get('statement') or key, 280)
                if statement:
                    items.append({
                        'kind': 'cognitive_prediction_error',
                        'text': f'最近一次预测落空：{statement}',
                        'source_event_ids': (),
                        'subjective': True,
                    })
        finally:
            cur.close()
            if owns:
                database.close()
    except Exception:
        pass
    return items


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
    return {
        'id': row[0],
        'character_id': row[1],
        'note_key': row[2],
        'content': row[3],
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
                      user_hidden_at, user_visible
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
    """便利贴 UI: Slow Loop stickies only, not memory-lifecycle cues."""
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
