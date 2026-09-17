"""Deterministic memory lifecycle helpers.

This module is intentionally model-free. It decides whether a newly extracted
user fact should stay as a short-lived state, become a sticky note, wait as an
episodic/candidate memory, or consolidate into durable long_memory.

Sticky notes written here are internal memory cues
(source=memory_lifecycle_fast_loop). They are not the user-facing 便利贴.
The 便利贴 UI only presents Slow Loop stickies (source=cognitive_slow_loop).
"""
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone

from config import CN_TZ
from cognitive_config import (
    COGNITIVE_DIARY_RECALL_ENTRY_CHARS,
    COGNITIVE_DIARY_RECALL_LIMIT,
)
from db import get_conn


MEMORY_LIFECYCLE_SOURCE = 'memory_lifecycle_fast_loop'

EPHEMERAL_TTL_SECONDS = 2 * 3600
CANDIDATE_TTL_SECONDS = 14 * 86400
EPISODIC_TTL_SECONDS = 30 * 86400
REACTIVATION_TTL_SECONDS = 14 * 86400

CONSOLIDATION_THRESHOLD = 3

ROUTINE_TERMS = (
    '洗澡', '洗漱', '睡觉', '睡了', '晚安', '吃饭', '去吃', '在吃',
    '上课', '下课', '复习', '学习', '写作业', '在路上', '出门', '回家',
    '上班', '下班', '改程序', '写代码', '赶路', '通勤',
)

GOAL_TERMS = (
    '考试', '面试', 'ddl', 'DDL', 'deadline', '截止', '交作业',
    '答辩', '开会', '复试', '手术', '签证', '汇报',
)

RELATIVE_DEADLINE_TERMS = (
    '明天', '后天', '今晚', '今天晚上', '早上', '上午', '下午', '晚上',
    '下周', '周一', '周二', '周三', '周四', '周五', '周六', '周日',
)

STATE_TOPIC_TERMS = {
    'sleep': ('没睡好', '睡不好', '失眠', '睡眠', '睡不着', '熬夜', '通宵'),
    'health': ('难受', '不舒服', '头疼', '头痛', '胃疼', '肚子疼', '发烧', '咳嗽', '腰疼', '腰酸'),
    'fatigue': ('很累', '好累', '累死', '疲惫', '困死', '没精神'),
    'mood': ('焦虑', '难过', '崩溃', '烦躁', '低落', '委屈'),
}

RECURRENCE_TERMS = (
    '又', '还是', '最近', '这几天', '这段时间', '一直', '经常', '总是',
    '反复', '连续', '老是', '每天', '每晚', '每次',
)

SALIENT_TERMS = (
    '摔倒', '受伤', '流血', '骨折', '晕倒', '昏倒', '事故', '医院',
    '急诊', '手术', '疼到', '哭了', '崩溃', '吵架', '分手', '被骂',
    '危险', '报警', '丢了', '失败', '没过', '挂科',
)

DURABLE_CATEGORIES = {'喜好', '厌恶', '身份', '关系'}
LONG_HEALTH_TERMS = ('长期', '一直以来', '病史', '确诊', '慢性', '多年')


