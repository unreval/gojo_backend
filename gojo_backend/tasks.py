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
    for k in ['title','category','due_date','due_time','reminder_minutes','completed',
              'repeat_type','last_completed_date','notification_id']:
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
