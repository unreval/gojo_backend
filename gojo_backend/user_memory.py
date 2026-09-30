# ═══════════════════════════════════════════════════════════════════
# ★★★ 这份是【BACKEND 私仓版】user_memory.py ★★★
#
# 用途:  推到你 backend 私仓 (你自己的完整版仓库)
# 路径:  你的_backend/gojo_backend/user_memory.py (或对应的 backend 目录)
# 行数:  ~1160 行
# 基底:  backend 私仓原版 (1157 行) + 时间阈值降到 10 分
#
# 包含:  ✅ backend 私仓原版所有功能全部保留 (没删任何东西)
#        ✅ 时间阈值 10 分钟 (修凌晨 1:59 问抽血 bug)
#
# ⚠️  【只改了 1 处】: get_short_memory 里的 gap_hours>=2 → gap_seconds>=600
#     其他地方一字未动
#
# ⚠️  这份【不能】推到 pub
#     — 是 backend 私仓自己代码基础上打的补丁
#     — pub 用另一份文件 PUB_user_memory.py
# ═══════════════════════════════════════════════════════════════════

"""用户记忆（原始事件 + 确定性自述投影 + 历史生成性记忆）

记忆四层结构：
  1. 她的事实      long_memory (character_id='shared')  —— 关于用户本人，全角色共享
  2. 我们之间的事  bond_memory (kind='between')          —— 她和某角色的共同经历，按角色独立
  3. 她告诉我的事  bond_memory (kind='told')             —— 她告诉某角色的、关于角色本人/其世界的信息
  4. 角色背景      character_memory                      —— 原作设定，只手动管理，聊天不写入

short_memory 只是近窗 compatibility cache / LLM recent view，不是 canonical evidence。
新的 conversational 事实以 chat_log event_id 为准，经 append_raw_event 写入。
get_short_memory(n) 保留兼容读取（优先 ledger，再合并 cache）。

私聊与群聊都只提交 canonical raw evidence，不调用模型提取事实或关系。
long/bond 的事实读取要求当前原始来源与确定性裁决；历史生成性记录不自动取得权威。
"""
import anthropic
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from config import ANTHROPIC_KEY, CN_TZ, DEFAULT_CHARACTER_ID
from db import get_conn
from memory_authority import authoritative_memory_sql
from structured_output import (
    StructuredOutputError,
    invoke_structured_llm,
    parse_structured_output,
)
from utils import is_emoji_only
from character_relations import get_relations_text

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ────────── 当前对话上下文范围（短期记忆喂给模型的部分）──────────
SHORT_MEMORY_HOURS = 24   # 把最近这么多小时的对话当"当前上下文"（想要两天就改 48）
SHORT_MEMORY_MAX   = 40   # ★ 20→40:聊得多时 20 条只能覆盖两三小时,导致"11 小时前聊的机械体"被挤掉

# ★ 跨角色共享的"用户事实"桶。
SHARED_CHARACTER_ID = 'shared'

# 全部角色名缓存（做违禁词用，启动后第一次用时查一次库）
_char_names_cache = None


def _all_character_names():
    """返回库里所有角色的名字列表（含常见简称），用作用户事实的违禁词。
    ★ 以后加新角色不用再手动改违禁词列表了。"""
    global _char_names_cache
    if _char_names_cache is not None:
        return _char_names_cache
    names = []
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('SELECT name FROM characters')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        for (n,) in rows:
            if not n:
                continue
            names.append(n)
            if len(n) >= 3:
                names.append(n[:2])   # 五条 / 夏油 / 波风
                names.append(n[-2:])  # 条悟 / 油杰 / 水门
    except Exception as e:
        print(f'[memory] 读取角色名失败：{e}')
    _char_names_cache = list(dict.fromkeys(names))  # 去重保序
    return _char_names_cache


# ────────── 短期记忆 ──────────

def _prune_short_memory(cur, user_id, character_id):
    cur.execute('''DELETE FROM short_memory WHERE user_id = %s AND character_id = %s AND id NOT IN (
        SELECT id FROM short_memory WHERE user_id = %s AND character_id = %s
        ORDER BY timestamp DESC LIMIT 100)''',
        (user_id, character_id, user_id, character_id))


INSERT_SHORT_EVENT_ONCE_SQL = '''
INSERT INTO short_memory (user_id, character_id, role, content, source_event_id)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (user_id, character_id, role, source_event_id)
WHERE source_event_id IS NOT NULL
DO NOTHING
RETURNING id
'''.strip()


def save_short_memory(user_id, role, content, character_id=DEFAULT_CHARACTER_ID,
                      source_event_id=None, metadata=None, subtitle='',
                      emotion=''):
    """Write the short_memory compatibility cache.

    Canonical conversational facts must go through append_raw_event / chat_log.
    Do not treat short_memory.content as evidence or a second source of truth.
    When event_id is present, the ledger is written first; keyed cache insert is
    idempotent on (user_id, character_id, role, source_event_id).
    Legacy rows with source_event_id IS NULL are not deduped.
    """
    event_id = _normalize_event_id(source_event_id)
    extra = dict(metadata or {}) if isinstance(metadata, dict) else {}
    if event_id:
        _mirror_raw_event(
            user_id, character_id, role=role, content=content, event_id=event_id,
            metadata=extra, subtitle=subtitle, emotion=emotion)
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        if event_id:
            cur.execute(
                INSERT_SHORT_EVENT_ONCE_SQL,
                (user_id, character_id, role, content, event_id),
            )
            if not cur.fetchone():
                conn.commit()
                return
        else:
            cur.execute(
                '''INSERT INTO short_memory
                       (user_id, character_id, role, content, source_event_id)
                   VALUES (%s, %s, %s, %s, %s)''',
                (user_id, character_id, role, content, None),
            )
        _prune_short_memory(cur, user_id, character_id)
        conn.commit()
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        if event_id and getattr(e, 'pgcode', None) == '23505':
            print(f'[memory] skip duplicate short_memory {role} {event_id}')
            return
        raise
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def commit_visible_assistant_message(
    user_id, content, character_id=DEFAULT_CHARACTER_ID, *,
    event_id, kind='proactive', subtitle='', emotion='', metadata=None,
):
    """User-visible assistant bubble: canonical Raw Event + short_memory cache.

    Internal diary/state that is never shown as a chat bubble must not use this.
    Retry with the same event_id does not create a second chat_log row.
    """
    event_id = _normalize_event_id(event_id)
    extra = dict(metadata or {})
    extra.setdefault('kind', kind)
    extra.setdefault('assistant_turn_id', event_id)
    extra.setdefault('segment_index', 0)
    save_short_memory(
        user_id, 'assistant', content, character_id,
        source_event_id=event_id,
        metadata=extra,
        subtitle=subtitle,
        emotion=emotion,
    )
    return event_id


def _mirror_raw_event(user_id, character_id, *, role, content, event_id,
                      content_type='text', metadata=None, reply_to_event_id=None,
                      subtitle='', emotion=''):
    """Best-effort write-through to the canonical chat_log ledger."""
    try:
        import raw_events
        raw_events.append_raw_event(
            user_id, character_id,
            event_id=event_id,
            role=role,
            content=content,
            content_type=content_type,
            metadata=metadata,
            reply_to_event_id=reply_to_event_id,
            subtitle=subtitle,
            emotion=emotion,
        )
    except Exception as e:
        print(f'[raw_events] mirror skipped:{e}')


def _normalize_event_id(source_event_id):
    value = str(source_event_id).strip() if source_event_id else ''
    return value or None


def _event_meta_json(event_meta):
    if not event_meta:
        return ''
    if isinstance(event_meta, str):
        return event_meta[:2000]
    try:
        return json.dumps(event_meta, ensure_ascii=False)[:2000]
    except Exception:
        return ''


def parse_event_meta(event_meta):
    if isinstance(event_meta, dict):
        return event_meta
    text = (event_meta or '').strip()
    if not text:
        return {}
    if text.startswith('{'):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    merged = {}
    for line in text.split('\n'):
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if isinstance(parsed, dict):
            merged.update(parsed)
    return merged


def _visual_summary_from_meta(event_meta):
    extra = parse_event_meta(event_meta)
    return (extra.get('visual_summary') or extra.get('visualSummary') or '').strip(), extra


INSERT_USER_EVENT_ONCE_SQL = '''
INSERT INTO short_memory (user_id, character_id, role, content, source_event_id, event_meta)
VALUES (%s, %s, 'user', %s, %s, %s)
ON CONFLICT (user_id, character_id, role, source_event_id)
WHERE source_event_id IS NOT NULL
DO NOTHING
RETURNING id
'''.strip()


