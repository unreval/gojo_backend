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
    conn.commit()
    cur.close()
    conn.close()
    print('[memory_jobs] 表已就绪')


def enqueue_private_extraction(user_id, user_text, assistant_text, character_id):
    return _enqueue('private', user_id, character_id, user_text, assistant_text, None)


def enqueue_group_extraction(user_id, user_text, round_transcript, members):
    extra = json.dumps(
        {'round_transcript': round_transcript, 'members': members},
        ensure_ascii=False,
    )
    return _enqueue('group', user_id, None, user_text, None, extra)


def _enqueue(kind, user_id, character_id, user_text, assistant_text, extra_json):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO memory_jobs
           (kind, user_id, character_id, user_text, assistant_text, extra_json, status)
           VALUES (%s, %s, %s, %s, %s, %s, 'pending') RETURNING id''',
        (kind, user_id, character_id, user_text, assistant_text, extra_json)
    )
    job_id = cur.fetchone()[0]
    conn.commit()
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
                      extra_json, attempts
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
                      extra_json, attempts
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
    job_id, kind, user_id, character_id, user_text, assistant_text, extra_json, attempts = row
    try:
        ok = False
        if kind == 'group':
            extra = json.loads(extra_json or '{}')
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
