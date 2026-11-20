"""Fast thinking chain.

This is not the cognitive Fast Loop. It writes an observation of what the
character understood about one candidate-producer batch. It does not write
long_memory, bond_memory, cognitive tables, or sticky notes, and nothing in
chat reads it.

The model sees short labels. Code maps those labels back to real ids and
drops a single bad item without dropping the rest of the batch. The model
call itself holds no database transaction. The per-user advisory lock stays
so two workers cannot write the same user at once; the budget lock is
released before the call.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging

from candidate_producer import (
    SGT,
    _failure_delay,
    _lookback_cutoff,
    _processor as _candidate_processor,
    _receipt_version,
)
from raw_events import SourceValidityError, any_source_inactive, get_thinking_window

_LOG = logging.getLogger(__name__)

MODULE_VERSION = '0.1'
MAX_ITEMS = 3
MAX_TOKENS = 2500
MAX_STEPS = 5
MAX_STEP_CHARS = 300
MAX_NOTE_CHARS = 100
LONG_MEMORY_TOP_K = 20
EPISODE_TOP_K = 6
BATCH_FAIL_LIMIT = 3
CONTROL_PROCESSOR = 'thinking_fast_control'
CONTROL_VERSION = 'control_v1'
_BATCH_PREFIX = 'thinking-fast:batch:'
_BUDGET_ID = 'thinking-fast:global-budget'
_ITEM_FIELDS = frozenset({
    'sources', 'context', 'steps', 'experiences', 'views', 'view_proposals',
})
_STEP_FIELDS = frozenset({'step', 'text'})
_PROPOSAL_FIELDS = frozenset({'view', 'direction', 'note'})
_DIRECTIONS = frozenset({'加强', '减弱', '新建', '没变'})


class BatchOutputError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _SourcesInactive(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _processor(user_id, character_id):
    return f'thinking_fast:{len(user_id)}:{user_id}:{character_id}'


def _control_id(user_id):
    digest = hashlib.sha256(str(user_id).encode('utf-8')).hexdigest()
    return 'thinking-fast:user:' + digest


def _lock_key(name):
    return int.from_bytes(hashlib.sha256(name.encode('utf-8')).digest()[:8], 'big', signed=True)


def _emit(record):
    print(json.dumps(dict(log_type='thinking_fast', **record),
                     ensure_ascii=False, separators=(',', ':')), flush=True)


def _prompt():
    from config import THINKING_PROMPT_VERSION
    from thinking_chain import PROMPTS
    version = THINKING_PROMPT_VERSION
    text = PROMPTS.get(version) if isinstance(version, str) else None
    if not isinstance(text, str):
        _LOG.error('thinking prompt version is not registered: %s', version)
        return None, None
    return version, text


def _load_control(cur, event_id):
    cur.execute('''SELECT result_ref FROM event_processing_state
        WHERE event_id=%s AND processor_type=%s AND processor_version=%s''',
        (event_id, CONTROL_PROCESSOR, CONTROL_VERSION))
    row = cur.fetchone()
    state = json.loads(row[0]) if row and row[0] else {}
    if not isinstance(state, dict):
        raise ValueError('invalid_control_state')
    return state


def _save_control(cur, event_id, state):
    cur.execute('''INSERT INTO event_processing_state
        (event_id,processor_type,processor_version,status,result_ref,retry_count)
        VALUES (%s,%s,%s,'control',%s,%s)
        ON CONFLICT (event_id,processor_type,processor_version) DO UPDATE SET
          result_ref=EXCLUDED.result_ref, retry_count=EXCLUDED.retry_count,
          updated_at=CURRENT_TIMESTAMP''',
        (event_id, CONTROL_PROCESSOR, CONTROL_VERSION,
         json.dumps(state, ensure_ascii=False), int(state.get('fail_count') or 0)))


def _json_ready(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _insert_audit(cur, *, user_id, character_id, batch_ref, kind, reason,
                  detail, prompt_version):
    """Observation only. A failed write must not change the batch result."""
    try:
        payload = json.dumps(
            _json_ready(detail if isinstance(detail, (dict, list)) else {}),
            ensure_ascii=False, default=str)
    except Exception as exc:
        print(f'[thinking_audit] write skipped:{exc}')
        return
    try:
        cur.execute('SAVEPOINT thinking_audit')
        cur.execute('''INSERT INTO thinking_audit_log
            (user_id,character_id,batch_ref,kind,reason,detail,prompt_version)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)''',
            (user_id, character_id, batch_ref, kind, reason, payload, prompt_version))
        cur.execute('RELEASE SAVEPOINT thinking_audit')
    except Exception as exc:
        print(f'[thinking_audit] write skipped:{exc}')
        try:
            cur.execute('ROLLBACK TO SAVEPOINT thinking_audit')
            cur.execute('RELEASE SAVEPOINT thinking_audit')
        except Exception as recover:
            print(f'[thinking_audit] savepoint recovery skipped:{recover}')


def _mark_batch(cur, user_id, character_id, batch_ref, status, last_error, now):
    cur.execute('''INSERT INTO event_processing_state
        (event_id,processor_type,processor_version,status,result_ref,last_error,
         consumed_at,retry_count)
        VALUES (%s,%s,%s,%s,%s,%s,%s,0)
        ON CONFLICT (event_id,processor_type,processor_version) DO UPDATE SET
          status=EXCLUDED.status, result_ref=EXCLUDED.result_ref,
          last_error=EXCLUDED.last_error, consumed_at=EXCLUDED.consumed_at,
          updated_at=CURRENT_TIMESTAMP''',
        (_BATCH_PREFIX + batch_ref, _processor(user_id, character_id), MODULE_VERSION,
         status, batch_ref, last_error, now))


def _pending_batches(cur, user_id, character_id, now):
    """Candidate receipts this pair has not thought about, oldest first."""
    from config import CANDIDATE_PRODUCER_SHADOW
    version = _receipt_version(CANDIDATE_PRODUCER_SHADOW)
    cutoff = _lookback_cutoff(now)
    cur.execute('''SELECT result_ref, event_id, consumed_at
        FROM event_processing_state
        WHERE processor_type=%s AND processor_version=%s
          AND status='succeeded' AND result_ref IS NOT NULL
          AND (%s IS NULL OR consumed_at >= %s)
        ORDER BY consumed_at ASC NULLS LAST, event_id ASC''',
        (_candidate_processor(user_id, character_id), version, cutoff, cutoff))
    groups = {}
    order = []
    for result_ref, event_id, consumed_at in cur.fetchall():
        if result_ref not in groups:
            groups[result_ref] = {
                'batch_ref': result_ref, 'event_ids': [], 'consumed_at': consumed_at,
            }
            order.append(result_ref)
        groups[result_ref]['event_ids'].append(event_id)
    if not order:
        return []
    cur.execute('''SELECT event_id FROM event_processing_state
        WHERE processor_type=%s AND processor_version=%s
          AND status IN ('succeeded','skipped')
          AND event_id LIKE %s''',
        (_processor(user_id, character_id), MODULE_VERSION, _BATCH_PREFIX + '%'))
    done = set()
    for (event_id,) in cur.fetchall():
        if isinstance(event_id, str) and event_id.startswith(_BATCH_PREFIX):
            done.add(event_id[len(_BATCH_PREFIX):])
    return [groups[ref] for ref in order if ref not in done]


def _pairs_with_receipts(cur, now):
    """Private chats whose candidate receipts may still need a fast pass.

    character_id is chat_id. The candidate processor string is not rebuilt
    here; a pair is kept only when _processor confirms a receipt.
    """
    from config import CANDIDATE_PRODUCER_SHADOW
    version = _receipt_version(CANDIDATE_PRODUCER_SHADOW)
    cutoff = _lookback_cutoff(now)
    cur.execute('''SELECT DISTINCT c.user_id, c.chat_id
        FROM event_processing_state p
        JOIN chat_log c
          ON COALESCE(NULLIF(c.event_id, ''), c.client_msg_id) = p.event_id
        WHERE p.status='succeeded' AND p.result_ref IS NOT NULL
          AND p.processor_version=%s
          AND (%s IS NULL OR p.consumed_at >= %s)
        ORDER BY c.user_id, c.chat_id''',
        (version, cutoff, cutoff))
    pairs = []
    seen = set()
    for user_id, chat_id in cur.fetchall():
        key = (user_id, chat_id)
        if not user_id or not chat_id or key in seen:
            continue
        cur.execute('''SELECT 1 FROM event_processing_state
            WHERE processor_type=%s AND processor_version=%s
              AND status='succeeded' AND result_ref IS NOT NULL
            LIMIT 1''', (_candidate_processor(user_id, chat_id), version))
        if cur.fetchone():
            seen.add(key)
            pairs.append(key)
    return pairs


def _clock(value):
    if not isinstance(value, datetime):
        return ''
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(SGT)
    return f'{local.month:02d}-{local.day:02d} {local.hour:02d}:{local.minute:02d}'


def _marker(section, row, source_ids):
    if section == 'before':
        return ''
    if section == 'after':
        return '【回复】'
    if row.get('role') != 'user' or row.get('event_id') in source_ids:
        return '【本批】'
    return ''


def _active_source_ids(conn, user_id, character_id, event_ids):
    """Receipt ids that are still active user rows, oldest first."""
    if not event_ids:
        raise _SourcesInactive()
    cur = conn.cursor()
    try:
        placeholders = ','.join(['%s'] * len(event_ids))
        cur.execute(
            f'''SELECT COALESCE(NULLIF(event_id, ''), client_msg_id)
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND COALESCE(status, 'active')='active'
                 AND role='user'
                 AND COALESCE(NULLIF(event_id, ''), client_msg_id) IN ({placeholders})
               ORDER BY created_at ASC, id ASC''',
            (user_id, character_id, *event_ids))
        found = []
        for (event_id,) in cur.fetchall():
            if event_id and event_id not in found:
                if any_source_inactive([event_id], user_id, character_id, conn=conn):
                    continue
                found.append(event_id)
    finally:
        cur.close()
    if not found:
        raise _SourcesInactive()
    return found


def _assemble(conn, user_id, character_id, event_ids, prompt_version):
    source_ids = _active_source_ids(conn, user_id, character_id, event_ids)
    window = get_thinking_window(
        user_id, character_id, source_ids[0], source_ids[-1], conn=conn)
    from db_chatlog import _history_content
    number = 1
    batch_user_labels = {}
    context_labels = {}
    sections = {'before': [], 'current': []}
    for section_name, rows in (
            ('before', window.get('before') or []),
            ('batch', window.get('batch') or []),
            ('after', window.get('after') or [])):
        bucket = 'before' if section_name == 'before' else 'current'
        for row in rows:
            if not isinstance(row, dict):
                continue
            body = _history_content(
                row.get('role'), row.get('text') or '', row.get('subtitle') or '',
                row.get('kind') or 'text', row.get('extra') if isinstance(row.get('extra'), dict) else {})
            if not body:
                continue
            label = f'R{number}'
            number += 1
            mark = _marker(section_name, row, set(source_ids))
            who = '她' if row.get('role') == 'user' else '我'
            sections[bucket].append(
                f'[{label}]{mark}{who}（{_clock(row.get("created_at"))}）：{body}')
            if mark == '【本批】' and row.get('role') == 'user' and row.get('event_id') in set(source_ids):
                batch_user_labels[label] = row.get('event_id')
            elif mark != '【本批】':
                context_labels[label] = row.get('event_id')
    if not batch_user_labels:
        raise _SourcesInactive()

    query = _source_query(window, source_ids)
    experiences, experience_labels = _experiences(user_id, character_id, query)
    views, view_labels = _views(conn, user_id, character_id)
    from thinking_chain import character_name, render_system
    system = render_system(prompt_version, character_name(character_id))
    if system is None:
        raise BatchOutputError('unknown_prompt')
    user_text = '\n'.join((
        '【之前的对话】\n' + ('\n'.join(sections['before']) or '（暂无）'),
        '【这次的对话】\n' + ('\n'.join(sections['current']) or '（暂无）'),
        '【可能相关的经历】\n' + (experiences or '（暂无）'),
        '【你现在的看法】\n' + (views or '（暂无）'),
    ))
    return {
        'system': system,
        'user': user_text,
        'batch_user_labels': batch_user_labels,
        'context_labels': context_labels,
        'experience_labels': experience_labels,
        'view_labels': view_labels,
    }


def _source_query(window, source_ids):
    wanted = set(source_ids)
    parts = []
    for row in window.get('batch') or []:
        if not isinstance(row, dict) or row.get('event_id') not in wanted or row.get('role') != 'user':
            continue
        text = row.get('text') if isinstance(row.get('text'), str) else ''
        if text.strip():
            parts.append(text.strip())
    return '\n'.join(parts)


def _experiences(user_id, character_id, query):
    lines = []
    labels = {}
    try:
        from memory_search import latest_long_memory_with_ids, search_long_memory
        found = search_long_memory(
            user_id, character_id, 'shared', query, top_k=LONG_MEMORY_TOP_K, with_ids=True)
    except Exception as exc:
        _LOG.warning('thinking_fast long memory skipped: %s', type(exc).__name__)
        found = None
    if found is None:
        try:
            from memory_search import latest_long_memory_with_ids
            found = latest_long_memory_with_ids(
                user_id, character_id, 'shared', limit=LONG_MEMORY_TOP_K)
        except Exception as exc:
            _LOG.warning('thinking_fast latest memory skipped: %s', type(exc).__name__)
            found = []
    for row in list(found or [])[:LONG_MEMORY_TOP_K]:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        memory_id, content = row[0], row[1]
        label = f'L{memory_id}'
        labels[label] = {'kind': 'long_memory', 'id': memory_id}
        lines.append(f'[{label}] {content}')
    try:
        from episodic_index import recall_episodes
        episodes = recall_episodes(
            user_id, character_id, query, limit=EPISODE_TOP_K) or []
    except Exception as exc:
        _LOG.warning('thinking_fast episode recall skipped: %s', type(exc).__name__)
        episodes = []
    shown = 0
    for episode in episodes:
        if shown >= EPISODE_TOP_K or not isinstance(episode, dict):
            continue
        episode_id = episode.get('id')
        if episode_id is None:
            episode_id = episode.get('episode_id')
        if episode_id is None:
            continue
        shown += 1
        label = f'E{shown}'
        labels[label] = {'kind': 'episode', 'id': episode_id}
        lines.append(f'[{label}] {episode.get("content") or ""}')
    return '\n'.join(lines), labels


def _views(conn, user_id, character_id):
    cur = conn.cursor()
    try:
        cur.execute('''SELECT id, about, statement, strength
            FROM character_views
            WHERE user_id=%s AND character_id=%s AND status='active'
            ORDER BY id ASC''', (user_id, character_id))
        rows = cur.fetchall()
    finally:
        cur.close()
    lines = []
    labels = {}
    for view_id, about, statement, strength in rows:
        label = f'V{view_id}'
        labels[label] = view_id
        lines.append(f'[{label}]（关于：{about}，强度 {strength}）{statement}')
    return '\n'.join(lines), labels


def _validate_shape(value):
    """Root must be an array. A bad element is one item, not a failed batch."""
    if not isinstance(value, list):
        raise ValueError('root_not_array')
    return value


def _item_reason(item, maps):
    if not isinstance(item, dict) or set(item) - _ITEM_FIELDS:
        return 'invalid_item_fields'
    sources = item.get('sources')
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(tag, str) or tag not in maps['batch_user_labels']
                   for tag in sources)):
        return 'source_not_in_batch'
    context = item.get('context') or []
    if (not isinstance(context, list)
            or any(not isinstance(tag, str) or tag not in maps['context_labels']
                   for tag in context)):
        return 'context_not_in_window'
    experiences = item.get('experiences') or []
    if (not isinstance(experiences, list)
            or any(not isinstance(tag, str) or tag not in maps['experience_labels']
                   for tag in experiences)):
        return 'experience_not_given'
    views = item.get('views') or []
    raw_proposals = item.get('view_proposals') or []
    if not isinstance(views, list) or any(
            not isinstance(tag, str) or tag not in maps['view_labels'] for tag in views):
        return 'view_not_given'
    if isinstance(raw_proposals, list):
        for proposal in raw_proposals:
            if not isinstance(proposal, dict):
                continue
            view = proposal.get('view', None)
            if view is not None and (not isinstance(view, str) or view not in maps['view_labels']):
                return 'view_not_given'
    steps = item.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return 'invalid_steps'
    for step in steps:
        if (not isinstance(step, dict) or set(step) != _STEP_FIELDS
                or not isinstance(step.get('step'), str) or not step.get('step')
                or not isinstance(step.get('text'), str) or not step.get('text')
                or len(step['text']) > MAX_STEP_CHARS):
            return 'invalid_steps'
    if not isinstance(raw_proposals, list) or len(raw_proposals) > 3:
        return 'invalid_view_proposal'
    for proposal in raw_proposals:
        if not isinstance(proposal, dict) or set(proposal) - _PROPOSAL_FIELDS:
            return 'invalid_view_proposal'
        if proposal.get('direction') not in _DIRECTIONS:
            return 'invalid_view_proposal'
        note = proposal.get('note', '')
        if not isinstance(note, str) or len(note) > MAX_NOTE_CHARS:
            return 'invalid_view_proposal'
    return None


def _mapped_item(item, maps):
    proposals = []
    for proposal in item.get('view_proposals') or []:
        view = proposal.get('view', None)
        proposals.append({
            'view': None if view is None else maps['view_labels'][view],
            'direction': proposal.get('direction'),
            'note': proposal.get('note') or '',
        })
    return {
        'source_event_ids': [maps['batch_user_labels'][tag] for tag in item['sources']],
        'context_refs': [maps['context_labels'][tag] for tag in (item.get('context') or [])],
        'steps': item['steps'],
        'cited_experiences': [maps['experience_labels'][tag] for tag in (item.get('experiences') or [])],
        'cited_views': [maps['view_labels'][tag] for tag in (item.get('views') or [])],
        'view_proposals': proposals,
    }


def _output_code(parsed, stop_reason):
    code = getattr(parsed, 'error_code', None)
    if stop_reason in {'max_tokens', 'length'} or code == 'truncated_response':
        return 'truncated_response'
    if stop_reason not in {'end_turn', 'stop'} or code == 'model_refused':
        return 'incomplete_response'
    return 'invalid_json'


def _persist_success(cur, user_id, character_id, batch_ref, items, maps,
                     prompt_version, model, now, control_id, state):
    reasons = Counter()
    accepted = 0
    for index, item in enumerate(items):
        if index >= MAX_ITEMS:
            _insert_audit(cur, user_id=user_id, character_id=character_id,
                          batch_ref=batch_ref, kind='item_rejected',
                          reason='too_many_items', detail={'item': item},
                          prompt_version=prompt_version)
            reasons['too_many_items'] += 1
            continue
        reason = _item_reason(item, maps)
        if reason:
            _insert_audit(cur, user_id=user_id, character_id=character_id,
                          batch_ref=batch_ref, kind='item_rejected', reason=reason,
                          detail={'item': item}, prompt_version=prompt_version)
            reasons[reason] += 1
            continue
        mapped = _mapped_item(item, maps)
        cur.execute('''INSERT INTO thinking_understandings
            (user_id,character_id,batch_ref,item_index,source_event_ids,context_refs,
             steps,cited_experiences,cited_views,view_proposals,prompt_version,model)
            VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,
                    %s::jsonb,%s,%s)
            ON CONFLICT (user_id,character_id,batch_ref,item_index) DO NOTHING''',
            (user_id, character_id, batch_ref, index,
             json.dumps(mapped['source_event_ids'], ensure_ascii=False),
             json.dumps(mapped['context_refs'], ensure_ascii=False),
             json.dumps(mapped['steps'], ensure_ascii=False),
             json.dumps(mapped['cited_experiences'], ensure_ascii=False, default=str),
             json.dumps(mapped['cited_views'], ensure_ascii=False, default=str),
             json.dumps(mapped['view_proposals'], ensure_ascii=False, default=str),
             prompt_version, model))
        accepted += 1
    _mark_batch(cur, user_id, character_id, batch_ref, 'succeeded', None, now)
    state.update(fail_count=0, retry_at=None, fail_batch=None)
    _save_control(cur, control_id, state)
    return accepted, reasons


def _summary(user_id, character_id, batch_ref, status, produced, accepted, reasons, version):
    return dict(
        user_id=user_id, character_id=character_id, batch_ref=batch_ref,
        status=status, items_produced=produced, items_accepted=accepted,
        reasons=dict(reasons), prompt_version=version)


def _skip_inactive(cur, user_id, character_id, batch_ref, now, control_id, state,
                   version, emit):
    _mark_batch(cur, user_id, character_id, batch_ref, 'skipped', 'sources_inactive', now)
    state.update(fail_count=0, retry_at=None, fail_batch=None)
    _save_control(cur, control_id, state)
    record = _summary(user_id, character_id, batch_ref, 'skipped', 0, 0,
                      {'sources_inactive': 1}, version)
    emit(record)
    return record


def _record_failure(cur, user_id, character_id, batch_ref, code, now, control_id,
                    state, version, emit):
    _insert_audit(cur, user_id=user_id, character_id=character_id, batch_ref=batch_ref,
                  kind='batch_failed', reason=code, detail={'error': code},
                  prompt_version=version)
    state['fail_batch'] = batch_ref
    if int(state.get('fail_count') or 0) >= BATCH_FAIL_LIMIT:
        _mark_batch(cur, user_id, character_id, batch_ref, 'skipped', code, now)
        state.update(fail_count=0, retry_at=None, fail_batch=None)
    _save_control(cur, control_id, state)
    record = _summary(user_id, character_id, batch_ref, 'failed', 0, 0, {code: 1}, version)
    emit(record)
    return record


def run_pair(user_id, character_id, *, now=None, create_chat_fn=None, conn=None,
             emit=None, enabled=None):
    """Think about at most one candidate batch for this pair.

    The injected connection must be idle. A session advisory lock serializes
    the user. The reservation is committed before the model call, and that
    call runs with no open transaction.
    """
    if enabled is None:
        from config import THINKING_FAST_ENABLED
        enabled = THINKING_FAST_ENABLED
    if not enabled:
        return {'status': 'disabled'}
    version, _prompt_text = _prompt()
    if _prompt_text is None:
        return {'status': 'unknown_prompt'}
    from config import MODEL_MAIN, THINKING_FAST_DAILY_LIMIT
    from db import get_conn

    current = now or _now()
    emit = emit or _emit
    database = conn if conn is not None else get_conn()
    close_connection = conn is None
    cur = database.cursor()
    control_id = _control_id(user_id)
    lock_key = _lock_key(control_id)
    locked = False
    reserved = False
    completed = False
    batch_ref = None
    state = None
    try:
        cur.execute('SELECT pg_try_advisory_lock(%s)', (lock_key,))
        locked = bool(cur.fetchone()[0])
        if not locked:
            database.rollback()
            return {'status': 'busy'}
        state = _load_control(cur, control_id)
        retry_at = state.get('retry_at')
        if retry_at:
            retry_dt = datetime.fromisoformat(retry_at)
            if retry_dt.tzinfo is None:
                retry_dt = retry_dt.replace(tzinfo=timezone.utc)
            if current < retry_dt:
                database.rollback()
                return {'status': 'cooldown', 'retry_at': retry_at}
        pending = _pending_batches(cur, user_id, character_id, current)
        if not pending:
            database.rollback()
            return {'status': 'idle'}
        batch = pending[0]
        batch_ref = batch['batch_ref']
        cur.execute('SELECT pg_advisory_xact_lock(%s)', (_lock_key(_BUDGET_ID),))
        budget = _load_control(cur, _BUDGET_ID)
        day = current.astimezone(SGT).date().isoformat()
        if budget.get('day') != day:
            budget = {'day': day, 'batches': 0}
        if int(budget.get('batches') or 0) >= THINKING_FAST_DAILY_LIMIT:
            database.rollback()
            record = _summary(user_id, character_id, batch_ref, 'daily_limit', 0, 0, {}, version)
            emit(record)
            return record
        budget['batches'] = int(budget.get('batches') or 0) + 1
        if state.get('fail_batch') not in (None, batch_ref):
            state['fail_count'] = 0
        state['fail_count'] = int(state.get('fail_count') or 0) + 1
        state['fail_batch'] = batch_ref
        state['retry_at'] = (current + timedelta(
            seconds=_failure_delay(state['fail_count']))).isoformat()
        _save_control(cur, _BUDGET_ID, budget)
        _save_control(cur, control_id, state)
        database.commit()
        reserved = True

        try:
            assembled = _assemble(database, user_id, character_id, batch['event_ids'], version)
        except (SourceValidityError, _SourcesInactive):
            try:
                database.rollback()
            except Exception:
                pass
            record = _skip_inactive(
                cur, user_id, character_id, batch_ref, current, control_id, state, version, emit)
            database.commit()
            completed = True
            return record
        # The read transaction ends here. The advisory lock stays.
        database.rollback()

        chat_fn = create_chat_fn
        if chat_fn is None:
            from ai_client import create_chat as chat_fn
        from structured_output import invoke_structured_llm
        call = invoke_structured_llm(
            domain='thinking_fast',
            create_chat_fn=lambda **kw: chat_fn(**kw, max_retries=0),
            model=MODEL_MAIN, messages=[{'role': 'user', 'content': assembled['user']}],
            system=assembled['system'], max_tokens=MAX_TOKENS,
            schema_validator=_validate_shape, schema_name='thinking_fast_batch',
            root_type='array')
        usage = call.usage or {}
        parsed = call.parsed
        stop_reason = usage.get('stop_reason') or usage.get('finish_reason')
        if not parsed.ok or stop_reason not in {'end_turn', 'stop'}:
            raise BatchOutputError(_output_code(parsed, stop_reason))
        items = parsed.value if isinstance(parsed.value, list) else None
        if items is None:
            raise BatchOutputError('invalid_json')
        accepted, reasons = _persist_success(
            cur, user_id, character_id, batch_ref, items, assembled, version,
            MODEL_MAIN, current, control_id, state)
        database.commit()
        completed = True
        record = _summary(user_id, character_id, batch_ref, 'succeeded',
                          len(items), accepted, reasons, version)
        try:
            emit(record)
        except Exception:
            _LOG.warning('thinking_fast emit skipped')
        return record
    except Exception as exc:
        try:
            database.rollback()
        except Exception:
            pass
        if reserved and not completed and batch_ref is not None and state is not None:
            code = exc.code if isinstance(exc, BatchOutputError) else type(exc).__name__
            try:
                record = _record_failure(
                    cur, user_id, character_id, batch_ref, code, current,
                    control_id, state, version, emit)
                database.commit()
                return record
            except Exception:
                try:
                    database.rollback()
                except Exception:
                    pass
        if completed:
            return {'status': 'failed', 'batch_ref': batch_ref}
        raise
    finally:
        try:
            if locked:
                cur.execute('SELECT pg_advisory_unlock(%s)', (lock_key,))
                database.rollback()
        except Exception as exc:
            _LOG.warning('thinking_fast unlock skipped: %s', type(exc).__name__)
        try:
            cur.close()
        finally:
            if close_connection:
                database.close()


def run_worker_once(*, enabled=None, now=None, create_chat_fn=None, emit=None):
    if enabled is None:
        from config import THINKING_FAST_ENABLED
        enabled = THINKING_FAST_ENABLED
    if not enabled:
        return []
    version, _prompt_text = _prompt()
    if _prompt_text is None:
        return []
    from db import get_conn

    current = now or _now()
    database = get_conn()
    cur = database.cursor()
    try:
        pairs = _pairs_with_receipts(cur, current)
    finally:
        cur.close()
        try:
            database.rollback()
        finally:
            database.close()
    results = []
    for user_id, character_id in pairs:
        try:
            result = run_pair(
                user_id, character_id, now=current, create_chat_fn=create_chat_fn,
                emit=emit, enabled=True)
            results.append(result)
            if isinstance(result, dict) and result.get('status') == 'daily_limit':
                break
        except Exception as exc:
            _LOG.exception('thinking_fast pair failed')
            (emit or _emit)(dict(
                kind='worker_failure', ts=_now().isoformat(), user_id=user_id,
                character_id=character_id, module_version=MODULE_VERSION,
                stage='worker', status='failed', reason=type(exc).__name__,
                prompt_version=version))
    return results