def save_user_short_memory_once(user_id, content, character_id=DEFAULT_CHARACTER_ID,
                                source_event_id=None, event_meta=None):
    """保存真实发生的用户发言。同一 source_event_id 用原子 INSERT ON CONFLICT 去重。

    content 只存用户原文/媒体占位；visual_summary 放 event_meta，prompt 读取时再拼。
    short_memory 是 compatibility cache，canonical Raw Event 在 chat_log。
    有 event id：INSERT ... ON CONFLICT DO NOTHING RETURNING id
      - RETURNING 有行 = 本次新插入，返回 True
      - RETURNING 空 = 已存在，返回 False
    无 event id：普通 INSERT，每次都写入。
    """
    event_id = _normalize_event_id(source_event_id)
    meta_text = _event_meta_json(event_meta)
    inserted = False
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        if event_id:
            cur.execute(
                INSERT_USER_EVENT_ONCE_SQL,
                (user_id, character_id, content, event_id, meta_text),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                print(f'[memory] skip duplicate user event {event_id}')
                inserted = False
            else:
                _prune_short_memory(cur, user_id, character_id)
                conn.commit()
                inserted = True
        else:
            cur.execute(
                '''INSERT INTO short_memory (user_id, character_id, role, content, source_event_id, event_meta)
                   VALUES (%s, %s, %s, %s, %s, %s)''',
                (user_id, character_id, 'user', content, None, meta_text)
            )
            _prune_short_memory(cur, user_id, character_id)
            conn.commit()
            inserted = True
        return inserted
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        pgcode = getattr(e, 'pgcode', None)
        if event_id and pgcode == '23505':
            print(f'[memory] skip duplicate user event {event_id} (unique)')
            return False
        raise
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if event_id:
            meta = event_meta if isinstance(event_meta, dict) else parse_event_meta(event_meta)
            kind = (meta or {}).get('kind') if isinstance(meta, dict) else ''
            content_type = kind if kind in ('image', 'video', 'voice') else 'text'
            _mirror_raw_event(
                user_id, character_id,
                role='user',
                content=content,
                event_id=event_id,
                content_type=content_type,
                metadata=meta if isinstance(meta, dict) else None,
            )


def attach_short_memory_event_meta(user_id, character_id, source_event_id, event_meta):
    """把 visual_summary 等结构字段补到已有 user short_memory 行上，不改 content。"""
    event_id = _normalize_event_id(source_event_id)
    meta_text = _event_meta_json(event_meta)
    if not event_id or not meta_text:
        return False
    conn = get_conn()
    cur = conn.cursor()
    updated = False
    try:
        cur.execute(
            '''UPDATE short_memory
               SET event_meta=%s
               WHERE user_id=%s AND character_id=%s AND role='user'
                 AND source_event_id=%s''',
            (meta_text, user_id, character_id, event_id))
        conn.commit()
        updated = cur.rowcount > 0
    finally:
        cur.close()
        conn.close()
    _attach_vision_annotation(event_id, event_meta)
    return updated


def _attach_vision_annotation(source_event_id, event_meta):
    summary, extra = _visual_summary_from_meta(event_meta)
    if not summary:
        return
    try:
        import raw_events
        raw_events.attach_event_annotation(
            source_event_id, 'vision_summary', summary,
            processor_version=raw_events.PROCESSOR_VISION_VERSION,
        )
        extra = extra or {}
        extra['visual_summary'] = summary
        # extra on chat_log is updated inside attach_event_annotation
    except Exception as e:
        print(f'[raw_events] vision annotation skipped:{e}')


def _short_limit(n):
    """调用方传入的 n 生效，但不会超过 SHORT_MEMORY_MAX。"""
    try:
        return min(max(int(n), 1), SHORT_MEMORY_MAX)
    except (TypeError, ValueError):
        return SHORT_MEMORY_MAX


def _time_marker(ts, now=None, today=None):
    if ts is None:
        return ''
    now = now or datetime.now(CN_TZ)
    today = today if today is not None else now.date()
    ts_cn = ts.replace(tzinfo=timezone.utc).astimezone(CN_TZ) if ts.tzinfo is None else ts.astimezone(CN_TZ)
    gap_seconds = (now - ts_cn).total_seconds()
    if gap_seconds < 600:
        return ''
    d = ts_cn.date()
    if d == today:
        day_label = '今天'
    elif (today - d).days == 1:
        day_label = '昨天'
    else:
        day_label = f'{d.month}月{d.day}日'
    return f'【{day_label}{ts_cn.strftime("%H:%M")}的消息】'


def _prompt_role(role):
    return 'user' if (role or '') == 'user' else 'assistant'


def _fetch_short_memory_cache(user_id, character_id, hours, limit):
    """Legacy short_memory rows only. Not canonical evidence; ledger is preferred."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT role, content, timestamp, source_event_id, event_meta
               FROM short_memory
               WHERE user_id = %s AND character_id = %s
                 AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
               ORDER BY timestamp DESC
               LIMIT %s''',
            (user_id, character_id, hours, limit)
        )
        return list(cur.fetchall())
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            cur.execute(
                '''SELECT role, content, timestamp FROM short_memory
                   WHERE user_id = %s AND character_id = %s
                     AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
                   ORDER BY timestamp DESC
                   LIMIT %s''',
                (user_id, character_id, hours, limit)
            )
            return [(*row, None, '') for row in cur.fetchall()]
        except Exception:
            return []
    finally:
        cur.close()
        conn.close()


def _merge_recent_context(user_id, character_id, n, hours):
    """Ledger is canonical. short_memory is a cache, not a second fact DB.

    Exact (role, content) matches are collapsed at read time only.
    Different event_ids with similar wording stay distinct.
    Source validity unknown != active: skip the whole batch rather than
    feeding unverified cache/ledger rows to the model.
    """
    limit = _short_limit(n)
    try:
        import raw_events
        deleted = raw_events.deleted_event_ids(user_id, character_id)
        ledger = raw_events.get_recent_events(
            user_id, character_id, n=SHORT_MEMORY_MAX, hours=hours)
    except Exception:
        return []

    cache = _fetch_short_memory_cache(
        user_id, character_id, hours, SHORT_MEMORY_MAX)

    merged = []
    seen_ids = set()
    seen_pairs = set()

    def _add(role, content, ts, event_id, event_meta):
        prompt_role = _prompt_role(role)
        eid = _normalize_event_id(event_id)
        if eid and eid in deleted:
            return
        if eid and eid in seen_ids:
            return
        pair = (prompt_role, content or '')
        # Unkeyed cache rows that copy an already-present ledger fact are
        # the same compatibility write, not a second occurrence.
        if not eid and pair in seen_pairs:
            return
        if eid:
            seen_ids.add(eid)
        seen_pairs.add(pair)
        merged.append({
            'role': prompt_role,
            'content': content or '',
            'timestamp': ts,
            'event_id': eid,
            'event_meta': event_meta,
        })

    for event in ledger:
        _add(
            event.get('role'),
            event.get('content'),
            event.get('timestamp'),
            event.get('event_id'),
            event.get('metadata') or {},
        )

    for row in reversed(cache):
        role, content, ts = row[0], row[1] or '', row[2]
        eid = row[3] if len(row) > 3 else None
        event_meta = row[4] if len(row) > 4 else ''
        _add(role, content, ts, eid, event_meta)

    def _ts_key(ts):
        if ts is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if getattr(ts, 'tzinfo', None) is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts

    merged.sort(key=lambda item: _ts_key(item['timestamp']))
    from assistant_turn import collapse_assistant_logical_turns
    try:
        import db_chatlog
        turn_facts = db_chatlog.assistant_turn_facts(user_id, character_id)
    except Exception:
        turn_facts = {}
    collapsed = collapse_assistant_logical_turns(merged, turn_facts=turn_facts)
    out = []
    for item in collapsed:
        out.append({
            'role': item.get('role'),
            'content': item.get('content') or '',
            'timestamp': item.get('timestamp'),
            'event_id': item.get('event_id'),
            'event_meta': item.get('event_meta') if item.get('event_meta') not in (None, '')
            else item.get('metadata') or {},
        })
    return out[-limit:]


def get_short_memory(user_id, n=6, character_id=DEFAULT_CHARACTER_ID):
    """Compatibility API: recent context for prompts.

    Prefers canonical chat_log Raw Events; short_memory is only a fallback cache.
    Deleted Raw Events never enter the result. n still caps the window.
    """
    rows = _merge_recent_context(user_id, character_id, n, SHORT_MEMORY_HOURS)
    now = datetime.now(CN_TZ)
    today = now.date()
    result = []
    for item in rows:
        marker = _time_marker(item['timestamp'], now, today)
        content = item['content']
        result.append((item['role'], marker + content if marker else content))
    return result


def get_short_memory_for_prompt(user_id, n=6, character_id=DEFAULT_CHARACTER_ID,
                                exclude_event_ids=None, exclude_messages=None):
    """角色经历层 → Anthropic messages。

    short_memory.content / chat_log.text 是用户原文/媒体占位；
    【图片摘要】只在这里按 event_meta.visual_summary 动态拼出。
    已删除事件不会进入 prompt。
    """
    excluded = {
        _normalize_event_id(item) for item in (exclude_event_ids or [])
        if _normalize_event_id(item)
    }
    excluded_pairs = [
        (_prompt_role(role), content or '')
        for role, content in (exclude_messages or [])
    ]
    limit = _short_limit(n)
    fetch_limit = _short_limit(
        limit + len(excluded) + len(excluded_pairs))
    rows = _merge_recent_context(
        user_id, character_id, fetch_limit, SHORT_MEMORY_HOURS)
    drop_indexes = set()
    for role, content in excluded_pairs:
        for index in range(len(rows) - 1, -1, -1):
            item = rows[index]
            if item['role'] == role and item['content'] == content:
                drop_indexes.add(index)
                break
    rows = [
        item for index, item in enumerate(rows)
        if index not in drop_indexes and item.get('event_id') not in excluded
    ]
    rows = rows[-limit:]
    now = datetime.now(CN_TZ)
    today = now.date()
    out = []
    for item in rows:
        marker = _time_marker(item['timestamp'], now, today)
        body = marker + item['content'] if marker else item['content']
        text = assemble_prompt_content(body, item.get('event_meta'))
        if text:
            out.append({'role': item['role'], 'content': text})
    return out


def assemble_prompt_content(content, event_meta=None):
    """读取时把 visual_summary 拼进 prompt，不回写 short_memory.content。"""
    summary, extra = _visual_summary_from_meta(event_meta)
    if not summary:
        return (content or '').strip()
    kind = '视频' if extra.get('kind') == 'video' else '图片'
    body = (content or '').strip()
    suffix = f'【{kind}摘要】{summary[:800]}'
    if suffix in body:
        return body
    return f'{body}\n{suffix}'.strip() if body else suffix


def format_media_short_memory(display_text, visual_summary='', event_meta=None):
    """Prompt-time helper。不要把返回值写进 short_memory.content。"""
    extra = dict(parse_event_meta(event_meta))
    if visual_summary:
        extra['visual_summary'] = visual_summary
    return assemble_prompt_content(display_text, extra)


def get_recent_openings(user_id, n=5, character_id=DEFAULT_CHARACTER_ID):
    """Legacy cache read of recent assistant openings. Not canonical evidence."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT content FROM short_memory
           WHERE user_id = %s AND character_id = %s AND role = 'assistant'
           ORDER BY timestamp DESC LIMIT %s''',
        (user_id, character_id, n)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0].strip()[:5] for r in rows if r[0].strip()]


def get_last_assistant_reply(user_id, character_id=DEFAULT_CHARACTER_ID):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT content FROM short_memory
           WHERE user_id = %s AND character_id = %s AND role = 'assistant'
           ORDER BY timestamp DESC LIMIT 1''',
        (user_id, character_id)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else ''


# ────────── 第 1 层：用户事实（长期记忆，shared 共享桶）──────────

def _bg_embed(table, row_id, content):
    """后台补 embedding（RAG 未启用时是空操作）。"""
    try:
        import memory_search, threading
        if not memory_search.is_vector_ready():
            return
        threading.Thread(target=memory_search.save_embedding,
                         args=(table, row_id, content), daemon=True).start()
    except Exception:
        pass


def _invalidate_rag(table=None):
    try:
        import memory_search
        if memory_search.is_vector_ready():
            memory_search.invalidate_cache(table)
    except Exception:
        pass


def notify_memory_changed(table, row_id=None, content=None, deleted=False):
    """编辑/删除记忆后同步 RAG 缓存；更新内容时后台重算 embedding。"""
    _invalidate_rag(table)
    if not deleted and row_id and content:
        _bg_embed(table, row_id, content)


def save_long_memory(user_id, content, category=None, character_id=DEFAULT_CHARACTER_ID,
                     lifecycle_kind='long_fact', recall_weight=1.0,
                     source_event_refs=None, expires_at=None):
    src_ids = _source_ids_from_refs(source_event_refs)
    if src_ids:
        import raw_events
        if not raw_events.sources_are_active(src_ids, user_id, character_id):
            print(f'[{user_id}] skip long_memory: required source deleted')
            return False
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT content FROM long_memory
           WHERE user_id = %s
             AND character_id = %s
             AND COALESCE(recall_status, 'active') = 'active'
             AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)''',
        (user_id, character_id)
    )
    existing = cur.fetchall()
    for (e,) in existing:
        if _too_similar(content, e):
            cur.close(); conn.close()
            print(f'[{user_id}] 记忆重复，跳过：{content}（已有：{e}）')
            return False
    refs_json = json.dumps(source_event_refs or [], ensure_ascii=False)
    cur.execute(
        '''INSERT INTO long_memory (
               user_id, character_id, content, category,
               lifecycle_kind, recall_status, recall_weight,
               source_event_refs, expires_at
           )
           VALUES (%s, %s, %s, %s, %s, 'active', %s, %s::jsonb, %s)
           RETURNING id''',
        (
            user_id, character_id, content, category,
            lifecycle_kind, recall_weight, refs_json, expires_at,
        )
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    _bg_embed('long_memory', new_id, content)   # ★ RAG 启用时后台补向量
    try:
        import raw_events
        raw_events.link_memory_sources(
            'long_memory', new_id, _source_ids_from_refs(source_event_refs))
    except Exception:
        pass
    return True


def get_long_memory(user_id, character_id=DEFAULT_CHARACTER_ID):
    """返回该角色专属记忆 + 共享用户事实（shared 桶）。[(content, timestamp, category)]"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f'''SELECT content, timestamp, category FROM long_memory
           WHERE user_id = %s AND character_id IN (%s, %s)
             AND {authoritative_memory_sql('long_memory')}
             AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
           ORDER BY timestamp DESC LIMIT 40''',
        (user_id, character_id, SHARED_CHARACTER_ID)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r[0], r[1], r[2] or '其他') for r in rows]


def _get_memories_with_id(user_id, character_id=DEFAULT_CHARACTER_ID):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f'''SELECT id, content FROM long_memory
           WHERE user_id = %s AND character_id IN (%s, %s)
             AND {authoritative_memory_sql('long_memory')}
             AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
           ORDER BY timestamp DESC LIMIT 40''',
        (user_id, character_id, SHARED_CHARACTER_ID)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def delete_long_memory(memory_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM long_memory WHERE id = %s', (memory_id,))
    conn.commit()
    cur.close()
    conn.close()
    notify_memory_changed('long_memory', deleted=True)


# ────────── 第 2/3 层：羁绊记忆（我们之间的事 / 她告诉我的事）──────────

def _loosely_matches(a: str, b: str) -> bool:
    """宽松匹配,只用于【找合并目标】,不用于去重。

    LLM 在 bond_merge.replaces 里复述旧记忆时经常有出入,
    严格匹配会让合并请求全部落空。这里放宽到"大致是那条"就行,
    误配的风险由 merge_bond_memories 里"每条旧记忆都要保留信号"的
    防线兜住；字数只能是弱信号，不能单独判定语义丢失。
    """
    if not a or not b:
        return False
    ca, cb = _clean_for_compare(a), _clean_for_compare(b)
    if not ca or not cb:
        return False
    if ca == cb or ca in cb or cb in ca:
        return True
    ga, gb = _bigrams(ca), _bigrams(cb)
    if not ga or not gb:
        return False
    # 阈值 0.2:实测完全无关的记忆二元组重合都是 0.00,
    # 而 LLM 复述同一条记忆最低也有 0.22 —— 中间空档很大,不会误配。
    return len(ga & gb) / min(len(ga), len(gb)) >= 0.2


def _source_ids_from_refs(refs):
    out = []
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except Exception:
            refs = []
    for item in refs or []:
        sid = ''
        if isinstance(item, dict):
            sid = str(
                item.get('source_id')
                or item.get('event_id')
                or item.get('source_event_id')
                or ''
            )
        elif item:
            sid = str(item)
        sid = sid.replace('memory_job:', '').replace('raw_event:', '').strip()
        if sid and sid not in out:
            out.append(sid)
    return out


def _too_similar(a: str, b: str) -> bool:
    """判断两条记忆是不是【几乎一模一样】。

    ★ 分工:
      · 语义重复(意思一样但措辞不同)→ 交给提取器 LLM 判断。
        它在 prompt 里能看到【已记录的羁绊记忆】和【已记录的她的事实】,
        判断"这件事记过没有"是它的活,比数字符靠谱得多。
      · 这个函数只拦【近乎完全相同】的:标点差异、多一个字、重复提交。

    ★ 为什么阈值这么严:
      之前设 0.62 想帮 LLM 兜底,结果误杀了真·新记忆——
        「她今天在家改程序」vs「她今天因腰酸没改成程序」
        单字重合 75% 被判重复,但这是两件事(一件在改,一件没改成)。
      中文单字太容易撞。误删是永久丢失,漏拦只是多一条,
      所以宁可让 LLM 去做语义判断,这里只做最后一道防线。
    """
    if not a or not b:
        return False
    if a == b:
        return True

    # A convention's visible payload is its identity.  Its fixed semantic
    # wrapper otherwise makes distinct symbols look nearly identical to this
    # low-level duplicate guard, which would block an explicit replacement.
    a_payload = _communication_convention_payload(a)
    b_payload = _communication_convention_payload(b)
    if a_payload and b_payload:
        if a_payload != b_payload:
            return False
        # The same visible symbol can legitimately serve two separately
        # user-grounded rules; an embedded slot distinguishes those records.
        if (_clean_for_compare(_communication_convention_slot(a))
                != _clean_for_compare(_communication_convention_slot(b))):
            return False

    ca, cb = _clean_for_compare(a), _clean_for_compare(b)
    if not ca or not cb:
        return False
    if ca == cb:          # 只是标点/空格不同
        return True

    short, long_ = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    # 长度差超过 15% 就不算"几乎一样"
    if len(short) / len(long_) < 0.85:
        return False

    # 单字几乎全同
    char_ratio = sum(1 for ch in set(short) if ch in long_) / len(set(short))
    if char_ratio < 0.95:
        return False

    # 二元组也几乎全同(保证语序一致,不是同样的字换个顺序)
    ga, gb = _bigrams(ca), _bigrams(cb)
    if not ga or not gb:
        return True
    return len(ga & gb) / min(len(ga), len(gb)) >= 0.9


def _clean_for_compare(s: str) -> str:
    """比较前去掉标点空白,只留实义字符。"""
    punc = set('，。,.、！!？?「」：:；;“”‘’\'" \t\n——…')
    return ''.join(ch for ch in s if ch not in punc)


def _bigrams(s: str) -> set:
    """相邻两字组成的集合。中文里二元组比单字更能代表语义。"""
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def save_bond_memory(user_id, character_id, kind, content, source_event_ids=None,
                     *, atomic_sources=False):
    """kind='between'（我们之间）或 'told'（她告诉我的）。带去重。"""
    if source_event_ids:
        import raw_events
        if not raw_events.sources_are_active(source_event_ids, user_id, character_id):
            print(f'[{user_id}] skip bond_memory: required source deleted')
            return False
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT content FROM bond_memory
           WHERE user_id = %s AND character_id = %s AND kind = %s
             AND COALESCE(recall_status, 'active') = 'active' ''',
        (user_id, character_id, kind)
    )
    existing = cur.fetchall()
    for (e,) in existing:
        if _too_similar(content, e):
            cur.close(); conn.close()
            print(f'[{user_id}] 羁绊记忆重复，跳过：{content}（已有：{e}）')
            return False
    cur.execute(
        'INSERT INTO bond_memory (user_id, character_id, kind, content) VALUES (%s, %s, %s, %s) RETURNING id',
        (user_id, character_id, kind, content)
    )
    new_id = cur.fetchone()[0]

    # ★ 两级召回：尝试关联到一条 long_memory
    try:
        from smart_recall import link_bond_to_fact
        fact_id = link_bond_to_fact(user_id, character_id, content)
        if fact_id:
            cur.execute('UPDATE bond_memory SET linked_fact_id = %s WHERE id = %s',
                        (fact_id, new_id))
            print(f'[{user_id}] 🔗 bond #{new_id} 关联到 fact #{fact_id}')
    except Exception:
        pass  # 关联失败不影响存入

    try:
        if atomic_sources:
            for source_id in dict.fromkeys(source_event_ids or []):
                cur.execute(
                    '''INSERT INTO memory_source_events
                       (memory_type, memory_id, source_event_id)
                       VALUES ('bond_memory', %s, %s)
                       ON CONFLICT (memory_type, memory_id, source_event_id) DO NOTHING''',
                    (new_id, source_id))
        conn.commit()
    except Exception:
        conn.rollback()
        cur.close()
        conn.close()
        raise
    cur.close()
    conn.close()
    _bg_embed('bond_memory', new_id, content)   # ★ RAG 启用时后台补向量
    try:
        import raw_events
        if not atomic_sources:
            raw_events.link_memory_sources('bond_memory', new_id, source_event_ids or [])
    except Exception:
        pass
    return True


