"""Read-only bridge from durable Slow Loop state into character context."""
import json
import re


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
        summary = _json_value(cycle_row[0], {}) if cycle_row else {}
        return {
            'cycle_summary': summary if isinstance(summary, dict) else {},
            'completed_at': cycle_row[1] if cycle_row else None,
            'beliefs': beliefs,
            'hypotheses': hypotheses,
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
    if not summary and not beliefs and not hypotheses:
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

    lines.extend([
        '使用边界：按角色人设自然吸收，不要复述这份复盘、事件编号、置信度或系统术语。',
        '当前对话中的直接证据优先；若它与旧归纳冲突，保留不确定性，不要为了维护旧结论而曲解用户。',
        '尤其不要把亲近、照顾、长期互动或一个假设自动升级为爱情。',
    ])
    return '\n'.join(lines)
