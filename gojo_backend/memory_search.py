"""memory_search.py —— 记忆检索（RAG）· 无 pgvector 版

★ 为什么不用 pgvector：
  Zeabur 自带的 PostgreSQL 镜像没有 vector 扩展（CREATE EXTENSION 报 not available）。
  但单用户场景根本不需要它 —— pgvector 是给百万级向量准备的。

  你的量级：5000 条记忆 × 1536 维 × 4 字节 ≈ 30 MB
  → 直接把向量读进进程内存，用 numpy 算余弦相似度，几毫秒出结果，比走数据库还快。

★ 方案：
  - embedding 存成普通 TEXT 列（JSON 数组），任何 PostgreSQL 都支持
  - 进程内缓存 {id: vector}，首次检索时加载，之后增量更新
  - 检索 = 内存里算余弦，取 top_k
  - numpy 有就用（快），没有就用纯 Python（慢一点，但几千条无所谓）

★ 环境变量：
    USE_RAG         —— 设成 1 才启用
    EMBED_API_KEY   —— OpenAI 兼容的 embedding key
    EMBED_BASE_URL  —— 默认 https://open.bigmodel.cn/api/paas/v4（智谱）
    EMBED_MODEL     —— 默认 embedding-3
    EMBED_DIM       —— 默认 2048（换模型时按模型维度改）

★ 降级策略：任何一步失败都返回 None，调用方自动退回"最新 N 条"全量注入，功能不受影响。
"""
import os
import json
import time
import threading
import requests
from db import get_conn

# ── 开关 ──
USE_RAG = os.environ.get('USE_RAG', '0') == '1'

EMBED_API_KEY  = os.environ.get('EMBED_API_KEY', '')
EMBED_BASE_URL = os.environ.get('EMBED_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4')
EMBED_MODEL    = os.environ.get('EMBED_MODEL', 'embedding-3')
# ★ 维度直接决定内存和速度，是最重要的旋钮：
#     20000 条 × 2048 维 = 156MB，检索 12ms
#     20000 条 × 1024 维 =  78MB，检索  6ms   ← 默认，这个档位性价比最高
#     20000 条 ×  512 维 =  39MB，检索  3ms
#   智谱 embedding-3 支持 dimensions 参数，1024 维对中文记忆检索完全够用。
EMBED_DIM      = int(os.environ.get('EMBED_DIM', '1024'))

# 内存里最多缓存多少条向量（每条 EMBED_DIM×4 字节）
# 1024 维时 20000 条 ≈ 78MB。聊几年也够，想更省就调小。
CACHE_MAX = int(os.environ.get('RAG_CACHE_MAX', '20000'))

_VECTOR_READY = False

# numpy 可选：有就快，没有就纯 Python 兜底
try:
    import numpy as _np
    _HAS_NUMPY = True
except ImportError:
    _np = None
    _HAS_NUMPY = False

# ── 进程内向量缓存 ──
# 结构：{ 'long_memory': {id: vec}, 'bond_memory': {id: vec} }
_CACHE = {'long_memory': {}, 'bond_memory': {}}
_CACHE_LOADED = {'long_memory': False, 'bond_memory': False}
_MATRIX = {'long_memory': None, 'bond_memory': None}   # 预拼好的矩阵，避免每次检索重建
_CACHE_LOCK = threading.Lock()


def init_vector_support():
    """启动时调用一次。只需要一个 TEXT 列，不依赖任何扩展。"""
    global _VECTOR_READY
    if not USE_RAG:
        print('[rag] 未启用（USE_RAG != 1），使用全量注入 + prompt 缓存')
        return False
    if not EMBED_API_KEY:
        print('[rag] ⚠️ 没配 EMBED_API_KEY，退回全量注入')
        return False
    try:
        conn = get_conn()
        cur = conn.cursor()
        # 普通 TEXT 列存 JSON 数组，任何 PG 都支持
        cur.execute('ALTER TABLE long_memory ADD COLUMN IF NOT EXISTS embedding_json TEXT')
        cur.execute('ALTER TABLE bond_memory ADD COLUMN IF NOT EXISTS embedding_json TEXT')
        conn.commit()
        cur.close()
        conn.close()
        _VECTOR_READY = True
        engine = 'numpy' if _HAS_NUMPY else '纯Python'
        print(f'[rag] ✅ 内存向量检索就绪，模型={EMBED_MODEL} 维度={EMBED_DIM} 引擎={engine}')
        return True
    except Exception as e:
        print(f'[rag] ⚠️ 初始化失败（{e}）→ 退回全量注入，功能不受影响')
        return False


