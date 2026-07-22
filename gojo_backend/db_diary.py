"""日记模块数据层：建表 + 全部 CRUD（含日记本名字 A+C）

五张表：
  1. char_diary          —— 他写的日记（不定期，第一人称心里话）
  2. char_diary_comment  —— 你在他某篇日记下的留言（他会"发现"）
  3. user_diary          —— 你写的日记（可见/私密，私密带密码=剧情机关）
  4. diary_visit         —— 他偷看你日记的访客记号（时间戳 + 是否解锁私密篇）
  5. diary_book          —— 日记本的名字（你那本 / 他那本；他那本他可自己取名）

不对称规则：
  · 你看他日记：纯读不留痕，除非留言（char_diary_comment）他才会发现。
  · 他看你日记：必留访客记号（diary_visit）。
"""
from datetime import datetime
from config import CN_TZ, DEFAULT_CHARACTER_ID
from db import get_conn


# ────────────────────────── 建表 ──────────────────────────

def init_diary_tables():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('''CREATE TABLE IF NOT EXISTS char_diary (
        id SERIAL PRIMARY KEY,
        character_id TEXT NOT NULL DEFAULT 'gojo',
        user_id TEXT NOT NULL DEFAULT 'default',
        content TEXT NOT NULL,
        emotion TEXT DEFAULT '平静',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    cur.execute('''CREATE TABLE IF NOT EXISTS char_diary_comment (
        id SERIAL PRIMARY KEY,
        diary_id INTEGER NOT NULL,
        user_id TEXT NOT NULL DEFAULT 'default',
        content TEXT NOT NULL,
        discovered BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    cur.execute('''CREATE TABLE IF NOT EXISTS user_diary (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        content TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'open',
        password TEXT DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    cur.execute('''CREATE TABLE IF NOT EXISTS diary_visit (
        id SERIAL PRIMARY KEY,
        diary_id INTEGER NOT NULL,
        character_id TEXT NOT NULL DEFAULT 'gojo',
        user_id TEXT NOT NULL DEFAULT 'default',
        unlocked BOOLEAN DEFAULT FALSE,
        reacted BOOLEAN DEFAULT FALSE,
        visited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ★ 日记本名字（A+C）。owner='user'=你那本；owner=角色id=他那本。
    cur.execute('''CREATE TABLE IF NOT EXISTS diary_book (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        owner TEXT NOT NULL,
        title TEXT NOT NULL,
        named_by_self BOOLEAN DEFAULT FALSE,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, owner))''')

    conn.commit()
    cur.close()
    conn.close()
    print('[diary] 日记表已就绪')


# ══════════════════════════════════════════════════════════
#  他的日记 char_diary
# ══════════════════════════════════════════════════════════

def add_char_diary(character_id, user_id, content, emotion='平静'):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO char_diary (character_id, user_id, content, emotion)
           VALUES (%s, %s, %s, %s) RETURNING id, created_at''',
        (character_id, user_id, content, emotion)
    )
    new_id, created_at = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return new_id, created_at


def list_char_diaries(character_id, user_id, limit=50, offset=0):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, content, emotion, created_at FROM char_diary
           WHERE character_id = %s AND user_id = %s
           ORDER BY created_at DESC LIMIT %s OFFSET %s''',
        (character_id, user_id, limit, offset)
    )
    diaries = cur.fetchall()
    result = []
    for did, content, emotion, created_at in diaries:
        cur.execute(
            '''SELECT id, content, created_at FROM char_diary_comment
               WHERE diary_id = %s ORDER BY created_at ASC''',
            (did,)
        )
        comments = [
            {'id': c[0], 'content': c[1], 'created_at': str(c[2]) if c[2] else None}
            for c in cur.fetchall()
        ]
        result.append({
            'id': did, 'content': content, 'emotion': emotion,
            'created_at': str(created_at) if created_at else None,
            'comments': comments,
        })
    cur.close()
    conn.close()
    return result


def count_char_diaries_since(character_id, user_id, since_dt):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT COUNT(*) FROM char_diary
           WHERE character_id = %s AND user_id = %s AND created_at >= %s''',
        (character_id, user_id, since_dt)
    )
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


def get_last_char_diary_time(character_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT MAX(created_at) FROM char_diary WHERE character_id = %s AND user_id = %s',
        (character_id, user_id)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def count_char_diaries_total(character_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT COUNT(*) FROM char_diary WHERE character_id = %s AND user_id = %s',
        (character_id, user_id)
    )
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


# ══════════════════════════════════════════════════════════
#  你留给他的评论 char_diary_comment
# ══════════════════════════════════════════════════════════

def add_diary_comment(diary_id, user_id, content):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO char_diary_comment (diary_id, user_id, content)
           VALUES (%s, %s, %s) RETURNING id, created_at''',
        (diary_id, user_id, content)
    )
    new_id, created_at = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return new_id, created_at


def get_undiscovered_comments(character_id, user_id, limit=5):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT c.id, d.content, c.content
           FROM char_diary_comment c
           JOIN char_diary d ON c.diary_id = d.id
           WHERE d.character_id = %s AND c.user_id = %s AND c.discovered = FALSE
           ORDER BY c.created_at ASC LIMIT %s''',
        (character_id, user_id, limit)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def mark_comments_discovered(comment_ids):
    if not comment_ids:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE char_diary_comment SET discovered = TRUE WHERE id = ANY(%s)', (list(comment_ids),))
    conn.commit()
    cur.close()
    conn.close()


# ══════════════════════════════════════════════════════════
#  你的日记 user_diary
# ══════════════════════════════════════════════════════════

def add_user_diary(user_id, content, visibility='open', password=None):
    if visibility not in ('open', 'locked'):
        visibility = 'open'
    if visibility != 'locked':
        password = None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO user_diary (user_id, content, visibility, password)
           VALUES (%s, %s, %s, %s) RETURNING id, created_at''',
        (user_id, content, visibility, password)
    )
    new_id, created_at = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return new_id, created_at


def list_user_diaries(user_id, limit=50, offset=0):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, content, visibility, password, created_at FROM user_diary
           WHERE user_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s''',
        (user_id, limit, offset)
    )
    diaries = cur.fetchall()
    result = []
    for did, content, visibility, password, created_at in diaries:
        cur.execute(
            '''SELECT character_id, unlocked, visited_at FROM diary_visit
               WHERE diary_id = %s ORDER BY visited_at DESC''',
            (did,)
        )
        visits = [
            {'character_id': v[0], 'unlocked': v[1], 'visited_at': str(v[2]) if v[2] else None}
            for v in cur.fetchall()
        ]
        result.append({
            'id': did, 'content': content, 'visibility': visibility,
            'has_password': bool(password),
            'created_at': str(created_at) if created_at else None,
            'visits': visits,
        })
    cur.close()
    conn.close()
    return result


def update_user_diary(diary_id, user_id, fields):
    cols, vals = [], []
    for k in ('content', 'visibility', 'password'):
        if k in fields:
            cols.append(f'{k} = %s')
            vals.append(fields[k])
    if not cols:
        return False
    vals.extend([diary_id, user_id])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f'UPDATE user_diary SET {", ".join(cols)} WHERE id = %s AND user_id = %s', vals)
    conn.commit()
    ok = cur.rowcount > 0
    cur.close()
    conn.close()
    return ok


def delete_user_diary(diary_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM diary_visit WHERE diary_id = %s', (diary_id,))
    cur.execute('DELETE FROM user_diary WHERE id = %s AND user_id = %s', (diary_id, user_id))
    conn.commit()
    cur.close()
    conn.close()


def change_diary_password(diary_id, user_id, new_password):
    conn = get_conn()
    cur = conn.cursor()
    if new_password:
        cur.execute(
            "UPDATE user_diary SET visibility='locked', password=%s WHERE id=%s AND user_id=%s",
            (new_password, diary_id, user_id)
        )
    else:
        cur.execute(
            "UPDATE user_diary SET visibility='open', password=NULL WHERE id=%s AND user_id=%s",
            (diary_id, user_id)
        )
    conn.commit()
    ok = cur.rowcount > 0
    cur.close()
    conn.close()
    return ok


def get_diaries_for_peeking(user_id, character_id, since_dt=None):
    conn = get_conn()
    cur = conn.cursor()
    q = '''SELECT d.id, d.content, d.visibility, d.password
           FROM user_diary d
           WHERE d.user_id = %s
             AND NOT EXISTS (
                 SELECT 1 FROM diary_visit v
                 WHERE v.diary_id = d.id AND v.character_id = %s)'''
    params = [user_id, character_id]
    if since_dt is not None:
        q += ' AND d.created_at >= %s'
        params.append(since_dt)
    q += ' ORDER BY d.created_at DESC LIMIT 10'
    cur.execute(q, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {'id': r[0], 'content': r[1], 'visibility': r[2], 'password': r[3],
         'is_locked': r[2] == 'locked'}
        for r in rows
    ]


# ══════════════════════════════════════════════════════════
#  他的访客记号 diary_visit
# ══════════════════════════════════════════════════════════

def add_diary_visit(diary_id, character_id, user_id, unlocked=False, visited_at=None):
    conn = get_conn()
    cur = conn.cursor()
    if visited_at is not None:
        cur.execute(
            '''INSERT INTO diary_visit (diary_id, character_id, user_id, unlocked, visited_at)
               VALUES (%s, %s, %s, %s, %s) RETURNING id''',
            (diary_id, character_id, user_id, unlocked, visited_at)
        )
    else:
        cur.execute(
            '''INSERT INTO diary_visit (diary_id, character_id, user_id, unlocked)
               VALUES (%s, %s, %s, %s) RETURNING id''',
            (diary_id, character_id, user_id, unlocked)
        )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def get_unreacted_visits(character_id, user_id, limit=3):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT v.id, d.content, d.visibility, v.unlocked
           FROM diary_visit v
           JOIN user_diary d ON v.diary_id = d.id
           WHERE v.character_id = %s AND v.user_id = %s AND v.reacted = FALSE
           ORDER BY v.visited_at ASC LIMIT %s''',
        (character_id, user_id, limit)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def mark_visits_reacted(visit_ids):
    if not visit_ids:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE diary_visit SET reacted = TRUE WHERE id = ANY(%s)', (list(visit_ids),))
    conn.commit()
    cur.close()
    conn.close()


# ══════════════════════════════════════════════════════════
#  日记本名字 diary_book（A+C）
# ══════════════════════════════════════════════════════════

def get_book_title(user_id, owner, default_title):
    """取某本日记的名字；没有就用默认名建一条并返回。
       owner='user'=你那本；owner=角色id=他那本。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT title, named_by_self FROM diary_book WHERE user_id=%s AND owner=%s',
        (user_id, owner)
    )
    row = cur.fetchone()
    if row:
        cur.close()
        conn.close()
        return {'title': row[0], 'named_by_self': row[1]}
    # 不存在 → 建默认
    cur.execute(
        'INSERT INTO diary_book (user_id, owner, title) VALUES (%s, %s, %s) ON CONFLICT (user_id, owner) DO NOTHING',
        (user_id, owner, default_title)
    )
    conn.commit()
    cur.close()
    conn.close()
    return {'title': default_title, 'named_by_self': False}


def set_book_title(user_id, owner, title, named_by_self=False):
    """改名（你手动改，或他第一次写日记时自己取）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO diary_book (user_id, owner, title, named_by_self, updated_at)
           VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
           ON CONFLICT (user_id, owner)
           DO UPDATE SET title=EXCLUDED.title, named_by_self=EXCLUDED.named_by_self, updated_at=CURRENT_TIMESTAMP''',
        (user_id, owner, title, named_by_self)
    )
    conn.commit()
    cur.close()
    conn.close()
    return True


def has_named_self(user_id, owner):
    """他那本有没有已经自己取过名。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT named_by_self FROM diary_book WHERE user_id=%s AND owner=%s',
        (user_id, owner)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return bool(row and row[0])