MEMORY_LIFECYCLE_DDL = (
    '''CREATE TABLE IF NOT EXISTS memory_lifecycle_items (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        source_type TEXT NOT NULL DEFAULT 'chat',
        source_id TEXT NOT NULL,
        content TEXT NOT NULL,
        memory_kind TEXT NOT NULL
            CHECK (memory_kind IN (
                'ephemeral', 'candidate', 'episodic', 'consolidated',
                'sticky', 'diary', 'reflection'
            )),
        topic_key TEXT NOT NULL DEFAULT 'general',
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN (
                'active', 'reactivated', 'superseded', 'expired', 'archived'
            )),
        source_event_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
        evidence_count INTEGER NOT NULL DEFAULT 1,
        importance REAL NOT NULL DEFAULT 0.4,
        recall_weight REAL NOT NULL DEFAULT 0.5,
        expires_at TIMESTAMPTZ,
        last_reinforced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        superseded_by_id BIGINT REFERENCES memory_lifecycle_items(id) ON DELETE SET NULL,
        long_memory_id INTEGER,
        bond_memory_id INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, character_id, memory_kind, topic_key, source_type, source_id)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_recall
       ON memory_lifecycle_items
       (user_id, character_id, status, memory_kind, recall_weight DESC, last_reinforced_at DESC)''',
    '''CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_topic
       ON memory_lifecycle_items (user_id, character_id, topic_key, status)''',
    '''ALTER TABLE long_memory
       ADD COLUMN IF NOT EXISTS lifecycle_kind TEXT NOT NULL DEFAULT 'legacy' ''',
    '''ALTER TABLE long_memory
       ADD COLUMN IF NOT EXISTS recall_status TEXT NOT NULL DEFAULT 'active' ''',
    '''ALTER TABLE long_memory
       ADD COLUMN IF NOT EXISTS recall_weight REAL NOT NULL DEFAULT 1.0 ''',
    '''ALTER TABLE long_memory
       ADD COLUMN IF NOT EXISTS source_event_refs JSONB NOT NULL DEFAULT '[]'::jsonb ''',
    '''ALTER TABLE long_memory
       ADD COLUMN IF NOT EXISTS superseded_by_id INTEGER''',
    '''ALTER TABLE long_memory
       ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ''',
    '''CREATE INDEX IF NOT EXISTS idx_long_memory_recall_status
       ON long_memory (user_id, character_id, recall_status, timestamp DESC)''',
    '''ALTER TABLE memory_lifecycle_items
       ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ''',
    '''ALTER TABLE memory_lifecycle_items
       ADD COLUMN IF NOT EXISTS salience REAL DEFAULT 0.5''',
    '''ALTER TABLE memory_lifecycle_items
       ADD COLUMN IF NOT EXISTS strength REAL DEFAULT 0.5''',
    '''ALTER TABLE memory_lifecycle_items
       ADD COLUMN IF NOT EXISTS decay_state TEXT DEFAULT 'active' ''',
)