def is_vector_ready():
    return _VECTOR_READY


def embed(text: str):
    """算一条 embedding。失败返回 None。"""
    if not (_VECTOR_READY and EMBED_API_KEY and text):
        return None
    try:
        r = requests.post(
            f'{EMBED_BASE_URL.rstrip("/")}/embeddings',
            headers={'Authorization': f'Bearer {EMBED_API_KEY}',
                     'Content-Type': 'application/json'},
            json={'model': EMBED_MODEL, 'input': text[:2000],
                  'dimensions': EMBED_DIM},   # 智谱 embedding-3 / OpenAI v3 都支持降维
            timeout=15,
        )
        if r.status_code != 200:
            print(f'[rag] embedding 失败 {r.status_code}: {r.text[:150]}')
            return None
        return r.json()['data'][0]['embedding']
    except Exception as e:
        print(f'[rag] embedding 异常：{e}')
        return None


# ══════════════════════════════════════════════
#  向量缓存
# ══════════════════════════════════════════════

def _to_vec(raw):
    """JSON 字符串 → 归一化后的向量（归一化后余弦相似度 = 点积，省一次开方）"""
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        if not v:
            return None
        if _HAS_NUMPY:
            # ★ 用 float32，不用 float16 ——
            #   实测 20000×2048 时 float16 检索要 183ms，float32 只要 12ms
            #   （numpy 的 float16 矩阵乘没有 SIMD 优化，内部反复转换）
            #   要省内存请调小 EMBED_DIM（1024 维内存减半、速度还更快）
            arr = _np.asarray(v, dtype=_np.float32)
            n = _np.linalg.norm(arr)
            if n <= 0:
                return None
            return arr / n
        n = sum(x * x for x in v) ** 0.5
        return [x / n for x in v] if n > 0 else None
    except Exception:
        return None


def _load_cache(table):
    """把表里的向量读进内存。只在首次检索时跑一次。

    ★ 上限保护：只加载最新 CACHE_MAX 条。
      聊几年后记忆可能上万条，全塞内存不划算 ——
      而且几年前的琐事本来就不该参与检索竞争。
      真要翻很老的东西，全量列表页(记忆页)一条都不少。
    """
    if _CACHE_LOADED[table]:
        return
    with _CACHE_LOCK:
        if _CACHE_LOADED[table]:
            return
        t0 = time.time()
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                f'''SELECT id, embedding_json FROM {table}
                    WHERE embedding_json IS NOT NULL
                    ORDER BY id DESC LIMIT %s''', (CACHE_MAX,))
            rows = cur.fetchall()
            cur.close()
            conn.close()
            store = {}
            for rid, raw in rows:
                v = _to_vec(raw)
                if v is not None:
                    store[rid] = v
            _CACHE[table] = store
            _MATRIX[table] = None
            _CACHE_LOADED[table] = True
            mb = len(store) * EMBED_DIM * 4 / 1024 / 1024   # float32 = 4 字节
            capped = ' (已达上限)' if len(rows) >= CACHE_MAX else ''
            print(f'[rag] 缓存 {table}: {len(store)} 条向量，约 {mb:.1f}MB{capped}，'
                  f'耗时 {(time.time()-t0)*1000:.0f}ms')
        except Exception as e:
            print(f'[rag] 加载 {table} 缓存失败：{e}')


def _cache_put(table, row_id, vec_raw):
    """新写入记忆时增量更新缓存，不用重新加载全表。"""
    v = _to_vec(vec_raw)
    if v is not None:
        _CACHE[table][row_id] = v
        _MATRIX[table] = None     # 矩阵失效，下次检索重建


def _cache_drop(table, row_ids):
    """记忆被删除/合并掉时清理缓存。"""
    for rid in row_ids:
        _CACHE[table].pop(rid, None)
    _MATRIX[table] = None


def _get_matrix(table):
    """拿到 (ids, 归一化矩阵)。★ 缓存住，不要每次检索都 np.stack —— 
    5000 条时重建一次要 200ms，缓存后只剩几毫秒。"""
    cached = _MATRIX.get(table)
    if cached is not None:
        return cached
    store = _CACHE[table]
    if not store:
        return None
    ids = list(store.keys())
    if _HAS_NUMPY:
        mat = _np.stack([store[i] for i in ids])
        built = (ids, mat)
    else:
        built = (ids, [store[i] for i in ids])
    _MATRIX[table] = built
    return built


