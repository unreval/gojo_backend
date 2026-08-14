"""smart_recall.py —— 两级智能召回

替代原来 prompt.py 里对 get_long_memory / get_bond_memories 的直接调用。

★ 设计目标：
  1. 省 token：不再把最近 40+30 条全灌进去，而是挑最相关的 15~20 条
  2. 精准：多因子评分，重要记忆不会被流水账挤掉
  3. 有上下文：召回重点事实时，自动带出它关联的羁绊细节

★ 两级召回流程：
  第一级：对所有 long_memory 打分，取 top_k 个用户事实
  第二级：对每个被选中的事实，拉 linked_fact_id 指向它的 bond_memory（最多 2 条/事实）
  兜底池：没有 linked_fact_id 的独立 bond，按时间/相关度单独取几条

★ 评分公式（借鉴 work 的 memoryRetrieval.js，适配你的三桶结构）：
  score = (关键词命中×2 + 向量相似度) × 提及强度 × 时间衰减 × 分类权重

★ 降级：任何环节出错都退回原来的 get_long_memory + get_bond_memories，绝不影响聊天。
"""

import math
import time as _time
from datetime import datetime, timezone

from db import get_conn
from config import CN_TZ

# ══════════════════════════════════════════════
#  评分参数（可调）
# ══════════════════════════════════════════════

# 身份相关分类，权重更高（这些忘了最伤）
IDENTITY_CATS = {'身份', '关系', '喜好', '厌恶', '健康'}
IDENTITY_WEIGHT = 1.3
NORMAL_WEIGHT = 1.0

# 时间衰减半衰期（天）：45 天后衰减到约 0.77，90 天约 0.66
DECAY_HALFLIFE = 45

# 向量相似度阈值：低于这个不算命中
VEC_THRESHOLD = 0.35
VEC_SCALE = 6  # 超过阈值的部分乘以这个系数

# 各级取多少条
FACT_TOP_K = 12         # 第一级：最多取多少条事实
BOND_PER_FACT = 2       # 第二级：每条事实最多带几条关联 bond
LOOSE_BOND_K = 6        # 兜底池：独立 bond 取几条
TOLD_TOP_K = 5          # told 桶单独取几条
PINNED_MAX = 15         # 钉住的记忆最多注入多少条

# 状态类记忆过期时间（小时）
STATUS_EXPIRE_HOURS = 48


# ══════════════════════════════════════════════
#  评分函数
# ══════════════════════════════════════════════

def _category_weight(category):
    return IDENTITY_WEIGHT if category in IDENTITY_CATS else NORMAL_WEIGHT


def _time_decay(last_ts, now):
    """时间衰减：最近的接近 1.0，越久越低但不会低于 0.55"""
    if not last_ts:
        return 0.7  # 没有时间戳的给个中间值
    if hasattr(last_ts, 'timestamp'):
        age_seconds = now - last_ts.timestamp()
    else:
        age_seconds = now - last_ts
    age_days = max(0, age_seconds / 86400)
    return 0.55 + 0.45 * math.exp(-age_days / DECAY_HALFLIFE)


def _mention_strength(count):
    """提及次数强化：被提起越多越重要，但边际递减"""
    return 1 + math.log2(1 + max(0, count - 1))


def _keyword_hits(content, user_message):
    """关键词命中：把记忆内容拆成片段，看多少在用户消息里出现"""
    if not user_message or not content:
        return 0
    msg = user_message.lower()
    # 按标点和空白拆成 ≥2 字的片段
    import re
    fragments = [f for f in re.split(r'[，。,.\s;；、！!？?：:]+', content) if len(f) >= 2]
    hits = 0
    for f in fragments:
        if f.lower() in msg:
            hits += 1
    # 兜底：如果片段没命中，试单个关键字（≥2 字的连续汉字块）
    if hits == 0:
        chars = re.findall(r'[\u4e00-\u9fff]{2,}', content)
        for c in chars:
            if c in msg:
                hits = 0.5
                break
    return hits


def score_fact(content, category, timestamp, mention_count, last_mentioned,
               user_message, query_embedding=None, memory_embedding=None):
    """给一条 long_memory 打分。分数越高越该被召回。"""
    kw = _keyword_hits(content, user_message)

    # 向量相似度（可选）
    vec = 0
    if query_embedding is not None and memory_embedding is not None:
        try:
            sim = _cosine_sim(query_embedding, memory_embedding)
            if sim > VEC_THRESHOLD:
                vec = (sim - VEC_THRESHOLD) * VEC_SCALE
        except Exception:
            pass

    # 时间衰减用 last_mentioned（如果有），否则用创建时间
    now_ts = _time.time()
    decay = _time_decay(last_mentioned or timestamp, now_ts)
    strength = _mention_strength(mention_count or 1)
    cat_w = _category_weight(category or '其他')

    return (kw * 2 + vec) * strength * decay * cat_w


