"""日记模块数据层：建表 + 增删查

四张表：
  char_diary          —— 他的日记（他不定期自己写）
  char_diary_comment  —— 你留给他日记的评论（他会"发现"你看过）
  user_diary          —— 你的日记（open=给他看 / locked=私密带密码）
  diary_visit         —— 他偷看你日记的访客记号（你能看到他几点看的）

设计要点（对应需求）：
  · 你偷看他日记：前端纯读，不写任何记号 → 他不知道
  · 你留评论：写 char_diary_comment，他下次会"发现"（discovered 防重复发现）
  · 他偷看你日记：必写 diary_visit（含时间戳）→ 你能看到
  · 私密篇 locked + password：默认他碰不到，极低概率"猜对密码"才解锁（unlocked=TRUE 记进 visit）
"""
from db import get_conn


# ══════════════════════════════════════════
#  建表（在 gojo_server.py 启动时调用一次）
# ══════════════════════════════════════════

def init_diary_tables():
    conn = get_conn()
    cur = conn.cursor()

    # ── 他的日记 ──
    cur.execute('''CREATE TABLE IF NOT EXISTS char_diary (
        id SERIAL PRIMARY KEY,
        character_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        content TEXT NOT NULL,
        emotion TEXT DEFAULT '平静',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── 你留给他日记的评论 ──
    #   discovered：他是否已经"发现"过这条评论（发现后置 TRUE，避免反复第一次发现）
    cur.execute('''CREATE TABLE IF NOT EXISTS char_diary_comment (
        id SERIAL PRIMARY KEY,
        diary_id INTEGER NOT NULL,
        user_id TEXT NOT NULL,
        content TEXT NOT NULL,
        discovered BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── 你的日记 ──
    #   visibility：'open'=给他看  'locked'=私密（需密码解锁）
    #   password：仅 locked 用，明文存（这是剧情机关不是安全锁，够用）
    #   emotion_flag：写日记时可标记情绪浓度（供"他看后要不要明着提"参考；也可后端自动判断）
    cur.execute('''CREATE TABLE IF NOT EXISTS user_diary (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        content TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'open',
        password TEXT DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── 他偷看你日记的访客记号 ──
    #   unlocked：TRUE 表示这次看的是私密篇（他"猜对密码"解锁了）——你会看到"他居然解开了上锁那篇"
    #   reacted_in_chat：他有没有在对话里就这次偷看做过反应（避免反复提同一次）
    cur.execute('''CREATE TABLE IF NOT EXISTS diary_visit (
        id SERIAL PRIMARY KEY,
        diary_id INTEGER NOT NULL,
        character_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        unlocked BOOLEAN DEFAULT FALSE,
        reacted_in_chat BOOLEAN DEFAULT FALSE,
        visited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    conn.commit()
    cur.close()
    conn.close()
    print('[diary] ✅ 日记四张表就绪')


# ══════════════════════════════════════════
#  他的日记 char_diary
# ══════════════════════════════════════════

def add_char_diary(character_id, user_id, content, emotion='平静'):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO char_diary (character_id, user_id, content, emotion)
           VALUES (%s, %s, %s, %s) RETURNING id''',
        (character_id, user_id, content, emotion)
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def list_char_diaries(character_id, user_id, limit=50):
    """他的日记，新→旧。每篇附上"你留过的评论"。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, content, emotion, created_at FROM char_diary
           WHERE character_id = %s AND user_id = %s
           ORDER BY created_at DESC LIMIT %s''',
        (character_id, user_id, limit)
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
            'id': did,
            'content': content,
            'emotion': emotion,
            'created_at': str(created_at) if created_at else None,
            'comments': comments,
        })
    cur.close()
    conn.close()
    return result


def get_last_char_diary_time(character_id, user_id):
    """他最近一篇日记的时间（排程用来判断"上次写是多久前"）。没有返回 None。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT created_at FROM char_diary
           WHERE character_id = %s AND user_id = %s
           ORDER BY created_at DESC LIMIT 1''',
        (character_id, user_id)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def count_char_diary_today(character_id, user_id):
    """他今天已经写了几篇（防一天写多篇）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT COUNT(*) FROM char_diary
           WHERE character_id = %s AND user_id = %s
             AND created_at::date = (NOW() AT TIME ZONE 'Asia/Shanghai')::date''',
        (character_id, user_id)
    )
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


# ══════════════════════════════════════════
#  你留给他的评论 char_diary_comment
# ══════════════════════════════════════════

def add_diary_comment(diary_id, user_id, content):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO char_diary_comment (diary_id, user_id, content)
           VALUES (%s, %s, %s) RETURNING id''',
        (diary_id, user_id, content)
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def get_undiscovered_comments(character_id, user_id, limit=5):
    """他还没"发现"的你留的评论 → 注入 prompt 让他发现。
    返回 [(comment_id, diary_id, comment_content, diary_content), ...]"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT c.id, c.diary_id, c.content, d.content
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
    cur.execute(
        'UPDATE char_diary_comment SET discovered = TRUE WHERE id = ANY(%s)',
        (list(comment_ids),)
    )
    conn.commit()
    cur.close()
    conn.close()


# ══════════════════════════════════════════
#  你的日记 user_diary
# ══════════════════════════════════════════

def add_user_diary(user_id, content, visibility='open', password=None):
    if visibility == 'locked' and not password:
        visibility = 'open'   # 没给密码就不算私密，保护你不会误锁
    if visibility != 'locked':
        password = None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO user_diary (user_id, content, visibility, password)
           VALUES (%s, %s, %s, %s) RETURNING id''',
        (user_id, content, visibility, password)
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def list_user_diaries(user_id, limit=100):
    """你的日记（含访客记号）。私密篇内容照常返回给你自己看，只是标记 locked。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, content, visibility, created_at FROM user_diary
           WHERE user_id = %s ORDER BY created_at DESC LIMIT %s''',
        (user_id, limit)
    )
    diaries = cur.fetchall()
    result = []
    for did, content, visibility, created_at in diaries:
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
            'id': did,
            'content': content,
            'visibility': visibility,
            'created_at': str(created_at) if created_at else None,
            'visits': visits,
        })
    cur.close()
    conn.close()
    return result


def update_user_diary_password(diary_id, user_id, new_password):
    """改私密篇密码（改密码功能）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''UPDATE user_diary SET password = %s
           WHERE id = %s AND user_id = %s AND visibility = 'locked' ''',
        (new_password, diary_id, user_id)
    )
    ok = cur.rowcount > 0
    conn.commit()
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


# ── 排程/偷看用的取数 ──

def get_user_diaries_for_peek(user_id, since_hours=48):
    """给"他偷看"用：最近 since_hours 小时内、且他还没看过的你的日记。
    open 篇优先；locked 篇也返回（但排程里只有极低概率会去解锁）。
    返回 [(id, content, visibility, password), ...] 新→旧。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT d.id, d.content, d.visibility, d.password
           FROM user_diary d
           WHERE d.user_id = %s
             AND d.created_at >= NOW() - (%s * INTERVAL '1 hour')
             AND NOT EXISTS (
                 SELECT 1 FROM diary_visit v
                 WHERE v.diary_id = d.id
             )
           ORDER BY d.created_at DESC''',
        (user_id, since_hours)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def add_diary_visit(diary_id, character_id, user_id, unlocked=False):
    """他看了你某篇日记 → 留访客记号。"""
    conn = get_conn()
    cur = conn.cursor()
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
    """他看过、但还没在对话里反应过的偷看记录 → 注入 prompt。
    返回 [(visit_id, diary_id, diary_content, unlocked, visited_at), ...]"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT v.id, v.diary_id, d.content, v.unlocked, v.visited_at
           FROM diary_visit v
           JOIN user_diary d ON v.diary_id = d.id
           WHERE v.character_id = %s AND v.user_id = %s AND v.reacted_in_chat = FALSE
           ORDER BY v.visited_at DESC LIMIT %s''',
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
    cur.execute(
        'UPDATE diary_visit SET reacted_in_chat = TRUE WHERE id = ANY(%s)',
        (list(visit_ids),)
    )
    conn.commit()
    cur.close()
    conn.close()