def init_memory_lifecycle_tables(conn=None):
    """Install lifecycle metadata tables and recall columns safely."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    cur = conn.cursor()
    try:
        for ddl in MEMORY_LIFECYCLE_DDL:
            cur.execute(ddl)
        if own_conn:
            conn.commit()
        print('[memory_lifecycle] 表已就绪')
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        cur.close()
        if own_conn:
            conn.close()


def _now():
    return datetime.now(timezone.utc)


def _contains_any(text, terms):
    return any(term in text for term in terms)


def _topic_for_text(text):
    for topic, terms in STATE_TOPIC_TERMS.items():
        if _contains_any(text, terms):
            return topic
    if _contains_any(text, ('考试', '复习', 'ddl', 'DDL', 'deadline', '答辩')):
        return 'goal.exam'
    if _contains_any(text, ('洗澡', '洗漱')):
        return 'routine.shower'
    if _contains_any(text, ('睡觉', '睡了', '晚安')):
        return 'routine.sleep'
    if _contains_any(text, ('吃饭', '去吃', '在吃')):
        return 'routine.meal'
    if _contains_any(text, ('上课', '下课', '复习', '学习')):
        return 'routine.study'
    if _contains_any(text, ('在路上', '出门', '回家', '赶路', '通勤')):
        return 'routine.transit'
    return 'general'


def _deadline_ttl_seconds(text, now=None):
    """Small deterministic expiry window for temporary goals."""
    if now is None:
        now = datetime.now(CN_TZ)
    if '后天' in text:
        target_days = 2
    elif '下周' in text:
        target_days = 8
    elif '明天' in text:
        target_days = 1
    elif '今晚' in text or '今天' in text:
        target_days = 0
    else:
        target_days = 3
    end_local = (now + timedelta(days=target_days)).replace(
        hour=23, minute=59, second=59, microsecond=0)
    return max(3600, int((end_local - now).total_seconds()))


def _has_recurrence_language(text):
    return _contains_any(text, RECURRENCE_TERMS)


def _is_long_health_statement(text):
    return _contains_any(text, LONG_HEALTH_TERMS)


def classify_memory_lifecycle(user_text, extracted_content='', category=None, now=None):
    """Classify a memory candidate without calling an LLM.

    Returns a plain dict so tests and callers can inspect the decision.
    """
    user_text = user_text or ''
    extracted_content = extracted_content or ''
    category = (category or '其他').strip() or '其他'
    text = f'{user_text}\n{extracted_content}'
    topic_key = _topic_for_text(text)
    has_salience = _contains_any(text, SALIENT_TERMS)
    has_routine = _contains_any(text, ROUTINE_TERMS)
    has_goal = _contains_any(text, GOAL_TERMS) and _contains_any(text, RELATIVE_DEADLINE_TERMS)
    has_state_topic = topic_key in STATE_TOPIC_TERMS
    has_recurrence = _has_recurrence_language(text)

    if has_goal:
        ttl = _deadline_ttl_seconds(text, now=now)
        return {
            'memory_kind': 'sticky',
            'topic_key': topic_key,
            'status': 'active',
            'should_save_long_memory': False,
            'should_record_lifecycle': True,
            'should_write_sticky': True,
            'ttl_seconds': ttl,
            'importance': 0.75,
            'recall_weight': 0.85,
            'reason': 'temporary_goal_or_deadline',
            'has_recurrence': has_recurrence,
        }

    if has_salience:
        return {
            'memory_kind': 'episodic',
            'topic_key': topic_key,
            'status': 'active',
            'should_save_long_memory': False,
            'should_record_lifecycle': True,
            'should_write_sticky': False,
            'ttl_seconds': EPISODIC_TTL_SECONDS,
            'importance': 0.78,
            'recall_weight': 0.78,
            'reason': 'salient_event_not_routine',
            'has_recurrence': has_recurrence,
        }

    if category in DURABLE_CATEGORIES:
        return {
            'memory_kind': 'long_fact',
            'topic_key': topic_key,
            'status': 'active',
            'should_save_long_memory': True,
            'should_record_lifecycle': False,
            'should_write_sticky': False,
            'ttl_seconds': None,
            'importance': 0.9,
            'recall_weight': 1.0,
            'reason': 'durable_category',
            'has_recurrence': has_recurrence,
        }

    if category == '健康' and _is_long_health_statement(text):
        return {
            'memory_kind': 'long_fact',
            'topic_key': topic_key,
            'status': 'active',
            'should_save_long_memory': True,
            'should_record_lifecycle': False,
            'should_write_sticky': False,
            'ttl_seconds': None,
            'importance': 0.9,
            'recall_weight': 1.0,
            'reason': 'long_running_health_fact',
            'has_recurrence': has_recurrence,
        }

    if has_state_topic:
        return {
            'memory_kind': 'candidate',
            'topic_key': topic_key,
            'status': 'active',
            'should_save_long_memory': False,
            'should_record_lifecycle': True,
            'should_write_sticky': False,
            'ttl_seconds': CANDIDATE_TTL_SECONDS,
            'importance': 0.58 if not has_recurrence else 0.7,
            'recall_weight': 0.55 if not has_recurrence else 0.7,
            'reason': 'transient_state_candidate',
            'has_recurrence': has_recurrence,
        }

    if has_routine or category == '状态':
        return {
            'memory_kind': 'ephemeral',
            'topic_key': topic_key,
            'status': 'active',
            'should_save_long_memory': False,
            'should_record_lifecycle': True,
            'should_write_sticky': False,
            'ttl_seconds': EPHEMERAL_TTL_SECONDS,
            'importance': 0.25,
            'recall_weight': 0.35,
            'reason': 'routine_or_current_state',
            'has_recurrence': has_recurrence,
        }

    return {
        'memory_kind': 'long_fact',
        'topic_key': topic_key,
        'status': 'active',
        'should_save_long_memory': True,
        'should_record_lifecycle': False,
        'should_write_sticky': False,
        'ttl_seconds': None,
        'importance': 0.65,
        'recall_weight': 0.9,
        'reason': 'default_durable_fact',
        'has_recurrence': has_recurrence,
    }


def build_source_ref(source_type, source_id, *, user_id=None, character_id=None):
    return {
        'source_type': source_type,
        'source_id': str(source_id),
        'user_id': user_id,
        'character_id': character_id,
        'captured_at': _now().isoformat(),
    }


def make_source_id(user_id, character_id, user_text, content, source_event_id=None):
    if source_event_id:
        sid = str(source_event_id).strip()
        if sid.startswith('raw_event:') or sid.startswith('memory_job:'):
            return sid
        return f'raw_event:{sid}'
    digest = hashlib.sha1(
        f'{user_id}\n{character_id}\n{user_text or ""}\n{content or ""}'.encode('utf-8')
    ).hexdigest()[:16]
    return f'chat:{digest}'


def build_sticky_content(content, topic_key):
    topic_hint = {
        'goal.exam': '临近考试/任务',
    }.get(topic_key, '临近事项')
    return f'{topic_hint}：{content}。到期后不要当作长期事实。'


def build_consolidated_summary(topic_key, evidence_contents):
    """Create a deterministic consolidation summary."""
    if topic_key == 'sleep':
        content = '她最近一段时间反复睡眠不好。'
    elif topic_key == 'health':
        content = '她最近一段时间身体状态反复不舒服。'
    elif topic_key == 'fatigue':
        content = '她最近一段时间经常觉得疲惫。'
    elif topic_key == 'mood':
        content = '她最近一段时间情绪状态反复不好。'
    else:
        clipped = '；'.join((c or '').strip()[:18] for c in evidence_contents[:3] if c)
        content = f'她最近反复提到同一类状态：{clipped}。' if clipped else '她最近反复提到同一类状态。'
    return {
        'content': content,
        'topic_key': topic_key,
        'memory_kind': 'consolidated',
        'category': '状态',
    }


def should_consolidate(evidence_count, *, has_recurrence=False):
    threshold = 2 if has_recurrence else CONSOLIDATION_THRESHOLD
    return evidence_count >= threshold


def _expires_at(ttl_seconds):
    if ttl_seconds is None:
        return None
    return _now() + timedelta(seconds=ttl_seconds)


def _json(value):
    return json.dumps(value or [], ensure_ascii=False)


def _record_lifecycle_item(cur, user_id, character_id, classification, content,
                           source_ref, source_type, source_id):
    expires_at = _expires_at(classification.get('ttl_seconds'))
    cur.execute(
        '''INSERT INTO memory_lifecycle_items (
               user_id, character_id, source_type, source_id, content,
               memory_kind, topic_key, status, source_event_refs,
               evidence_count, importance, recall_weight, expires_at
           )
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s::jsonb,
                   1, %s, %s, %s)
           ON CONFLICT (user_id, character_id, memory_kind, topic_key, source_type, source_id)
           DO UPDATE SET
               content = EXCLUDED.content,
               status = 'active',
               source_event_refs = memory_lifecycle_items.source_event_refs || EXCLUDED.source_event_refs,
               evidence_count = memory_lifecycle_items.evidence_count + 1,
               importance = GREATEST(memory_lifecycle_items.importance, EXCLUDED.importance),
               recall_weight = GREATEST(memory_lifecycle_items.recall_weight, EXCLUDED.recall_weight),
               expires_at = EXCLUDED.expires_at,
               last_reinforced_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
           RETURNING id''',
        (
            user_id, character_id, source_type, source_id, content,
            classification['memory_kind'], classification['topic_key'],
            _json([source_ref]), classification.get('importance', 0.4),
            classification.get('recall_weight', 0.5), expires_at,
        ),
    )
    return cur.fetchone()[0]


def _upsert_sticky_note(cur, user_id, character_id, classification, content, source_ref):
    note_key = f'memory_lifecycle.{classification["topic_key"]}'
    expires_at = _expires_at(classification.get('ttl_seconds'))
    sticky_content = build_sticky_content(content, classification['topic_key'])
    cur.execute(
        '''INSERT INTO cognitive_sticky_notes (
               user_id, character_id, note_key, content, status, source,
               source_event_refs, expires_at
           )
           VALUES (%s, %s, %s, %s, 'active', %s, %s::jsonb, %s)
           ON CONFLICT (user_id, character_id, note_key)
           DO UPDATE SET
               content = EXCLUDED.content,
               status = 'active',
               source = EXCLUDED.source,
               source_event_refs = cognitive_sticky_notes.source_event_refs || EXCLUDED.source_event_refs,
               expires_at = CASE
                   WHEN cognitive_sticky_notes.expires_at IS NULL THEN EXCLUDED.expires_at
                   WHEN EXCLUDED.expires_at IS NULL THEN cognitive_sticky_notes.expires_at
                   ELSE GREATEST(cognitive_sticky_notes.expires_at, EXCLUDED.expires_at)
               END,
               completed_at = NULL,
               updated_at = CURRENT_TIMESTAMP
           RETURNING id''',
        (
            user_id, character_id, note_key, sticky_content,
            MEMORY_LIFECYCLE_SOURCE, _json([source_ref]), expires_at,
        ),
    )
    return cur.fetchone()[0]


def _collect_source_refs(rows):
    refs = []
    seen = set()
    for row in rows:
        raw_refs = row.get('source_event_refs') or []
        if isinstance(raw_refs, str):
            try:
                raw_refs = json.loads(raw_refs)
            except Exception:
                raw_refs = []
        for ref in raw_refs:
            if not isinstance(ref, dict):
                continue
            key = (ref.get('source_type'), ref.get('source_id'))
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
    return refs


def _maybe_consolidate_candidate(cur, user_id, character_id, topic_key,
                                 has_recurrence=False):
    cur.execute(
        '''SELECT id, content, source_event_refs
           FROM memory_lifecycle_items
           WHERE user_id = %s
             AND character_id = %s
             AND topic_key = %s
             AND memory_kind = 'candidate'
             AND status IN ('active', 'reactivated')
             AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
           ORDER BY last_reinforced_at DESC''',
        (user_id, character_id, topic_key),
    )
    rows = [
        {'id': r[0], 'content': r[1], 'source_event_refs': r[2]}
        for r in cur.fetchall()
    ]
    refs = _collect_source_refs(rows)
    independent_source_ids = {
        ref.get('source_id') for ref in refs if ref.get('source_id')
    }
    if not should_consolidate(len(independent_source_ids), has_recurrence=has_recurrence):
        return None

    summary = build_consolidated_summary(topic_key, [r['content'] for r in rows])
    source_refs = refs[:30]
    source_id = f'consolidated:{topic_key}'

    cur.execute(
        '''INSERT INTO memory_lifecycle_items (
               user_id, character_id, source_type, source_id, content,
               memory_kind, topic_key, status, source_event_refs,
               evidence_count, importance, recall_weight
           )
           VALUES (%s, %s, %s, %s, %s, 'consolidated', %s, 'active',
                   %s::jsonb, %s, 0.86, 0.9)
           ON CONFLICT (user_id, character_id, memory_kind, topic_key, source_type, source_id)
           DO UPDATE SET
               content = EXCLUDED.content,
               source_event_refs = EXCLUDED.source_event_refs,
               evidence_count = EXCLUDED.evidence_count,
               status = 'active',
               recall_weight = GREATEST(memory_lifecycle_items.recall_weight, EXCLUDED.recall_weight),
               last_reinforced_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
           RETURNING id, long_memory_id''',
        (
            user_id, character_id, MEMORY_LIFECYCLE_SOURCE, source_id,
            summary['content'], topic_key, _json(source_refs),
            len(independent_source_ids),
        ),
    )
    lifecycle_id, long_memory_id = cur.fetchone()

    if long_memory_id:
        cur.execute(
            '''UPDATE long_memory
               SET content = %s,
                   recall_status = 'active',
                   recall_weight = 0.95,
                   source_event_refs = %s::jsonb,
                   mention_count = COALESCE(mention_count, 1) + 1,
                   last_mentioned = CURRENT_TIMESTAMP
               WHERE id = %s''',
            (summary['content'], _json(source_refs), long_memory_id),
        )
    else:
        cur.execute(
            '''INSERT INTO long_memory (
                   user_id, character_id, content, category, lifecycle_kind,
                   recall_status, recall_weight, source_event_refs
               )
               VALUES (%s, %s, %s, %s, 'consolidated', 'active', 0.95, %s::jsonb)
               RETURNING id''',
            (
                user_id, character_id, summary['content'],
                summary['category'], _json(source_refs),
            ),
        )
        long_memory_id = cur.fetchone()[0]
        cur.execute(
            '''UPDATE memory_lifecycle_items
               SET long_memory_id = %s, updated_at = CURRENT_TIMESTAMP
               WHERE id = %s''',
            (long_memory_id, lifecycle_id),
        )

    candidate_ids = [r['id'] for r in rows]
    if candidate_ids:
        cur.execute(
            '''UPDATE memory_lifecycle_items
               SET status = 'superseded',
                   superseded_by_id = %s,
                   recall_weight = LEAST(recall_weight, 0.15),
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ANY(%s)''',
            (lifecycle_id, candidate_ids),
        )

    return {
        'lifecycle_id': lifecycle_id,
        'long_memory_id': long_memory_id,
        'content': summary['content'],
        'source_event_refs': source_refs,
        'evidence_count': len(independent_source_ids),
    }


def apply_user_fact_lifecycle(user_id, character_id, user_text, content, category,
                              *, source_event_id=None, temporal_context=None):
    """Apply lifecycle routing for an extracted user fact.

    Returns:
      {
        'should_save_long_memory': bool,
        'classification': dict,
        'long_memory_kwargs': dict,
        ...
      }
    """
    classification = classify_memory_lifecycle(user_text, content, category)
    source_id = make_source_id(
        user_id, character_id, user_text, content, source_event_id=source_event_id)
    source_ref = build_source_ref(
        'raw_event' if source_event_id else 'chat',
        source_event_id or source_id,
        user_id=user_id,
        character_id=character_id,
    )
    if temporal_context:
        source_ref['temporal_context'] = {
            'elapsed_label': temporal_context.get('elapsed_label'),
            'gap_bucket': temporal_context.get('gap_bucket'),
            'now_utc': temporal_context.get('now_utc'),
        }

    if classification.get('should_save_long_memory'):
        return {
            'should_save_long_memory': True,
            'classification': classification,
            'long_memory_kwargs': {
                'lifecycle_kind': classification['memory_kind'],
                'recall_weight': classification.get('recall_weight', 0.9),
                'source_event_refs': [source_ref],
                'expires_at': None,
            },
        }

    result = {
        'should_save_long_memory': False,
        'classification': classification,
        'recorded_lifecycle_id': None,
        'sticky_note_id': None,
        'consolidated': None,
    }

    conn = get_conn()
    cur = conn.cursor()
    try:
        if classification.get('should_record_lifecycle'):
            result['recorded_lifecycle_id'] = _record_lifecycle_item(
                cur, user_id, character_id, classification, content,
                source_ref, 'raw_event' if source_event_id else 'chat', source_id,
            )
        if classification.get('should_write_sticky'):
            result['sticky_note_id'] = _upsert_sticky_note(
                cur, user_id, character_id, classification, content, source_ref)
        if classification['memory_kind'] == 'candidate':
            result['consolidated'] = _maybe_consolidate_candidate(
                cur, user_id, character_id, classification['topic_key'],
                has_recurrence=classification.get('has_recurrence', False),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    if result.get('recorded_lifecycle_id') and source_event_id:
        try:
            import raw_events
            raw_events.link_memory_sources(
                'lifecycle', result['recorded_lifecycle_id'], [source_event_id])
        except Exception:
            pass
    return result


def _parse_refs(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _score_recall_entry(content, user_message, weight=0.5, ts=None):
    text = content or ''
    kw = 0.0
    if user_message and text:
        msg = user_message.lower()
        pieces = [p for p in re.split(r'[，。,.\s;；、！!？?：:]+', text) if len(p) >= 2]
        for piece in pieces[:8]:
            if piece.lower() in msg:
                kw += 1.0
        for chunk in re.findall(r'[\u4e00-\u9fff]{2,}', text):
            if chunk in msg:
                kw += 0.35
                break
    recency = 0.65
    if ts and hasattr(ts, 'timestamp'):
        age_days = max(0, (_now().timestamp() - ts.timestamp()) / 86400)
        recency = 0.55 + 0.45 * math.exp(-age_days / 30)
    return (kw + weight) * recency


def recall_lifecycle_memories(user_id, character_id, user_message='', limit=6):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, memory_kind, topic_key, content, source_event_refs,
                      created_at, updated_at, expires_at, recall_weight, status
               FROM memory_lifecycle_items
               WHERE user_id = %s
                 AND character_id = %s
                 AND memory_kind IN ('ephemeral', 'candidate', 'episodic', 'consolidated')
                 AND status IN ('active', 'reactivated')
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
               ORDER BY recall_weight DESC, last_reinforced_at DESC
               LIMIT %s''',
            (user_id, character_id, limit * 3),
        )
        items = []
        for row in cur.fetchall():
            item = {
                'id': row[0],
                'memory_kind': row[1],
                'topic_key': row[2],
                'content': row[3],
                'source_event_refs': _parse_refs(row[4]),
                'created_at': row[5],
                'updated_at': row[6],
                'expires_at': row[7],
                'recall_weight': row[8],
                'status': row[9],
                'source_type': 'lifecycle',
            }
            item['score'] = _score_recall_entry(
                item['content'], user_message, item['recall_weight'], item['updated_at'])
            items.append(item)
        items.sort(key=lambda it: (it['score'], it['updated_at'] or datetime.min), reverse=True)
        return items[:limit]
    except Exception as e:
        print(f'[memory_lifecycle] lifecycle recall failed: {e}')
        return []
    finally:
        cur.close()
        conn.close()


def recall_sticky_notes(user_id, character_id, user_message='', limit=3):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, note_key, content, source, source_event_refs, expires_at, updated_at
               FROM cognitive_sticky_notes
               WHERE user_id = %s
                 AND character_id = %s
                 AND status = 'active'
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
               ORDER BY updated_at DESC
               LIMIT %s''',
            (user_id, character_id, limit * 2),
        )
        items = []
        for row in cur.fetchall():
            item = {
                'id': row[0],
                'note_key': row[1],
                'content': row[2],
                'source': row[3],
                'source_event_refs': _parse_refs(row[4]),
                'expires_at': row[5],
                'updated_at': row[6],
                'source_type': 'sticky_note',
            }
            item['score'] = _score_recall_entry(item['content'], user_message, 0.85, item['updated_at'])
            items.append(item)
        items.sort(key=lambda it: (it['score'], it['updated_at'] or datetime.min), reverse=True)
        return items[:limit]
    except Exception as e:
        print(f'[memory_lifecycle] sticky recall failed: {e}')
        return []
    finally:
        cur.close()
        conn.close()


def _diary_query_terms(user_message):
    # Remove conversational scaffolding before matching Chinese bigrams/words.
    query = re.sub(
        r'怎么想|怎么看|为什么|还记得|记不记得|那段时间|那时候|那件事|'
        r'当时|之前|今天|最近|现在|那天|想起|觉得|自己|我们|你们|什么|时候|'
        r'[我你她他的是了呢吗啊呀又还在和与]',
        ' ', (user_message or '').lower(),
    )
    terms = set()
    for chunk in re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]+', query):
        if re.fullmatch(r'[\u4e00-\u9fff]+', chunk):
            terms.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
        elif len(chunk) >= 3 and chunk not in {
            'the', 'and', 'you', 'was', 'were', 'that', 'this', 'about',
            'how', 'did', 'think', 'remember', 'then', 'what', 'your',
        }:
            terms.add(chunk)
    return sorted(terms)


def _select_diary_memories(items, terms, limit):
    topic_terms = set(GOAL_TERMS).union(*STATE_TOPIC_TERMS.values())
    selected = []
    for item in items:
        content = item['content'] or ''
        matched_terms = {
            term for term in terms
            if (re.search(r'\b' + re.escape(term) + r'\b', content.lower())
                if term.isascii() else term in content.lower())
        }
        relevance = len(matched_terms) / len(terms)
        # An explicit shared topic remains relevant when the user corrects its details.
        if relevance < 0.5 and not matched_terms.intersection(topic_terms):
            continue
        # Bound the visible excerpt around a match, keeping the original entry ID.
        maximum = COGNITIVE_DIARY_RECALL_ENTRY_CHARS
        first_match = min(content.lower().find(term) for term in matched_terms)
        start = max(0, first_match - maximum // 3) if len(content) > maximum else 0
        item['content'] = content[start:start + maximum]
        item['excerpt_truncated'] = start > 0 or len(content) > maximum
        item['score'] = relevance
        ts = item.get('timestamp')
        if ts and ts.tzinfo is None:
            item['timestamp'] = ts.replace(tzinfo=timezone.utc)
        selected.append(item)
    # Recency only breaks ties among relevant entries; it never grants eligibility.
    selected.sort(key=lambda item: (
        item['score'], item['timestamp'].timestamp() if item.get('timestamp') else 0,
    ), reverse=True)
    return selected[:limit]


def recall_diary_memories(user_id, character_id, user_message='', limit=3):
    """Read relevant subjective entries without creating or reinforcing evidence."""
    limit = max(0, min(limit, COGNITIVE_DIARY_RECALL_LIMIT))
    terms = _diary_query_terms(user_message)
    if not limit or not terms:
        return []
    conn = get_conn()
    cur = conn.cursor()
    items = []
    try:
        cur.execute(
            '''SELECT id, diary_key, content, reflection_kind, source_event_refs, occurred_at
               FROM cognitive_diary_entries
               WHERE user_id = %s AND character_id = %s
                 AND EXISTS (
                     SELECT 1 FROM unnest(%s::text[]) AS term(value)
                     WHERE strpos(lower(content), term.value) > 0)
               ORDER BY occurred_at DESC, id DESC LIMIT %s''',
            (user_id, character_id, terms, 30),
        )
        for row in cur.fetchall():
            item = {
                'id': row[0],
                'diary_key': row[1],
                'content': row[2],
                'reflection_kind': row[3],
                'source_event_refs': _parse_refs(row[4]),
                'timestamp': row[5],
                'source_type': 'reflection',
                'source_ref': {
                    'source_type': 'cognitive_diary_entries',
                    'source_id': row[0],
                    'user_id': user_id,
                    'character_id': character_id,
                },
            }
            items.append(item)

        cur.execute(
            '''SELECT id, content, emotion, created_at
               FROM char_diary
               WHERE user_id = %s AND character_id = %s
                 AND EXISTS (
                     SELECT 1 FROM unnest(%s::text[]) AS term(value)
                     WHERE strpos(lower(content), term.value) > 0)
               ORDER BY created_at DESC, id DESC LIMIT %s''',
            (user_id, character_id, terms, 30),
        )
        for row in cur.fetchall():
            item = {
                'id': row[0],
                'diary_key': f'char_diary:{row[0]}',
                'content': row[1],
                'reflection_kind': row[2] or 'diary',
                'source_event_refs': [{
                    'source_type': 'char_diary',
                    'source_id': row[0],
                    'user_id': user_id,
                    'character_id': character_id,
                }],
                'timestamp': row[3],
                'source_type': 'diary',
            }
            item['source_ref'] = dict(item['source_event_refs'][0])
            items.append(item)
        return _select_diary_memories(items, terms, limit)
    except Exception as e:
        print(f'[memory_lifecycle] diary recall failed: {e}')
        return []
    finally:
        cur.close()
        conn.close()


def detect_reactivation_query(user_text):
    text = user_text or ''
    if not _contains_any(text, ('想起', '又', '还是', '再说', '之前', '那次', '当时', '最近')):
        return None
    topic = _topic_for_text(text)
    if topic == 'general':
        return None
    return topic


def reactivate_lifecycle_memories(user_id, character_id, user_text):
    """Bring faded memories for a topic back into recall when new evidence appears."""
    topic_key = detect_reactivation_query(user_text)
    if not topic_key:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    try:
        expires_at = _expires_at(REACTIVATION_TTL_SECONDS)
        cur.execute(
            '''UPDATE memory_lifecycle_items
               SET status = 'reactivated',
                   expires_at = %s,
                   recall_weight = GREATEST(recall_weight, 0.65),
                   last_reinforced_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE user_id = %s
                 AND character_id = %s
                 AND topic_key = %s
                 AND status IN ('archived', 'expired')''',
            (expires_at, user_id, character_id, topic_key),
        )
        count = cur.rowcount
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
