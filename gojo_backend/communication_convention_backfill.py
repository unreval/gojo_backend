"""Narrow, operator-invoked repair for historical communication conventions.

This tool deliberately has no discovery mode for the whole database.  An
operator must supply one or more canonical user event ids, or one bounded
user/character time window.  Planning is the default and never writes.  Apply
reuses the exact candidate emitted during that dry-run, then rereads canonical
sources and runs the ordinary deterministic convention gate before writing.
"""
from __future__ import annotations

import argparse
import json

from db import get_conn
import raw_events
import user_memory


BACKFILL_PROCESSOR_TYPE = 'communication_convention_backfill'
BACKFILL_PROCESSOR_VERSION = 'v1'
DEFAULT_LIMIT = 100


def _event_id(value):
    return str(value or '').strip()


def _event_view(event):
    return {
        'event_id': _event_id(event.get('event_id')),
        'role': event.get('role') or '',
        'content': event.get('content') or '',
        'timestamp': str(event.get('timestamp') or ''),
    }


def _require_scope(event_ids, start_at, end_at):
    if event_ids:
        return
    if start_at and end_at:
        return
    raise ValueError('provide --event-id or both --from and --to; full-database scans are disabled')


def _scope_clause(event_ids, start_at, end_at, params):
    if event_ids:
        params.append(list(dict.fromkeys(_event_id(value) for value in event_ids if _event_id(value))))
        return "AND COALESCE(NULLIF(event_id, ''), client_msg_id) = ANY(%s)"
    params.extend([start_at, end_at])
    return 'AND created_at >= %s AND created_at < %s'


def list_canonical_user_events(user_id, character_id, *, event_ids=None,
                               start_at=None, end_at=None, limit=DEFAULT_LIMIT,
                               conn=None):
    """Read only, bounded primary events for a prospective backfill."""
    _require_scope(event_ids, start_at, end_at)
    own = conn is None
    database = conn or get_conn()
    cur = database.cursor()
    params = [user_id, character_id]
    clause = _scope_clause(event_ids, start_at, end_at, params)
    params.append(max(1, min(int(limit or DEFAULT_LIMIT), 500)))
    try:
        cur.execute(
            f'''SELECT COALESCE(NULLIF(event_id, ''), client_msg_id), role, text, created_at
                FROM chat_log
                WHERE user_id=%s AND chat_id=%s
                  AND role='user'
                  AND COALESCE(status, 'active')='active'
                  AND COALESCE(NULLIF(event_id, ''), client_msg_id) IS NOT NULL
                  {clause}
                ORDER BY created_at ASC, id ASC
                LIMIT %s''',
            params,
        )
        return [
            {
                'event_id': _event_id(event_id),
                'role': 'user',
                'content': content or '',
                'timestamp': timestamp,
            }
            for event_id, _role, content, timestamp in cur.fetchall()
            if _event_id(event_id)
        ]
    finally:
        cur.close()
        if own:
            database.close()


def list_symbol_events(user_id, character_id, symbol, *, event_ids=None,
                       start_at=None, end_at=None, limit=DEFAULT_LIMIT,
                       conn=None):
    """Read matching raw events without treating the symbol as authorization."""
    if not symbol:
        return []
    _require_scope(event_ids, start_at, end_at)
    own = conn is None
    database = conn or get_conn()
    cur = database.cursor()
    params = [user_id, character_id]
    clause = _scope_clause(event_ids, start_at, end_at, params)
    params.extend([f'%{symbol}%', max(1, min(int(limit or DEFAULT_LIMIT), 500))])
    try:
        cur.execute(
            f'''SELECT COALESCE(NULLIF(event_id, ''), client_msg_id), role, text, created_at
                FROM chat_log
                WHERE user_id=%s AND chat_id=%s
                  AND COALESCE(status, 'active')='active'
                  AND COALESCE(NULLIF(event_id, ''), client_msg_id) IS NOT NULL
                  {clause}
                  AND text LIKE %s
                ORDER BY created_at ASC, id ASC
                LIMIT %s''',
            params,
        )
        return [
            {
                'event_id': _event_id(event_id),
                'role': 'user' if role == 'user' else 'assistant',
                'content': content or '',
                'timestamp': timestamp,
            }
            for event_id, role, content, timestamp in cur.fetchall()
            if _event_id(event_id)
        ]
    finally:
        cur.close()
        if own:
            database.close()


