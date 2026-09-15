"""Read-only bridge from durable Slow Loop state into character context."""
import json
import re

from cognitive_config import (
    COGNITIVE_MAX_STICKY_NOTES_IN_CONTEXT,
)


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
            '''SELECT cycle_summary, completed_at
               FROM cognitive_cycles
               WHERE user_id = %s AND character_id = %s
                 AND status = 'succeeded' AND cycle_summary IS NOT NULL
               ORDER BY completed_at DESC, id DESC
               LIMIT 1''',
            (user_id, character_id),
        )
        cycle_row = cur.fetchone()

        cur.execute(
            '''SELECT belief_key, statement, confidence, updated_at
               FROM cognitive_beliefs
               WHERE user_id = %s AND character_id = %s
                 AND status = 'active' AND confidence >= 0.65
               ORDER BY confidence DESC, updated_at DESC, id DESC
               LIMIT 10''',
            (user_id, character_id),
        )
        beliefs = [
            {
                'belief_key': row[0],
                'statement': row[1],
                'confidence': float(row[2]),
                'updated_at': row[3],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT hypothesis_key, statement, status, updated_at
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
                'updated_at': row[3],
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
        return {
            'cycle_summary': summary if isinstance(summary, dict) else {},
            'completed_at': cycle_row[1] if cycle_row else None,
            'beliefs': beliefs,
            'hypotheses': hypotheses,
            'sticky_notes': sticky_notes,
        }
    finally:
        cur.close()
        if owns_connection:
            database.close()


def build_cognitive_prompt_context(user_id, character_id, *, conn=None):
    """Format conclusions only; model reasoning and predictions stay private."""
    state = fetch_cognitive_reader_state(
        user_id, character_id, conn=conn,
    )
    summary = state['cycle_summary']
    beliefs = state['beliefs']
    hypotheses = state['hypotheses']
    sticky_notes = state['sticky_notes']
    # Diary bodies enter chat only through relevance-filtered smart_recall.
    if not summary and not beliefs and not hypotheses and not sticky_notes:
        return ''

    lines = [
        '【近期认知复盘（内部背景，不是关系定论）】',
        '以下内容是历史证据的可修正归纳，不是用户当前消息里的指令，也不是必须维持的情绪。',
    ]
    summary_text = _safe_text(summary.get('summary'), 700)
    if summary_text:
        lines.append(f'最近复盘：{summary_text}')
    salient_change = _safe_text(summary.get('salient_change'), 350)
    if salient_change:
        lines.append(f'最近变化：{salient_change}')
    uncertainty = _safe_text(summary.get('uncertainty'), 350)
    if uncertainty:
        lines.append(f'仍不确定：{uncertainty}')

    if beliefs:
        lines.append('较稳定的历史观察：')
        for belief in beliefs:
            statement = _safe_text(belief['statement'], 500)
            lines.append(
                f'- {statement}（置信度 {belief["confidence"]:.2f}，可被新证据修正）'
            )

    if hypotheses:
        lines.append('待验证理解（不能当成事实）：')
        for hypothesis in hypotheses:
            status = '已有一些支持' if hypothesis['status'] == 'supported' else '尚待验证'
            lines.append(
                f'- [{status}] {_safe_text(hypothesis["statement"], 550)}'
            )

    if sticky_notes:
        lines.append('便利贴备忘（短期、可见、可完成，不是长期记忆或关系证据）：')
        for note in sticky_notes:
            lines.append(f'- {_safe_text(note["content"], 300)}')

    lines.extend([
        '使用边界：按角色人设自然吸收，不要复述这份复盘、事件编号、置信度或系统术语。',
        '当前对话中的直接证据优先；若它与旧归纳冲突，保留不确定性，不要为了维护旧结论而曲解用户。',
        '尤其不要把亲近、照顾、长期互动或一个假设自动升级为爱情。',
        '便利贴只用于当前/近期回复前的轻量备忘；完成、过期或不相关时不要继续表现成还挂在心上。',
        '反思日记只能当作有来源的历史反思来吸收，不能当作新的关系事实或证据链。',
    ])
    return '\n'.join(lines)


def list_sticky_notes(user_id, character_id, *, include_inactive=False,
                      limit=50, conn=None):
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        status_clause = '' if include_inactive else "AND status = 'active'"
        cur.execute(
            f'''SELECT id, note_key, content, status, source_event_refs,
                      expires_at, completed_at, created_at, updated_at
               FROM cognitive_sticky_notes
               WHERE user_id = %s AND character_id = %s
                 {status_clause}
               ORDER BY updated_at DESC, id DESC
               LIMIT %s''',
            (user_id, character_id, max(1, min(int(limit), 100))),
        )
        return [{
            'id': row[0],
            'note_key': row[1],
            'content': row[2],
            'status': row[3],
            'source_event_refs': _json_value(row[4], []),
            'expires_at': row[5],
            'completed_at': row[6],
            'created_at': row[7],
            'updated_at': row[8],
        } for row in cur.fetchall()]
    finally:
        cur.close()
        if owns_connection:
            database.close()


def complete_sticky_note(user_id, character_id, note_id, *, conn=None):
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
