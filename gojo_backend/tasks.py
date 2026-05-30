"""日程任务数据库操作"""
from db import get_conn


def list_tasks(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, title, category, due_date, due_time, reminder_minutes, completed,
                  repeat_type, last_completed_date, notification_id, created_at
           FROM tasks WHERE user_id = %s
           ORDER BY completed ASC, due_date ASC NULLS LAST, created_at DESC''',
        (user_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'id': r[0], 'title': r[1], 'category': r[2],
        'due_date': r[3], 'due_time': r[4],
        'reminder_minutes': r[5], 'completed': r[6],
        'repeat_type': r[7] or 'none',
        'last_completed_date': r[8],
        'notification_id': r[9],
        'created_at': str(r[10]) if r[10] else None,
    } for r in rows]


def create_task(user_id, title, category='个人', due_date=None, due_time=None,
                reminder_minutes=None, repeat_type='none'):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO tasks (user_id, title, category, due_date, due_time, reminder_minutes, repeat_type)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id''',
        (user_id, title, category, due_date, due_time, reminder_minutes, repeat_type)
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def update_task(task_id, fields):
    cols = []
    vals = []
    for k in ['title', 'category', 'due_date', 'due_time', 'reminder_minutes', 'completed',
              'repeat_type', 'last_completed_date', 'notification_id']:
        if k in fields:
            cols.append(f'{k} = %s')
            vals.append(fields[k])
    if not cols:
        return False
    vals.append(task_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f'UPDATE tasks SET {", ".join(cols)} WHERE id = %s', vals)
    conn.commit()
    cur.close()
    conn.close()
    return True


def delete_task(task_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM tasks WHERE id = %s', (task_id,))
    conn.commit()
    cur.close()
    conn.close()


# ────────── 去重 & 取消辅助函数 ──────────

def find_duplicate_task(user_id, title, due_date, due_time):
    """
    查同 user_id + 同标题 + 同日期 + 同时间的未完成任务。
    返回 (task_id, notification_id) 或 None
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, notification_id FROM tasks
           WHERE user_id = %s AND title = %s
             AND due_date IS NOT DISTINCT FROM %s
             AND due_time IS NOT DISTINCT FROM %s
             AND completed = FALSE
           ORDER BY created_at DESC LIMIT 1''',
        (user_id, title, due_date, due_time)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def find_and_delete_tasks_by_keyword(user_id, keyword, latest_only=True):
    """
    根据关键词模糊匹配标题，删除未完成的任务。
    latest_only=True 时只删最近创建的那一条。
    返回 [(task_id, notification_id), ...]
    """
    conn = get_conn()
    cur = conn.cursor()

    if latest_only:
        cur.execute(
            '''SELECT id, notification_id FROM tasks
               WHERE user_id = %s AND title ILIKE %s
                 AND completed = FALSE
               ORDER BY created_at DESC LIMIT 1''',
            (user_id, f'%{keyword}%')
        )
    else:
        cur.execute(
            '''SELECT id, notification_id FROM tasks
               WHERE user_id = %s AND title ILIKE %s
                 AND completed = FALSE''',
            (user_id, f'%{keyword}%')
        )
    rows = cur.fetchall()

    deleted = []
    for task_id, notif_id in rows:
        cur.execute('DELETE FROM tasks WHERE id = %s', (task_id,))
        deleted.append((task_id, notif_id))

    conn.commit()
    cur.close()
    conn.close()
    return deleted


def delete_latest_task(user_id):
    """没指定关键词时，删最近创建的那条未完成任务。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, notification_id FROM tasks
           WHERE user_id = %s AND completed = FALSE
           ORDER BY created_at DESC LIMIT 1''',
        (user_id,)
    )
    row = cur.fetchone()
    deleted = []
    if row:
        task_id, notif_id = row
        cur.execute('DELETE FROM tasks WHERE id = %s', (task_id,))
        deleted.append((task_id, notif_id))
    conn.commit()
    cur.close()
    conn.close()
    return deleted