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

"""用户记忆 v3（短期 + 长期 + 羁绊 + 统一三桶提取 + 自动纠错）

记忆四层结构：
  1. 她的事实      long_memory (character_id='shared')  —— 关于用户本人，全角色共享
  2. 我们之间的事  bond_memory (kind='between')          —— 她和某角色的共同经历，按角色独立
  3. 她告诉我的事  bond_memory (kind='told')             —— 她告诉某角色的、关于角色本人/其世界的信息
  4. 角色背景      character_memory                      —— 原作设定，只手动管理，聊天不写入

short_memory 只是近窗 compatibility cache / LLM recent view，不是 canonical evidence。
新的 conversational 事实以 chat_log event_id 为准，经 append_raw_event 写入。
get_short_memory(n) 保留兼容读取（优先 ledger，再合并 cache）。

提取只用一次 Haiku 调用，同时产出 1/2/3 三类，成本和原来一样。
"""
import anthropic
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from config import ANTHROPIC_KEY, CN_TZ, DEFAULT_CHARACTER_ID
from db import get_conn
from utils import extract_json
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
        '''SELECT content, timestamp, category FROM long_memory
           WHERE user_id = %s AND character_id IN (%s, %s)
             AND COALESCE(recall_status, 'active') = 'active'
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
        '''SELECT id, content FROM long_memory
           WHERE user_id = %s AND character_id IN (%s, %s)
             AND COALESCE(recall_status, 'active') = 'active'
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
    误配的风险由 merge_bond_memories 里"新内容不能比旧的短"那道防线兜住。
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


def save_bond_memory(user_id, character_id, kind, content, source_event_ids=None):
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

    conn.commit()
    cur.close()
    conn.close()
    _bg_embed('bond_memory', new_id, content)   # ★ RAG 启用时后台补向量
    try:
        import raw_events
        raw_events.link_memory_sources('bond_memory', new_id, source_event_ids or [])
    except Exception:
        pass
    return True