def invalidate_cache(table=None):
    """外部改动记忆后强制重载（合并、批量删除时用）。"""
    targets = [table] if table else ['long_memory', 'bond_memory']
    with _CACHE_LOCK:
        for t in targets:
            _CACHE[t] = {}
            _MATRIX[t] = None
            _CACHE_LOADED[t] = False
    print(f'[rag] 缓存已失效，下次检索时重载：{targets}')


def save_embedding(table: str, row_id: int, content: str):
    """写记忆后调用（后台线程里跑，失败无所谓）。"""
    if not _VECTOR_READY:
        return
    vec = embed(content)
    if not vec:
        return
    try:
        raw = json.dumps(vec)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(f'UPDATE {table} SET embedding_json = %s WHERE id = %s', (raw, row_id))
        conn.commit()
        cur.close()
        conn.close()
        _cache_put(table, row_id, vec)
    except Exception as e:
        print(f'[rag] 存 embedding 失败：{e}')


# ══════════════════════════════════════════════
#  检索
# ══════════════════════════════════════════════

def _top_k_ids(table, query_vec, candidate_ids, top_k):
    """在候选 id 里找和 query 最像的 top_k。返回 [(id, 相似度)]，高→低。"""
    _load_cache(table)
    built = _get_matrix(table)
    if not built:
        return []
    ids, mat = built
    cand = set(candidate_ids)

    if _HAS_NUMPY:
        sims = mat @ query_vec                    # 点积 = 余弦（已归一化）
        order = _np.argsort(-sims)
        out = []
        for i in order:                            # 按相似度从高到低，只挑候选内的
            rid = ids[i]
            if rid in cand:
                out.append((rid, float(sims[i])))
                if len(out) >= top_k:
                    break
        return out

    scored = [(ids[i], sum(a * b for a, b in zip(mat[i], query_vec)))
              for i in range(len(ids)) if ids[i] in cand]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def search_long_memory(user_id, character_id, shared_id, query_text, top_k=8):
    """语义检索用户事实。返回 [(content, timestamp, category)] 或 None（退回全量）。"""
    if not _VECTOR_READY:
        return None
    qv = _to_vec(embed(query_text))
    if qv is None:
        return None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT id, content, timestamp, category FROM long_memory
               WHERE user_id = %s AND character_id IN (%s, %s)''',
            (user_id, character_id, shared_id)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            return None

        by_id = {r[0]: r for r in rows}
        hits = _top_k_ids('long_memory', qv, list(by_id.keys()), top_k)
        if not hits:
            return None
        out = []
        for rid, _sim in hits:
            r = by_id[rid]
            out.append((r[1], r[2], r[3] or '其他'))
        return out
    except Exception as e:
        print(f'[rag] 检索 long_memory 失败，退回全量：{e}')
        return None


def search_bond_memory(user_id, character_id, kind, query_text, top_k=6):
    """语义检索羁绊记忆。返回 [(id, content, timestamp)] 或 None。"""
    if not _VECTOR_READY:
        return None
    qv = _to_vec(embed(query_text))
    if qv is None:
        return None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT id, content, timestamp FROM bond_memory
               WHERE user_id = %s AND character_id = %s AND kind = %s''',
            (user_id, character_id, kind)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            return None

        by_id = {r[0]: r for r in rows}
        hits = _top_k_ids('bond_memory', qv, list(by_id.keys()), top_k)
        if not hits:
            return None
        return [by_id[rid] for rid, _sim in hits]
    except Exception as e:
        print(f'[rag] 检索 bond_memory 失败，退回全量：{e}')
        return None