def _cosine_sim(a, b):
    """余弦相似度（纯 Python，向量已归一化时等于点积）"""
    if not a or not b or len(a) != len(b):
        return 0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0
    return dot / (na * nb)


# ══════════════════════════════════════════════
#  两级召回主函数
# ══════════════════════════════════════════════

def two_level_recall(user_id, character_id, user_message,
                     shared_id='shared', query_embedding=None):
    """两级智能召回。返回结构化结果，供 prompt.py 组装。

    返回 dict:
      {
        'facts': [
            {
                'id': int, 'content': str, 'timestamp': datetime,
                'category': str, 'pinned': bool, 'score': float,
                'bonds': [  # 关联的 bond，最多 BOND_PER_FACT 条
                    {'content': str, 'timestamp': datetime}
                ]
            }
        ],
        'loose_bonds': [  # 没有关联事实的独立 bond
            {'id': int, 'content': str, 'timestamp': datetime}
        ],
        'tolds': [  # 她告诉角色的事
            {'id': int, 'content': str, 'timestamp': datetime}
        ]
      }

    失败时返回 None，调用方退回旧逻辑。
    """
    try:
        t0 = _time.time()

        # ── 1. 拉全量 long_memory（不再 LIMIT 40，全部拉出来打分）──
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT id, content, timestamp, category,
                      COALESCE(mention_count, 1) as mention_count,
                      last_mentioned,
                      COALESCE(pinned, FALSE) as pinned
               FROM long_memory
               WHERE user_id = %s AND character_id IN (%s, %s)
               ORDER BY timestamp DESC''',
            (user_id, character_id, shared_id)
        )
        all_facts = cur.fetchall()

        if not all_facts:
            cur.close()
            conn.close()
            return {'facts': [], 'loose_bonds': [], 'tolds': []}

        # ── 向量缓存（如果 RAG 开着的话）──
        fact_embeddings = {}
        if query_embedding is not None:
            try:
                import memory_search
                if memory_search.is_vector_ready():
                    memory_search._load_cache('long_memory')
                    fact_embeddings = memory_search._CACHE.get('long_memory', {})
            except Exception:
                pass

        # ── 2. 分离 pinned 和普通记忆 ──
        pinned = []
        pool = []
        now_utc = datetime.utcnow()

        for row in all_facts:
            fid, content, ts, category, mention_count, last_mentioned, is_pinned = row
            category = category or '其他'

            # 状态类过期检查
            if category == '状态' and ts is not None:
                age_hours = (now_utc - ts).total_seconds() / 3600
                if age_hours > STATUS_EXPIRE_HOURS:
                    continue

            entry = {
                'id': fid, 'content': content, 'timestamp': ts,
                'category': category, 'pinned': bool(is_pinned),
                'mention_count': mention_count,
                'last_mentioned': last_mentioned,
            }

            if is_pinned:
                pinned.append(entry)
            else:
                pool.append(entry)

        # ── 3. 给普通记忆打分 ──
        for entry in pool:
            mem_emb = fact_embeddings.get(entry['id'])
            entry['score'] = score_fact(
                entry['content'], entry['category'],
                entry['timestamp'], entry['mention_count'],
                entry['last_mentioned'], user_message,
                query_embedding, mem_emb
            )

        # 按分数排序，取 top_k
        pool.sort(key=lambda x: x['score'], reverse=True)
        selected_facts = pinned[:PINNED_MAX] + pool[:FACT_TOP_K]

        # 身份类强制补回（和你原来的逻辑一样）
        selected_ids = {f['id'] for f in selected_facts}
        for entry in pool:
            if entry['id'] not in selected_ids and entry['category'] == '身份':
                selected_facts.append(entry)
                selected_ids.add(entry['id'])

        # ── 4. 第二级：拉关联 bond ──
        fact_ids = list(selected_ids)
        linked_bonds = {}  # fact_id → [bonds]

        if fact_ids:
            # 用 linked_fact_id 查关联 bond
            placeholders = ','.join(['%s'] * len(fact_ids))
            cur.execute(
                f'''SELECT id, content, timestamp, linked_fact_id
                    FROM bond_memory
                    WHERE user_id = %s AND character_id = %s
                      AND kind = 'between'
                      AND linked_fact_id IN ({placeholders})
                    ORDER BY timestamp DESC''',
                (user_id, character_id, *fact_ids)
            )
            for bid, bcontent, bts, linked_id in cur.fetchall():
                if linked_id not in linked_bonds:
                    linked_bonds[linked_id] = []
                if len(linked_bonds[linked_id]) < BOND_PER_FACT:
                    linked_bonds[linked_id].append({
                        'id': bid, 'content': bcontent, 'timestamp': bts
                    })

        # 给每条事实挂上它的 bonds
        for fact in selected_facts:
            fact['bonds'] = linked_bonds.get(fact['id'], [])
            fact.setdefault('score', 0)

        # ── 5. 兜底池：没有 linked_fact_id 的独立 bond ──
        linked_bond_ids = set()
        for bonds in linked_bonds.values():
            for b in bonds:
                linked_bond_ids.add(b['id'])

        cur.execute(
            '''SELECT id, content, timestamp FROM bond_memory
               WHERE user_id = %s AND character_id = %s AND kind = 'between'
                 AND (linked_fact_id IS NULL OR linked_fact_id = 0)
               ORDER BY timestamp DESC LIMIT %s''',
            (user_id, character_id, LOOSE_BOND_K * 2)  # 多取一些，后面可以按相关度筛
        )
        loose_candidates = cur.fetchall()

        # 对独立 bond 也做简单的关键词打分
        loose_bonds = []
        for bid, bcontent, bts in loose_candidates:
            if bid in linked_bond_ids:
                continue
            kw_score = _keyword_hits(bcontent, user_message)
            loose_bonds.append({
                'id': bid, 'content': bcontent, 'timestamp': bts,
                'score': kw_score
            })

        # 有关键词命中的排前面，没命中的按时间排
        loose_bonds.sort(key=lambda x: (x['score'] > 0, x['score'], x['timestamp'] or datetime.min),
                         reverse=True)
        loose_bonds = loose_bonds[:LOOSE_BOND_K]

        # ── 6. told 桶 ──
        cur.execute(
            '''SELECT id, content, timestamp FROM bond_memory
               WHERE user_id = %s AND character_id = %s AND kind = 'told'
               ORDER BY timestamp DESC LIMIT %s''',
            (user_id, character_id, TOLD_TOP_K)
        )
        tolds = [{'id': r[0], 'content': r[1], 'timestamp': r[2]} for r in cur.fetchall()]

        cur.close()
        conn.close()

        elapsed = (_time.time() - t0) * 1000
        total_injected = len(selected_facts) + sum(len(f['bonds']) for f in selected_facts) + len(loose_bonds) + len(tolds)
        print(f'[recall] 两级召回完成：{len(selected_facts)} 条事实'
              f'（{len(pinned)} pinned + {len(selected_facts) - len(pinned)} scored）'
              f' + {sum(len(f["bonds"]) for f in selected_facts)} 条关联bond'
              f' + {len(loose_bonds)} 条独立bond'
              f' + {len(tolds)} 条told'
              f' = 共 {total_injected} 条'
              f'，耗时 {elapsed:.0f}ms')

        return {
            'facts': selected_facts,
            'loose_bonds': loose_bonds,
            'tolds': tolds,
        }

    except Exception as e:
        print(f'[recall] ⚠️ 两级召回失败，退回旧逻辑：{e}')
        import traceback
        traceback.print_exc()
        return None


# ══════════════════════════════════════════════
#  提及计数（每次聊天后调用）
# ══════════════════════════════════════════════

def reinforce_mentioned_facts(user_id, character_id, user_message, shared_id='shared'):
    """用户消息命中了哪些 long_memory 的关键词，给它们加 mention_count。

    不需要精确，只是让"经常被聊到"的记忆排名更高。
    在 extract_and_save_memory 之后调用即可。
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT id, content FROM long_memory
               WHERE user_id = %s AND character_id IN (%s, %s)''',
            (user_id, character_id, shared_id)
        )
        rows = cur.fetchall()

        reinforced = 0
        for mid, content in rows:
            if _keyword_hits(content, user_message) > 0:
                cur.execute(
                    '''UPDATE long_memory
                       SET mention_count = COALESCE(mention_count, 1) + 1,
                           last_mentioned = CURRENT_TIMESTAMP
                       WHERE id = %s''',
                    (mid,)
                )
                reinforced += 1

        conn.commit()
        cur.close()
        conn.close()

        if reinforced:
            print(f'[recall] 强化了 {reinforced} 条被提起的记忆')

    except Exception as e:
        print(f'[recall] 记忆强化失败（不影响主流程）：{e}')


