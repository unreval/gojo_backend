"""课程表 · 四张表初始化

    courses           —— 课程本身（名字/老师/教室/颜色/学期起止）
    course_sessions   —— 每周固定的第几节（周几 + 起止时间 + 周次）
    course_exceptions —— 单次的调课 / 请假 / 临时加课（extra）
    course_day_off    —— 某天全部放假（法定节假日、校运会、突发放假）

设计取舍：
1. 学期起止（semester_start / semester_end）挂在 courses 上而不是全局。
   同一个 App 里可能同时有正课 + 短期培训班,学期长度不一样,放全局配置反而僵硬。
2. weeks 字段用字符串 "1-16" / "1,3,5,7-16"（空串 = 学期内每周都有）,
   不做 JSON 也不做 int[]，前后端传值直接是字符串。
3. course_exceptions 三种 exception_type：
     - cancel      : 那天这节课不上（请假 / 停课）
     - reschedule  : 那天这节课挪到 new_date + new_start_time
     - extra       : 那天临时加一节课（补课 / 加课）
                     此时 exception_date = new_date = 加课日期
   三种都可能带 new_location（临时换教室）。
4. course_day_off 用独立表而不是复用 course_exceptions,因为放假跟具体课程无关
   —— 说"这天全部放假"不该被绑到某一门课上。删掉某门课也不该影响放假记录。

四张表都在 gojo_server.py 启动时调用一次 init_course_tables()。
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
    # weekday: 1=周一 ... 7=周日（和 ISO 8601 一致）
    cur.execute('''CREATE TABLE IF NOT EXISTS course_sessions (
        id SERIAL PRIMARY KEY,
        course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        weekday INTEGER NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        weeks TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_course_sessions_course ON course_sessions(course_id)')

    # ── 调课 / 请假 / 临时加课 ──
    # exception_type ∈ { 'cancel' | 'reschedule' | 'extra' }
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

    # ── ★ 全部放假 · 独立表 ──
    cur.execute('''CREATE TABLE IF NOT EXISTS course_day_off (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        off_date DATE NOT NULL,
        note TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, off_date))''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_course_day_off_user ON course_day_off(user_id, off_date)')

    conn.commit()
    cur.close()
    conn.close()
    print('[init] 课程表已就绪：courses / course_sessions / course_exceptions / course_day_off')