def backfill_embeddings(limit=500):
    """把已有记忆补上 embedding。启用 RAG 后调 /rag/backfill 触发，可以多跑几次。"""
    if not _VECTOR_READY:
        return {'ok': False, 'reason': 'RAG 未就绪（检查 USE_RAG 和 EMBED_API_KEY）'}
    done, failed = 0, 0
    for table in ['long_memory', 'bond_memory']:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            f'SELECT id, content FROM {table} WHERE embedding_json IS NULL LIMIT %s', (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        for rid, content in rows:
            vec = embed(content)
            if not vec:
                failed += 1
                continue
            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(f'UPDATE {table} SET embedding_json = %s WHERE id = %s',
                            (json.dumps(vec), rid))
                conn.commit()
                cur.close()
                conn.close()
                _cache_put(table, rid, vec)
                done += 1
            except Exception as e:
                print(f'[rag] backfill 写入失败 {table}#{rid}：{e}')
                failed += 1

    remaining = _count_missing()
    print(f'[rag] 补齐 {done} 条（失败 {failed}），剩余未补 {remaining} 条')
    return {'ok': True, 'filled': done, 'failed': failed, 'remaining': remaining}


def _count_missing():
    """还有多少条没补 embedding。"""
    total = 0
    try:
        conn = get_conn()
        cur = conn.cursor()
        for table in ['long_memory', 'bond_memory']:
            cur.execute(f'SELECT COUNT(*) FROM {table} WHERE embedding_json IS NULL')
            total += cur.fetchone()[0]
        cur.close()
        conn.close()
    except Exception:
        return -1
    return total


def rag_status():
    """给 /rag/status 用：看清楚现在到底是什么状态。"""
    return {
        'use_rag_env': USE_RAG,
        'has_api_key': bool(EMBED_API_KEY),
        'vector_ready': _VECTOR_READY,
        'engine': 'numpy' if _HAS_NUMPY else 'pure-python',
        'model': EMBED_MODEL,
        'dim': EMBED_DIM,
        'base_url': EMBED_BASE_URL,
        'cached': {t: len(_CACHE[t]) for t in _CACHE},
        'cache_mb': round(
            sum(len(_CACHE[t]) for t in _CACHE) * EMBED_DIM * 4 / 1024 / 1024, 1),
        'missing_embeddings': _count_missing() if _VECTOR_READY else None,
        'auto_backfill_running': _AUTO_BACKFILL_RUNNING,
    }


# ══════════════════════════════════════════════
#  自动补齐：启动后台跑，永远不用手动调 /rag/backfill
# ══════════════════════════════════════════════

_AUTO_BACKFILL_RUNNING = False
_AUTO_THREAD = None

# 每批补多少条 / 批次之间歇多久（别把 embedding API 打爆，也别抢 CPU）
AUTO_BATCH = int(os.environ.get('RAG_AUTO_BATCH', '100'))
AUTO_INTERVAL = int(os.environ.get('RAG_AUTO_INTERVAL', '60'))


def _auto_backfill_loop():
    """后台慢慢补 embedding，补完就安静下来，有新的漏网again再补。

    为什么要有这个：
      - 启用 RAG 时历史记忆全都没向量，手动跑 backfill 很烦
      - 偶尔写记忆时 embedding API 抽风失败，会留下漏网的
      → 这个循环兜住所有情况，你永远不用管
    """
    global _AUTO_BACKFILL_RUNNING
    time.sleep(20)   # 等其它初始化先跑完
    idle_rounds = 0
    while True:
        try:
            if not _VECTOR_READY:
                time.sleep(300)
                continue
            missing = _count_missing()
            if missing <= 0:
                # 补完了，进入低频巡检（只为了兜住偶发失败）
                idle_rounds += 1
                time.sleep(min(3600, 300 * idle_rounds))
                continue
            idle_rounds = 0
            _AUTO_BACKFILL_RUNNING = True
            r = backfill_embeddings(limit=AUTO_BATCH)
            print(f'[rag] 自动补齐 +{r.get("filled", 0)}，还剩 {r.get("remaining", "?")}')
            _AUTO_BACKFILL_RUNNING = False
            time.sleep(AUTO_INTERVAL)
        except Exception as e:
            _AUTO_BACKFILL_RUNNING = False
            print(f'[rag] 自动补齐出错（不影响主流程）：{e}')
            time.sleep(300)


def start_auto_backfill():
    """在 gojo_server.py 启动时调一次。没启用 RAG 就什么都不做。"""
    global _AUTO_THREAD
    if not _VECTOR_READY or _AUTO_THREAD is not None:
        return
    _AUTO_THREAD = threading.Thread(target=_auto_backfill_loop, daemon=True)
    _AUTO_THREAD.start()
    print(f'[rag] 自动补齐已启动（每批 {AUTO_BATCH} 条，间隔 {AUTO_INTERVAL}s）')