def merge_bond_memories(user_id, character_id, kind, replaces, new_content):
    """★ 记忆合并:把几条零散的旧记忆替换成一条更完整的。

    场景:同一件事分几次聊,库里存成 3-5 条碎片
      "她让我把尾巴改成可拆卸"
      "她说要做Q版的"
      "她说六眼要还原"
    合并成:"她要给我做Q版手办:猫耳、尾巴可拆卸、六眼还原,资金到位要几个月"

    安全限制(长期使用必须严格,删错东西比漏记更糟):
      1. 一次最多替换 3 条
      2. 每条 replaces 必须在库里真实存在(用相似度匹配,允许 LLM 复述有出入)
      3. 新内容必须【不短于】被替换内容里最长的那条 —— 防止越合并信息越少
      4. 匹配不到的 replaces 直接忽略,不影响其他条目
      5. 全过程打日志,可追溯

    返回 (是否成功, 实际删除条数)
    """
    if not new_content or not isinstance(replaces, list) or not replaces:
        return False, 0
    replaces = [r for r in replaces if isinstance(r, str) and r.strip()][:3]
    if not replaces:
        return False, 0

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, content FROM bond_memory
               WHERE user_id=%s AND character_id=%s AND kind=%s
                 AND COALESCE(recall_status, 'active') = 'active'
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)''',
            (user_id, character_id, kind)
        )
        rows = cur.fetchall()

        # 找出真实存在的目标。
        # ★ 这里用【宽松匹配】,不用 _too_similar ——
        #   LLM 复述旧记忆时常有出入(漏字、改标点、换语序),
        #   严格匹配会导致合并请求全部落空。
        #   宽松没关系:后面还有"不能信息缩水"那道防线兜着。
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

        # 防信息缩水:新内容不能比被替换的任何一条更短
        longest_old = max(len(c) for _i, c in targets)
        if len(new_content) < longest_old:
            print(f'[{user_id}] ❌ 合并被拒:新内容({len(new_content)}字)比旧的({longest_old}字)还短,可能丢信息')
            cur.close(); conn.close()
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

    notify_memory_changed('bond_memory', row_id=new_id, content=new_content)
    return True, deleted


BOND_RESOLUTION_REASONS = frozenset({
    'completed', 'cancelled', 'superseded', 'corrected',
})


def resolve_bond_memories(user_id, character_id, kind, replaces,
                          new_content=None, reason='superseded'):
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
                 AND COALESCE(recall_status, 'active') = 'active' ''',
            (user_id, character_id, kind)
        )
        rows = cur.fetchall()
        for want in replaces:
            best = None
            for mid, mcontent in rows:
                if mid in [item[0] for item in targets]:
                    continue
                if _loosely_matches(want, mcontent):
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
                 AND COALESCE(recall_status, 'active') = 'active' ''',
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


def get_bond_memories(user_id, character_id, kind=None, limit=30):
    """返回 [(id, content, timestamp)]，新→旧。kind=None 时返回全部种类。"""
    conn = get_conn()
    cur = conn.cursor()
    if kind:
        cur.execute(
            '''SELECT id, content, timestamp FROM bond_memory
               WHERE user_id = %s AND character_id = %s AND kind = %s
                 AND COALESCE(recall_status, 'active') = 'active'
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
               ORDER BY timestamp DESC LIMIT %s''',
            (user_id, character_id, kind, limit)
        )
    else:
        cur.execute(
            '''SELECT id, content, timestamp FROM bond_memory
               WHERE user_id = %s AND character_id = %s
                 AND COALESCE(recall_status, 'active') = 'active'
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
    cur.execute('''SELECT LEAST(
        COALESCE((SELECT MIN(first_interaction_at) FROM temporal_awareness WHERE user_id=%s AND character_id=%s), 'infinity'::timestamp),
        COALESCE((SELECT MIN(timestamp) FROM bond_memory  WHERE user_id=%s AND character_id=%s), 'infinity'::timestamp),
        COALESCE((SELECT MIN(timestamp) FROM long_memory  WHERE user_id=%s AND character_id=%s), 'infinity'::timestamp),
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

def plan_memory_corrections(user_id, user_text, character_id=DEFAULT_CHARACTER_ID):
    """只判断哪些旧记忆该删，不写库。返回 [(id, content), ...]。"""
    correction_keywords = [
        '不是', '我说错', '说错了', '其实', '不对', '搞错', '记错',
        '哪有', '才不', '说反了', '重新说', '纠正',
    ]
    if not any(kw in user_text for kw in correction_keywords):
        return []

    memories = _get_memories_with_id(user_id, character_id)
    if not memories:
        return []

    memory_list = '\n'.join(f'[ID:{mid}] {content}' for mid, content in memories)
    by_id = {int(mid): content for mid, content in memories}

    try:
        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y-%m-%d')
        weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]

        from ai_client import create_chat
        from config import MODEL_CN_AUX
        correction_prompt = f'''你是记忆纠错助手。用户正在纠正自己之前说过的错误信息，你要找出哪些旧记忆需要删除。

【今天日期】{today_str}（{weekday_cn}）

【用户这次说的话】
{user_text}

【已有的记忆列表】
{memory_list}

任务：
1. 看用户这次在纠正什么。
2. 在记忆列表里找到被纠正的那一条（可能多条），返回它的 ID。
3. 如果用户只是在否定话题、撒娇、开玩笑，并没有纠正某条具体事实，返回 none。

判断示例：
- 用户说"我生日不是今天，是5月26号" → 删掉含"今天""当天日期"的生日记忆
- 用户说"我才不喜欢吃甜食" → 删掉"她喜欢甜食"那条
- 用户说"不是，我开玩笑的" → 没有纠正具体事实，返回 none
- 拿不准时，宁可返回 none，不要乱删

【输出格式——严格 JSON，只输出一行】
要删除：{{"action":"delete","ids":[1,2]}}
不删除：{{"action":"none","ids":[]}}'''
        raw, _usage = create_chat(
            model=MODEL_CN_AUX, max_tokens=1500,
            messages=[{'role': 'user', 'content': correction_prompt}],
        )
        raw = raw.strip()
        print(f'[{user_id}] 纠错扫描 ({MODEL_CN_AUX}): {raw[:120]}')

        parsed = extract_json(raw)
        if not parsed or parsed.get('action') != 'delete' or not parsed.get('ids'):
            return []

        planned = []
        for mem_id in parsed['ids']:
            try:
                mid_int = int(mem_id)
            except (ValueError, TypeError):
                continue
            if mid_int in by_id:
                planned.append((mid_int, by_id[mid_int]))
        return planned

    except Exception as e:
        print(f'[{user_id}] 纠错扫描失败：{e}')
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
                            source_event_ids=None):
    """一次 Haiku 调用同时提取三类记忆和一类 self-claim 证据：
    A user_fact —— 她透露的关于她自己的新事实 → long_memory(shared)
    B bond      —— 她和这个角色之间发生的事/约定/共同经历 → bond_memory(between)
    C told      —— 她告诉这个角色的、关于角色本人或其世界的信息（含剧透）→ bond_memory(told)
    """
    source_ids = []
    for item in list(source_event_ids or []):
        val = str(item).strip() if item else ''
        if val and val not in source_ids:
            source_ids.append(val)
    if source_event_id:
        val = str(source_event_id).strip()
        if val and val not in source_ids:
            source_ids.insert(0, val)
    primary_event_id = source_ids[0] if source_ids else None

    processor = None
    processor_version = None
    source_key = None
    proc_id = None

    def _finish_extract(status, error=None):
        if not proc_id or not processor:
            return
        try:
            import raw_events
            raw_events.finish_processor(
                proc_id, processor, processor_version, status, last_error=error)
            if status == 'succeeded' and source_key:
                raw_events.record_derived(
                    processor, processor_version, source_key,
                    'memory_extractor', None)
        except Exception:
            pass

    try:
        import raw_events
        processor = raw_events.PROCESSOR_MEMORY_EXTRACTOR
        processor_version = raw_events.PROCESSOR_MEMORY_EXTRACTOR_VERSION
        source_key = raw_events.derivation_source_key(source_ids)
        proc_id = source_key or primary_event_id
        if source_ids:
            try:
                if not raw_events.sources_are_active(source_ids, user_id, character_id):
                    print(f'[{user_id}] skip extract: source event deleted')
                    _finish_extract('skipped', 'deleted_source')
                    return True
            except raw_events.SourceValidityError as e:
                print(f'[{user_id}] skip extract: source validity unknown:{e}')
                _finish_extract('failed', 'source_validity_unknown')
                return False
        if source_key and raw_events.already_derived(
                processor, processor_version, source_key):
            print(f'[{user_id}] skip extract: already derived {source_key}')
            return True
        if proc_id:
            state = raw_events.claim_processor(
                proc_id, processor, processor_version)
            if state == 'already_succeeded':
                return True
    except Exception as e:
        try:
            import raw_events as _raw_events
            if isinstance(e, _raw_events.SourceValidityError):
                print(f'[{user_id}] skip extract: source validity unknown:{e}')
                _finish_extract('failed', 'source_validity_unknown')
                return False
        except Exception:
            pass
        print(f'[{user_id}] extract provenance guard skipped:{e}')

    try:
        pending_corrections = plan_memory_corrections(user_id, user_text, character_id)
        correction_hint = ''
        if pending_corrections:
            listed = '\n'.join(f'- [ID:{mid}] {content}' for mid, content in pending_corrections)
            correction_hint = (
                '\n【提示】用户刚纠正了之前说错的信息。下面这些旧记忆将在本轮提取成功后删除，'
                '请提取她这次给出的正确事实（不要再复述旧的错误信息）：\n'
                f'{listed}'
            )

        char_names = _all_character_names()
        from characters import get_character
        char = get_character(character_id)
        char_name = char['name'] if char else character_id

        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y-%m-%d')
        weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
        tomorrow_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')

        existing = get_long_memory(user_id, character_id)
        existing_text = '\n'.join(f'- {m[0]}' for m in existing) if existing else '（暂无）'
        existing_bond = get_bond_memories(user_id, character_id, limit=20)
        bond_text = '\n'.join(f'- {r[1]}' for r in existing_bond) if existing_bond else '（暂无）'

        recent_messages = get_short_memory_for_prompt(
            user_id, n=6, character_id=character_id,
            exclude_event_ids=source_ids,
            exclude_messages=[
                ('user', user_text),
                ('assistant', assistant_text),
            ],
        )
        recent_lines = []
        for message in recent_messages:
            speaker = '她说' if message.get('role') == 'user' else f'{char_name}回复'
            recent_lines.append(f'{speaker}：{message.get("content") or ""}')
        recent_text = '\n'.join(recent_lines) if recent_lines else '（暂无）'

        # ★ 该角色世界里的重要人物 —— 让 Haiku 知道名字对应的身份,别把"杰"猜成学生
        relations_block = get_relations_text(character_id)
        relations_intro = (f'\n{relations_block}\n' if relations_block else '')

        # ★ 本轮开始前的真实时间跨度 —— 由 route_chat 入队时保存，避免 worker 延迟造成错位
        temporal_intro = ''
        try:
            from temporal_awareness import build_memory_context
            temporal_intro = build_memory_context(temporal_context)
        except Exception:
            temporal_intro = ''

        # ★ 记忆提取:纯中文结构化任务,走 MODEL_CN_AUX(默认 deepseek-chat 便宜好用)
        from ai_client import create_chat
        from config import MODEL_CN_AUX
        prompt_content = f'''你是记忆整理助手。从下面这轮对话中提取值得长期记住的信息，分成四类。

【对话双方】
- "她" = 用户
- "{char_name}" = 角色（她的聊天对象）
{relations_intro}
【今天日期】{today_str}（{weekday_cn}）{correction_hint}
{temporal_intro}

【已记录的她的事实】
（查重区：只能用于查重、merge、resolution，不能用于解析当前 turn 的指代）
{existing_text}

【已记录的羁绊记忆】
（查重区：只能用于查重、merge、resolution，不能用于解析当前 turn 的指代）
{bond_text}

【最近对话上下文】
（仅包含当前 turn 之前最近约 3 轮对话）
{recent_text}

【上下文使用边界】
1. 最近对话上下文可以用于解析当前 turn 的省略和指代，例如“这个”“那个”“明天拍给你看”“刚才那个”。
2. 【已记录的她的事实】和【已记录的羁绊记忆】只能用于查重、merge、resolution，禁止拿它们猜当前 turn 没有明确说出的对象。
3. 如果最近对话上下文也无法确定对象，只能泛化（例如“她答应明天拍照片给我看”）或填 null，绝不能从旧记忆补全对象。
4. 禁止因为旧 bond 里有“谷子”就把当前未明确的对象脑补成谷子。

【这次对话】
她说：{user_text}
{char_name}回复：{assistant_text}

【四类记忆/证据的定义——每类独立判断，可以同时有，也可以都没有】
A. user_fact：她透露的、关于她自己的新事实（生日/喜好/近况/经历等）。
   - 内容里【不许】出现角色名字，只写她自己的事。
   - 【只记她本人】：她在讲别人（朋友/同事/家人）的事时，不属于 user_fact，填 null。见通用规则第 9 条。
B. bond：她和{char_name}之间这次发生的、值得记住的事——约定、承诺、重要表态、她表达的重要情感、{char_name}对她说的重要的话。
   - 【视角】：以{char_name}的第一人称写，"我"就是{char_name}——这是要存进他自己脑子里的回忆。
     她做的写"她…对我…"；{char_name}自己做的写"我…"；共同的写"我和她…"。绝不把我说的话写成"她说过"。
   - 【必须是一句话的总结，30 字以内】：像人脑记事一样只记"发生了什么"，不是聊天记录存档。
     ❌ 绝对禁止：抄日语原文、附中文翻译、加"——这是我在她…时期对她的鼓励"这种旁白解说、写成小作文。
     ✅ 正确："我鼓励她签证快点下来"、"我劝她早点回家别后悔"、"我安慰她说长辈不是在怨她"。
   - 日常寒暄闲聊不算，只记"以后会被提起"级别的事。
   - 例："我和她约好2026-07-10一起看电影"；"她夸了我的新发型"。
C. told：她告诉{char_name}的、关于{char_name}本人或他的世界的信息——包括原作剧情、他的未来、他不知道的设定。
   - content 用"她说过..."或"她告诉过{char_name}..."开头的转述。例："她说过{char_name}的未来会发生某某事"。
   - 只有当她明确在陈述这类信息时才提取；她提问、开玩笑不算。
D. character_self_claim：{char_name}对"我是什么样的人/我为什么这样做/这是我的风格"的自我解释。
   - 这不是 bond，不是长期事实，不是关系事实；最多只是低置信 self-model evidence。
   - 例："我就是这种性格"、"被人说想念就直接应一声是我的风格"、"我不擅长直接表达"。
   - content 用第一人称简短转述，如"我说被人想念就直接回应是我的风格"。

【通用规则】

0. ★★【必抓清单——优先级最高,凌驾于下面所有"别记"的规则】★★
   以下这些是【她这个人的核心信息】,一旦出现就【必须记】,不受第 3/7/8/12 条限制:

   ▸ 【姓名 / 昵称 / 希望被怎么称呼】——最高优先级
     · "我叫XXX" / "你可以叫我XXX" / "我的小名是XXX" / "别叫我XXX,叫我YYY"
     · 记成:"她叫XXX"、"她希望被叫做XXX"、"她的小名是XXX"
     · ★ 就算她只说过一次,也【必须记】——名字不是"一次分享",是身份本身
     · ★ 就算她是笑着说的、随口说的、夹在别的话里说的,也【必须记】
   ▸ 【生日 / 年龄】:"我生日是X月X日"、"我今年X岁"
   ▸ 【居住地 / 家乡】:"我在XX""我老家在XX"
   ▸ 【职业 / 学业】:她【明确陈述】自己的工作或专业(不是从一次分享推断)
   ▸ 【重要家人关系】:"我妈妈""我弟弟"这类她主动提到的直系亲属
   ▸ 【重大健康状况 / 长期困扰】:她明说的、会持续影响她的事
   ▸ 【明确表达的强烈喜好或厌恶】:"我最讨厌XX""我超喜欢XX"(注意是她【明说】的,不是你推断的)

   【判断方法】问自己:"如果她下次问'我叫什么名字',我答不上来会不会很奇怪?"
   会 → 这条必须记。

   ⚠️ 这一条是【白名单】,命中就记,不要再拿第 3 条(简单回应不算)、
   第 8 条(大多数都是 null)、第 12 条(一次分享不算身份)去否决它。

1. 【事实只信她】：user_fact 和 told 只能从"她说"里提取，{char_name}的回复绝不作为这两类的来源。
2. 【我的话记成我的】：{char_name}（也就是"我"）的重要表态可以记入 bond，写成"我说过/我认为/我答应了…"，
   绝不写成"她说过"。我随口报的数字、天数、结论（如"我们认识35天了"）多半只是顺着聊，一般不值得记；
   真要记也只能记成"我当时说…"，绝不能当客观事实。
   ★ 但"我是这种人/这是我的风格/我不擅长表达/我习惯这样"这类自我解释，不许记成 bond；
     只能放进 character_self_claim，交给持续认知层以后用更多行为证据验证。
3. 撒娇/调侃/情绪宣泄/问候/提问/简单回应都不算。"她问了XX"这类只有在话题本身重大时才值得记。
4. "确认了认识多少天"这类元对话不要提取；"讨论了是什么关系"只有当某一方给出了值得记住的正式表态时才记，且主语写对。
5. 时间换算成绝对日期："明天"→{tomorrow_str}，"昨天"→{yesterday_str}。
6. user_fact 和 told 必须以"她"开头；bond 以"我""我们"或"她"开头（第一人称，"我"={char_name}）。
7. ★★【查重是你的活——每次输出前都要对照上面的已记录列表】★★
   系统只拦【一字不差】的重复,【意思一样但换了说法】必须由你来判断。

   【判断方法】把要写的内容和上面【已记录的...】逐条对照,问自己:
     "这条如果加进去,会不会让人觉得同一件事记了两遍?"
     会 → 填 null(或者用 bond_merge 合并)
     不会 → 正常输出

   【★ 关键:别把"看起来像"当成"是同一件事"】
   下面这些【字面很像但是不同的事】,必须照常记:
     · 已有「她今天在家改程序」← 新的「她因腰酸没改成程序」
       → 【不同】:一件是在改,一件是没改成。要记。
     · 已有「她说要买手办」← 新的「她说手办涨价了不买了」
       → 【不同】:计划变了。要记。
     · 已有「她昨天熬夜」← 新的「她今天早睡了」
       → 【不同】:是新的状态。要记。
   判断看【说的是不是同一件事实】,不是看用了多少相同的字。

   【真正该跳过的重复长这样】
     · 已有「她主动给我起了昵称琳」← 新的「她给了我专属称呼琳」
       → 【同一件事】,换了个说法而已 → null
     · 已有「她学计算机专业」← 新的「她是学计算机的」
       → 【同一件事】→ null

   【状态类记忆的特殊规则】
   "她去洗澡/吃饭/睡觉/上课/在路上/正在复习"这类普通当前状态,
   不要当成永久 user_fact。它们只用于短期上下文或便利贴/生命周期候选。
   "今天没睡好/头疼/很累"这类一次性状态,也先作为候选,不要扩写成长期特征。
   只有反复、长期、强烈或有后果的状态,才可能整理成一条概括性的长期记忆。
7.5 所有记忆都必须是【简短的一句话】（30 字以内），只记事实和事件本身。
    禁止引用原文对话、禁止附翻译、禁止补充解说和背景铺垫——那是聊天记录该干的事，不是记忆。
8. 某类没有就填 null。日常闲聊确实多数是 null,这很正常——
   【但如果命中了第 0 条必抓清单,就绝不能填 null】。
9. 【必须分清"她自己"和"她转述的别人"——非常重要】：
   她说的话里，有些是关于她本人，有些是她在讲【别人】（她的朋友、同学、同事、家人等）的事。
   - 只有【明确是她本人】的事，才提取成 user_fact。
   - 她在转述"我朋友…""我有个朋友…""她（指第三人）…"这类【别人的事】，
     【绝对不要】记成关于她的 user_fact，也不要记成 bond。这类基本应该直接 null。
   - 唯一例外：如果"她对这件别人的事的反应/感受"本身是关于她的重要情绪（且明确是她的感受），
     可以只记那份感受，写清楚是"她因为朋友的事而…"，绝不能把朋友的处境写成她的处境。
   - 分不清是她还是她朋友时：宁可 null，绝不猜成她本人。
   - 反例（禁止）：用户说"我朋友陷入情感困境整天哭" → ❌ 不许记成"她陷入情感困境整天哭"。
     正确处理：这是她朋友的事，不是她的事，user_fact 填 null。

10. ★【词汇中性化——非常重要】你作为记忆提取器，作为语言模型你在描述"男女互动"时训练默认词汇偏言情小说，
    这会污染记忆库、把中性事件写成暧昧场景。你必须【主动对抗】这种引力：
    ▸ 只用【中性动词】描述行为，不加解读：
      · "问"（不是"试探"、"探"、"暗示"）
      · "说"（不是"直言"、"坦言"、"表白"——除非她真的在正式表白）
      · "回答/回应"（不是"承认"、"默认"——除非上下文明确）
      · "告诉"（不是"透露"、"吐露心声"）
      · "约"、"答应"、"拒绝"、"劝"、"催"、"提醒"、"关心"——都是好动词
    ▸ 【禁用的言情腔词汇】（这些词写进记忆就是污染）：
      试探 / 心思 / 心动 / 动心 / 暗示 / 若有所思 / 眼神交汇 /
      直言说出 / 主动靠近 / 依赖 / 撒娇（除非明显是撒娇动作）/
      引导她坦率表达 / 探问 / 情愫 / 心事
    ▸ 【正例 vs 反例】：
      ❌ 她用疑问句试探我心里想的答案
      ✅ 她问我怎么想
      ❌ 她直言说出喜欢我
      ✅ 她跟我说她喜欢我（★ 注意"跟我说"是中性,"直言说出"是言情腔）
      ❌ 我引导她坦率表达
      ✅ 我劝她把真话说出来（或直接写"她后来告诉我 XX"）
      ❌ 她眼神里带着期待
      ✅ 她等着我回话（如果发生了）；否则不记
    ▸ 【判断标准】：写完 bond 后自查——把内容读一遍，"这句话是否像言情小说的旁白？"
      像 → 重写成流水账；不像 → OK。宁可平实到无聊，也不要暧昧文艺。

11. ★【遇到人名先查上面的"重要人物"表——非常重要】
    你可能不熟悉"{char_name}"这个角色的世界里谁是谁。当对话里出现名字时:
    - 先看上方【★ 你世界里的重要人物】那段(如果有的话)
    - 记忆里必须写对身份 —— 例:"她说杰是叛徒"应该写"她说我挚友是叛徒"(因为杰=我的挚友),
      不是"她说学生是叛徒"或"她说某人是叛徒"
    - 【禁止】在不知道对方是谁时,瞎猜身份("学生""同事""朋友")——那会写错记忆
    - 关系表里没有的名字 → 直接用原名,别猜(例:"她说小张是叛徒" → "她说小张是叛徒",别改成"她说朋友是叛徒")

12. ★【一次分享 ≠ 身份特征——非常重要】
    ⚠️ 【例外】:第 0 条必抓清单里的内容(名字/称呼/生日/居住地等)【不受本条限制】——
    她说一次名字就要记名字,那不是"推断",那是她【直接告诉你的事实】。
    本条只针对【你自己推断出来的】身份特征。

    她**发了一次某样东西 / 提了一句某件事**,只能提取那**一次的行为**,不能扩展成【她是 X】这种身份/专业/长期状态的判断。
    -  ❌ 错误(过度推断):
      · 她发了一张芙莉莲的图 → "她喜欢芙莉莲"
      · 她说她刷到某个视频 → "她爱看某某类型视频"
      · 她说她今天吃了寿司 → "她喜欢寿司"
      · 她提了一次她的学校 → "她学 XX 专业"
    -  ✅ 正确(只记那次事件本身):
      · 她今天分享了一张芙莉莲的图给我
      · 她今天说她刷到某个视频
      · 她今天吃了寿司
      · 她提到自己的学校
    - 【判断标准】:她说"我喜欢/常常/一直/我是 X 的" → 才能提"她 X"。
      她只是【单次分享/提及】 → 只能记"她今天分享了/提了 XX"。
    - 【禁止】把一次分享推断成【爱好/习惯/身份】,那是言情小说主角推断女主的写法,不是记忆整理。

13. ★★【记忆合并——把碎片收拢成完整的一条】★★
    同一件事常常分好几次聊,库里就会留下一堆碎片:
      「她让我把尾巴改成可拆卸」
      「她说要做Q版的」
      「她说六眼要还原」
    ——这三条其实是【同一件事】的三个片段。

    当这轮对话【把一件旧事补充完整了】、或者【某一方完整复述了一遍】时,
    输出 bond_merge 字段,把碎片合并成一条完整的。

    【使用条件——都满足才输出】
    · 【已记录的羁绊记忆】里确实存在这些碎片(replaces 要抄那边的原文,不能凭空编)
    · 这轮对话让这件事变完整了(补了新细节 / 有人完整总结了一遍)
    · 合并后的内容【包含】所有碎片的信息,不能越合越少

    【限制】
    · replaces 最多 3 条
    · content 必须比任何一条碎片都更完整(更长、信息更全)
    · 拿不准就别合并,输出 null —— 漏合并只是效率问题,错误合并会【丢失记忆】

    【正例】
    她吐槽"说好要做机器人形体和可拆卸尾巴结果都没记住",
    我回"全都记着:Q版、猫耳、尾巴可拆卸、六眼还原,资金要几个月" →
    bond_merge 的 replaces 填 ["她夸我可爱的样子,我嘴上否认但让她把尾巴改成可拆卸"],
    content 填 "她要给我做Q版手办:猫耳、尾巴可拆卸、六眼还原,资金到位要几个月"

    【反例——不要合并】
    · 只是又提了一次同一件事,没有新信息 → null(交给去重就行)
    · 两件不同的事凑一起 → null
    · 记不清旧记忆原文 → null
    · 【禁止】拿 bond_merge 处理"旧事件已经有结果"。
      旧事件仍然成立、只是补了细节 → merge。
      旧计划/条件/赌约已经完成、失败、取消、被新事实覆盖、条件没触发 → 必须用 bond_resolution。

14. ★★【旧事件状态变化——禁止把已结束的条件继续写成 active】★★
    如果本轮说明一个旧计划/条件/赌约/担忧已经：
    完成 / 失败 / 取消 / 被新事实覆盖 / 条件没有触发 / 已经有结果
    → 禁止用 bond_merge 把旧的"待发生条件"继续保留成 active。
    → 必须输出 bond_resolution，把旧记录关掉。
    旧行不要删，系统会把它标成 superseded，不再进入普通聊天召回。

    典型例：
    旧：「她抽不中就拍甜品照片」
    新：「她已经抽到了」
    → 旧条件结束。bond_resolution.replaces 填旧原文，reason 填 completed。
    → 不得再输出「我和她约好……她抽不中拍甜品照片」作为新的 active bond。
    → 结果如果 user_fact 已经写了「她抽到了两个系列」，bond_resolution.content 可以 null。

    另一例：
    旧：「如果明天没下雨就出去」
    新：「已经下雨了」
    → 未满足的分支不能继续当 future plan。reason 填 cancelled 或 superseded。

【输出格式——严格 JSON，只输出一行】
{{"user_fact":{{"content":"她XXX","category":"喜好"}},"bond":{{"content":"我和她XXX 或 我说过XXX 或 她对我XXX"}},"told":{{"content":"她说过XXX"}},"character_self_claim":{{"content":"我说被人想念就直接回应是我的风格"}},"bond_merge":{{"replaces":["旧记忆原文"],"content":"合并后的完整版"}},"bond_resolution":{{"replaces":["旧 bond 原文"],"content":null,"reason":"completed"}}}}
没有的类填 null，例如全都没有：
{{"user_fact":null,"bond":null,"told":null,"character_self_claim":null,"bond_merge":null,"bond_resolution":null}}
category 只能选：喜好/厌恶/身份/状态/健康/经历/关系/其他'''
        raw, _usage = create_chat(
            model=MODEL_CN_AUX, max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt_content}],
        )
        raw = raw.strip()
        print(f'[{user_id}][{character_id}] 提取器({MODEL_CN_AUX}) 完整输出: {raw}')

        parsed = extract_json(raw)

        # ★ 解析失败重试一次:一次抽风就丢掉整轮记忆太亏。
        #   重试时加一句硬约束,压掉推理模型的思考冲动。
        if not parsed:
            print(f'[{user_id}] ⚠️ 首次解析失败,重试一次...')
            try:
                raw2, _u2 = create_chat(
                    model=MODEL_CN_AUX, max_tokens=2000,
                    messages=[{
                        'role': 'user',
                        'content': prompt_content +
                        '\n\n【⚠️ 重要】不要输出任何思考过程、解释、markdown 代码块标记。'
                        '直接输出那一行 JSON,第一个字符必须是 {,最后一个字符必须是 }。'
                    }],
                )
                raw2 = raw2.strip()
                print(f'[{user_id}] 重试输出: {raw2}')
                parsed = extract_json(raw2)
            except Exception as _re:
                print(f'[{user_id}] 重试也失败:{_re}')

        if not parsed:
            print(f'[{user_id}] ❌ 提取器输出无法解析成 JSON,本轮记忆全丢（纠错删除已取消，旧记忆保留）: {raw[:200]}')
            _finish_extract('failed', 'unparsed')
            return False

        if source_ids:
            try:
                import raw_events
                if not raw_events.sources_are_active(source_ids, user_id, character_id):
                    print(f'[{user_id}] skip extract commit: source deleted after claim')
                    _finish_extract('skipped', 'deleted_source')
                    return True
            except Exception as e:
                print(f'[{user_id}] extract pre-commit source check failed:{e}')
                _finish_extract('failed', 'source_validity_unknown')
                return False

        # 提取 JSON 成功后再删旧记忆，避免先删后存失败导致两边都空
        if pending_corrections:
            apply_memory_corrections(
                user_id, [mid for mid, _ in pending_corrections], character_id
            )

        # A. 用户事实 → shared 桶
        uf = parsed.get('user_fact')
        if isinstance(uf, dict):
            content = _clean_content(uf.get('content'))
            category = (uf.get('category') or '其他').strip()
            category = _norm_category(category)
            if _valid_user_fact(user_id, content, char_names, category):
                try:
                    from memory_lifecycle import apply_user_fact_lifecycle
                    lifecycle = apply_user_fact_lifecycle(
                        user_id, character_id, user_text, content, category,
                        source_event_id=source_event_id,
                        temporal_context=temporal_context,
                    )
                except Exception as _e:
                    import raw_events as _raw_events
                    if isinstance(_e, _raw_events.SourceValidityError):
                        raise
                    print(f'[{user_id}] ⚠️ 记忆生命周期处理失败,按旧逻辑保留:{_e}')
                    lifecycle = {
                        'should_save_long_memory': True,
                        'long_memory_kwargs': {},
                        'classification': {'reason': 'lifecycle_error_fallback'},
                    }

                if lifecycle.get('should_save_long_memory'):
                    kwargs = lifecycle.get('long_memory_kwargs') or {}
                    # ★ 单聊里说的只有这个角色知道（谁在场谁知道）；群聊说的才进 shared
                    if save_long_memory(user_id, content, category, character_id, **kwargs):
                        print(f'[{user_id}] ✅ 用户事实 [{category}]（{character_id} 专属）：{content}')
                else:
                    cls = lifecycle.get('classification') or {}
                    if lifecycle.get('consolidated'):
                        print(f'[{user_id}] ✅ 状态候选已巩固：{lifecycle["consolidated"]["content"]}')
                    print(f'[{user_id}] 🧠 生命周期记忆 [{cls.get("memory_kind")}/{cls.get("reason")}]: {content}')

        resolved_texts = []
        br = parsed.get('bond_resolution')
        if br is None:
            br = parsed.get('bond_update')
        if isinstance(br, dict):
            resolve_replaces = br.get('replaces')
            resolve_reason = br.get('reason') or 'superseded'
            resolve_content = _clean_content(br.get('content')) if br.get('content') else ''
            if isinstance(resolve_replaces, list):
                try:
                    _ok, resolved_rows = resolve_bond_memories(
                        user_id, character_id, 'between',
                        resolve_replaces,
                        new_content=resolve_content or None,
                        reason=resolve_reason,
                    )
                    resolved_texts = [content for _mid, content in resolved_rows]
                except Exception as _e:
                    print(f'[{user_id}] ❌ 结束旧 bond 出错(不影响其他记忆):{_e}')

        # ★ B0. 记忆合并:只允许"旧事件仍成立、补更多细节"。先处理,把碎片收成一条。
        bm = parsed.get('bond_merge')
        if isinstance(bm, dict):
            merge_content = _clean_content(bm.get('content'))
            merge_replaces = bm.get('replaces')
            if merge_content and isinstance(merge_replaces, list):
                if _valid_bond(user_id, merge_content, char_name):
                    try:
                        merge_bond_memories(
                            user_id, character_id, 'between',
                            merge_replaces, merge_content
                        )
                    except Exception as _e:
                        print(f'[{user_id}] ❌ 记忆合并出错(不影响其他记忆):{_e}')
                else:
                    print(f'[{user_id}] ❌ 合并内容格式不合规,跳过:{merge_content[:40]}')

        # B-guard. 角色临场自我解释不进 bond，只入持续认知层的低置信证据。
        sc = parsed.get('character_self_claim')
        if isinstance(sc, dict):
            content = _clean_content(sc.get('content'))
            if _looks_like_character_self_claim(content):
                result = _record_character_self_claim_evidence(
                    user_id, character_id, content,
                    source_event_id=source_event_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                )
                print(f'[{user_id}] 🧠 自我陈述证据（{character_id}）：{content} -> {result}')

        # B. 我们之间的事 → bond_memory(between)
        bd = parsed.get('bond')
        if isinstance(bd, dict):
            content = _clean_content(bd.get('content'))
            if _looks_like_character_self_claim(content):
                result = _record_character_self_claim_evidence(
                    user_id, character_id, content,
                    source_event_id=source_event_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                )
                print(f'[{user_id}] 🧠 bond 改道自我陈述证据（{character_id}）：{content} -> {result}')
            elif _valid_bond(user_id, content, char_name):
                if any(_loosely_matches(content, old) for old in resolved_texts):
                    print(f'[{user_id}] 跳过把已结束条件再写成 active bond：{content}')
                elif save_bond_memory(
                    user_id, character_id, 'between', content,
                    source_event_ids=source_ids,
                ):
                    print(f'[{user_id}] ✅ 羁绊记忆（{character_id}）：{content}')

        # C. 她告诉我的事 → bond_memory(told)
        td = parsed.get('told')
        if isinstance(td, dict):
            content = _clean_content(td.get('content'))
            if _valid_told(user_id, content):
                if save_bond_memory(
                    user_id, character_id, 'told', content,
                    source_event_ids=source_ids,
                ):
                    print(f'[{user_id}] ✅ 告知记忆（{character_id}）：{content}')

        # ★ 两级召回：强化被提起的记忆（mention_count + 1）
        try:
            from smart_recall import reinforce_mentioned_facts
            reinforce_mentioned_facts(user_id, character_id, user_text)
        except Exception:
            pass

        try:
            from memory_lifecycle import reactivate_lifecycle_memories
            reactivated = reactivate_lifecycle_memories(
                user_id, character_id, user_text)
            if reactivated:
                print(f'[{user_id}] 🔁 重新激活 {reactivated} 条生命周期记忆')
        except Exception as _e:
            print(f'[{user_id}] 生命周期记忆激活跳过:{_e}')

        _finish_extract('succeeded')
        return True

    except Exception as e:
        print(f'记忆提取失败：{e}')
        _finish_extract('failed', str(e))
        return False


# ────────── ★ 群聊统一提取（用户事实 + 定向告知）──────────

def extract_and_save_group_memory(user_id, user_text, round_transcript, members):
    """群聊版提取（bond 在群里语义模糊，只做 A 和 C 两类）：
    A user_fact —— 她的新事实 → long_memory(shared)
    C told      —— 她在群里告诉【某个具体角色】的关于他/他世界的信息 → 该角色的 bond_memory(told)

    members: [{'id','name'}, ...] 群里全部角色。
    """
    try:
        try:
            from memory_lifecycle import reactivate_lifecycle_memories
            reactivated = reactivate_lifecycle_memories(
                user_id, SHARED_CHARACTER_ID, user_text)
            if reactivated:
                print(f'[{user_id}][group] 🔁 重新激活 {reactivated} 条生命周期记忆')
        except Exception as _e:
            print(f'[{user_id}][group] 生命周期记忆激活跳过:{_e}')

        pending_corrections = plan_memory_corrections(user_id, user_text, SHARED_CHARACTER_ID)
        correction_hint = ''
        if pending_corrections:
            listed = '\n'.join(f'- [ID:{mid}] {content}' for mid, content in pending_corrections)
            correction_hint = (
                '\n【提示】用户刚纠正了之前说错的信息。下面这些旧记忆将在本轮提取成功后删除，'
                '请提取她这次给出的正确事实（不要再复述旧的错误信息）：\n'
                f'{listed}'
            )

        character_names = [m['name'] for m in members]
        name_to_id = {m['name']: m['id'] for m in members}
        names_str = '、'.join(character_names)
        char_names_all = _all_character_names()

        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y-%m-%d')
        weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
        tomorrow_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')

        existing = get_long_memory(user_id, SHARED_CHARACTER_ID)
        existing_text = '\n'.join(f'- {m[0]}' for m in existing) if existing else '（暂无）'

        # ★ 群聊记忆提取:纯中文结构化任务,走 MODEL_CN_AUX
        from ai_client import create_chat
        from config import MODEL_CN_AUX
        group_prompt = f'''你是记忆整理助手。下面是一个群聊的一轮对话记录。

【群里的说话人】
- "群主" = 用户本人（她）——你【只能】从她的发言里提取
- {names_str} = 虚构角色——他们说的任何话都不得提取

【今天日期】{today_str}（{weekday_cn}）{correction_hint}

【已记录的她的事实】
{existing_text}

【群主这一轮说的话】
{user_text}

【本轮完整对话（仅供理解语境）】
{round_transcript}

【三类记忆——各自独立判断】
A. user_fact：她透露的、关于她自己的新事实。内容里不许出现角色名。
B. told：她在这句话里告诉【某个具体角色】的、关于那个角色本人或他世界的信息（含剧情/未来）。
   - target 必须是这些名字之一：{names_str}
   - content 用"她说过..."开头的转述。她是泛泛对全群说的、没有明确对象时，target 填 null。
C. char_bonds：这一轮里发生的、值得【某个角色】记进自己回忆的互动——角色之间的交流、角色和群主之间的重要往来都算。
   - 为每个相关角色各写一条（0~3条），以【该角色的第一人称】写，"我"=该角色本人。
   - target 是这条回忆属于谁；content 例："我和杰在群里为说话方式拌了几句嘴，她在旁边看着"（存进五条悟）、
     "我和悟斗了几句嘴，她说我们像老夫老妻"（存进夏油杰）。
   - 日常寒暄不记，只记有内容的互动。

【通用规则】
1. 【事实只信群主】：user_fact 和 told 只能来自群主的发言；角色说的话（哪怕角色说"她喜欢XX"）不得作为这两类的来源。
   但 char_bonds 记录的是互动事件本身，谁参与了、发生了什么，可以基于完整对话判断。
2. 撒娇/调侃/提问/简单回应不算 user_fact 和 told。
3. 时间换算绝对日期："明天"→{tomorrow_str}，"昨天"→{yesterday_str}。
4. user_fact 和 told 以"她"开头；char_bonds 以"我"或"我们"开头。与已有记录重复的不提。没有就填 null。
5. ★【词汇中性化——同样重要】跟单聊记忆一样，你在描述"男女互动"时训练数据默认走言情风，
   必须【主动对抗】。char_bonds 里只用中性动词（问/说/告诉/约/答应/劝/催/提醒/关心），
   禁用言情腔词（试探/心思/心动/暗示/直言/坦言/表白/引导/情愫/心事）。
   写完自查：是否像言情小说旁白？像就重写成流水账。宁可平实无聊，也不要暧昧文艺。

【输出格式——严格 JSON，只输出一行】
{{"user_fact":{{"content":"她XXX","category":"喜好"}},"told":{{"target":"角色名","content":"她说过XXX"}},"char_bonds":[{{"target":"角色名","content":"我XXX"}}]}}
没有的类填 null（char_bonds 没有就填 []）。category 只能选：喜好/厌恶/身份/状态/健康/经历/关系/其他'''
        raw, _usage = create_chat(
            model=MODEL_CN_AUX, max_tokens=2000,
            messages=[{'role': 'user', 'content': group_prompt}],
        )
        raw = raw.strip()
        print(f'[{user_id}][group] {MODEL_CN_AUX}: {raw[:150]}')

        parsed = extract_json(raw)
        if not parsed:
            print(f'[{user_id}][group] ❌ 提取器输出无法解析成 JSON,本轮记忆全丢（纠错删除已取消）: {raw[:200]}')
            return False

        if pending_corrections:
            apply_memory_corrections(
                user_id, [mid for mid, _ in pending_corrections], SHARED_CHARACTER_ID
            )

        # A. 用户事实 → shared
        uf = parsed.get('user_fact')
        if isinstance(uf, dict):
            content = _clean_content(uf.get('content'))
            category = (uf.get('category') or '其他').strip()
            category = _norm_category(category)
            if _valid_user_fact(user_id, content, char_names_all):
                try:
                    from memory_lifecycle import apply_user_fact_lifecycle
                    lifecycle = apply_user_fact_lifecycle(
                        user_id, SHARED_CHARACTER_ID, user_text, content, category)
                except Exception as _e:
                    print(f'[{user_id}][group] ⚠️ 记忆生命周期处理失败,按旧逻辑保留:{_e}')
                    lifecycle = {
                        'should_save_long_memory': True,
                        'long_memory_kwargs': {},
                    }
                if lifecycle.get('should_save_long_memory'):
                    kwargs = lifecycle.get('long_memory_kwargs') or {}
                    if save_long_memory(user_id, content, category, SHARED_CHARACTER_ID, **kwargs):
                        print(f'[{user_id}][group] ✅ 用户事实 [{category}]：{content}')
                else:
                    cls = lifecycle.get('classification') or {}
                    if lifecycle.get('consolidated'):
                        print(f'[{user_id}][group] ✅ 状态候选已巩固：{lifecycle["consolidated"]["content"]}')
                    print(f'[{user_id}][group] 🧠 生命周期记忆 [{cls.get("memory_kind")}/{cls.get("reason")}]: {content}')

        # C. 定向告知 → 目标角色的 told 桶
        td = parsed.get('told')
        if isinstance(td, dict):
            content = _clean_content(td.get('content'))
            target_name = (td.get('target') or '').strip()
            target_id = name_to_id.get(target_name)
            if target_id and _valid_told(user_id, content):
                if save_bond_memory(user_id, target_id, 'told', content):
                    print(f'[{user_id}][group] ✅ 告知记忆（{target_id}）：{content}')

        # D. ★ 角色互动回忆 → 各自的 bond 桶（第一人称）
        cbs = parsed.get('char_bonds')
        if isinstance(cbs, list):
            for cb in cbs[:3]:
                if not isinstance(cb, dict):
                    continue
                content = _clean_content(cb.get('content'))
                target_name = (cb.get('target') or '').strip()
                target_id = name_to_id.get(target_name)
                if target_id and _valid_bond(user_id, content):
                    if save_bond_memory(user_id, target_id, 'between', content):
                        print(f'[{user_id}][group] ✅ 互动记忆（{target_id}）：{content}')

        return True

    except Exception as e:
        print(f'群聊记忆提取失败：{e}')
        return False
