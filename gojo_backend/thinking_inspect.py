"""Read-only inspection of thinking-chain observations.

Nothing here writes a table or calls a model. Understandings and views are
not authority; this script only prints them.
"""
import argparse
import json
from datetime import datetime, timezone


def _json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _row_dict(columns, row):
    item = {}
    for name, value in zip(columns, row):
        if name in {
            'source_event_ids', 'context_refs', 'steps', 'cited_experiences',
            'cited_views', 'view_proposals', 'evidence', 'revision_log', 'detail',
        }:
            item[name] = _json_value(value, [] if name != 'detail' else {})
        else:
            item[name] = value
    return item


def _source_texts(cur, user_id, character_id, event_ids):
    texts = []
    for event_id in event_ids or []:
        cur.execute('''SELECT text FROM chat_log
            WHERE user_id=%s AND chat_id=%s
              AND COALESCE(NULLIF(event_id, ''), client_msg_id)=%s
            ORDER BY id DESC LIMIT 1''', (user_id, character_id, event_id))
        row = cur.fetchone()
        texts.append(row[0] if row else None)
    return texts


def fetch_thinking_snapshot(user_id, character_id='gojo', *, limit=10, conn=None, now=None):
    """Return pending work, control, recent understandings, audit rows, and views."""
    from config import THINKING_FAST_DAILY_LIMIT
    from raw_events import any_source_inactive
    from thinking_fast import (
        SGT, _BUDGET_ID, _control_id, _load_control, _pending_batches,
    )

    owns_connection = conn is None
    if conn is None:
        from db import get_conn
        conn = get_conn()
    current = now or datetime.now(timezone.utc)
    limit = max(1, int(limit))
    cur = conn.cursor()
    try:
        pending = _pending_batches(cur, user_id, character_id, current)
        state = _load_control(cur, _control_id(user_id))
        budget = _load_control(cur, _BUDGET_ID)
        today = current.astimezone(SGT).date().isoformat()
        used = budget.get('batches', 0) if budget.get('day') == today else 0

        understanding_columns = (
            'id', 'user_id', 'character_id', 'batch_ref', 'item_index',
            'source_event_ids', 'context_refs', 'steps', 'cited_experiences',
            'cited_views', 'view_proposals', 'prompt_version', 'model', 'status',
            'consumed_by_run', 'created_at',
        )
        cur.execute('''SELECT id, user_id, character_id, batch_ref, item_index,
                source_event_ids, context_refs, steps, cited_experiences,
                cited_views, view_proposals, prompt_version, model, status,
                consumed_by_run, created_at
            FROM thinking_understandings
            WHERE user_id=%s AND character_id=%s
            ORDER BY created_at DESC, id DESC
            LIMIT %s''', (user_id, character_id, limit))
        understandings = []
        for row in cur.fetchall():
            item = _row_dict(understanding_columns, row)
            event_ids = item.get('source_event_ids') or []
            item['sources_text'] = _source_texts(cur, user_id, character_id, event_ids)
            item['sources_active'] = not any_source_inactive(
                event_ids, user_id, character_id, conn=conn)
            understandings.append(item)

        audit_columns = (
            'id', 'user_id', 'character_id', 'batch_ref', 'kind', 'reason',
            'detail', 'prompt_version', 'created_at',
        )
        cur.execute('''SELECT id, user_id, character_id, batch_ref, kind, reason,
                detail, prompt_version, created_at
            FROM thinking_audit_log
            WHERE user_id=%s AND character_id=%s
            ORDER BY created_at DESC, id DESC
            LIMIT %s''', (user_id, character_id, limit))
        rejected = [_row_dict(audit_columns, row) for row in cur.fetchall()]

        view_columns = (
            'id', 'user_id', 'character_id', 'about', 'statement', 'strength',
            'status', 'evidence', 'revision_log', 'last_supported_at',
            'prompt_version', 'model', 'created_at', 'updated_at',
        )
        cur.execute('''SELECT id, user_id, character_id, about, statement, strength,
                status, evidence, revision_log, last_supported_at,
                prompt_version, model, created_at, updated_at
            FROM character_views
            WHERE user_id=%s AND character_id=%s
            ORDER BY id ASC''', (user_id, character_id))
        views = [_row_dict(view_columns, row) for row in cur.fetchall()]
        return {
            'user_id': user_id,
            'character_id': character_id,
            'pending_batches': len(pending),
            'control': {
                'fail_count': state.get('fail_count', 0),
                'retry_at': state.get('retry_at'),
                'fail_batch': state.get('fail_batch'),
                'daily_used': used,
                'daily_limit': THINKING_FAST_DAILY_LIMIT,
            },
            'understandings': understandings,
            'rejected': rejected,
            'views': views,
        }
    finally:
        cur.close()
        if owns_connection:
            try:
                conn.rollback()
            finally:
                conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Print thinking-chain observations as JSON.')
    parser.add_argument('--user', required=True)
    parser.add_argument('--character', default='gojo')
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args(argv)
    snapshot = fetch_thinking_snapshot(args.user, args.character, limit=args.limit)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