def list_convention_memories(user_id, character_id, *, conn=None):
    """Read every convention state and its provenance; never changes rows."""
    own = conn is None
    database = conn or get_conn()
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT bm.id, bm.content, COALESCE(bm.recall_status, 'active'), bm.timestamp,
                      COALESCE(array_remove(array_agg(mse.source_event_id), NULL), ARRAY[]::TEXT[])
               FROM bond_memory AS bm
               LEFT JOIN memory_source_events AS mse
                 ON mse.memory_type='bond_memory' AND mse.memory_id=bm.id
               WHERE bm.user_id=%s AND bm.character_id=%s AND bm.kind='between'
                 AND bm.content LIKE %s
               GROUP BY bm.id, bm.content, bm.recall_status, bm.timestamp
               ORDER BY bm.timestamp ASC, bm.id ASC''',
            (user_id, character_id, f'%{user_memory.COMMUNICATION_CONVENTION_MARKER}%'),
        )
        return [
            {
                'memory_id': memory_id,
                'content': content or '',
                'recall_status': recall_status or 'active',
                'timestamp': timestamp,
                'source_event_ids': [
                    _event_id(source_id) for source_id in (source_ids or [])
                    if _event_id(source_id)
                ],
            }
            for memory_id, content, recall_status, timestamp, source_ids in cur.fetchall()
        ]
    finally:
        cur.close()
        if own:
            database.close()


def inspect_scope(user_id, character_id, *, event_ids=None, start_at=None,
                  end_at=None, symbol=None, limit=DEFAULT_LIMIT):
    """Return the raw/provenance evidence needed for an operator review."""
    primary_events = list_canonical_user_events(
        user_id, character_id, event_ids=event_ids, start_at=start_at,
        end_at=end_at, limit=limit)
    memories = list_convention_memories(user_id, character_id)
    contexts = []
    for primary in primary_events:
        context = raw_events.get_previous_active_turn_events(
            user_id, character_id, primary['event_id'], n=6, hours=24)
        current = raw_events.get_active_events_by_ids(
            user_id, character_id, [primary['event_id']])
        contexts.append({
            'anchor_event_id': primary['event_id'],
            'events': [_event_view(event) for event in context + current],
        })
    source_ids = []
    for memory in memories:
        for source_id in memory['source_event_ids']:
            if source_id not in source_ids:
                source_ids.append(source_id)
    source_events = raw_events.get_active_events_by_ids(
        user_id, character_id, source_ids)
    return {
        'user_id': user_id,
        'character_id': character_id,
        'primary_events': primary_events,
        'symbol_events': list_symbol_events(
            user_id, character_id, symbol, event_ids=event_ids,
            start_at=start_at, end_at=end_at, limit=limit) if symbol else [],
        'conventions': memories,
        'source_events': [_event_view(event) for event in source_events],
        'contexts': contexts,
    }


def _plan_classification(decision):
    status = (decision or {}).get('status')
    if status == 'rejected':
        return 'conflict'
    if status in ('would_add', 'would_replace', 'would_revoke'):
        return status
    if status == 'unchanged':
        return 'no_change'
    return status or 'no_decision'


def plan_event(user_id, character_id, event):
    """Run the same extractor and gate in a no-write convention-only mode."""
    result = {}
    try:
        ok = user_memory.extract_and_save_memory(
            user_id, event.get('content') or '', '', character_id,
            source_event_id=event['event_id'],
            source_event_ids=[event['event_id']],
            convention_only=True,
            dry_run=True,
            backfill_result=result,
            processor_type=BACKFILL_PROCESSOR_TYPE,
            processor_version=BACKFILL_PROCESSOR_VERSION,
        )
    except Exception as exc:
        return {
            'event_id': event['event_id'],
            'timestamp': str(event.get('timestamp') or ''),
            'status': 'error',
            'error': str(exc),
        }
    decision = result.get('decision', {'status': 'no_decision'})
    return {
        'event_id': event['event_id'],
        'timestamp': str(event.get('timestamp') or ''),
        'status': 'planned' if ok else 'error',
        'candidate': result.get('candidate'),
        'classification': _plan_classification(decision),
        'decision': decision,
    }


def plan_backfill(user_id, character_id, events):
    return [plan_event(user_id, character_id, event) for event in events]


def apply_plan_item(user_id, character_id, plan):
    """Apply one preplanned candidate, rechecking canonical sources and gate."""
    decision = plan.get('decision') or {}
    if decision.get('status') not in ('would_add', 'would_replace', 'would_revoke'):
        return {
            'event_id': plan.get('event_id'),
            'status': 'skipped',
            'reason': decision.get('status', 'not_actionable'),
        }
    candidate = plan.get('candidate')
    if not isinstance(candidate, dict):
        return {
            'event_id': plan.get('event_id'),
            'status': 'skipped',
            'reason': 'missing_candidate',
        }
    result = {}
    ok = user_memory.extract_and_save_memory(
        user_id, '', '', character_id,
        source_event_id=plan['event_id'],
        source_event_ids=[plan['event_id']],
        convention_only=True,
        dry_run=False,
        backfill_result=result,
        parsed_override={'communication_convention': candidate},
        processor_type=BACKFILL_PROCESSOR_TYPE,
        processor_version=BACKFILL_PROCESSOR_VERSION,
    )
    return {
        'event_id': plan['event_id'],
        'status': 'applied' if ok else 'error',
        'decision': result.get('decision'),
    }


def _active_bond_rows(memories):
    return [
        (memory['memory_id'], memory['content'], memory.get('timestamp'))
        for memory in memories
        if memory.get('recall_status', 'active') == 'active'
    ]


def validate_existing_convention(user_id, character_id, memory, active_bonds):
    """Confirm linked authorization without treating missing links as absence.

    memory_source_events stores individual links, not an authoritative manifest
    of all sources considered at creation. Neither those links nor a bounded
    chat window proves completeness. Until that proof exists, unsupported
    legacy rows must remain ambiguous and cannot be retired by this audit.
    """
    symbol = user_memory._communication_convention_payload(memory.get('content'))
    source_ids = memory.get('source_event_ids') or []
    if not symbol or not source_ids:
        return {
            'memory_id': memory.get('memory_id'),
            'status': 'ambiguous',
            'reason': 'missing_payload_or_provenance',
        }
    try:
        events = raw_events.get_active_events_by_ids(
            user_id, character_id, source_ids)
    except Exception:
        return {
            'memory_id': memory.get('memory_id'),
            'status': 'ambiguous',
            'reason': 'canonical_source_unavailable',
            'canonical_source_complete': False,
        }
    if len(events) != len(set(source_ids)):
        return {
            'memory_id': memory.get('memory_id'),
            'status': 'ambiguous',
            'reason': 'missing_or_deleted_source',
        }
    canonical_events = events
    canonical_event_ids = [
        _event_id(event.get('event_id')) for event in canonical_events
        if _event_id(event.get('event_id'))
    ]
    symbol_event = next(
        (event for event in events if symbol in str(event.get('content') or '')),
        None)
    user_events = [
        event for event in canonical_events
        if event.get('role') == 'user' and _event_id(event.get('event_id'))
    ]
    if not symbol_event or not user_events:
        return {
            'memory_id': memory.get('memory_id'),
            'status': 'ambiguous',
            'reason': 'incomplete_canonical_source_set',
            'recorded_source_event_ids': list(source_ids),
            'canonical_source_event_ids': canonical_event_ids,
        }

    decisions = []
    slot = user_memory._communication_convention_slot(memory.get('content'))
    for primary in user_events:
        primary_id = _event_id(primary.get('event_id'))
        if not primary_id:
            continue
        item = {
            'action': 'set',
            'slot': slot or None,
            'symbol': symbol,
            'user_evidence_quote': primary.get('content') or '',
            'user_evidence_event_ids': [primary_id],
            'symbol_event_id': symbol_event.get('event_id'),
            'symbol_evidence_quote': symbol,
            'replaces': [],
        }
        outcome = {}
        user_memory._apply_communication_convention(
            user_id, character_id, item,
            user_events, primary_id,
            canonical_events, canonical_events,
            active_bonds, dry_run=True, outcome=outcome,
        )
        decisions.append(outcome)
        if outcome.get('status') in ('would_add', 'unchanged'):
            return {
                'memory_id': memory.get('memory_id'),
                'status': 'confirmed',
                'decision': outcome,
                'recorded_source_event_ids': list(source_ids),
                'canonical_source_event_ids': canonical_event_ids,
            }

    return {
        'memory_id': memory.get('memory_id'),
        'status': 'ambiguous',
        'reason': 'provenance_completeness_unproven',
        'canonical_source_complete': False,
        'recorded_source_event_ids': list(source_ids),
        'canonical_source_event_ids': canonical_event_ids,
        'decisions': decisions,
    }


def validate_existing_conventions(user_id, character_id, memories):
    active_bonds = _active_bond_rows(memories)
    return [
        validate_existing_convention(user_id, character_id, memory, active_bonds)
        for memory in memories
        if memory.get('recall_status', 'active') == 'active'
    ]


def retire_unsupported_conventions(user_id, character_id, validations, *, dry_run=True):
    """Reread canonical evidence before any retirement; caller flags are no proof."""
    ids = [
        item['memory_id'] for item in validations
        if (item.get('status') == 'would_retire_unsupported'
            and item.get('canonical_source_complete') is True)
    ]
    if not ids:
        return {'status': 'no_unsupported_conventions', 'memory_ids': []}
    if dry_run:
        return {'status': 'would_retire', 'memory_ids': ids}
    try:
        memories = list_convention_memories(user_id, character_id)
        current_validations = validate_existing_conventions(
            user_id, character_id, memories)
    except Exception:
        return {
            'status': 'ambiguous', 'memory_ids': [],
            'reason': 'canonical_source_unavailable',
        }
    ids = [
        item['memory_id'] for item in current_validations
        if (item.get('memory_id') in ids
            and item.get('status') == 'would_retire_unsupported'
            and item.get('canonical_source_complete') is True)
    ]
    if not ids:
        return {
            'status': 'no_unsupported_conventions', 'memory_ids': [],
            'validation': current_validations,
        }
    ok, rows = user_memory.invalidate_bond_memories(
        user_id, character_id, 'between', ids, reason='unsupported_convention')
    return {
        'status': 'retired' if ok else 'not_retired',
        'memory_ids': [memory_id for memory_id, _content in rows],
        'validation': current_validations,
    }


def run_backfill(user_id, character_id, *, event_ids=None, start_at=None,
                 end_at=None, symbol=None, limit=DEFAULT_LIMIT, apply=False,
                 retire_unconfirmed=False):
    """Inspect, dry-run, and optionally apply only unambiguous convention work."""
    inspection = inspect_scope(
        user_id, character_id, event_ids=event_ids, start_at=start_at,
        end_at=end_at, symbol=symbol, limit=limit)
    plans = plan_backfill(user_id, character_id, inspection['primary_events'])
    validations = validate_existing_conventions(
        user_id, character_id, inspection['conventions'])
    report = {
        'mode': 'apply' if apply else 'dry_run',
        'inspection': inspection,
        'plans': plans,
        'existing_validation': validations,
        'applied': [],
        'retirement': retire_unsupported_conventions(
            user_id, character_id, validations, dry_run=True),
    }
    if not apply:
        return report

    report['applied'] = [
        apply_plan_item(user_id, character_id, plan) for plan in plans
    ]
    if retire_unconfirmed:
        report['retirement'] = retire_unsupported_conventions(
            user_id, character_id, validations, dry_run=False)
        report['retirement_validation'] = report['retirement'].get('validation', [])
    report['final_conventions'] = list_convention_memories(user_id, character_id)
    return report


def main():
    parser = argparse.ArgumentParser(
        description='Read and repair bounded historical communication conventions')
    parser.add_argument('--user-id', required=True)
    parser.add_argument('--character-id', required=True)
    parser.add_argument('--event-id', action='append', default=[])
    parser.add_argument('--from', dest='start_at')
    parser.add_argument('--to', dest='end_at')
    parser.add_argument('--symbol', help='optional audit filter only; never an authorization shortcut')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    parser.add_argument('--apply', action='store_true',
                        help='apply only candidates that passed this run\'s dry-run gate')
    parser.add_argument('--retire-unconfirmed', action='store_true',
                        help='with --apply, retire only if complete canonical provenance proves no authorization; legacy links alone remain ambiguous')
    args = parser.parse_args()
    try:
        _require_scope(args.event_id, args.start_at, args.end_at)
    except ValueError as exc:
        parser.error(str(exc))
    if args.retire_unconfirmed and not args.apply:
        parser.error('--retire-unconfirmed requires --apply')

    report = run_backfill(
        args.user_id, args.character_id, event_ids=args.event_id,
        start_at=args.start_at, end_at=args.end_at, symbol=args.symbol,
        limit=args.limit, apply=args.apply,
        retire_unconfirmed=args.retire_unconfirmed,
    )
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2))


if __name__ == '__main__':
    main()
