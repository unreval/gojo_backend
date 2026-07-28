"""数据库连接 + 初始化 + 自动迁移"""
import psycopg2
from config import DATABASE_URL


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # ── 角色表(核心新增)──
    cur.execute('''CREATE TABLE IF NOT EXISTS characters (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        name_en TEXT,
        avatar_url TEXT,
        voice_id TEXT,
        core_prompt TEXT NOT NULL,
        greeting TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── 角色背景记忆表(替代 gojo_memory)──
    cur.execute('''CREATE TABLE IF NOT EXISTS character_memory (
        id SERIAL PRIMARY KEY,
        character_id TEXT NOT NULL,
        content TEXT NOT NULL,
        category TEXT DEFAULT '其他',
        keywords TEXT DEFAULT '',
        importance REAL DEFAULT 0.5,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── 短期记忆 ──
    cur.execute('''CREATE TABLE IF NOT EXISTS short_memory (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        character_id TEXT NOT NULL DEFAULT 'gojo',
        role TEXT,
        content TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── 用户长期记忆 ──
    cur.execute('''CREATE TABLE IF NOT EXISTS long_memory (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        character_id TEXT NOT NULL DEFAULT 'gojo',
        content TEXT,
        category TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── 用户统计 ──
    cur.execute('''CREATE TABLE IF NOT EXISTS user_stats (
        user_id TEXT PRIMARY KEY,
        first_chat_date TEXT NOT NULL,
        last_chat_date TEXT NOT NULL,
        total_days INTEGER DEFAULT 1)''')

    # ── 日程任务 ──
    cur.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        title TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '个人',
        due_date TEXT,
        due_time TEXT,
        reminder_minutes INTEGER,
        completed BOOLEAN DEFAULT FALSE,
        notification_id VARCHAR(255) DEFAULT NULL,
        repeat_type TEXT DEFAULT 'none',
        last_completed_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── ★ 记账·账户 ──
    cur.execute('''CREATE TABLE IF NOT EXISTS accounts (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        name TEXT NOT NULL,
        initial_balance REAL DEFAULT 0,
        icon TEXT DEFAULT '💰',
        sort_order INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── ★ 记账·收支记录 ──
    # type: 'in' 收入 / 'out' 支出。转账走 is_transfer=TRUE + transfer_id 配对。
    cur.execute('''CREATE TABLE IF NOT EXISTS accounting_records (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        category TEXT DEFAULT '其他',
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        record_date DATE NOT NULL,
        record_time TEXT,
        is_transfer BOOLEAN DEFAULT FALSE,
        transfer_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # 查询多按 user + 日期倒序,加索引
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_accounting_records_user_date
                   ON accounting_records (user_id, record_date DESC)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_accounting_records_account
                   ON accounting_records (account_id)''')

    # ── 老表自动加列(向后兼容)──
    cur.execute("ALTER TABLE short_memory ADD COLUMN IF NOT EXISTS character_id TEXT DEFAULT 'gojo'")
    cur.execute("ALTER TABLE long_memory ADD COLUMN IF NOT EXISTS character_id TEXT DEFAULT 'gojo'")
    cur.execute("ALTER TABLE long_memory ADD COLUMN IF NOT EXISTS category VARCHAR(50) DEFAULT NULL")
    cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notification_id VARCHAR(255) DEFAULT NULL")
    cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS repeat_type TEXT DEFAULT 'none'")
    cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_completed_date TEXT")
    cur.execute("UPDATE long_memory SET content = REPLACE(content, '用户', '她') WHERE content LIKE '用户%'")

    conn.commit()
    cur.close()
    conn.close()


def migrate_old_gojo_memory():
    """如果存在老的 gojo_memory 表,把数据迁到 character_memory(id='gojo')"""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.gojo_memory')")
        exists = cur.fetchone()[0]
        if not exists:
            cur.close()
            conn.close()
            return

        cur.execute("SELECT COUNT(*) FROM character_memory WHERE character_id = 'gojo'")
        new_count = cur.fetchone()[0]
        if new_count > 0:
            print(f'[migrate] character_memory 已有 {new_count} 条 gojo 数据,跳过迁移')
            cur.close()
            conn.close()
            return

        cur.execute('''
            INSERT INTO character_memory (character_id, content, category, keywords, importance, timestamp)
            SELECT 'gojo', content, category, keywords, importance, timestamp FROM gojo_memory
        ''')
        moved = cur.rowcount
        conn.commit()
        print(f'[migrate] 已从 gojo_memory 迁移 {moved} 条到 character_memory')
    except Exception as e:
        print(f'[migrate] 跳过:{e}')
    finally:
        cur.close()
        conn.close()