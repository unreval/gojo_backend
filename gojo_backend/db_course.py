"""课程表 · 三张表初始化

    courses           —— 课程本身（名字/老师/教室/颜色/学期起止）
    course_sessions   —— 每周固定的第几节（周几 + 起止时间 + 周次）
    course_exceptions —— 单次的调课 / 请假

设计取舍：
1. 学期起止（semester_start / semester_end）挂在 courses 上而不是全局。
   理由是同一个 App 里可能同时有正课 + 短期培训班，学期长度不一样，
   放全局配置反而僵硬。
2. weeks 字段用字符串 "1-16" / "1,3,5,7-16"（空串 = 学期内每周都有），
   不做 JSON 也不做 int[]，前端后端拼字符串比较省事。
3. course_exceptions 分两种：
     - cancel      : 那天这节课不上（请假 / 停课 / 放假）
     - reschedule  : 那天这节课挪到 new_date + new_start_time
   两种都可能带 new_location（临时换教室），reschedule 还可能改时长。
4. session_id 允许为空。理由：如果 course 只有一个 session，不填也能推断出来；
   如果 course 有多个 session（比如一门课周一 + 周三两次），必须填，
   不然不知道请的是哪一节。前端会自动带上。

三张表都在 gojo_server.py 启动时调用一次 init_course_tables()，
和 db_group / db_bond / db_diary 保持一致的独立初始化风格。
"""
from db import get_conn


def init_course_tables():
    conn = get_conn()
    cur = conn.cursor()

    # ── 课程本身 ──
    cur.execute('''CREATE TABLE IF NOT EXISTS courses (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        name TEXT NOT NULL,
        teacher TEXT DEFAULT '',
        location TEXT DEFAULT '',
        color TEXT DEFAULT '#3b82f6',
        note TEXT DEFAULT '',
        semester_start DATE,
        semester_end DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── 每周固定的节次 ──
    # weekday: 1=周一 ... 7=周日（和 ISO 8601 一致，也是 postgres date_part('isodow') 的口径）
    cur.execute('''CREATE TABLE IF NOT EXISTS course_sessions (
        id SERIAL PRIMARY KEY,
        course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        weekday INTEGER NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        weeks TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_course_sessions_course ON course_sessions(course_id)')

    # ── 调课 / 请假 ──
    cur.execute('''CREATE TABLE IF NOT EXISTS course_exceptions (
        id SERIAL PRIMARY KEY,
        course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        session_id INTEGER,
        exception_date DATE NOT NULL,
        exception_type TEXT NOT NULL,
        new_date DATE,
        new_start_time TEXT,
        new_end_time TEXT,
        new_location TEXT DEFAULT '',
        note TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_course_exc_course_date ON course_exceptions(course_id, exception_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_course_exc_new_date ON course_exceptions(new_date)')

    conn.commit()
    cur.close()
    conn.close()
    print('[init] 课程表已就绪：courses / course_sessions / course_exceptions')