# ══════════════════════════════════════════════
#  关联绑定（提取记忆时调用）
# ══════════════════════════════════════════════

def link_bond_to_fact(user_id, character_id, bond_content, shared_id='shared'):
    """新存入一条 bond 后，尝试找到它最相关的 long_memory，返回 fact_id。

    用简单的关键词重叠来匹配，不需要向量。
    返回 int (fact_id) 或 None。
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT id, content FROM long_memory
               WHERE user_id = %s AND character_id IN (%s, %s)''',
            (user_id, character_id, shared_id)
        )
        facts = cur.fetchall()
        cur.close()
        conn.close()

        if not facts:
            return None

        best_id = None
        best_score = 0
        bond_lower = bond_content.lower()

        for fid, fcontent in facts:
            import re
            fragments = [f for f in re.split(r'[，。,.\s;；、！!？?：:]+', fcontent) if len(f) >= 2]
            hits = sum(1 for f in fragments if f.lower() in bond_lower)
            if hits > best_score:
                best_score = hits
                best_id = fid

        # 至少要命中 1 个片段才算关联
        return best_id if best_score >= 1 else None

    except Exception as e:
        print(f'[recall] 关联绑定失败（不影响主流程）：{e}')
        return None


# ══════════════════════════════════════════════
#  Prompt 组装辅助
# ══════════════════════════════════════════════

def format_recall_for_prompt(recall_result):
    """把 two_level_recall 的结果格式化成 prompt 文本。

    返回 (memory_text, bond_text, told_text) 三个字符串，
    和原来 prompt.py 里的格式对齐，可以直接替换。
    """
    if not recall_result:
        return '', '', ''

    facts = recall_result.get('facts', [])
    loose_bonds = recall_result.get('loose_bonds', [])
    tolds = recall_result.get('tolds', [])

    # ── 用户事实（带关联 bond 缩进显示）──
    memory_text = ''
    if facts:
        lines = []
        for f in facts:
            ts = f['timestamp']
            date_str = ts.strftime('%Y-%m-%d') if ts else '?'
            tag = '（当时的状态，仅当天有效）' if f['category'] == '状态' else ''
            pin_mark = '📌 ' if f.get('pinned') else ''
            lines.append(f'- {pin_mark}[{date_str}] {f["content"]}{tag}')

            # 关联的 bond 缩进显示
            for b in f.get('bonds', []):
                bdate = b['timestamp'].strftime('%Y-%m-%d') if b.get('timestamp') else '?'
                lines.append(f'  · [{bdate}] {b["content"]}')

        memory_text = f'''

【关于对方的已确认事实——这些都是真实发生过的，你必须当作确实知道】
{chr(10).join(lines)}

使用规则：
1. 这些是关于【对方/用户本人】的事实，当作真的、不要质疑。但它们只约束"你对用户的了解"，绝不能拿来推翻或补充角色自己的原作设定——一旦涉及角色设定，一律以上面的【设定铁律】为准。
2. 自然融入回复，不要刻意背诵清单。
3. 列表里有的事必须当作记得，没有的可以说不记得。
4. 标着"（当时的状态）"的条目只代表记录当天的情况——不代表此刻仍然成立。她说过已经好了/过去了，就是过去了。
5. 【关心的分寸】同一件事的叮嘱（吃药/早睡/多喝水这类）点到为止。
6. 带 · 缩进的是和这条事实相关的具体经历细节，帮你回忆起语境。'''

    # ── 独立羁绊（没有关联事实的）──
    bond_text = ''
    if loose_bonds:
        bond_lines = []
        for b in loose_bonds:
            bdate = b['timestamp'].strftime('%Y-%m-%d') if b.get('timestamp') else '?'
            bond_lines.append(f'- [{bdate}] {b["content"]}')
        bond_text = f'''

【你们之间的事——你和她共同的回忆】
（这些是以你自己的视角记下的回忆——条目里的"我"就是你本人。）
{chr(10).join(bond_lines)}'''

    # ── told ──
    told_text = ''
    if tolds:
        told_lines = []
        for t in tolds:
            tdate = t['timestamp'].strftime('%Y-%m-%d') if t.get('timestamp') else '?'
            told_lines.append(f'- [{tdate}] {t["content"]}')
        told_text = f'''

【她告诉过你的事——关于你自己或你的世界】
（这些是她在过去的对话里亲口告诉你的。你清楚地记得"她说过这些话"。）
{chr(10).join(told_lines)}

处理规则——非常重要：
1. 你【记得她说过】这些，绝不能表现得从没听过。她再次提起时，你要接得上。
2. 但这些是"她的说法"，不是你亲身经历的事实。信、半信半疑、觉得荒唐、心情复杂——由你的性格决定。
3. 这些说法不改变你的原作设定和你所处的时间点。'''

    return memory_text, bond_text, told_text
