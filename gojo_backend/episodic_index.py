"""Derived episodic-memory index backed by canonical Raw Events.

Episodes are intentionally not a second fact store.  Each row is a rebuildable
index entry whose only provenance is the ordered set of ``chat_log`` event ids
that produced it.  Chat recall may read an episode only after fresh canonical
source validation.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from typing import Dict, Sequence, Tuple

from db import get_conn


EPISODE_PROCESSOR_VERSION = 'episodic_index_v1'
KIND = 'episodic_index'
EPISODE_STATUSES = ('active', 'stale', 'invalidated', 'superseded')
# Episode recall only ranks a bounded recent pool.  The index remains the
# derived record; this limit is not a retention policy for Raw Events.
EPISODE_RECALL_CANDIDATE_LIMIT = 160
EPISODE_RECALL_TOP_K = 3

EPISODIC_INDEX_DDL = (
    '''CREATE TABLE IF NOT EXISTS episodic_memory_index (
        episode_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        title TEXT,
        what_happened TEXT NOT NULL,
        outcome TEXT,
        unresolved TEXT,
        participants TEXT NOT NULL DEFAULT '[]',
        source_event_ids TEXT NOT NULL,
        source_key TEXT NOT NULL,
        range_start TIMESTAMPTZ,
        range_end TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'active',
        rebuild_required BOOLEAN NOT NULL DEFAULT FALSE,
        processor_version TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        superseded_by TEXT,
        retrieval_text TEXT,
        embedding_metadata TEXT,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    )''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS idx_episodic_index_source_version
       ON episodic_memory_index
          (user_id, character_id, source_key, processor_version, version)''',
    '''CREATE INDEX IF NOT EXISTS idx_episodic_index_user_char_status
       ON episodic_memory_index
          (user_id, character_id, status, range_end DESC)''',
)

_memory_lock = threading.RLock()
_USE_MEMORY_STORE = False
_EPISODES: Dict[str, dict] = {}
_EPISODE_JOBS = []


def use_memory_store(enabled=True):
    """Tests only: keep the derived index and queued jobs in process memory."""
    global _USE_MEMORY_STORE
    _USE_MEMORY_STORE = bool(enabled)
    if enabled:
        reset_memory_store()


def reset_memory_store():
    with _memory_lock:
        _EPISODES.clear()
        _EPISODE_JOBS.clear()


def _uses_memory_store() -> bool:
    if _USE_MEMORY_STORE:
        return True
    try:
        from context_layer import _USE_MEMORY_STORE as context_memory_store
        return bool(context_memory_store)
    except Exception:
        return False


def init_episodic_index_tables():
    conn = get_conn()
    cur = conn.cursor()
    try:
        for stmt in EPISODIC_INDEX_DDL:
            cur.execute(stmt)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def _now():
    return datetime.now(timezone.utc)


def _json_values(value) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        text = str(value).strip()
        if not text:
            return ()
        try:
            parsed = json.loads(text)
            raw = parsed if isinstance(parsed, list) else [text]
        except Exception:
            raw = [text]
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _json_dict(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _source_ids(value) -> Tuple[str, ...]:
    seen = set()
    ids = []
    for item in _json_values(value):
        if item not in seen:
            seen.add(item)
            ids.append(item)
    return tuple(ids)


def _dump_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _iso(value) -> str:
    if value is None:
        return '-'
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def segment_key(source_event_ids: Sequence[str],
                processor_version=EPISODE_PROCESSOR_VERSION) -> str:
    """Stable idempotency key for a set of canonical source events."""
    ids = sorted(_source_ids(source_event_ids))
    if not ids:
        return ''
    raw = f'{processor_version}|' + '|'.join(ids)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _episode_id(user_id, character_id, source_key, processor_version, version) -> str:
    raw = '|'.join((
        str(user_id or ''), str(character_id or ''), str(source_key or ''),
        str(processor_version or ''), str(version or 1),
    ))
    return 'episode:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:40]


def _trace_episode(item=None, *, source_count=0, range_start=None,
                   range_end=None, status='unknown', version='-'):
    item = item or {}
    episode_id = item.get('episode_id') or '-'
    count = len(item.get('source_event_ids') or ()) if item else source_count
    start = item.get('range_start') if item else range_start
    end = item.get('range_end') if item else range_end
    current_status = item.get('status') if item else status
    current_version = item.get('version') if item else version
    print(
        '[episode_trace] '
        f'id={episode_id} source_count={count} '
        f'range={_iso(start)}..{_iso(end)} '
        f'status={current_status} version={current_version}'
    )


def _row_episode(row) -> dict:
    return {
        'episode_id': row[0],
        'user_id': row[1],
        'character_id': row[2],
        'title': row[3] or '',
        'what_happened': row[4] or '',
        'outcome': row[5] or '',
        'unresolved': row[6] or '',
        'participants': _json_values(row[7]),
        'source_event_ids': _source_ids(row[8]),
        'source_key': row[9] or '',
        'range_start': row[10],
        'range_end': row[11],
        'status': row[12] or 'active',
        'rebuild_required': bool(row[13]),
        'processor_version': row[14] or '',
        'version': int(row[15] or 1),
        'superseded_by': row[16],
        'retrieval_text': row[17] or '',
        'embedding_metadata': _json_dict(row[18]),
        'created_at': row[19],
        'updated_at': row[20],
    }


def _sort_time(value):
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _episode_payload(
    user_id,
    character_id,
    *,
    title='',
    what_happened='',
    outcome='',
    unresolved='',
    participants=(),
    source_event_ids=(),
    range_start=None,
    range_end=None,
    status='active',
    rebuild_required=False,
    processor_version=EPISODE_PROCESSOR_VERSION,
    version=1,
    superseded_by=None,
    retrieval_text=None,
    embedding_metadata=None,
    episode_id=None,
):
    ids = _source_ids(source_event_ids)
    if not ids:
        raise ValueError('episode requires canonical source_event_ids')
    if status not in EPISODE_STATUSES:
        raise ValueError('invalid episode status')
    source_key = segment_key(ids, processor_version)
    version = max(1, int(version or 1))
    what_happened = str(what_happened or '').strip()
    if not what_happened:
        what_happened = f'由 {len(ids)} 条可追溯 Raw Event 构成的对话经历。'
    title = str(title or '').strip() or f'对话经历（{len(ids)} 条事件）'
    people = _source_ids(participants)
    text_parts = [title, what_happened, str(outcome or '').strip(), str(unresolved or '').strip()]
    return {
        'episode_id': (episode_id or _episode_id(
            user_id, character_id, source_key, processor_version, version))[:120],
        'user_id': user_id,
        'character_id': character_id,
        'title': title,
        'what_happened': what_happened,
        'outcome': str(outcome or '').strip(),
        'unresolved': str(unresolved or '').strip(),
        'participants': people,
        'source_event_ids': ids,
        'source_key': source_key,
        'range_start': range_start,
        'range_end': range_end,
        'status': status,
        'rebuild_required': bool(rebuild_required),
        'processor_version': processor_version or EPISODE_PROCESSOR_VERSION,
        'version': version,
        'superseded_by': superseded_by,
        # Optional derived retrieval cache.  It never carries provenance or
        # authorizes a chat recall without fresh Raw Event validation.
        'retrieval_text': (retrieval_text or '\n'.join(p for p in text_parts if p)).strip(),
        'embedding_metadata': _json_dict(embedding_metadata),
        'created_at': _now(),
        'updated_at': _now(),
    }


def _copy_episode(item):
    copied = dict(item)
    copied['source_event_ids'] = tuple(item.get('source_event_ids') or ())
    copied['participants'] = tuple(item.get('participants') or ())
    copied['embedding_metadata'] = _json_dict(item.get('embedding_metadata'))
    return copied


def list_episodes(user_id, character_id, *, status='active', limit=50):
    limit = max(1, min(int(limit or 50), 1000))
    if _USE_MEMORY_STORE:
        with _memory_lock:
            rows = [
                _copy_episode(item) for item in _EPISODES.values()
                if item.get('user_id') == user_id
                and item.get('character_id') == character_id
                and (status is None or item.get('status') == status)
            ]
        rows.sort(
            key=lambda item: (_sort_time(item.get('range_end') or item.get('updated_at')),
                              item.get('episode_id') or ''),
            reverse=True,
        )
        return rows[:limit]

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT episode_id, user_id, character_id, title, what_happened,
                      outcome, unresolved, participants, source_event_ids,
                      source_key, range_start, range_end, status,
                      rebuild_required, processor_version, version,
                      superseded_by, retrieval_text, embedding_metadata,
                      created_at, updated_at
               FROM episodic_memory_index
               WHERE user_id=%s AND character_id=%s
                 AND (%s IS NULL OR status=%s)
               ORDER BY range_end DESC NULLS LAST, updated_at DESC
               LIMIT %s''',
            (user_id, character_id, status, status, limit),
        )
        return [_row_episode(row) for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def _episode_retrieval_text(item) -> str:
    """Build retrieval text only from documented episode fields.

    ``retrieval_text`` is a cacheable convenience field, not an authority.  By
    rebuilding this value here, a stale cache or metadata payload cannot add a
    hidden claim to a chat recall candidate.
    """
    parts = []
    for value in (
        (item or {}).get('title'),
        (item or {}).get('what_happened'),
        (item or {}).get('outcome'),
        (item or {}).get('unresolved'),
    ):
        text = str(value or '').strip()
        if text:
            parts.append(text)
    return '\n'.join(parts)


def _search_terms(value) -> set:
    """Return small, deterministic lexical terms for Chinese and word text."""
    text = str(value or '').lower()
    terms = set(re.findall(r'[a-z0-9_]{2,}', text))
    for chunk in re.findall(r'[\u4e00-\u9fff]{2,}', text):
        # Whole phrases preserve exact matches; short n-grams preserve useful
        # overlap when the same trip/event is phrased differently.
        terms.add(chunk)
        for width in (2, 3):
            for index in range(max(0, len(chunk) - width + 1)):
                terms.add(chunk[index:index + width])
    return terms


def _lexical_score(query, text) -> float:
    query_text = str(query or '').strip().lower()
    text_value = str(text or '').strip().lower()
    if not query_text or not text_value:
        return 0.0
    if query_text in text_value:
        return 1.0
    query_terms = _search_terms(query_text)
    text_terms = _search_terms(text_value)
    if not query_terms or not text_terms:
        return 0.0
    return min(1.0, len(query_terms & text_terms) / float(len(query_terms)))


def _normalised_vector(value):
    """Use the existing memory-search normalizer when it is available."""
    if value is None:
        return None
    try:
        import memory_search
        vector = memory_search._to_vec(value)
        if vector is not None:
            return vector
    except Exception:
        pass
    try:
        raw = list(value)
        if not raw:
            return None
        norm = sum(float(part) * float(part) for part in raw) ** 0.5
        if norm <= 0:
            return None
        return [float(part) / norm for part in raw]
    except Exception:
        return None


def _episode_vector(item):
    metadata = _json_dict((item or {}).get('embedding_metadata'))
    for key in ('vector', 'embedding', 'embedding_json'):
        if metadata.get(key) is not None:
            return _normalised_vector(metadata.get(key))
    return None


def _cosine_score(left, right) -> float:
    try:
        if left is None or right is None or len(left) != len(right):
            return 0.0
        dot = sum(float(a) * float(b) for a, b in zip(left, right))
        left_norm = sum(float(a) * float(a) for a in left) ** 0.5
        right_norm = sum(float(b) * float(b) for b in right) ** 0.5
        if left_norm <= 0 or right_norm <= 0:
            return 0.0
        return max(0.0, min(1.0, dot / (left_norm * right_norm)))
    except Exception:
        return 0.0


def _light_recency_score(value) -> float:
    if not isinstance(value, datetime):
        return 0.0
    stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (_now() - stamp).total_seconds() / 86400.0)
    return 0.03 / (1.0 + age_days / 30.0)


def _active_episode_rows(user_id, character_id, rows):
    """Keep only episodes whose complete provenance is active *right now*.

    One bounded tombstone read plus one canonical lookup validate all candidate
    source ids.  A lookup error intentionally returns no rows: unknown source
    validity is not active.
    """
    candidates = [dict(row) for row in (rows or []) if row]
    if not candidates:
        return []
    try:
        import raw_events
        deleted = {
            str(event_id).strip()
            for event_id in raw_events.deleted_event_ids(user_id, character_id)
            if str(event_id).strip()
        }
    except Exception as exc:
        print('[recall_trace] episode_source_validation status=unavailable '
              f'error={type(exc).__name__}')
        return []
    if deleted:
        candidates = [
            row for row in candidates
            if not (set(_source_ids(row.get('source_event_ids'))) & deleted)
        ]
    all_ids = _source_ids([
        event_id
        for row in candidates
        for event_id in _source_ids(row.get('source_event_ids'))
    ])
    if not candidates or not all_ids:
        return []
    try:
        canonical = raw_events.get_active_events_by_ids(
            user_id, character_id, all_ids)
    except Exception as exc:
        print('[recall_trace] episode_source_validation status=unavailable '
              f'error={type(exc).__name__}')
        return []
    active_ids = {
        str(row.get('event_id') or '').strip()
        for row in (canonical or []) if isinstance(row, dict)
    }
    return [
        row for row in candidates
        if _source_ids(row.get('source_event_ids'))
        and set(_source_ids(row.get('source_event_ids'))).issubset(active_ids)
    ]


def _trace_episode_recall(label, rows, *, vector_enabled=False):
    refs = []
    for row in list(rows or [])[:12]:
        try:
            score = f'{float(row.get("score") or 0):.3f}'
        except (TypeError, ValueError):
            score = 'n/a'
        refs.append(
            f'id={row.get("episode_id") or row.get("id") or "-"},'
            f'score={score},source_count={len(_source_ids(row.get("source_event_ids")))},'
            f'range={_iso(row.get("range_start"))}..{_iso(row.get("range_end"))}'
        )
    suffix = '' if len(rows or []) <= 12 else ',…'
    print(f'[recall_trace] {label} count={len(rows or [])} '
          f'vector_enabled={bool(vector_enabled)} items=[{";".join(refs)}{suffix}]')


def recall_episodes(
    user_id,
    character_id,
    user_message,
    query_embedding=None,
    *,
    limit=EPISODE_RECALL_TOP_K,
    candidate_limit=EPISODE_RECALL_CANDIDATE_LIMIT,
):
    """Read active, fully-valid episodes for the existing chat recall pipeline.

    This is intentionally a read-only retrieval function.  It never writes a
    Raw Event, a long/bond/lifecycle memory, relationship state, or an episode
    embedding.  Semantic scoring is used only when an already-stored episode
    vector exists; lexical recall remains the safe fallback.
    """
    try:
        pool_limit = max(100, min(int(candidate_limit or EPISODE_RECALL_CANDIDATE_LIMIT), 200))
        top_k = max(1, min(int(limit or EPISODE_RECALL_TOP_K), 6))
    except (TypeError, ValueError):
        pool_limit = EPISODE_RECALL_CANDIDATE_LIMIT
        top_k = EPISODE_RECALL_TOP_K

    try:
        rows = list_episodes(user_id, character_id, status='active', limit=pool_limit)
    except Exception as exc:
        print('[recall_trace] episode_candidates status=unavailable '
              f'error={type(exc).__name__}')
        return []
    candidates = [
        row for row in rows
        if not row.get('rebuild_required')
        and _source_ids(row.get('source_event_ids'))
    ]
    valid_rows = _active_episode_rows(user_id, character_id, candidates)
    print('[recall_trace] episode_candidates '
          f'count={len(candidates)} source_validated={len(valid_rows)} '
          f'vector_enabled={query_embedding is not None}')

    scored = []
    for row in valid_rows:
        retrieval_text = _episode_retrieval_text(row)
        if not retrieval_text:
            continue
        lexical = _lexical_score(user_message, retrieval_text)
        try:
            episode_vector = _episode_vector(row)
        except Exception:
            # A malformed optional embedding cannot make provenance-valid
            # lexical recall unavailable.
            episode_vector = None
        semantic = _cosine_score(query_embedding, episode_vector)
        # Recency only breaks close ties.  Relevance is always dominant, so an
        # older relevant episode cannot lose merely because a newer one exists.
        relevance = 0.66 * semantic + 0.30 * lexical
        if relevance <= 0:
            continue
        source_quality = min(0.01, 0.002 * len(_source_ids(row.get('source_event_ids'))))
        score = relevance + _light_recency_score(row.get('range_end')) + source_quality
        candidate = dict(row)
        candidate.update({
            'id': row.get('episode_id'),
            'content': retrieval_text,
            'score': score,
            'lexical_score': lexical,
            'semantic_score': semantic,
            'provenance_quality': 'linked',
            'recall_kind': 'episode',
        })
        scored.append(candidate)
    scored.sort(
        key=lambda row: (float(row.get('score') or 0), _sort_time(row.get('range_end'))),
        reverse=True,
    )

    # Validate again immediately before a result crosses the retrieval boundary.
    selected = _active_episode_rows(user_id, character_id, scored[:top_k])
    _trace_episode_recall('episode_ranked', selected,
                          vector_enabled=query_embedding is not None)
    return selected


def get_episode(episode_id):
    if not episode_id:
        return None
    if _USE_MEMORY_STORE:
        with _memory_lock:
            item = _EPISODES.get(episode_id)
            return _copy_episode(item) if item else None

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT episode_id, user_id, character_id, title, what_happened,
                      outcome, unresolved, participants, source_event_ids,
                      source_key, range_start, range_end, status,
                      rebuild_required, processor_version, version,
                      superseded_by, retrieval_text, embedding_metadata,
                      created_at, updated_at
               FROM episodic_memory_index WHERE episode_id=%s''',
            (episode_id,),
        )
        row = cur.fetchone()
        return _row_episode(row) if row else None
    finally:
        cur.close()
        conn.close()


def _existing_source_version(user_id, character_id, source_key,
                             processor_version, version):
    for item in list_episodes(user_id, character_id, status=None, limit=1000):
        if (item.get('source_key') == source_key
                and item.get('processor_version') == processor_version
                and int(item.get('version') or 1) == int(version or 1)):
            return item
    return None


def _existing_active_segment(user_id, character_id, source_key, processor_version):
    for item in list_episodes(user_id, character_id, status='active', limit=1000):
        if (item.get('source_key') == source_key
                and item.get('processor_version') == processor_version):
            return item
    return None


def save_episode(
    user_id,
    character_id,
    *,
    title='',
    what_happened='',
    outcome='',
    unresolved='',
    participants=(),
    source_event_ids=(),
    range_start=None,
    range_end=None,
    status='active',
    rebuild_required=False,
    processor_version=EPISODE_PROCESSOR_VERSION,
    version=1,
    superseded_by=None,
    retrieval_text=None,
    embedding_metadata=None,
    episode_id=None,
    supersedes_episode_id=None,
):
    """Deterministically upsert one derived episode.

    ``source_event_ids`` is mandatory even for non-active rows.  The builder
    below additionally verifies those ids against canonical Raw Events before
    it calls this storage primitive.
    """
    payload = _episode_payload(
        user_id, character_id,
        title=title,
        what_happened=what_happened,
        outcome=outcome,
        unresolved=unresolved,
        participants=participants,
        source_event_ids=source_event_ids,
        range_start=range_start,
        range_end=range_end,
        status=status,
        rebuild_required=rebuild_required,
        processor_version=processor_version,
        version=version,
        superseded_by=superseded_by,
        retrieval_text=retrieval_text,
        embedding_metadata=embedding_metadata,
        episode_id=episode_id,
    )

    if _USE_MEMORY_STORE:
        with _memory_lock:
            existing = _existing_source_version(
                user_id, character_id, payload['source_key'],
                payload['processor_version'], payload['version'])
            if existing:
                payload['episode_id'] = existing['episode_id']
                payload['created_at'] = existing.get('created_at') or payload['created_at']
            _EPISODES[payload['episode_id']] = payload
            if supersedes_episode_id:
                old = _EPISODES.get(supersedes_episode_id)
                if old and old.get('status') in ('active', 'stale'):
                    old['status'] = 'superseded'
                    old['rebuild_required'] = False
                    old['superseded_by'] = payload['episode_id']
                    old['updated_at'] = _now()
        _trace_episode(payload)
        return _copy_episode(payload)

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''INSERT INTO episodic_memory_index
               (episode_id, user_id, character_id, title, what_happened,
                outcome, unresolved, participants, source_event_ids, source_key,
                range_start, range_end, status, rebuild_required,
                processor_version, version, superseded_by, retrieval_text,
                embedding_metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (user_id, character_id, source_key, processor_version, version)
               DO UPDATE SET
                 title=EXCLUDED.title,
                 what_happened=EXCLUDED.what_happened,
                 outcome=EXCLUDED.outcome,
                 unresolved=EXCLUDED.unresolved,
                 participants=EXCLUDED.participants,
                 source_event_ids=EXCLUDED.source_event_ids,
                 range_start=EXCLUDED.range_start,
                 range_end=EXCLUDED.range_end,
                 status=EXCLUDED.status,
                 rebuild_required=EXCLUDED.rebuild_required,
                 superseded_by=EXCLUDED.superseded_by,
                 retrieval_text=EXCLUDED.retrieval_text,
                 embedding_metadata=EXCLUDED.embedding_metadata,
                 updated_at=CURRENT_TIMESTAMP
               RETURNING episode_id, created_at, updated_at''',
            (
                payload['episode_id'], user_id, character_id, payload['title'],
                payload['what_happened'], payload['outcome'], payload['unresolved'],
                _dump_json(list(payload['participants'])),
                _dump_json(list(payload['source_event_ids'])), payload['source_key'],
                range_start, range_end, payload['status'], payload['rebuild_required'],
                payload['processor_version'], payload['version'], superseded_by,
                payload['retrieval_text'], _dump_json(payload['embedding_metadata']),
            ),
        )
        saved = cur.fetchone()
        if saved:
            payload['episode_id'] = saved[0]
            payload['created_at'] = saved[1] or payload['created_at']
            payload['updated_at'] = saved[2] or payload['updated_at']
        if supersedes_episode_id:
            cur.execute(
                '''UPDATE episodic_memory_index
                   SET status='superseded', rebuild_required=FALSE,
                       superseded_by=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE episode_id=%s AND user_id=%s AND character_id=%s
                     AND status IN ('active', 'stale')
                     AND (superseded_by IS NULL OR superseded_by=%s)''',
                (payload['episode_id'], supersedes_episode_id,
                 user_id, character_id, payload['episode_id']),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    _trace_episode(payload)
    return payload


def invalidate_episode(episode_id, *, status='invalidated',
                       rebuild_required=False, superseded_by=None):
    if status not in ('stale', 'invalidated', 'superseded'):
        raise ValueError('invalid episode invalidation status')
    if _USE_MEMORY_STORE:
        with _memory_lock:
            item = _EPISODES.get(episode_id)
            if not item:
                return None
            item['status'] = status
            item['rebuild_required'] = bool(rebuild_required)
            if superseded_by:
                item['superseded_by'] = superseded_by
            item['updated_at'] = _now()
            result = _copy_episode(item)
        _trace_episode(result)
        return result

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE episodic_memory_index
               SET status=%s, rebuild_required=%s,
                   superseded_by=COALESCE(%s, superseded_by),
                   updated_at=CURRENT_TIMESTAMP
               WHERE episode_id=%s
               RETURNING episode_id, user_id, character_id, title, what_happened,
                         outcome, unresolved, participants, source_event_ids,
                         source_key, range_start, range_end, status,
                         rebuild_required, processor_version, version,
                         superseded_by, retrieval_text, embedding_metadata,
                         created_at, updated_at''',
            (status, bool(rebuild_required), superseded_by, episode_id),
        )
        row = cur.fetchone()
        conn.commit()
        result = _row_episode(row) if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    if result:
        _trace_episode(result)
    return result


def _current_deleted_ids(user_id, character_id, deleted_ids) -> set:
    known = {str(item).strip() for item in (deleted_ids or ()) if str(item).strip()}
    from raw_events import deleted_event_ids
    known.update(deleted_event_ids(user_id, character_id))
    return known


def reconcile_deleted_sources(user_id, character_id, deleted_ids):
    """Fail closed when an episode loses canonical sources.

    Full loss invalidates an episode.  A partial loss preserves its complete
    provenance for audit, but changes it to ``stale`` and requires an explicit
    rebuild; it is never silently kept active from an incomplete summary.
    """
    deleted = _current_deleted_ids(user_id, character_id, deleted_ids)
    if not deleted:
        return []
    changed = []
    for item in list_episodes(user_id, character_id, status=None, limit=1000):
        if item.get('status') not in ('active', 'stale'):
            continue
        ids = _source_ids(item.get('source_event_ids'))
        if not ids or not (set(ids) & deleted):
            continue
        alive = [event_id for event_id in ids if event_id not in deleted]
        if not alive:
            invalidate_episode(item['episode_id'], status='invalidated')
            changed.append(('invalidated', item['episode_id']))
            continue
        save_episode(
            user_id, character_id,
            title=item.get('title') or '',
            what_happened=item.get('what_happened') or '',
            outcome=item.get('outcome') or '',
            unresolved=item.get('unresolved') or '',
            participants=item.get('participants') or (),
            source_event_ids=ids,
            range_start=item.get('range_start'),
            range_end=item.get('range_end'),
            status='stale',
            rebuild_required=True,
            processor_version=item.get('processor_version') or EPISODE_PROCESSOR_VERSION,
            version=item.get('version') or 1,
            superseded_by=item.get('superseded_by'),
            retrieval_text=item.get('retrieval_text') or '',
            embedding_metadata=item.get('embedding_metadata') or {},
            episode_id=item['episode_id'],
        )
        changed.append(('stale', item['episode_id']))
    return changed


def _summary_sections(summary_text: str) -> dict:
    sections = {}
    for segment in re.split(r'[；\n]+', str(summary_text or '')):
        clean = ' '.join(segment.split()).strip()
        for label in ('发生', '决定', '任务', '未决', '事实变化'):
            marker = f'{label}：'
            if marker in clean:
                value = clean.split(marker, 1)[1].strip()
                if value:
                    sections[label] = value
                break
    return sections


def derive_episode_fields(canonical_events: Sequence[dict], *, summary_text='') -> dict:
    """Make non-provenance episode fields without an episode-specific LLM call.

    We reuse the already-produced rolling summary only for narrative text,
    deliberately ignoring its emotional/relationship section.  Participants
    and every provenance id are derived from Raw Events, never from a model
    payload.  v1 deliberately accepts no free-form model field payload.
    """
    sections = _summary_sections(summary_text)
    happened = [sections[label] for label in ('发生', '事实变化') if sections.get(label)]
    outcome = [sections[label] for label in ('决定', '任务') if sections.get(label)]
    what_happened = '；'.join(happened)
    if not what_happened:
        what_happened = f'已归档 {len(canonical_events or ())} 条可追溯对话事件。'
    outcome_text = '；'.join(outcome)
    unresolved = sections.get('未决') or ''
    title = (happened[0] if happened else what_happened)[:80]

    participants = []
    for event in canonical_events or ():
        role = str(event.get('role') or '').strip().lower()
        label = 'assistant' if role in ('assistant', 'gojo') else 'user'
        if label not in participants:
            participants.append(label)
    return {
        'title': title,
        'what_happened': what_happened,
        'outcome': outcome_text,
        'unresolved': unresolved,
        'participants': tuple(participants),
    }


def _canonical_events(user_id, character_id, source_event_ids):
    ids = _source_ids(source_event_ids)
    if not ids:
        return None
    try:
        import raw_events
        if not raw_events.sources_are_active(ids, user_id, character_id):
            return None
        rows = raw_events.get_active_events_by_ids(user_id, character_id, ids)
    except Exception:
        return None
    actual_ids = tuple(str(row.get('event_id') or '').strip() for row in rows or ())
    if actual_ids != ids:
        return None
    return [dict(row) for row in rows]


def build_episode_from_events(
    user_id,
    character_id,
    events: Sequence[dict],
    *,
    summary_text='',
    processor_version=EPISODE_PROCESSOR_VERSION,
    version=None,
    supersedes_episode_id=None,
):
    """Build one episode after verifying every input id in ``chat_log``.

    The caller supplies a stable segment.  Only its event ids are accepted as
    provenance; text and ids in a rolling-summary/LLM payload cannot add a
    source.  A failed source lookup is a no-op, so no active episode is written
    from uncertain or deleted input.
    """
    source_ids = _source_ids(
        [event.get('event_id') for event in (events or []) if event]
    )
    if not source_ids:
        _trace_episode(source_count=0, status='invalidated', version='-')
        return None
    canonical = _canonical_events(user_id, character_id, source_ids)
    if canonical is None:
        _trace_episode(source_count=len(source_ids), status='source_unverified', version='-')
        return None

    source_key = segment_key(source_ids, processor_version)
    if version is None and not supersedes_episode_id:
        existing = _existing_active_segment(
            user_id, character_id, source_key, processor_version)
        if existing:
            _trace_episode(existing)
            return existing
        version = 1
    fields = derive_episode_fields(canonical, summary_text=summary_text)
    range_start = canonical[0].get('timestamp') if canonical else None
    range_end = canonical[-1].get('timestamp') if canonical else None
    return save_episode(
        user_id, character_id,
        title=fields['title'],
        what_happened=fields['what_happened'],
        outcome=fields['outcome'],
        unresolved=fields['unresolved'],
        participants=fields['participants'],
        source_event_ids=source_ids,
        range_start=range_start,
        range_end=range_end,
        processor_version=processor_version,
        version=version,
        supersedes_episode_id=supersedes_episode_id,
    )


def rebuild_episode(episode_id, events: Sequence[dict], *, summary_text=''):
    """Create one successor version and mark its prior episode superseded."""
    previous = get_episode(episode_id)
    if not previous:
        return None
    if previous.get('superseded_by'):
        successor = get_episode(previous['superseded_by'])
        if successor:
            return successor
    return build_episode_from_events(
        previous['user_id'], previous['character_id'], events,
        summary_text=summary_text,
        processor_version=previous.get('processor_version') or EPISODE_PROCESSOR_VERSION,
        version=int(previous.get('version') or 1) + 1,
        supersedes_episode_id=previous['episode_id'],
    )


def list_episode_jobs():
    with _memory_lock:
        return [dict(job) for job in _EPISODE_JOBS]


def enqueue_episode_job(user_id, character_id, source_event_ids, *, summary_text=''):
    """Queue non-LLM episode construction after a rolling summary succeeds."""
    ids = _source_ids(source_event_ids)
    if not ids:
        return None
    key = segment_key(ids, EPISODE_PROCESSOR_VERSION)
    extra = {
        'source_event_ids': list(ids),
        'summary_text': str(summary_text or ''),
        'processor_version': EPISODE_PROCESSOR_VERSION,
    }
    if _uses_memory_store():
        with _memory_lock:
            for job in _EPISODE_JOBS:
                if (job.get('source_event_id') == key
                        and job.get('status') in ('pending', 'running')):
                    return job.get('id')
            job_id = len(_EPISODE_JOBS) + 1
            _EPISODE_JOBS.append({
                'id': job_id,
                'kind': KIND,
                'user_id': user_id,
                'character_id': character_id,
                'source_event_id': key,
                'status': 'pending',
                'extra': extra,
            })
        return job_id
    try:
        from memory_jobs import enqueue_kind
        return enqueue_kind(
            KIND, user_id, character_id,
            source_event_id=key,
            extra=extra,
        )
    except Exception as exc:
        print(f'[episodic_index] enqueue skipped:{type(exc).__name__}')
        return None


def process_episode_job(user_id, character_id, extra, source_event_id=None) -> bool:
    """Worker entrypoint.  It has no LLM dependency and verifies Raw Events."""
    extra = extra or {}
    ids = _source_ids(extra.get('source_event_ids'))
    processor_version = extra.get('processor_version') or EPISODE_PROCESSOR_VERSION
    expected = segment_key(ids, processor_version)
    if not ids or (source_event_id and source_event_id != expected):
        _trace_episode(source_count=len(ids), status='invalid_payload', version='-')
        return True
    built = build_episode_from_events(
        user_id, character_id,
        [{'event_id': event_id} for event_id in ids],
        summary_text=extra.get('summary_text') or '',
        processor_version=processor_version,
    )
    return bool(built)
