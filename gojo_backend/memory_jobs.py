"""Durable memory extraction queue.

聊天请求先把提取任务写入 PostgreSQL，再由后台 worker 执行。
进程重启后 pending / 中断的 running 会继续跑，避免 daemon 线程把整轮记忆带走。
同一时刻只处理一条，减轻连续快聊时 extractor 乱序写入。
"""
import json
import threading
import time

from db import get_conn

MAX_ATTEMPTS = 3
_WAKE = threading.Event()
_THREAD = None
_LOCK = threading.Lock()


def init_memory_jobs_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS memory_jobs (
        id SERIAL PRIMARY KEY,
        kind TEXT NOT NULL DEFAULT 'private',
        user_id TEXT NOT NULL,
        character_id TEXT,
        user_text TEXT,
        assistant_text TEXT,
        extra_json TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_memory_jobs_status
                   ON memory_jobs (status, id)''')
    cur.execute('''UPDATE memory_jobs
                   SET status = 'pending', updated_at = CURRENT_TIMESTAMP
                   WHERE status = 'running' ''')
    cur.execute("ALTER TABLE memory_jobs ADD COLUMN IF NOT EXISTS source_event_id TEXT")
    cur.execute("ALTER TABLE memory_jobs ADD COLUMN IF NOT EXISTS assistant_event_id TEXT")
    cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_jobs_inflight
                   ON memory_jobs (kind, user_id, (COALESCE(character_id, '')), source_event_id)
                   WHERE status IN ('pending', 'running')
                     AND source_event_id IS NOT NULL AND source_event_id <> '' ''')
    conn.commit()
    cur.close()
    conn.close()
    print('[memory_jobs] 表已就绪')


def enqueue_private_extraction(user_id, user_text, assistant_text, character_id,
                               temporal_context=None, source_event_id=None,
                               assistant_event_id=None):
    extra = {}
    if temporal_context:
        try:
            from temporal_awareness import serialize_snapshot
            extra['temporal_context'] = serialize_snapshot(temporal_context)
        except Exception:
            pass
    if source_event_id:
        extra['source_event_id'] = str(source_event_id).strip()
    if assistant_event_id:
        extra['assistant_event_id'] = str(assistant_event_id).strip()
    extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
    return _enqueue(
        'private', user_id, character_id, user_text, assistant_text, extra_json,
        source_event_id=source_event_id,
        assistant_event_id=assistant_event_id,
    )


def enqueue_kind(kind, user_id, character_id, source_event_id=None, extra=None,
                 user_text=None, assistant_text=None):
    """Generic idempotent enqueue used by rolling summary and similar jobs."""
    extra_json = json.dumps(extra or {}, ensure_ascii=False) if extra is not None else None
    return _enqueue(
        kind, user_id, character_id, user_text, assistant_text, extra_json,
        source_event_id=source_event_id,
    )


def enqueue_group_extraction(user_id, user_text, round_transcript, members):
    extra = json.dumps(
        {'round_transcript': round_transcript, 'members': members},
        ensure_ascii=False,
    )
    return _enqueue('group', user_id, None, user_text, None, extra)


def _enqueue(kind, user_id, character_id, user_text, assistant_text, extra_json,
             source_event_id=None, assistant_event_id=None):
    source_event_id = (str(source_event_id).strip() if source_event_id else '') or None
    assistant_event_id = (str(assistant_event_id).strip() if assistant_event_id else '') or None
    conn = get_conn()
    cur = conn.cursor()
    job_id = None
    try:
        if source_event_id:
            cur.execute(
                '''SELECT id FROM memory_jobs
                   WHERE kind=%s AND user_id=%s
                     AND COALESCE(character_id, '') = COALESCE(%s, '')
                     AND source_event_id=%s
                     AND status IN ('pending', 'running')
                   ORDER BY id DESC LIMIT 1''',
                (kind, user_id, character_id, source_event_id))
            existing = cur.fetchone()
            if existing:
                conn.commit()
                job_id = existing[0]
            else:
                cur.execute(
                    '''INSERT INTO memory_jobs
                       (kind, user_id, character_id, user_text, assistant_text, extra_json,
                        status, source_event_id, assistant_event_id)
                       VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id''',
                    (kind, user_id, character_id, user_text, assistant_text, extra_json,
                     source_event_id, assistant_event_id)
                )
                job_id = cur.fetchone()[0]
                conn.commit()
        else:
            cur.execute(
                '''INSERT INTO memory_jobs
                   (kind, user_id, character_id, user_text, assistant_text, extra_json,
                    status, source_event_id, assistant_event_id)
                   VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id''',
                (kind, user_id, character_id, user_text, assistant_text, extra_json,
                 source_event_id, assistant_event_id)
            )
            job_id = cur.fetchone()[0]
            conn.commit()
    except Exception as e:
        conn.rollback()
        pgcode = getattr(e, 'pgcode', None)
        if source_event_id and pgcode == '23505':
            cur.execute(
                '''SELECT id FROM memory_jobs
                   WHERE kind=%s AND user_id=%s
                     AND COALESCE(character_id, '') = COALESCE(%s, '')
                     AND source_event_id=%s
                   ORDER BY id DESC LIMIT 1''',
                (kind, user_id, character_id, source_event_id))
            row = cur.fetchone()
            conn.commit()
            job_id = row[0] if row else None
        else:
            cur.close()
            conn.close()
            raise
    cur.close()
    conn.close()
    _WAKE.set()
    print(f'[memory_jobs] queued #{job_id} kind={kind} user={user_id}')
    return job_id