def _merge_retains_target_signal(new_content, old_content):
    """Require evidence of every merged fragment without treating length as truth."""
    return _loosely_matches(new_content, old_content)


def merge_bond_memories(user_id, character_id, kind, replaces, new_content,
                        source_event_ids=None):
    """★ 记忆合并:把几条零散的旧记忆替换成一条更完整的。

    场景:同一件事分几次聊,库里存成 3-5 条碎片
      "她让我把尾巴改成可拆卸"
      "她说要做Q版的"
      "她说六眼要还原"
    合并成:"她要给我做Q版手办:猫耳、尾巴可拆卸、六眼还原,资金到位要几个月"

    安全限制(长期使用必须严格,删错东西比漏记更糟):
      1. 一次最多替换 3 条
      2. 每条 replaces 必须在库里真实存在(用相似度匹配,允许 LLM 复述有出入)
      3. 新内容必须对每条被替换记忆保留可匹配的信息信号；允许语义压缩
      4. 匹配不到的 replaces 直接忽略,不影响其他条目
      5. 全过程打日志,可追溯

    返回 (是否成功, 实际删除条数)
    """
    if not new_content or not isinstance(replaces, list) or not replaces:
        return False, 0
    replaces = [r for r in replaces if isinstance(r, str) and r.strip()][:3]
    if not replaces:
        return False, 0
    source_ids = _source_ids_from_refs(source_event_ids)

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, content FROM bond_memory
               WHERE user_id=%s AND character_id=%s AND kind=%s
                 AND COALESCE(recall_status, 'active') = 'active'
                 AND authority IS DISTINCT FROM 'canonical_evidence_v1'
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)''',
            (user_id, character_id, kind)
        )
        rows = cur.fetchall()

        # 找出真实存在的目标。
        # ★ 这里用【宽松匹配】,不用 _too_similar ——
        #   LLM 复述旧记忆时常有出入(漏字、改标点、换语序),
        #   严格匹配会导致合并请求全部落空。
        #   宽松没关系:后面还会确认新内容保留了每个旧目标的信息信号。
        targets = []
        for want in replaces:
            best = None
            for mid, mcontent in rows:
                if mid in [t[0] for t in targets]:
                    continue
                if _loosely_matches(want, mcontent):
                    best = (mid, mcontent)
                    break
            if best:
                targets.append(best)
            else:
                print(f'[{user_id}] 合并:找不到要替换的旧记忆,跳过 →「{want[:30]}」')

        if not targets:
            cur.close(); conn.close()
            return False, 0

        # 字数是弱信号：短句可以完整压缩长句，不能单独否决 merge。
        longest_old = max(len(c) for _i, c in targets)
        if len(new_content) < longest_old:
            print(
                f'[{user_id}] 合并内容较短({len(new_content)}<{longest_old})，'
                '继续检查每条旧记忆的保留信号'
            )

        missing = [
            old for _mid, old in targets
            if not _merge_retains_target_signal(new_content, old)
        ]
        if missing:
            print(
                f'[{user_id}] ❌ 合并被拒:无法确认新内容保留 '
                f'{len(missing)} 条旧记忆的信息'
            )
            cur.close(); conn.close()
            return False, 0

        # This is the last provenance check before replacing old rows. A
        # deleted selected user source must not trigger a destructive merge.
        if source_ids:
            import raw_events
            if not raw_events.sources_are_active(
                    source_ids, user_id, character_id):
                print(f'[{user_id}] skip merge: required source deleted')
                return False, 0

        ids = [i for i, _c in targets]
        cur.execute('DELETE FROM bond_memory WHERE id = ANY(%s)', (ids,))
        deleted = cur.rowcount
        cur.execute(
            'INSERT INTO bond_memory (user_id, character_id, kind, content) VALUES (%s,%s,%s,%s) RETURNING id',
            (user_id, character_id, kind, new_content)
        )
        new_id = cur.fetchone()[0]

        # ★ 合并后重新挂 linked_fact_id，否则二级召回断链
        try:
            from smart_recall import link_bond_to_fact
            fact_id = link_bond_to_fact(user_id, character_id, new_content)
            if fact_id:
                cur.execute('UPDATE bond_memory SET linked_fact_id = %s WHERE id = %s',
                            (fact_id, new_id))
                print(f'[{user_id}] 🔗 合并后 bond #{new_id} 关联到 fact #{fact_id}')
        except Exception:
            pass

        conn.commit()

        for _i, old in targets:
            print(f'[{user_id}] 🔗 合并吸收:「{old[:40]}」')
        print(f'[{user_id}] ✅ 合并完成 #{new_id}(替换 {deleted} 条):{new_content}')
    finally:
        cur.close()
        conn.close()

    try:
        if source_ids:
            import raw_events
            if not raw_events.sources_are_active(source_ids, user_id, character_id):
                print(f'[{user_id}] skip merge provenance link: required source deleted')
            else:
                raw_events.link_memory_sources('bond_memory', new_id, source_ids)
    except Exception:
        pass

    notify_memory_changed('bond_memory', row_id=new_id, content=new_content)
    return True, deleted


BOND_RESOLUTION_REASONS = frozenset({
    'completed', 'cancelled', 'superseded', 'corrected',
})


def resolve_bond_memories(user_id, character_id, kind, replaces,
                          new_content=None, reason='superseded', *, exact=False):
    """Close resolved/cancelled/superseded bonds without DELETE.

    bond_merge is additive (old event still true, more detail).
    This is terminal: the old pending condition is no longer active recall.
    """
    if not isinstance(replaces, list) or not replaces:
        return False, []
    replaces = [item for item in replaces if isinstance(item, str) and item.strip()][:5]
    if not replaces:
        return False, []
    reason = str(reason or 'superseded').strip().lower()
    if reason not in BOND_RESOLUTION_REASONS:
        reason = 'superseded'

    conn = get_conn()
    cur = conn.cursor()
    targets = []
    try:
        cur.execute(
            '''SELECT id, content FROM bond_memory
               WHERE user_id=%s AND character_id=%s AND kind=%s
                 AND COALESCE(recall_status, 'active') = 'active'
                 AND authority IS DISTINCT FROM 'canonical_evidence_v1' ''',
            (user_id, character_id, kind)
        )
        rows = cur.fetchall()
        for want in replaces:
            best = None
            for mid, mcontent in rows:
                if mid in [item[0] for item in targets]:
                    continue
                if (_clean_for_compare(want) == _clean_for_compare(mcontent)
                        if exact else _loosely_matches(want, mcontent)):
                    best = (mid, mcontent)
                    break
            if best:
                targets.append(best)
            else:
                print(f'[{user_id}] 关闭:找不到要结束的旧记忆,跳过 →「{want[:30]}」')
        if not targets:
            return False, []
        ids = [mid for mid, _content in targets]
        cur.execute(
            '''UPDATE bond_memory
               SET recall_status = 'superseded'
               WHERE id = ANY(%s)
                 AND COALESCE(recall_status, 'active') = 'active'
                 AND authority IS DISTINCT FROM 'canonical_evidence_v1' ''',
            (ids,),
        )
        conn.commit()
        for mid, old in targets:
            print(
                f'[{user_id}] bond #{mid} recall_status=superseded '
                f'({reason}): {old[:40]}'
            )
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    summary = _clean_content(new_content) if new_content else ''
    if summary and not any(_loosely_matches(summary, old) for _mid, old in targets):
        if _valid_bond(user_id, summary):
            save_bond_memory(user_id, character_id, kind, summary)
        else:
            print(f'[{user_id}] 关闭后的结果摘要不合规,只结束旧记录:{summary[:40]}')
    elif summary:
        print(f'[{user_id}] 跳过把已结束条件再写成 active bond:{summary[:40]}')
    return True, targets


def invalidate_bond_memories(user_id, character_id, kind, memory_ids,
                             *, reason='invalid'):
    """Retire unsupported derived bonds without deleting their history or sources."""
    ids = []
    for memory_id in memory_ids or []:
        try:
            value = int(memory_id)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return False, []

    conn = get_conn()
    cur = conn.cursor()
    rows = []
    try:
        cur.execute(
            '''SELECT id, content FROM bond_memory
               WHERE user_id=%s AND character_id=%s AND kind=%s
                 AND id = ANY(%s)
                 AND COALESCE(recall_status, 'active') = 'active'
                 AND authority IS DISTINCT FROM 'canonical_evidence_v1' ''',
            (user_id, character_id, kind, ids),
        )
        rows = cur.fetchall()
        if not rows:
            return False, []
        cur.execute(
            '''UPDATE bond_memory
               SET recall_status = 'deleted'
               WHERE id = ANY(%s)
                 AND COALESCE(recall_status, 'active') = 'active'
                 AND authority IS DISTINCT FROM 'canonical_evidence_v1' ''',
            ([memory_id for memory_id, _content in rows],),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    for memory_id, content in rows:
        print(
            f'[{user_id}] bond #{memory_id} recall_status=deleted '
            f'({reason}): {content[:40]}'
        )
    return True, rows


def get_bond_memories(user_id, character_id, kind=None, limit=30):
    """返回 [(id, content, timestamp)]，新→旧。kind=None 时返回全部种类。"""
    conn = get_conn()
    cur = conn.cursor()
    if kind:
        cur.execute(
            f'''SELECT id, content, timestamp FROM bond_memory
               WHERE user_id = %s AND character_id = %s AND kind = %s
                 AND {authoritative_memory_sql('bond_memory')}
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
               ORDER BY timestamp DESC LIMIT %s''',
            (user_id, character_id, kind, limit)
        )
    else:
        cur.execute(
            f'''SELECT id, content, timestamp FROM bond_memory
               WHERE user_id = %s AND character_id = %s
                 AND {authoritative_memory_sql('bond_memory')}
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
               ORDER BY timestamp DESC LIMIT %s''',
            (user_id, character_id, limit)
        )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def delete_bond_memory(memory_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM bond_memory WHERE id = %s', (memory_id,))
    conn.commit()
    cur.close()
    conn.close()
    notify_memory_changed('bond_memory', deleted=True)


# ────────── 认识时长（按角色最早共同痕迹算，不是全局app天数）──────────

def get_first_interaction_days(user_id, character_id):
    """返回和【这个角色】最早的共同痕迹距今多少天；完全没有痕迹返回 None。
    依据：持久时间账本 + 羁绊记忆 + 角色专属长期记忆 + 短期记忆，取最早时间。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f'''SELECT LEAST(
        COALESCE((SELECT MIN(first_interaction_at) FROM temporal_awareness WHERE user_id=%s AND character_id=%s), 'infinity'::timestamp),
        COALESCE((SELECT MIN(timestamp) FROM bond_memory  WHERE user_id=%s AND character_id=%s AND {authoritative_memory_sql('bond_memory')}), 'infinity'::timestamp),
        COALESCE((SELECT MIN(timestamp) FROM long_memory  WHERE user_id=%s AND character_id=%s AND {authoritative_memory_sql('long_memory')}), 'infinity'::timestamp),
        COALESCE((SELECT MIN(timestamp) FROM short_memory WHERE user_id=%s AND character_id=%s), 'infinity'::timestamp)
    )''', (user_id, character_id, user_id, character_id,
            user_id, character_id, user_id, character_id))
    row = cur.fetchone()
    cur.close()
    conn.close()
    earliest = row[0] if row else None
    # psycopg2 会把 'infinity' 转成 9999 年的 datetime.max
    if earliest is None or str(earliest) == 'infinity' or getattr(earliest, 'year', 0) >= 9000:
        return None
    days = (datetime.utcnow() - earliest).days
    return max(days, 0)


# ────────── 用户统计（聊天天数）──────────

def update_chat_days(user_id):
    today = datetime.now(CN_TZ).strftime('%Y-%m-%d')
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT first_chat_date, last_chat_date, total_days FROM user_stats WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            'INSERT INTO user_stats (user_id, first_chat_date, last_chat_date, total_days) VALUES (%s, %s, %s, 1)',
            (user_id, today, today)
        )
        total_days = 1
    else:
        first_date, last_date, total_days = row
        if last_date != today:
            total_days += 1
            cur.execute(
                'UPDATE user_stats SET last_chat_date = %s, total_days = %s WHERE user_id = %s',
                (today, total_days, user_id)
            )
    conn.commit()
    cur.close()
    conn.close()
    return total_days


def get_chat_days(user_id):
    """实际"聊过天的天数"（不含没说话的日子）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT total_days FROM user_stats WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else 0


def get_companion_days(user_id):
    """★ 陪伴的日子 = 从第一次聊天那天到今天的【日历天数】。
    主页显示用这个：哪怕某天没说话，日子也照样在走——这才叫陪伴。
    （旧的 total_days 只数"开口说过话的天数"，所以会停在 27 不动。）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT first_chat_date FROM user_stats WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row[0]:
        return 0
    try:
        first = datetime.strptime(str(row[0])[:10], '%Y-%m-%d').date()
        today = datetime.now(CN_TZ).date()
        return max((today - first).days + 1, 1)
    except Exception:
        return 0


# ────────── 记忆自动纠错 ──────────

_MEMORY_OBJECT_FIELDS = (
    'user_fact', 'bond', 'told', 'character_self_claim', 'bond_merge',
    'bond_resolution', 'bond_update', 'communication_convention',
    'bond_delta', 'cognitive_update',
)
_MEMORY_TEXT_FIELDS = (
    'content', 'category', 'evidence_quote', 'reason', 'kind', 'actor',
    'question_key', 'question_text', 'value', 'type', 'action', 'slot',
    'symbol', 'user_evidence_quote', 'symbol_event_id', 'symbol_evidence_quote',
    'target',
)


def _validate_memory_item(item, field):
    """Validate wire types only; evidence and meaning stay in the domain gates."""
    if item is None:
        return
    if not isinstance(item, dict):
        raise StructuredOutputError(field + '_not_object')
    for key in _MEMORY_TEXT_FIELDS:
        if key in item and item[key] is not None and not isinstance(item[key], str):
            raise StructuredOutputError(field + '_' + key + '_not_string')
    for key in ('evidence_event_ids', 'user_evidence_event_ids', 'replaces'):
        if key in item and item[key] is not None:
            if (not isinstance(item[key], list)
                    or any(not isinstance(value, str) for value in item[key])):
                raise StructuredOutputError(field + '_' + key + '_not_string_array')
    if 'novel' in item and not isinstance(item['novel'], bool):
        raise StructuredOutputError(field + '_novel_not_boolean')


def _validate_memory_output(value):
    if not any(key in value for key in _MEMORY_OBJECT_FIELDS):
        raise StructuredOutputError('memory_fields_missing')
    for field in _MEMORY_OBJECT_FIELDS:
        _validate_memory_item(value.get(field), field)
    return value


def _validate_group_memory_output(value):
    if not any(key in value for key in ('user_fact', 'told', 'char_bonds')):
        raise StructuredOutputError('group_memory_fields_missing')
    for field in ('user_fact', 'told'):
        _validate_memory_item(value.get(field), field)
    bonds = value.get('char_bonds')
    if bonds is not None:
        if not isinstance(bonds, list):
            raise StructuredOutputError('char_bonds_not_array')
        for item in bonds:
            if item is None:
                raise StructuredOutputError('char_bond_not_object')
            _validate_memory_item(item, 'char_bond')
    return value


def _validate_correction_output(value):
    if value.get('action') not in ('delete', 'none'):
        raise StructuredOutputError('correction_action_invalid')
    ids = value.get('ids')
    if (not isinstance(ids, list)
            or any(type(item) not in (int, str) for item in ids)):
        raise StructuredOutputError('correction_ids_not_array')
    return value


def _validate_delta_output(value):
    if 'bond_delta' not in value:
        raise StructuredOutputError('bond_delta_missing')
    _validate_memory_item(value['bond_delta'], 'bond_delta')
    return value


def plan_memory_corrections(user_id, user_text, character_id=DEFAULT_CHARACTER_ID):
    """Legacy destructive model plans are disabled; canonical revision retains history."""
    return []


def apply_memory_corrections(user_id, ids, character_id=DEFAULT_CHARACTER_ID):
    """提取成功后再删旧记忆。"""
    if not ids:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    deleted = 0
    try:
        for mem_id in ids:
            try:
                mid_int = int(mem_id)
            except (ValueError, TypeError):
                continue
            cur.execute(
                '''DELETE FROM long_memory
                   WHERE id = %s AND user_id = %s AND character_id IN (%s, %s)''',
                (mid_int, user_id, character_id, SHARED_CHARACTER_ID)
            )
            if cur.rowcount:
                deleted += cur.rowcount
                print(f'[{user_id}] ✂️ 纠错删除记忆 #{mid_int}')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    if deleted:
        notify_memory_changed('long_memory', deleted=True)
        print(f'[{user_id}] 纠错完成：删除了 {deleted} 条旧记忆')
    return deleted


def correct_memories(user_id, user_text, character_id=DEFAULT_CHARACTER_ID):
    """兼容旧调用：只计划、不删除。真正删除请走 extract 成功后的 apply。"""
    return bool(plan_memory_corrections(user_id, user_text, character_id))


# ────────── 提取结果的通用校验小工具 ──────────

def _clean_content(raw_content):
    return (raw_content or '').strip().strip('「」"\'').rstrip('。.')


_SELF_CLAIM_RE = re.compile(
    r'(我的(风格|性格|方式|习惯|倾向)|'
    r'我(就是|本来就是|一向|向来|通常|习惯|倾向|不擅长|擅长|属于)|'
    r'我(是|算是).{0,12}(这种|那种|这样|那样).{0,12}(人|性格)|'
    r'我(不太|很|比较)?(会|不会).{0,8}(直接|主动|轻易).{0,8}(回应|表达|承认))'
)


def _looks_like_character_self_claim(content):
    """Detect first-person self-model claims before they become bond facts."""
    text = re.sub(r'\s+', '', str(content or ''))
    if not text.startswith('我'):
        return False
    return bool(_SELF_CLAIM_RE.search(text))


def _short_excerpt(text, limit=160):
    return re.sub(r'\s+', ' ', str(text or '')).strip()[:limit]


_MAX_PREVIOUS_CANONICAL_USER_EVIDENCE_EVENTS = 2
_CANONICAL_USER_EVIDENCE_HOURS = 24
_ROLEPLAY_MARKERS = (
    '剧本', '人设', '设定', '扮演', '角色扮演', 'cosplay', 'roleplay',
    'role-play', 'ロールプレイ', 'ロールプレー', 'なりきり', '台本',
)
_STABLE_SELF_CLAIM_RE = re.compile(
    r'(一向|向来|通常|总是|习惯|风格|性格|原则|我这个人|'
    r'不擅长|擅长|不会轻易|会直接|一直)'
)
_USER_ASSERTS_CHARACTER_HISTORY_RE = re.compile(
    r'(?:你|您|あなた|君|お前|you).{0,12}'
    r'(?:之前|以前|上次|曾经|明明|本来|前に|以前|この前|before)?.{0,12}'
    r'(?:答应|承诺|说过|约过|約束|言った|promise(?:d)?|said)'
)


def _compact_evidence(text):
    """Normalize display-only differences while preserving meaningful Unicode."""
    normalized = unicodedata.normalize('NFKC', str(text or '')).casefold()
    return ''.join(
        ch for ch in normalized
        if not unicodedata.category(ch).startswith(('P', 'Z', 'C'))
    )


def _evidence_quote(item):
    if not isinstance(item, dict):
        return ''
    return _clean_content(item.get('evidence_quote'))


def _evidence_events(source):
    """Normalize canonical evidence without introducing a second representation."""
    if isinstance(source, str):
        return [{'event_id': '', 'content': source}]
    out = []
    for event in source or []:
        if not isinstance(event, dict):
            continue
        content = str(event.get('content') or '')
        if not content:
            continue
        event_id = str(event.get('event_id') or '').strip()
        out.append({'event_id': event_id, 'content': content})
    return out


def _candidate_evidence_event_ids(item, events, primary_event_id):
    """Keep extractor hints inside the job's canonical user provenance."""
    known = {
        event['event_id']: event for event in events
        if event.get('event_id')
    }
    if not known:
        return []

    primary_event_id = str(primary_event_id or '').strip()
    if primary_event_id and primary_event_id not in known:
        return None

    requested = item.get('evidence_event_ids') if isinstance(item, dict) else None
    if requested is None:
        # Single-event output from an older extractor remains safe: infer only
        # the event that contains its verbatim anchor. Multi-event summaries
        # must name their sources explicitly so their provenance is retained.
        quote = _compact_evidence(_evidence_quote(item))
        ids = [
            event_id for event_id, event in known.items()
            if quote and quote in _compact_evidence(event['content'])
        ]
    elif not isinstance(requested, list):
        return None
    else:
        ids = []
        for value in requested:
            event_id = str(value or '').strip()
            if event_id and event_id not in ids:
                ids.append(event_id)

    # The memory job's primary canonical user event is authoritative. A
    # list-shaped extractor hint may suggest extra canonical context, but a
    # bad list must never select another historical event. Bind it to the
    # primary and let the quote gate verify that binding.
    if (primary_event_id
            and (not ids
                 or any(event_id not in known for event_id in ids)
                 or primary_event_id not in ids)):
        return [primary_event_id]
    if not ids or any(event_id not in known for event_id in ids):
        return None
    return ids


def _has_faithful_evidence(user_id, item, source, label, primary_event_id=None):
    """Return canonical provenance for a candidate with a real source anchor."""
    quote = _evidence_quote(item)
    compact_quote = _compact_evidence(quote)
    if not compact_quote:
        print(f'[{user_id}] ❌ {label} 拒绝（缺少原文证据锚点）：{quote}')
        return None

    events = _evidence_events(source)
    event_ids = _candidate_evidence_event_ids(item, events, primary_event_id)
    if event_ids is None:
        print(f'[{user_id}] ❌ {label} 拒绝（evidence_event_ids 不指向本轮 canonical 用户事件）')
        return None
    selected = [
        event for event in events
        if not event_ids or event.get('event_id') in event_ids
    ]
    if not any(compact_quote in _compact_evidence(event['content']) for event in selected):
        print(f'[{user_id}] ❌ {label} 拒绝（证据不在权威原文中）：{quote}')
        return None
    return {
        'event_ids': event_ids,
        'events': selected,
        'compact_quote': compact_quote,
    }


def _has_japanese_kana(text):
    return any(
        '\u3040' <= ch <= '\u30ff' for ch in str(text or '')
    )


def _is_faithful_compression(user_id, content, evidence, label):
    """Require a conservative, language-neutral support signal for summaries.

    The quote is a provenance anchor, not a dictionary of allowed meanings.
    For same-script text, require an actual shared phrase between the candidate
    and its selected canonical evidence. Japanese-to-Chinese summaries cannot
    be judged by character overlap, so they need selected canonical context
    beyond the quote anchor itself (another selected event or more text in its
    source event) plus the extractor's conservative schema instruction.
    """
    compact_content = _compact_evidence(content)
    source_text = ''.join(event['content'] for event in evidence['events'])
    compact_source = _compact_evidence(source_text)
    if not compact_content or not compact_source:
        return False
    if _has_japanese_kana(content) != _has_japanese_kana(source_text):
        if any(
                _compact_evidence(event['content']) != evidence['compact_quote']
                for event in evidence['events']):
            return True
        print(
            f'[{user_id}] ❌ {label} 拒绝（跨语言 quote 没有额外 canonical 上下文）：'
            f'{source_text}'
        )
        return False
    shared = SequenceMatcher(
        None, compact_content, compact_source, autojunk=False
    ).find_longest_match().size
    min_shared = 2 if min(len(compact_content), len(compact_source)) <= 4 else 3
    if shared < min_shared:
        print(
            f'[{user_id}] ❌ {label} 拒绝（canonical evidence 无法支持该压缩）：'
            f'{content}'
        )
        return False
    return True


def _looks_like_question(text):
    raw = str(text or '')
    compact = _compact_evidence(raw)
    return (
        '?' in raw or '？' in raw
        or bool(re.search(r'(能不能|可不可以|要不要|会不会|是不是|有没有)', compact))
        or compact.endswith(('吗', '么', '呢', 'か'))
    )


def _looks_like_roleplay(text):
    compact = _compact_evidence(text)
    return any(marker in compact for marker in _ROLEPLAY_MARKERS)


def _valid_durable_bond(user_id, content, char_name, item,
                        canonical_user_events, primary_event_id=None,
                        allow_existing_memory=False):
    """Accept only a current, user-grounded shared event as a durable bond."""
    if not _valid_bond(user_id, content, char_name):
        return None
    evidence = _has_faithful_evidence(
        user_id, item, canonical_user_events, 'bond', primary_event_id)
    if not evidence:
        return None
    if (not allow_existing_memory
            and not _is_faithful_compression(user_id, content, evidence, 'bond')):
        return None
    quote = _evidence_quote(item)
    if _looks_like_question(quote) or _looks_like_roleplay(quote + content):
        print(f'[{user_id}] ❌ bond 拒绝（单轮提问或 roleplay）：{content}')
        return None
    if _USER_ASSERTS_CHARACTER_HISTORY_RE.search(quote):
        print(f'[{user_id}] ❌ bond 拒绝（用户对角色旧话的断言不是已验证历史）：{content}')
        return None
    if content.startswith('我') and not content.startswith(('我和她', '我们')):
        print(f'[{user_id}] ❌ bond 拒绝（角色单次自述不能成为关系事实）：{content}')
        return None
    if char_name and content.startswith(char_name):
        print(f'[{user_id}] ❌ bond 拒绝（角色单次自述不能成为关系事实）：{content}')
        return None
    if content.startswith(('她问', '我问', '我们聊')):
        print(f'[{user_id}] ❌ bond 拒绝（聊天过程不是 durable event）：{content}')
        return None
    return evidence


# A communication convention is still a normal ``bond_memory(kind='between')``.
# The marker makes its semantic payload available to the existing recall scorer;
# it does not introduce another memory store or relationship model.
COMMUNICATION_CONVENTION_MARKER = '【交流约定】'
_COMMUNICATION_CONVENTION_PAYLOAD_RE = re.compile(
    rf'{re.escape(COMMUNICATION_CONVENTION_MARKER)}.*?「([^」]{{1,32}})」')
_COMMUNICATION_CONVENTION_SLOT_RE = re.compile(
    rf'{re.escape(COMMUNICATION_CONVENTION_MARKER)}(?:（槽位：([^）]{{1,32}})）)?')
_CONVENTION_SET_CUE_RE = re.compile(
    r'(约定|约好|说好|(?:当|作|用).{0,8}(暗号|信号)|'
    r'(我们|咱们).{0,12}(暗号|信号|专属).{0,12}(是|用|回|回复)|'
    r'固定.{0,8}(回|回复|符号|表情|用)|'
    r'以后.{0,12}(回|回复|用|发)|记住.{0,12}(回|回复|符号|这个)|'
    r'約束|from now on|set\s+(?:a\s+)?(?:symbol|signal))', re.I)
_CONVENTION_REPLACE_CUE_RE = re.compile(
    r'(改成|换成|改用|换用|替换|从.{0,16}(改|换)|変更|change(?:d)?\s+to)', re.I)
_CONVENTION_REVOKE_CUE_RE = re.compile(
    r'(取消|撤销|作废|不算|不要再用|别再用|不再(用|算)|废除|やめ|'
    r'revoke|cancel)', re.I)
_CONVENTION_SET_NEGATION_RE = re.compile(
    r'(?:不(?:是|算).{0,12}(?:约定|约好|说好|暗号|信号|专属|固定|规则)|'
    r'(?:别|不要|不用).{0,12}(?:记|记住|约定|当作.{0,4}约定|这个|它)|'
    r'(?:这|这个).{0,8}(?:不是|不算).{0,6}(?:约定|暗号|规则))', re.I)


def _normalize_convention_symbol(raw):
    """Accept a short visible symbol payload without naming any one emoji."""
    if not isinstance(raw, str):
        return ''
    symbol = raw.strip()
    if not symbol or len(symbol) > 32 or any(ch in symbol for ch in '\r\n「」'):
        return ''
    if is_emoji_only(symbol):
        return symbol
    has_symbol = False
    for char in symbol:
        category = unicodedata.category(char)
        codepoint = ord(char)
        if category.startswith(('S', 'P')):
            has_symbol = True
            continue
        if category in ('Mn', 'Me', 'Sk') or codepoint in (0x200D, 0xFE0E, 0xFE0F):
            continue
        return ''
    return symbol if has_symbol else ''


def _normalize_convention_slot(raw):
    """Keep a short, user-grounded rule label without adding a schema column."""
    if not isinstance(raw, str):
        return ''
    slot = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', raw).strip())
    if (not slot or len(slot) > 32
            or any(ch in slot for ch in '\r\n【】（）「」')):
        return ''
    return slot if any(ch.isalnum() for ch in slot) else ''


def _communication_convention_content(symbol, slot=''):
    slot = _normalize_convention_slot(slot)
    slot_label = f'（槽位：{slot}）' if slot else ''
    return (
        f'我和她的{COMMUNICATION_CONVENTION_MARKER}{slot_label}：在私聊中，「{symbol}」是我们'
        '约定好的专属交流回复符号'
    )


def _communication_convention_payload(content):
    match = _COMMUNICATION_CONVENTION_PAYLOAD_RE.search(str(content or ''))
    return match.group(1) if match else ''


def _communication_convention_slot(content):
    match = _COMMUNICATION_CONVENTION_SLOT_RE.search(str(content or ''))
    return _normalize_convention_slot(match.group(1) if match else '')


def _is_communication_convention(content):
    return COMMUNICATION_CONVENTION_MARKER in str(content or '')


def _convention_event_map(*event_groups):
    events = {}
    for group in event_groups:
        for event in group or []:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get('event_id') or '').strip()
            if event_id and event.get('content'):
                events[event_id] = event
    return events


def _valid_convention_user_evidence(user_id, item, canonical_user_events,
                                    primary_event_id):
    candidate = {
        'evidence_quote': item.get('user_evidence_quote'),
        'evidence_event_ids': item.get('user_evidence_event_ids'),
    }
    return _has_faithful_evidence(
        user_id, candidate, canonical_user_events,
        'communication_convention', primary_event_id)


def _explicit_convention_action(action, evidence):
    text = ''.join(str(event.get('content') or '') for event in evidence['events'])
    if action == 'set':
        return (not _CONVENTION_SET_NEGATION_RE.search(text)
                and bool(_CONVENTION_SET_CUE_RE.search(text)))
    if action == 'replace':
        return bool(_CONVENTION_REPLACE_CUE_RE.search(text))
    if action == 'revoke':
        return bool(_CONVENTION_REVOKE_CUE_RE.search(text))
    return False


def _convention_symbol_source(item, symbol, event_map, user_evidence):
    event_id = str(item.get('symbol_event_id') or '').strip()
    quote = str(item.get('symbol_evidence_quote') or '').strip()
    event = event_map.get(event_id)
    if not event or not quote:
        return None
    content = str(event.get('content') or '')
    if quote not in content or symbol not in content:
        return None
    role = str(event.get('role') or '')
    if role == 'user' and event_id not in user_evidence['event_ids']:
        return None
    if role not in ('user', 'assistant'):
        return None
    return event_id


def _convention_slot_is_grounded(slot, user_evidence):
    """A candidate slot is metadata only when its wording is in user evidence."""
    slot_key = _compact_evidence(slot)
    if len(slot_key) < 2:
        return False
    evidence_text = ''.join(
        str(event.get('content') or '') for event in user_evidence['events'])
    return slot_key in _compact_evidence(evidence_text)


def _convention_replacement_targets(item, existing_bond, user_evidence, slot=''):
    """Resolve one explicitly identified active convention, never a vague batch."""
    requested = item.get('replaces')
    if not isinstance(requested, list) or len(requested) != 1:
        return []
    active = [
        row[1] for row in existing_bond or []
        if len(row) > 1 and _is_communication_convention(row[1])
    ]
    want = requested[0]
    if not isinstance(want, str) or not want.strip():
        return []
    matches = [
        content for content in active
        if _clean_for_compare(want) == _clean_for_compare(content)
    ]
    if len(matches) != 1:
        return []

    target = matches[0]
    target_slot = _communication_convention_slot(target)
    if (slot and target_slot
            and _clean_for_compare(slot) != _clean_for_compare(target_slot)):
        return []

    evidence_text = _compact_evidence(''.join(
        str(event.get('content') or '') for event in user_evidence['events']))
    payload = _communication_convention_payload(target)
    mentions_payload = bool(
        payload and _compact_evidence(payload) in evidence_text)
    mentions_slot = bool(
        target_slot and _compact_evidence(target_slot) in evidence_text)
    # A vague "that one" is only resolvable when there is exactly one active
    # convention.  In a multi-convention state the extractor cannot choose.
    if not (mentions_payload or mentions_slot or len(active) == 1):
        return []
    return [target]


def _apply_communication_convention(
        user_id, character_id, item, canonical_user_events, primary_event_id,
        canonical_turn_events, convention_context_events, existing_bond,
        *, dry_run=False, outcome=None):
    """Persist a user-confirmed symbol convention through the existing bond path."""
    outcome = outcome if isinstance(outcome, dict) else {}

    def reject(reason, message):
        outcome.update({'status': 'rejected', 'reason': reason})
        print(f'[{user_id}] communication convention rejected: {message}')
        return False

    if not isinstance(item, dict):
        return reject('invalid_candidate', 'invalid candidate')
    action = str(item.get('action') or '').strip().lower()
    if action not in ('set', 'replace', 'revoke'):
        return reject('invalid_action', 'invalid action')
    user_evidence = _valid_convention_user_evidence(
        user_id, item, canonical_user_events, primary_event_id)
    if not user_evidence or not _explicit_convention_action(action, user_evidence):
        return reject('no_explicit_user_action', 'no explicit user action')

    slot = _normalize_convention_slot(item.get('slot'))
    if item.get('slot') is not None and not slot:
        return reject('invalid_slot', 'invalid slot')
    if slot and not _convention_slot_is_grounded(slot, user_evidence):
        return reject('ungrounded_slot', 'ungrounded slot')

    symbol = ''
    source_ids = list(user_evidence['event_ids'])
    if action != 'revoke':
        symbol = _normalize_convention_symbol(item.get('symbol'))
        event_map = _convention_event_map(
            convention_context_events, canonical_turn_events)
        symbol_event_id = _convention_symbol_source(
            item, symbol, event_map, user_evidence) if symbol else None
        if not symbol_event_id:
            return reject('unverified_symbol_payload', 'unverified symbol payload')
        if symbol_event_id not in source_ids:
            source_ids.append(symbol_event_id)

    targets = []
    active_conventions = [
        row[1] for row in existing_bond or []
        if len(row) > 1 and _is_communication_convention(row[1])
    ]
    content = _communication_convention_content(symbol, slot) if symbol else ''
    if action == 'set' and symbol:
        already_active = any(
            _clean_for_compare(existing) == _clean_for_compare(content)
            for existing in active_conventions)
        if not already_active and active_conventions and not slot:
            return reject('slot_required_for_second_convention',
                          'slot required for a second convention')
        if (not already_active and slot and any(
                _clean_for_compare(slot)
                == _clean_for_compare(_communication_convention_slot(existing))
                for existing in active_conventions
                if _communication_convention_slot(existing))):
            return reject('replacement_target_required',
                          'replacement target required')
    if action in ('replace', 'revoke'):
        targets = _convention_replacement_targets(
            item, existing_bond, user_evidence, slot)
        if not targets:
            return reject('no_exact_active_target', 'no exact active target')
        if action == 'replace' and not slot:
            # The target has already been resolved deterministically (by its
            # payload/slot or because it is the sole active convention).  Keep
            # its stored, previously user-grounded slot instead of asking the
            # extractor to recreate it from a vague current reference.
            slot = _communication_convention_slot(targets[0])
            content = _communication_convention_content(symbol, slot)

    try:
        import raw_events
        if source_ids and not raw_events.sources_are_active(
                source_ids, user_id, character_id):
            return reject('source_deleted', 'source deleted')
    except Exception as e:
        return reject('source_validity_unknown', f'source check failed:{e}')

    already_active = bool(content) and any(
        _clean_for_compare(row[1]) == _clean_for_compare(content)
        for row in existing_bond or [] if len(row) > 1
    )
    if action == 'replace' and targets and _clean_for_compare(content) == _clean_for_compare(targets[0]):
        # Replaying an already-applied replacement must not retire its only
        # active row merely because the new payload equals the target payload.
        already_active = True
        targets = []

    status = {
        'set': 'unchanged' if already_active else 'would_add',
        'replace': 'unchanged' if not targets else 'would_replace',
        'revoke': 'would_revoke',
    }[action]
    outcome.update({
        'status': status,
        'action': action,
        'symbol': symbol or None,
        'slot': slot or None,
        'content': content or None,
        'source_event_ids': list(source_ids),
        'target_contents': list(targets),
    })
    if dry_run:
        print(f'[{user_id}] communication convention dry-run: {status}')
        return True

    if status == 'unchanged':
        print(f'[{user_id}] communication convention unchanged')
        return True

    if action != 'revoke':
        saved = already_active or save_bond_memory(
            user_id, character_id, 'between', content,
            source_event_ids=source_ids,
            atomic_sources=True,
        )
        if not saved:
            # Retry after a crash between saving the new record and retiring the
            # old one: an already-active identical record is enough to finish.
            try:
                saved = any(
                    _clean_for_compare(row[1]) == _clean_for_compare(content)
                    for row in get_bond_memories(user_id, character_id, 'between', limit=100)
                )
            except Exception:
                saved = False
        if not saved:
            print(f'[{user_id}] communication convention was not saved')
            return False

    if targets:
        reason = 'cancelled' if action == 'revoke' else 'superseded'
        resolved, rows = resolve_bond_memories(
            user_id, character_id, 'between', targets,
            reason=reason, exact=True)
        if not resolved:
            raise ValueError('communication_convention_resolution_failed')
        try:
            import raw_events
            for memory_id, _content in rows:
                raw_events.link_memory_sources('bond_memory', memory_id, source_ids)
        except Exception as e:
            print(f'[{user_id}] communication convention revision provenance skipped:{e}')

    print(f'[{user_id}] communication convention {action}: {symbol or "revoked"}')
    return True


def _valid_stable_character_self_claim(user_id, content, item, assistant_text):
    """Keep only explicit, general self-model evidence; never a one-turn excuse."""
    if not _looks_like_character_self_claim(content):
        return False
    if not _has_faithful_evidence(
            user_id, item, assistant_text, 'character_self_claim'):
        return False
    quote = _evidence_quote(item)
    if (_looks_like_question(quote) or _looks_like_roleplay(quote + content)
            or not _STABLE_SELF_CLAIM_RE.search(content)):
        print(f'[{user_id}] ❌ self-claim 拒绝（非稳定自我模型）：{content}')
        return False
    return True


def _canonical_turn_sources(user_id, character_id, source_event_ids,
                            primary_event_id, user_text, assistant_text):
    """Prefer existing chat_log provenance without creating another event store."""
    result = {
        'user_text': user_text,
        'assistant_text': assistant_text,
        'user_is_canonical': False,
        'assistant_is_canonical': False,
        'canonical_user_events': [],
        'canonical_turn_events': [],
        'convention_context_events': [],
    }
    if not source_event_ids:
        result['canonical_user_events'] = [
            {'event_id': '', 'content': user_text}
        ] if user_text else []
        return result
    import raw_events
    getter = getattr(raw_events, 'get_active_events_by_ids', None)
    if not callable(getter):
        raise raw_events.SourceValidityError('canonical source reader unavailable')

    events = getter(user_id, character_id, source_event_ids)
    by_id = {
        str(event.get('event_id') or ''): event
        for event in events if isinstance(event, dict)
    }
    primary = by_id.get(str(primary_event_id or ''))
    if not primary or str(primary.get('role') or '') != 'user' or not primary.get('content'):
        # No active row is not permission to trust the copied job payload. It
        # may simply be waiting for its canonical commit, so the queue retries
        # it a bounded number of times; explicit deletion was handled earlier.
        raise raw_events.SourceValidityError('canonical primary user event unavailable')

    result['user_text'] = str(primary['content'])
    result['user_is_canonical'] = True
    result['canonical_turn_events'] = [dict(primary)]
    canonical_users = [{
        'event_id': str(primary.get('event_id') or ''),
        'content': result['user_text'],
    }]
    for event in events:
        role = str(event.get('role') or '')
        content = str(event.get('content') or '')
        if role == 'assistant' and content:
            result['assistant_text'] = content
            result['assistant_is_canonical'] = True
            # Only the actual reply to this user event can support a decision
            # delta. Arbitrary assistant rows carried by a job are not evidence.
            event_id = str(event.get('event_id') or '')
            meta = event.get('metadata') or {}
            reply_to = (event.get('reply_to_event_id') or meta.get('reply_to_event_id')
                        or (event.get('extra') or {}).get('reply_to_event_id'))
            if reply_to and str(reply_to) != str(primary_event_id):
                continue
            if (event_id in (f'chat_reply:{primary_event_id}', f'image_reply:{primary_event_id}')
                    or str(event.get('reply_to_event_id') or meta.get('reply_to_event_id') or '')
                    == str(primary_event_id)):
                linked_event = dict(event)
                linked_event['reply_to_event_id'] = str(primary_event_id)
                result['canonical_turn_events'].append(linked_event)

    # A short confirmation can complete one of the two immediately preceding
    # user turns. The raw-event query enforces the existing chat_log scope,
    # active status, strict temporal ordering, and 24-hour bound; never use
    # arbitrary user rows carried alongside the job's primary source ID.
    prior_getter = getattr(raw_events, 'get_previous_active_user_events', None)
    if not callable(prior_getter):
        raise raw_events.SourceValidityError(
            'previous canonical evidence reader unavailable')
    prior_events = prior_getter(
        user_id, character_id, primary_event_id,
        n=_MAX_PREVIOUS_CANONICAL_USER_EVIDENCE_EVENTS,
        hours=_CANONICAL_USER_EVIDENCE_HOURS,
    )
    known_ids = {item['event_id'] for item in canonical_users}
    prior = []
    for event in prior_events:
        if str(event.get('role') or '') != 'user':
            continue
        event_id = str(event.get('event_id') or '')
        content = str(event.get('content') or '')
        if not event_id or not content or event_id in known_ids:
            continue
        prior.append({'event_id': event_id, 'content': content})
        known_ids.add(event_id)
    canonical_users = prior + canonical_users
    result['canonical_user_events'] = canonical_users
    nearby_getter = getattr(raw_events, 'get_previous_active_turn_events', None)
    if callable(nearby_getter):
        try:
            result['convention_context_events'] = nearby_getter(
                user_id, character_id, primary_event_id, n=6,
                hours=_CANONICAL_USER_EVIDENCE_HOURS,
            )
        except Exception as e:
            # This optional context must never weaken ordinary fact extraction.
            # A convention candidate simply cannot use neighbouring dialogue
            # until the canonical reader is available again.
            print(f'[{user_id}] convention context unavailable:{e}')
    return result


def _canonical_user_evidence_prompt(events):
    lines = []
    for event in events:
        event_id = str(event.get('event_id') or '').strip()
        content = str(event.get('content') or '')
        if event_id and content:
            lines.append(f'- [event_id:{event_id}] {content}')
    return '\n'.join(lines) or '（无可用 canonical 用户事件）'


def _with_evidence_source_refs(kwargs, evidence_event_ids):
    """Extend existing long-memory refs with only the selected user evidence."""
    if not evidence_event_ids:
        return kwargs
    updated = dict(kwargs or {})
    refs = list(updated.get('source_event_refs') or [])
    existing_ids = set(_source_ids_from_refs(refs))
    for event_id in evidence_event_ids:
        if event_id and event_id not in existing_ids:
            refs.append({'source_id': event_id})
            existing_ids.add(event_id)
    updated['source_event_refs'] = refs
    return updated


def _same_event_as_merge(content, merge_content, replaces):
    candidates = [merge_content] + [
        item for item in (replaces or []) if isinstance(item, str)
    ]
    return bool(content) and any(
        _loosely_matches(content, candidate) for candidate in candidates if candidate
    )


from cognitive_events import QUESTION_DELTA_KINDS as _BOND_DELTA_KINDS


def _validated_turn_delta(user_id, item, canonical_events, primary_event_id):
    """A scoped utterance/state change, never a new personality inference.

    Ordinary facts retain their user-only gate. This exception requires the
    real current user and linked assistant events, plus a verbatim anchor in
    the actual assistant answer; copied/generated payloads cannot qualify.
    """
    if not isinstance(item, dict) or not primary_event_id:
        return None
    from cognitive_events import validate_question_operation_evidence
    try:
        validate_question_operation_evidence(item, canonical_events)
    except ValueError:
        return None
    ids = item.get('evidence_event_ids')
    if not isinstance(ids, list) or primary_event_id not in ids:
        return None
    quote = _compact_evidence(_evidence_quote(item))
    if not quote or _looks_like_question(_evidence_quote(item)):
        return None
    actor = item.get('actor', 'character')
    if actor not in ('character', 'user'):
        return None
    role = 'assistant' if actor == 'character' else 'user'
    speakers = [event for event in canonical_events if event.get('role') == role
                and event.get('event_id') in ids]
    if not any(quote in _compact_evidence(event.get('content')) for event in speakers):
        return None
    content = _clean_content(item.get('content'))
    if not content or _looks_like_roleplay(content + _evidence_quote(item)):
        return None
    evidence = _has_faithful_evidence(
        user_id, item, canonical_events, 'explicit_state_delta', primary_event_id)
    if not evidence:
        return None
    # Binary answers already passed the shared full-fragment polarity gate.
    if (str(item.get('value') or '').casefold() not in {'yes', 'no'}
            and not _is_faithful_compression(user_id, content, evidence, 'explicit_state_delta')):
        return None
    return evidence


def _apply_extracted_bond_delta(user_id, character_id, item, canonical_events,
                               primary_event_id, existing_bonds, *, merge_ok=False,
                               merge_rejected=False, merged_content='', merged_replaces=()):
    """Separate overwrite safety from semantic novelty in the same extractor.

    The extractor compares actual existing memories and supplies delta-only
    content. Repeated wording is additionally rejected locally. A rejected
    merge never authorizes appending the full candidate or arbitrary fallback.
    """
    if not isinstance(item, dict):
        if merge_rejected:
            print('[memory_merge] merge_rejected=true novel_delta=false delta_saved=false reason=no_delta')
        return []
    kind = item.get('kind')
    content = _clean_content(item.get('content'))
    evidence = _validated_turn_delta(user_id, item, canonical_events, primary_event_id)
    if (kind not in _BOND_DELTA_KINDS or item.get('novel') is not True or not evidence
            or (kind != 'explicit_promise' and item.get('value') in (None, ''))
            or not item.get('question_text')):
        if merge_rejected:
            reason = 'no_novel_state' if item.get('novel') is not True else 'invalid_delta_evidence_or_schema'
            print('[memory_merge] merge_rejected=true novel_delta=false delta_saved=false reason=' + reason)
        return []
    # Compare against persisted memories, not the model's claimed replaces.
    existing_texts = [str(row[1]) for row in existing_bonds]
    if merge_ok and merged_content:
        # Additive merge success is not proof it included the independent delta.
        existing_texts = [old for old in existing_texts if old not in merged_replaces] + [merged_content]
    requested = item.get('replaces') or []
    exact_targets = [old for old in existing_texts if old in requested
                     and not _too_similar(content, old)]
    if (merge_ok and merged_content and merged_replaces
            and all(old in requested for old in merged_replaces)
            and not _too_similar(content, merged_content)):
        exact_targets.append(merged_content)
    duplicate = any(_too_similar(content, old) for old in existing_texts)
    if duplicate and not exact_targets:
        print('[memory_merge] merge_rejected=%s novel_delta=false delta_saved=false reason=duplicate'
              % str(merge_rejected).lower())
        return []
    import raw_events
    if not raw_events.sources_are_active(evidence['event_ids'], user_id, character_id):
        raise raw_events.SourceValidityError('delta_source_deleted')
    from cognitive_events import ingest_question_update
    update = dict(item)
    update['type'] = 'pending_answer' if kind == 'explicit_promise' else 'resolution'
    result = ingest_question_update(
        user_id=user_id, character_id=character_id, update=update,
        canonical_events=canonical_events)
    if result.get('status') == 'already_resolved':
        return []
    if result.get('status') not in ('resolved', 'inserted', 'pending', 'duplicate', 'active'):
        raise ValueError('explicit_delta_lifecycle_not_committed')
    saved = False
    if not duplicate:
        saved = save_bond_memory(user_id, character_id, 'between', content,
                                 source_event_ids=evidence['event_ids'], atomic_sources=True)
        # On a retry the delta may already exist. Closing old rows is safe only
        # after persistence (or a confirmed duplicate), never before the save.
        if not saved:
            current = get_bond_memories(user_id, character_id, 'between', limit=100)
            if not any(_too_similar(content, row[1]) for row in current):
                raise ValueError('explicit_delta_not_saved')
    replaced = []
    if update['type'] == 'resolution':
        if exact_targets:
            _ok, rows = resolve_bond_memories(
                user_id, character_id, 'between', exact_targets,
                reason='completed', exact=True)
            replaced = [old for _mid, old in rows]
    print('[memory_merge] merge_rejected=%s novel_delta=true delta_saved=%s reason=%s'
          % (str(merge_rejected).lower(), str(saved or duplicate).lower(), kind))
    return replaced


def _record_character_self_claim_evidence(
    user_id,
    character_id,
    content,
    *,
    source_event_id=None,
    user_text='',
    assistant_text='',
):
    """Send self-claims to cognitive evidence instead of bond memory."""
    if not content:
        return None
    seed = f'{user_id}\x00{character_id}\x00{source_event_id or ""}\x00{content}'
    digest = hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]
    event_source_id = (
        f'memory-self-claim:{source_event_id}'
        if source_event_id is not None else f'memory-self-claim:{digest}'
    )
    try:
        from cognitive_events import record_source_event
        from cognitive_triggers import create_trigger_occurrence
        from cognitive_queue import aggregate_pending_triggers

        conn = get_conn()
        try:
            event_id = record_source_event(
                conn,
                user_id=user_id,
                character_id=character_id,
                source_event_type='character_self_claim',
                source_event_id=event_source_id,
                source='memory_extractor_guard',
                occurred_at=datetime.now(timezone.utc),
                payload={
                    'evidence_category': 'character_self_claim',
                    'claim_text': content,
                    'confidence': 'low',
                    'storage_boundary': 'not_bond_memory',
                    'user_text_excerpt': _short_excerpt(user_text),
                    'assistant_text_excerpt': _short_excerpt(assistant_text),
                },
            )
            if event_id is None:
                conn.commit()
                return {'status': 'duplicate', 'event_id': None}
            trigger_id = create_trigger_occurrence(
                conn,
                event_id=event_id,
                user_id=user_id,
                character_id=character_id,
                trigger_class='self_model_evidence',
                occurrence_key='character_self_claim',
                payload={
                    'evidence_category': 'character_self_claim',
                    'confidence_weight': 0.35,
                    'claim_text': content,
                },
            )
            conn.commit()
            cycle = aggregate_pending_triggers(
                user_id, character_id, conn=conn,
            )
            return {
                'status': 'inserted',
                'event_id': event_id,
                'trigger_id': trigger_id,
                'cycle': cycle,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        print(f'[{user_id}] ⚠️ self-claim evidence skipped:{exc}')
        return None


def _valid_user_fact(user_id, content, char_names, category=''):
    """用户事实：必须"她"开头、不含任何角色名（角色相关的应归入 bond/told）。

    ★ 修复:
      - 原来 len<4 会【静默】丢弃,"她叫琳"(3字)直接消失且没有日志 → 降到 3 并打日志
      - 原来任何含角色名的都拒 → 但"她让五条悟叫她琳"这种【称呼类身份信息】
        天然会带角色名,不该被拒。category='身份' 时豁免角色名检查。
    """
    if not content or content == '无':
        return False
    if len(content) < 3:
        print(f'[{user_id}] ❌ user_fact 拒绝（太短 {len(content)} 字）：{content}')
        return False
    if not content.startswith('她'):
        print(f'[{user_id}] ❌ user_fact 拒绝（非"她"开头）：{content}')
        return False
    # ★ 身份类(名字/称呼)豁免角色名检查——"她让我叫她琳"这种必然会提到角色
    forbidden = ['AI', '机器人'] if category == '身份' else (['AI', '机器人'] + char_names)
    for word in forbidden:
        if word and word in content:
            print(f'[{user_id}] ❌ user_fact 拒绝（含违禁词 {word}）：{content}')
            return False
    return True


def _valid_bond(user_id, content, char_name=''):
    """羁绊记忆：主语可以是 她 / 他们 / 角色本人（他的表态记成他的）。"""
    if not content or content == '无' or len(content) < 4:
        return False
    if _looks_like_character_self_claim(content):
        print(f'[{user_id}] ❌ bond 拒绝（自我解释只入 self-claim evidence）：{content}')
        return False
    ok_prefixes = ['我', '我们', '她', '他们']
    if char_name:
        ok_prefixes.append(char_name)   # 兼容旧格式
    if not any(content.startswith(p) for p in ok_prefixes):
        print(f'[{user_id}] ❌ bond 拒绝（主语不合规）：{content}')
        return False
    return True


def _valid_told(user_id, content):
    """告知记忆：她告诉角色的事，必须"她"开头。"""
    if not content or content == '无' or len(content) < 4:
        return False
    if not content.startswith('她'):
        print(f'[{user_id}] ❌ told 拒绝（非"她"开头）：{content}')
        return False
    return True


VALID_CATS = ('喜好', '厌恶', '身份', '状态', '经历', '关系', '健康', '其他')

# 模型爱自创分类名,映射到合法值,别一律降级成"其他"
CAT_ALIAS = {
    '健康状况': '健康', '身体': '健康', '身体状况': '健康', '疾病': '健康', '病史': '健康',
    '情绪': '状态', '近况': '状态', '当前状态': '状态',
    '爱好': '喜好', '兴趣': '喜好', '偏好': '喜好',
    '讨厌': '厌恶', '反感': '厌恶',
    '个人信息': '身份', '基本信息': '身份', '职业': '身份', '专业': '身份',
    '人际': '关系', '人际关系': '关系',
    '往事': '经历', '过去': '经历',
}


def _norm_category(cat: str) -> str:
    """把模型输出的分类名归一化到合法值。"""
    cat = (cat or '').strip()
    if cat in VALID_CATS:
        return cat
    if cat in CAT_ALIAS:
        return CAT_ALIAS[cat]
    return '其他'


# ────────── ★ 统一提取（私聊）──────────

def extract_and_save_memory(user_id, user_text, assistant_text,
                            character_id=DEFAULT_CHARACTER_ID,
                            temporal_context=None, source_event_id=None,
                            source_event_ids=None, *,
                            convention_only=False, dry_run=False,
                            backfill_result=None, parsed_override=None,
                            processor_type=None, processor_version=None):
    """Canonical evidence ingress. Extracted/model-authored candidates have no authority.

    The source message has already been durably recorded by the chat commit gate.
    Unsupported language remains pending in the same cognitive issue store.
    """
    from cognitive_events import ingest_canonical_turn
    ids = list(dict.fromkeys(([source_event_id] if source_event_id else [])
                             + list(source_event_ids or [])))
    if dry_run or convention_only:
        if isinstance(backfill_result, dict):
            backfill_result.update(status='pending', reason='model_candidate_not_authoritative')
        return True
    if not ids:
        return False
    for event_id in ids:
        result = ingest_canonical_turn(user_id=user_id, character_id=character_id,
                                       source_event_id=event_id, allow_assistant=event_id != ids[0])
        if event_id == ids[0] and result['status'] not in {'inserted', 'duplicate'}:
            return False
    return True


# ────────── ★ 群聊统一提取（用户事实 + 定向告知）──────────

def extract_and_save_group_memory(user_id, user_text, round_transcript, members,
                                  *, source_event_id=None, source_chat_id=None):
    """Compatibility signature; copied prose and model labels are never evidence.

    Missing historical source ids cannot be reconstructed from an LLM summary.
    Supported literal reports and unsupported messages enter the same queue.
    """
    if not source_event_id or not str(source_chat_id or '').startswith('group:'):
        return False
    from raw_events import get_active_events_by_ids
    from cognitive_events import ingest_canonical_turn
    events = get_active_events_by_ids(user_id, source_chat_id, [source_event_id])
    if len(events) != 1 or events[0]['metadata'].get('canonical_group') is not True:
        return False
    raw = events[0]
    meta = raw['metadata']
    if raw['role'] == 'user':
        target = meta.get('target_character_id') or SHARED_CHARACTER_ID
    else:
        target = meta.get('speaker_character_id')
        if target not in meta.get('audience', []):
            return False
    result = ingest_canonical_turn(user_id=user_id, character_id=target,
        source_event_id=source_event_id, source_chat_id=source_chat_id, allow_assistant=True)
    return result['status'] in {'inserted', 'duplicate'}
