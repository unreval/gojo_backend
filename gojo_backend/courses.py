"""课程表数据库操作 · CRUD + 按周查询

按周查询是这里的核心：给一个 monday 日期，返回那一周所有具体的课
（把 weeks 字段解析开、把 exceptions 应用上）。前端拿到就能直接铺格子，
不用自己算学期第几周。
"""
from datetime import date, datetime, timedelta
from db import get_conn


# ══════════════════════════════════════════════════════════════
#  courses · CRUD
# ══════════════════════════════════════════════════════════════

def list_courses(user_id):
    """列出用户所有课程，每个课程带上它的 sessions。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, name, teacher, location, color, note,
                  semester_start, semester_end, created_at
           FROM courses WHERE user_id = %s
           ORDER BY created_at DESC''',
        (user_id,)
    )
    rows = cur.fetchall()
    courses = [{
        'id': r[0], 'name': r[1], 'teacher': r[2], 'location': r[3],
        'color': r[4], 'note': r[5],
        'semester_start': str(r[6]) if r[6] else None,
        'semester_end': str(r[7]) if r[7] else None,
        'created_at': str(r[8]) if r[8] else None,
        'sessions': [],
    } for r in rows]

    if courses:
        ids = [c['id'] for c in courses]
        cur.execute(
            '''SELECT id, course_id, weekday, start_time, end_time, weeks
               FROM course_sessions WHERE course_id = ANY(%s)
               ORDER BY weekday, start_time''',
            (ids,)
        )
        by_course = {}
        for s in cur.fetchall():
            by_course.setdefault(s[1], []).append({
                'id': s[0], 'weekday': s[2],
                'start_time': s[3], 'end_time': s[4],
                'weeks': s[5] or '',
            })
        for c in courses:
            c['sessions'] = by_course.get(c['id'], [])

    cur.close()
    conn.close()
    return courses


def get_course(course_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, user_id, name, teacher, location, color, note,
                  semester_start, semester_end
           FROM courses WHERE id = %s''',
        (course_id,)
    )
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close()
        return None
    course = {
        'id': r[0], 'user_id': r[1], 'name': r[2], 'teacher': r[3],
        'location': r[4], 'color': r[5], 'note': r[6],
        'semester_start': str(r[7]) if r[7] else None,
        'semester_end': str(r[8]) if r[8] else None,
        'sessions': [],
    }
    cur.execute(
        '''SELECT id, weekday, start_time, end_time, weeks FROM course_sessions
           WHERE course_id = %s ORDER BY weekday, start_time''',
        (course_id,)
    )
    course['sessions'] = [{
        'id': s[0], 'weekday': s[1], 'start_time': s[2],
        'end_time': s[3], 'weeks': s[4] or '',
    } for s in cur.fetchall()]
    cur.close()
    conn.close()
    return course


def create_course(user_id, name, teacher='', location='', color='#3b82f6',
                  note='', semester_start=None, semester_end=None, sessions=None):
    """创建课程，可以一次性把 sessions 传进来一起写。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO courses (user_id, name, teacher, location, color, note,
                                semester_start, semester_end)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''',
        (user_id, name, teacher, location, color, note,
         semester_start or None, semester_end or None)
    )
    new_id = cur.fetchone()[0]

    for s in (sessions or []):
        try:
            wd = int(s.get('weekday'))
            if wd < 1 or wd > 7:
                continue
            st = s.get('start_time') or ''
            et = s.get('end_time') or ''
            if not st or not et:
                continue
            cur.execute(
                '''INSERT INTO course_sessions (course_id, weekday, start_time, end_time, weeks)
                   VALUES (%s, %s, %s, %s, %s)''',
                (new_id, wd, st, et, s.get('weeks') or '')
            )
        except Exception as e:
            print(f'[courses] session 跳过: {e}')

    conn.commit()
    cur.close()
    conn.close()
    return new_id


def update_course(course_id, fields):
    """只更新 courses 表里的字段，sessions 走单独接口。"""
    cols, vals = [], []
    for k in ['name', 'teacher', 'location', 'color', 'note',
              'semester_start', 'semester_end']:
        if k in fields:
            cols.append(f'{k} = %s')
            v = fields[k]
            # 空字符串的日期字段当作 NULL
            if k in ('semester_start', 'semester_end') and not v:
                v = None
            vals.append(v)
    if not cols:
        return False
    vals.append(course_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f'UPDATE courses SET {", ".join(cols)} WHERE id = %s', vals)
    conn.commit()
    cur.close()
    conn.close()
    return True


def delete_course(course_id):
    # sessions / exceptions 靠 ON DELETE CASCADE 跟着删
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM courses WHERE id = %s', (course_id,))
    conn.commit()
    cur.close()
    conn.close()


def replace_sessions(course_id, sessions):
    """整批替换某门课的 sessions（编辑时用）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM course_sessions WHERE course_id = %s', (course_id,))
    for s in (sessions or []):
        try:
            wd = int(s.get('weekday'))
            if wd < 1 or wd > 7:
                continue
            st = s.get('start_time') or ''
            et = s.get('end_time') or ''
            if not st or not et:
                continue
            cur.execute(
                '''INSERT INTO course_sessions (course_id, weekday, start_time, end_time, weeks)
                   VALUES (%s, %s, %s, %s, %s)''',
                (course_id, wd, st, et, s.get('weeks') or '')
            )
        except Exception as e:
            print(f'[courses] session 跳过: {e}')
    conn.commit()
    cur.close()
    conn.close()


def add_session(course_id, weekday, start_time, end_time, weeks=''):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO course_sessions (course_id, weekday, start_time, end_time, weeks)
           VALUES (%s, %s, %s, %s, %s) RETURNING id''',
        (course_id, int(weekday), start_time, end_time, weeks or '')
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def delete_session(session_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM course_sessions WHERE id = %s', (session_id,))
    conn.commit()
    cur.close()
    conn.close()


# ══════════════════════════════════════════════════════════════
#  course_exceptions · CRUD
# ══════════════════════════════════════════════════════════════

def list_exceptions(user_id, start_date=None, end_date=None):
    """列出用户所有 exceptions（按日期范围过滤，两端都不给就全返）。"""
    conn = get_conn()
    cur = conn.cursor()
    sql = '''SELECT e.id, e.course_id, e.session_id, e.exception_date, e.exception_type,
                    e.new_date, e.new_start_time, e.new_end_time, e.new_location, e.note,
                    c.name, c.color
             FROM course_exceptions e
             JOIN courses c ON c.id = e.course_id
             WHERE c.user_id = %s'''
    args = [user_id]
    if start_date:
        sql += ' AND (e.exception_date >= %s OR e.new_date >= %s)'
        args.extend([start_date, start_date])
    if end_date:
        sql += ' AND (e.exception_date <= %s OR e.new_date <= %s)'
        args.extend([end_date, end_date])
    sql += ' ORDER BY e.exception_date DESC'
    cur.execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'id': r[0], 'course_id': r[1], 'session_id': r[2],
        'exception_date': str(r[3]) if r[3] else None,
        'exception_type': r[4],
        'new_date': str(r[5]) if r[5] else None,
        'new_start_time': r[6], 'new_end_time': r[7],
        'new_location': r[8] or '',
        'note': r[9] or '',
        'course_name': r[10], 'course_color': r[11],
    } for r in rows]


