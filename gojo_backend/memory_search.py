"""memory_search.py —— 记忆检索（RAG）

★ 设计原则：能升级就升级，升不了也绝不坏事。
  1. 启动时自动探测数据库有没有 pgvector 扩展（CREATE EXTENSION IF NOT EXISTS vector）
     - 装得上 → 用向量检索（语义相关，"我头疼"能召回"她有偏头痛"）
     - 装不上（Zeabur 自带 PG 可能没有）→ 自动退回关键词检索，功能照常，不报错
  2. 需要 EMBED_API_KEY 才启用向量；没配也自动退回关键词。
     环境变量：
       EMBED_API_KEY   —— OpenAI 兼容的 embedding key（DeepSeek/OpenAI 都行）
       EMBED_BASE_URL  —— 默认 https://api.openai.com/v1
       EMBED_MODEL     —— 默认 text-embedding-3-small
       EMBED_DIM       —— 默认 1536（换模型时按模型维度改）
  3. ★ 重要：本模块只服务于"检索"。记忆页的全量列表仍走 get_long_memory，一条都不会少。

★ 什么时候真正需要它：记忆条数上千以后。几百条时全量注入 + prompt 缓存反而更省
  （见 USE_RAG 开关说明）。
"""
import os
import json
import requests
from db import get_conn

# ── 开关 ──
# 默认 off：记忆量不大时，全量注入 + prompt 缓存（1/10 计费）比 RAG 更省也更不会漏记。
# 记忆涨到上千条、或觉得注入太长时，把环境变量 USE_RAG 设成 1 即可启用。
USE_RAG = os.environ.get('USE_RAG', '0') == '1'

EMBED_API_KEY  = os.environ.get('EMBED_API_KEY', '')
EMBED_BASE_URL = os.environ.get('EMBED_BASE_URL', 'https://api.openai.com/v1')
EMBED_MODEL    = os.environ.get('EMBED_MODEL', 'text-embedding-3-small')
EMBED_DIM      = int(os.environ.get('EMBED_DIM', '1536'))

_VECTOR_READY = False


def init_vector_support():
    """启动时调用一次：探测并准备 pgvector。失败不抛异常，只是退回关键词检索。"""
    global _VECTOR_READY
    if not USE_RAG:
        print('[rag] 未启用（USE_RAG != 1），使用全量注入 + prompt 缓存')
        return False
    if not EMBED_API_KEY:
        print('[rag] ⚠️ 没配 EMBED_API_KEY，退回关键词检索')
        return False
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('CREATE EXTENSION IF NOT EXISTS vector')
        conn.commit()
        # 加向量列（幂等）
        cur.execute(f'ALTER TABLE long_memory ADD COLUMN IF NOT EXISTS embedding vector({EMBED_DIM})')
        cur.execute(f'ALTER TABLE bond_memory ADD COLUMN IF NOT EXISTS embedding vector({EMBED_DIM})')
        conn.commit()
        # 索引（少量数据时可有可无，上千条后明显加速）
        try:
            cur.execute('CREATE INDEX IF NOT EXISTS idx_long_mem_vec ON long_memory '
                        'USING hnsw (embedding vector_cosine_ops)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_bond_mem_vec ON bond_memory '
                        'USING hnsw (embedding vector_cosine_ops)')
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f'[rag] 索引创建跳过（数据量小时无影响）：{e}')
        cur.close()
        conn.close()
        _VECTOR_READY = True
        print(f'[rag] ✅ pgvector 就绪，模型={EMBED_MODEL} 维度={EMBED_DIM}')
        return True
    except Exception as e:
        print(f'[rag] ⚠️ pgvector 不可用（{e}）→ 自动退回关键词检索，功能不受影响')
        return False


def is_vector_ready():
    return _VECTOR_READY


def embed(text: str):
    """算一条 embedding。失败返回 None（调用方自动退回关键词）。"""
    if not (_VECTOR_READY and EMBED_API_KEY and text):
        return None
    try:
        r = requests.post(
            f'{EMBED_BASE_URL}/embeddings',
            headers={'Authorization': f'Bearer {EMBED_API_KEY}',
                     'Content-Type': 'application/json'},
            json={'model': EMBED_MODEL, 'input': text[:2000]},
            timeout=10,
        )
        if r.status_code != 200:
            print(f'[rag] embedding 失败 {r.status_code}: {r.text[:120]}')
            return None
        return r.json()['data'][0]['embedding']
    except Exception as e:
        print(f'[rag] embedding 异常：{e}')
        return None


def save_embedding(table: str, row_id: int, content: str):
    """写记忆后调用（后台线程里跑，失败无所谓）。table: long_memory / bond_memory"""
    if not _VECTOR_READY:
        return
    vec = embed(content)
    if not vec:
        return
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(f'UPDATE {table} SET embedding = %s WHERE id = %s',
                    (json.dumps(vec), row_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f'[rag] 存 embedding 失败：{e}')


def _keyword_fallback(rows, query_text, top_k):
    """关键词检索兜底：按查询里的字在内容里的命中数排序。"""
    if not query_text:
        return rows[:top_k]
    chars = {c for c in query_text if len(c.strip()) and not c.isspace()}
    scored = []
    for r in rows:
        content = r[0] if isinstance(r, (tuple, list)) else r
        hit = sum(1 for c in chars if c in content)
        scored.append((hit, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for hit, r in scored if hit > 0][:top_k]


def search_long_memory(user_id, character_id, shared_id, query_text, top_k=8):
    """语义检索用户事实。返回 [(content, timestamp, category)]。
    向量不可用时返回 None，调用方继续用全量注入。"""
    if not _VECTOR_READY:
        return None
    vec = embed(query_text)
    if not vec:
        return None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT content, timestamp, category FROM long_memory
               WHERE user_id = %s AND character_id IN (%s, %s) AND embedding IS NOT NULL
               ORDER BY embedding <=> %s::vector LIMIT %s''',
            (user_id, character_id, shared_id, json.dumps(vec), top_k)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [(r[0], r[1], r[2] or '其他') for r in rows]
    except Exception as e:
        print(f'[rag] 检索失败，退回全量：{e}')
        return None


def search_bond_memory(user_id, character_id, kind, query_text, top_k=6):
    """语义检索羁绊记忆。返回 [(id, content, timestamp)] 或 None。"""
    if not _VECTOR_READY:
        return None
    vec = embed(query_text)
    if not vec:
        return None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT id, content, timestamp FROM bond_memory
               WHERE user_id = %s AND character_id = %s AND kind = %s AND embedding IS NOT NULL
               ORDER BY embedding <=> %s::vector LIMIT %s''',
            (user_id, character_id, kind, json.dumps(vec), top_k)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [(r[0], r[1], r[2]) for r in rows]
    except Exception as e:
        print(f'[rag] 检索失败，退回全量：{e}')
        return None


def backfill_embeddings(limit=500):
    """一次性把已有记忆补上 embedding。启用 RAG 后手动调 /rag/backfill 触发。"""
    if not _VECTOR_READY:
        return {'ok': False, 'reason': 'pgvector 未就绪'}
    done = 0
    for table in ['long_memory', 'bond_memory']:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(f'SELECT id, content FROM {table} WHERE embedding IS NULL LIMIT %s', (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        for rid, content in rows:
            vec = embed(content)
            if not vec:
                continue
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(f'UPDATE {table} SET embedding = %s WHERE id = %s', (json.dumps(vec), rid))
            conn.commit()
            cur.close()
            conn.close()
            done += 1
    print(f'[rag] 补齐 {done} 条 embedding')
    return {'ok': True, 'filled': done}