def _set_status(job_id, status, last_error=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''UPDATE memory_jobs
           SET status = %s, last_error = %s, updated_at = CURRENT_TIMESTAMP
           WHERE id = %s''',
        (status, last_error, job_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def _claim_one():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, kind, user_id, character_id, user_text, assistant_text,
                      extra_json, attempts, source_event_id, assistant_event_id
               FROM memory_jobs
               WHERE status = 'pending' AND attempts < %s
               ORDER BY id ASC
               LIMIT 1''',
            (MAX_ATTEMPTS,)
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return None
        job_id = row[0]
        cur.execute(
            '''UPDATE memory_jobs
               SET status = 'running',
                   attempts = attempts + 1,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = %s AND status = 'pending' ''',
            (job_id,)
        )
        if cur.rowcount == 0:
            conn.commit()
            return None
        conn.commit()
        cur.execute(
            '''SELECT id, kind, user_id, character_id, user_text, assistant_text,
                      extra_json, attempts, source_event_id, assistant_event_id
               FROM memory_jobs WHERE id = %s''',
            (job_id,)
        )
        claimed = cur.fetchone()
        conn.commit()
        return claimed
    except Exception as e:
        conn.rollback()
        print(f'[memory_jobs] claim 失败：{e}')
        return None
    finally:
        cur.close()
        conn.close()


def _run_job(row):
    job_id = row[0]
    kind = row[1]
    user_id = row[2]
    character_id = row[3]
    user_text = row[4]
    assistant_text = row[5]
    extra_json = row[6]
    attempts = row[7]
    source_event_id = row[8] if len(row) > 8 else None
    assistant_event_id = row[9] if len(row) > 9 else None
    try:
        ok = False
        extra = json.loads(extra_json or '{}') if extra_json else {}
        source_event_id = source_event_id or extra.get('source_event_id')
        assistant_event_id = assistant_event_id or extra.get('assistant_event_id')
        source_ids = [
            item for item in (source_event_id, assistant_event_id) if item
        ]
        if kind == 'rolling_summary':
            from rolling_summary import process_summary_job
            ok = process_summary_job(
                user_id, character_id, extra, source_event_id=source_event_id)
        elif kind == 'group':
            from user_memory import extract_and_save_group_memory
            ok = extract_and_save_group_memory(
                user_id,
                user_text or '',
                extra.get('round_transcript') or '',
                extra.get('members') or [],
            )
        else:
            from user_memory import extract_and_save_memory
            ok = extract_and_save_memory(
                user_id,
                user_text or '',
                assistant_text or '',
                character_id,
                temporal_context=extra.get('temporal_context'),
                source_event_id=source_event_id,
                source_event_ids=source_ids or None,
            )
        if ok:
            _set_status(job_id, 'done')
            print(f'[memory_jobs] done #{job_id}')
            return
        err = 'extraction returned False'
    except Exception as e:
        err = f'{type(e).__name__}: {e}'
        print(f'[memory_jobs] #{job_id} 执行失败：{err}')

    if attempts >= MAX_ATTEMPTS:
        _set_status(job_id, 'failed', err)
        print(f'[memory_jobs] failed #{job_id} after {attempts} attempts')
    else:
        _set_status(job_id, 'pending', err)


def _loop():
    while True:
        try:
            row = _claim_one()
            if row:
                _run_job(row)
                continue
            _WAKE.wait(timeout=2.0)
            _WAKE.clear()
        except Exception as e:
            print(f'[memory_jobs] worker 出错：{e}')
            time.sleep(2.0)


def start_memory_worker():
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_loop, name='memory-jobs', daemon=True)
        _THREAD.start()
        print('[memory_jobs] worker 已启动')