def create_exception(course_id, exception_date, exception_type,
                     session_id=None, new_date=None,
                     new_start_time=None, new_end_time=None,
                     new_location='', note=''):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO course_exceptions
           (course_id, session_id, exception_date, exception_type,
            new_date, new_start_time, new_end_time, new_location, note)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (course_id, session_id, exception_date, exception_type,
         new_date or None, new_start_time, new_end_time,
         new_location, note)
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def delete_exception(exception_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM course_exceptions WHERE id = %s', (exception_id,))
    conn.commit()
    cur.close()
    conn.close()


# ══════════════════════════════════════════════════════════════
#  按周查询 · 把 sessions + exceptions 组合成一周的实际课表
# ══════════════════════════════════════════════════════════════

def _parse_weeks(weeks_str):
    """把 "1-16" / "1,3,5,7-16" / "" 解析成 set[int]，空串表示 None（意为不过滤）。"""
    s = (weeks_str or '').strip()
    if not s:
        return None
    result = set()
    for token in s.split(','):
        token = token.strip()
        if not token:
            continue
        if '-' in token:
            try:
                a, b = token.split('-', 1)
                a, b = int(a), int(b)
                if a > b:
                    a, b = b, a
                for x in range(a, b + 1):
                    result.add(x)
            except ValueError:
                continue
        else:
            try:
                result.add(int(token))
            except ValueError:
                continue
    return result or None


def _week_number(target_date, semester_start):
    """target_date 是学期第几周（从 1 开始，学期开始那周算第 1 周）。
    semester_start 没给 → 返回 None（表示不按周次过滤）。"""
    if not semester_start:
        return None
    if isinstance(semester_start, str):
        semester_start = datetime.strptime(semester_start, '%Y-%m-%d').date()
    # 学期开始日所在的那一周（从周一算起）为第 1 周
    sem_monday = semester_start - timedelta(days=semester_start.weekday())
    tgt_monday = target_date - timedelta(days=target_date.weekday())
    delta_days = (tgt_monday - sem_monday).days
    if delta_days < 0:
        return 0   # 学期开始前
    return delta_days // 7 + 1


def get_week_view(user_id, monday_date):
    """给一个"周一"日期，返回那一周所有具体课（已应用调课/请假）。

    返回结构（前端直接铺格子用）：
    [{
        instance_id: "session_12_2025-09-15",   # 前端做 key 用
        course_id, session_id, name, color, teacher, note,
        date: "2025-09-15",     # 具体这天
        weekday: 1,             # 1-7
        start_time: "08:00",
        end_time: "09:40",
        location: "教学楼A101",
        is_exception: false,    # 是不是被调过课
        exception_type: null,   # 'reschedule' 时是 'reschedule'
        exception_id: null,
        note: '',               # exception 的备注
    }, ...]
    """
    if isinstance(monday_date, str):
        monday_date = datetime.strptime(monday_date, '%Y-%m-%d').date()
    # 强制归到周一（万一前端传的不是周一）
    monday_date = monday_date - timedelta(days=monday_date.weekday())
    sunday_date = monday_date + timedelta(days=6)

    courses = list_courses(user_id)
    exceptions = list_exceptions(user_id, str(monday_date), str(sunday_date))

    # index: (course_id, session_id, exception_date) → exception 对象
    exc_by_source = {}
    # index: 从别处调过来的（new_date 落在本周内）
    exc_moved_in = []
    for e in exceptions:
        if e['exception_date']:
            src_day = datetime.strptime(e['exception_date'], '%Y-%m-%d').date()
            if monday_date <= src_day <= sunday_date:
                exc_by_source[(e['course_id'], e['session_id'], e['exception_date'])] = e
        if e['exception_type'] == 'reschedule' and e['new_date']:
            new_day = datetime.strptime(e['new_date'], '%Y-%m-%d').date()
            if monday_date <= new_day <= sunday_date:
                exc_moved_in.append(e)

    result = []
    for c in courses:
        sem_start = c['semester_start']
        sem_end = c['semester_end']
        for s in c['sessions']:
            weekday = int(s['weekday'])  # 1-7
            day = monday_date + timedelta(days=weekday - 1)
            date_str = str(day)

            # 学期范围过滤
            if sem_start and str(day) < sem_start:
                continue
            if sem_end and str(day) > sem_end:
                continue

            # 周次过滤
            allowed_weeks = _parse_weeks(s['weeks'])
            if allowed_weeks is not None:
                wnum = _week_number(day, sem_start)
                if wnum is not None and wnum not in allowed_weeks:
                    continue

            # 查这节课这天有没有 exception（cancel / reschedule）
            # 优先精确匹配 session_id，其次匹配 course_id + date（session_id 为空的老数据）
            exc = exc_by_source.get((c['id'], s['id'], date_str)) or \
                  exc_by_source.get((c['id'], None, date_str))

            if exc:
                if exc['exception_type'] == 'cancel':
                    # 请假/停课：这次不出现
                    continue
                if exc['exception_type'] == 'reschedule':
                    # 原时段不出现（会以另一条 exc_moved_in 的形式在新日子出现）
                    continue

            result.append({
                'instance_id': f'session_{s["id"]}_{date_str}',
                'course_id': c['id'],
                'session_id': s['id'],
                'name': c['name'],
                'color': c['color'],
                'teacher': c['teacher'],
                'note': c['note'],
                'date': date_str,
                'weekday': weekday,
                'start_time': s['start_time'],
                'end_time': s['end_time'],
                'location': c['location'],
                'is_exception': False,
                'exception_type': None,
                'exception_id': None,
            })

    # 补入被调过来的课
    for e in exc_moved_in:
        # 找到源 course
        source_course = next((c for c in courses if c['id'] == e['course_id']), None)
        if not source_course:
            continue
        new_day = datetime.strptime(e['new_date'], '%Y-%m-%d').date()
        weekday = new_day.isoweekday()   # 1-7
        result.append({
            'instance_id': f'exc_{e["id"]}',
            'course_id': e['course_id'],
            'session_id': e['session_id'],
            'name': source_course['name'],
            'color': source_course['color'],
            'teacher': source_course['teacher'],
            'note': e['note'] or source_course['note'],
            'date': e['new_date'],
            'weekday': weekday,
            'start_time': e['new_start_time'] or '',
            'end_time': e['new_end_time'] or '',
            'location': e['new_location'] or source_course['location'],
            'is_exception': True,
            'exception_type': 'reschedule',
            'exception_id': e['id'],
        })

    # 按日期 + 起始时间排序，前端不用再排
    result.sort(key=lambda x: (x['date'], x['start_time']))
    return result
