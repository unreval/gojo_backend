"""Thinking-chain tables and prompt text.

Understandings and views are model output. They are not memory, not
cognitive state, and not evidence. Nothing in the chat path reads them.
The gate does not branch on prompt_version; a new rule is another entry
in PROMPTS.
"""
import logging

_LOG = logging.getLogger(__name__)

THINKING_DDL = (
    '''CREATE TABLE IF NOT EXISTS thinking_understandings (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        batch_ref TEXT NOT NULL,
        item_index INTEGER NOT NULL,
        source_event_ids JSONB NOT NULL,
        context_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
        steps JSONB NOT NULL,
        cited_experiences JSONB NOT NULL DEFAULT '[]'::jsonb,
        cited_views JSONB NOT NULL DEFAULT '[]'::jsonb,
        view_proposals JSONB NOT NULL DEFAULT '[]'::jsonb,
        prompt_version TEXT NOT NULL,
        model TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        consumed_by_run TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, character_id, batch_ref, item_index)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_thinking_understandings_pair
        ON thinking_understandings (user_id, character_id, created_at DESC)''',
    '''CREATE TABLE IF NOT EXISTS character_views (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        about TEXT NOT NULL,
        statement TEXT NOT NULL,
        strength SMALLINT NOT NULL CHECK (strength BETWEEN 1 AND 5),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active','retracted','open_question')),
        evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
        revision_log JSONB NOT NULL DEFAULT '[]'::jsonb,
        last_supported_at TIMESTAMPTZ,
        prompt_version TEXT,
        model TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_character_views_pair
        ON character_views (user_id, character_id, status)''',
    '''CREATE TABLE IF NOT EXISTS thinking_audit_log (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        batch_ref TEXT,
        kind TEXT NOT NULL,
        reason TEXT,
        detail JSONB NOT NULL DEFAULT '{}'::jsonb,
        prompt_version TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_thinking_audit_pair
        ON thinking_audit_log (user_id, character_id, created_at DESC)''',
)

NEUTRAL_V0 = '''你是{name}。下面是你和她最近的对话、你可能想起的经历、你现在对一些事的看法。每一条前面的方括号是编号。
对话里标【本批】的，是她这次说的话；标【回复】的，是你当时的回复。【之前的对话】只是帮你理解上下文。
请写下你对【本批】这段对话的理解，用第一人称。如果这段对话里聊了几件不同的事，可以分开写，最多 3 条；没什么值得想的，输出 []。
每条理解写这五步：
1. 看到什么：发生了什么，她说了什么。
2. 想起了什么：让你想起了哪段对话、哪段经历、哪个看法。只写编号，不要编，没有就写"没有"。
3. 对我意味着什么。
4. 我怎么回应的：你当时怎么回的、为什么。
5. 看法有没有变：你现有的看法有没有被加强、减弱，或者想到一个新的看法。没有就写"没变"。
不确定的地方就写不确定，不要硬下结论。
只输出 JSON 数组，不要 Markdown。每项字段：
sources：这条理解说的是【本批】哪几句，填编号数组，必须是【本批】里她说的话。
context：理解时用到的【之前的对话】编号，可省略。
steps：五步，每步 {"step": "看到什么", "text": "……"}，step 依次是 看到什么、想起了什么、对我意味着什么、我怎么回应的、看法有没有变；每步 text 不超过 300 字。
experiences：第 2 步用到的经历编号（L 或 E 开头），可省略。
views：第 2 步用到的看法编号（V 开头），可省略。
view_proposals：第 5 步的提议，每项 {"view": 看法编号或 null, "direction": "加强" 或 "减弱" 或 "新建" 或 "没变", "note": 不超过 100 字}，可省略。
所有编号必须来自上面给出的内容，不能自己编。字符串里引用词语用中文引号「」，不要用英文双引号。'''

PROMPTS = {'neutral_v0': NEUTRAL_V0}


def render_system(version, name):
    """Inject the character name. No other placeholder is filled."""
    text = PROMPTS.get(version)
    if not isinstance(text, str):
        return None
    return text.replace('{name}', name if isinstance(name, str) else '', 1)


def init_thinking_tables(conn=None):
    """Install the observation tables. Safe to call repeatedly.

    A borrowed connection stays in the caller's transaction. An owned
    connection is committed and closed here.
    """
    own = conn is None
    if own:
        from db import get_conn
        conn = get_conn()
    cur = None
    try:
        cur = conn.cursor()
        for ddl in THINKING_DDL:
            cur.execute(ddl)
        if own:
            conn.commit()
    except Exception:
        if own:
            conn.rollback()
        raise
    finally:
        try:
            if cur is not None:
                cur.close()
        finally:
            if own:
                conn.close()


def character_name(character_id):
    """Name from the character config. The id itself is the fallback."""
    try:
        from characters_data._loader import load_core
        core = load_core(character_id)
    except Exception as exc:
        _LOG.warning('thinking character name skipped: %s', type(exc).__name__)
        core = None
    if isinstance(core, dict):
        name = core.get('name')
        if isinstance(name, str) and name.strip():
            return name.strip()
    return character_id
