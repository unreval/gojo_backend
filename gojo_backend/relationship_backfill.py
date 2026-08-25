"""relationship_backfill.py —— v4 上线后，一次性把已有用户的关系账本初始化到合理基线

**用途**：
在 v4 系统上线前你已经和角色聊了 N 天，这时候 rel_state 是空，
所有老用户都会被判成"陌生人 · 普通陌生人"。这个脚本读取三个历史数据源：
  - long_memory     (关于用户的事实)
  - bond_memory/between (共同经历)
  - bond_memory/told    (用户告诉过角色的事)
把它们拼给 Opus 4.6 打分，一次性把 rel_state 初始化到合理数值。

**特性**：
- 只作用于 rel_state 全空的行（不会覆盖已积累的账本）
- 每处理一个 (user_id, character_id) 都会把估算过程写 provenance（可倒查）
- 幂等：重复跑不会重复叠加
- 单进程串行，不并发（避免撞 API 限流；老用户没多少个，慢点无所谓）

**使用**：
```bash
# 干跑：只打印会做什么，不写数据库
python3 relationship_backfill.py --dry-run

# 处理某一个 user + character
python3 relationship_backfill.py --user_id user_xxx --character_id gojo

# 处理某个 user 的所有角色
python3 relationship_backfill.py --user_id user_xxx

# 处理所有满足条件的 (user, character)（谨慎！）
python3 relationship_backfill.py --all
```
"""
import argparse
import json
import re
import sys
from typing import Dict, List, Optional, Tuple

from db import get_conn
from ai_client import create_chat
from config import MODEL_MAIN

from relationship_db import ensure_state_row
from relationship_state import (
    load_state,
    apply_warmth, apply_intimacy, apply_trust,
    apply_attachment, apply_commitment, apply_passion,
    apply_friction,
)


# ══════════════════════════════════════════════════════════════
# LLM 打分 prompt
# ══════════════════════════════════════════════════════════════
_BACKFILL_SYSTEM_PROMPT = '''你是一个关系状态评估员。你会看到某个用户 (user) 和某个角色 (character)
过去若干天累积的记忆片段，你的任务是【估算】这段关系目前处在什么位置。

【★ 铁律】
- 只给出估算，不要解释、不要评论、不要煽情。
- 只输出 JSON，不要 markdown 围栏、不要前后缀。
- 数值范围 0-100，不要给负数（对某人的负面感情，通过 friction 表达，不通过负 W）。
- 如果证据不足以支撑某维度，就给 0 或 5 这种"很浅"的值，不要瞎给中值。
- 用户单方面撒娇/自我宣告不算证据。真实证据是"共同经历过什么""角色是怎么反应的""关系是不是自然到了这一步"。

【要输出的 JSON schema】
{
  "warmth":     <0-100>,   // 角色对用户的温度：想靠近、有好感
  "intimacy":   <0-100>,   // 亲密度：愿意露出多少真实自己（洋葱：日常→价值观→核心）
  "trust":      <0-100>,   // 信任：承诺兑现、边界被尊重的稳定表现
  "attachment": <0-100>,   // 依恋：这个人对角色的心理位置有多重要
  "commitment": <0-100>,   // 承诺：愿意为关系投入的程度（重成本行为，不重频率）
  "passion":    <0-100>,   // 心动/暧昧张力（记忆里如果没有真实心动瞬间就给 0）
  "friction_categories": {
    "<category_name>": <0-100>,
    ...
  },                       // 记忆里如果没有明显冲突就给 {}
  "reasoning": "<一两句话说清楚你为什么给这些值，方便审计。50 字以内。>"
}

【常见误判——需要主动纠正】
- 记忆里用户说了很多次"我喜欢你" → 不代表 Passion 高（那是用户单方面）
- 记忆里都是日常小事 → I 给 20-40，不是 60+
- 记忆里角色主动关心用户细节多 → W 和 Attachment 能给中高档
- 记忆里角色对用户暴露过自己的事 → I 中档+，如果有核心创伤级 → I 高档
- 记忆里从没吵架也没冲突 → friction 就是 {}，不要生编
- 从"聊了很多天"这个事实本身不能推 Trust —— Trust 要具体的承诺兑现证据
'''


# ══════════════════════════════════════════════════════════════
# 从三个数据源拼装历史材料
# ══════════════════════════════════════════════════════════════
def collect_history(user_id: str, character_id: str) -> Dict:
    """收集用于评估的所有历史。返回 dict，包含 long_memory / bond_between / bond_told 三段。"""
    conn = get_conn()
    cur = conn.cursor()

    # long_memory 是用户事实（shared，不分角色）
    cur.execute('''SELECT content, category, timestamp FROM long_memory
                   WHERE user_id = %s AND character_id IN (%s, 'shared')
                   ORDER BY timestamp ASC''',
                (user_id, character_id))
    long_mem = [{'content': r[0], 'category': r[1], 'ts': str(r[2])} for r in cur.fetchall()]

    cur.execute('''SELECT content, timestamp FROM bond_memory
                   WHERE user_id = %s AND character_id = %s AND kind = 'between'
                   ORDER BY timestamp ASC''',
                (user_id, character_id))
    bond_between = [{'content': r[0], 'ts': str(r[1])} for r in cur.fetchall()]

    cur.execute('''SELECT content, timestamp FROM bond_memory
                   WHERE user_id = %s AND character_id = %s AND kind = 'told'
                   ORDER BY timestamp ASC''',
                (user_id, character_id))
    bond_told = [{'content': r[0], 'ts': str(r[1])} for r in cur.fetchall()]

    # 顺便统计一下聊天天数，写进 prompt 里当上下文
    cur.execute('''SELECT COALESCE(total_days, 1) FROM user_stats WHERE user_id = %s''',
                (user_id,))
    row = cur.fetchone()
    total_days = row[0] if row else 1

    cur.close()
    conn.close()

    return {
        'long_memory': long_mem,
        'bond_between': bond_between,
        'bond_told': bond_told,
        'total_days': total_days,
    }


def _build_prompt(history: Dict, user_id: str, character_id: str) -> str:
    parts = []
    parts.append(f'用户 ID：{user_id}')
    parts.append(f'角色 ID：{character_id}')
    parts.append(f'聊天累计天数：{history["total_days"]} 天')
    parts.append('')

    parts.append('【关于用户本人的事实】（角色对她的了解）')
    if history['long_memory']:
        for m in history['long_memory'][:80]:  # 太多的话截断，避免爆 context
            cat = f'[{m["category"]}] ' if m.get('category') else ''
            parts.append(f'- {cat}{m["content"]}')
    else:
        parts.append('（无）')
    parts.append('')

    parts.append('【共同经历/互动/约定】（"我们之间"发生过的事）')
    if history['bond_between']:
        for m in history['bond_between'][:80]:
            parts.append(f'- {m["content"]}')
    else:
        parts.append('（无）')
    parts.append('')

    parts.append('【用户告诉过角色的事】（关于角色本人或其世界，包括剧透）')
    if history['bond_told']:
        for m in history['bond_told'][:40]:
            parts.append(f'- {m["content"]}')
    else:
        parts.append('（无）')
    parts.append('')

    parts.append('请按 schema 输出 JSON，只输出 JSON。')
    return '\n'.join(parts)


def _extract_json(text: str) -> Optional[Dict]:
    text = (text or '').strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def estimate_baseline(user_id: str, character_id: str) -> Optional[Dict]:
    """调 LLM 估算一个基线。返回 estimate dict 或 None（失败）。"""
    history = collect_history(user_id, character_id)

    # 如果历史几乎啥都没有，直接返回 None（skip，不 backfill）
    total_items = (len(history['long_memory'])
                   + len(history['bond_between'])
                   + len(history['bond_told']))
    if total_items < 3:
        print(f'  [skip] {user_id}/{character_id} 历史材料太少（{total_items} 条），跳过 backfill')
        return None

    prompt = _build_prompt(history, user_id, character_id)

    try:
        raw, _usage = create_chat(
            model=MODEL_MAIN,
            messages=[{'role': 'user', 'content': prompt}],
            system=_BACKFILL_SYSTEM_PROMPT,
            max_tokens=800,
        )
    except Exception as e:
        print(f'  [error] LLM 调用失败: {e}')
        return None

    parsed = _extract_json(raw)
    if not parsed:
        print(f'  [error] JSON 解析失败，raw={raw[:200]}')
        return None

    # 校验
    required = ['warmth', 'intimacy', 'trust', 'attachment', 'commitment', 'passion']
    for k in required:
        if k not in parsed:
            print(f'  [error] 缺字段 {k}，raw={raw[:200]}')
            return None
        v = parsed[k]
        if not isinstance(v, (int, float)) or v < 0 or v > 100:
            print(f'  [error] 字段 {k} 值不合法: {v}')
            return None

    return {
        'warmth': float(parsed['warmth']),
        'intimacy': float(parsed['intimacy']),
        'trust': float(parsed['trust']),
        'attachment': float(parsed['attachment']),
        'commitment': float(parsed['commitment']),
        'passion': float(parsed['passion']),
        'friction_categories': parsed.get('friction_categories') or {},
        'reasoning': parsed.get('reasoning', ''),
        'history_summary': {
            'long_memory_count': len(history['long_memory']),
            'bond_between_count': len(history['bond_between']),
            'bond_told_count': len(history['bond_told']),
            'total_days': history['total_days'],
        },
    }


def apply_baseline(user_id: str, character_id: str, est: Dict):
    """把估算结果写入 rel_state。每个非零维度写一条 provenance。
    只走 apply_* 系列 API，不直接 UPDATE 表——保持 provenance 完整。
    """
    ensure_state_row(user_id, character_id)
    ref = f'backfill:{est["history_summary"]["long_memory_count"]}lm+{est["history_summary"]["bond_between_count"]}bond+{est["history_summary"]["bond_told_count"]}told'
    note = f'v4上线后基线估算（{est["history_summary"]["total_days"]}天）：{est.get("reasoning", "")}'

    for field, apply_fn in [
        ('warmth', apply_warmth),
        ('intimacy', apply_intimacy),
        ('trust', apply_trust),
        ('attachment', apply_attachment),
        ('commitment', apply_commitment),
        ('passion', apply_passion),
    ]:
        v = est[field]
        if v > 0:
            apply_fn(user_id, character_id, v,
                     signal_type='backfill', confidence='medium',
                     rule='baseline_estimate_v1',
                     evidence_refs=[ref], note=note)

    for cat, v in (est.get('friction_categories') or {}).items():
        if isinstance(v, (int, float)) and v > 0:
            apply_friction(user_id, character_id, cat, float(v),
                           signal_type='backfill', confidence='medium',
                           rule='baseline_estimate_v1',
                           evidence_refs=[ref], note=note)


def is_state_empty(user_id: str, character_id: str) -> bool:
    """判定 rel_state 是否是"全空"状态（可以 backfill）。"""
    state = load_state(user_id, character_id)
    total_f = sum(float(v) for v in (state.get('friction') or {}).values())
    total = (state['warmth'] + state['intimacy'] + state['trust']
             + state['attachment'] + state['commitment'] + state['passion']
             + total_f)
    return total < 1.0


# ══════════════════════════════════════════════════════════════
# 目标发现：找出哪些 (user_id, character_id) 有历史但账本空
# ══════════════════════════════════════════════════════════════
def list_candidates(user_id: Optional[str] = None,
                    character_id: Optional[str] = None) -> List[Tuple[str, str]]:
    """列出所有满足条件的 (user_id, character_id) 对。
    条件：bond_memory 里有过记录 且 rel_state 全空。
    """
    conn = get_conn()
    cur = conn.cursor()

    where = []
    params = []
    if user_id:
        where.append('bm.user_id = %s')
        params.append(user_id)
    if character_id:
        where.append('bm.character_id = %s')
        params.append(character_id)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    cur.execute(f'''SELECT DISTINCT bm.user_id, bm.character_id
                    FROM bond_memory bm
                    {where_sql}
                    ORDER BY bm.user_id, bm.character_id''', params)
    all_pairs = cur.fetchall()
    cur.close()
    conn.close()

    # 过滤只保留 rel_state 空的
    candidates = []
    for uid, cid in all_pairs:
        if is_state_empty(uid, cid):
            candidates.append((uid, cid))
    return candidates


# ══════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════
def process_one(user_id: str, character_id: str, dry_run: bool = False) -> bool:
    """处理一个 (user_id, character_id)。返回是否成功写入。"""
    print(f'\n=== 处理 {user_id} / {character_id} ===')

    if not is_state_empty(user_id, character_id):
        print(f'  [skip] rel_state 已有数据，不覆盖')
        return False

    est = estimate_baseline(user_id, character_id)
    if not est:
        return False

    print(f'  [估算结果] W={est["warmth"]:.0f}  I={est["intimacy"]:.0f}  '
          f'Trust={est["trust"]:.0f}  Attach={est["attachment"]:.0f}  '
          f'C={est["commitment"]:.0f}  P={est["passion"]:.0f}')
    if est.get('friction_categories'):
        print(f'  [摩擦] {est["friction_categories"]}')
    print(f'  [理由] {est.get("reasoning", "")}')
    print(f'  [来源] long_memory:{est["history_summary"]["long_memory_count"]} '
          f'/ bond_between:{est["history_summary"]["bond_between_count"]} '
          f'/ bond_told:{est["history_summary"]["bond_told_count"]}')

    if dry_run:
        print(f'  [dry-run] 未写入数据库')
        return False

    apply_baseline(user_id, character_id, est)
    print(f'  ✅ 已写入 rel_state 并留 provenance')
    return True


def main():
    parser = argparse.ArgumentParser(description='v4 感情账本一次性 backfill')
    parser.add_argument('--user_id', help='只处理这个 user_id')
    parser.add_argument('--character_id', help='只处理这个 character_id')
    parser.add_argument('--all', action='store_true', help='处理所有满足条件的 (user, character) 对')
    parser.add_argument('--dry-run', action='store_true', help='只打印会做什么，不写数据库')
    args = parser.parse_args()

    if not (args.user_id or args.all):
        print('用法：')
        print('  python3 relationship_backfill.py --dry-run --user_id user_xxx  # 干跑单个')
        print('  python3 relationship_backfill.py --user_id user_xxx           # 单个 user 所有角色')
        print('  python3 relationship_backfill.py --user_id user_xxx --character_id gojo')
        print('  python3 relationship_backfill.py --all                        # 全部（谨慎！）')
        sys.exit(1)

    if args.user_id and args.character_id:
        candidates = [(args.user_id, args.character_id)]
        # 手动指定时不检查是否空——用户可能明确想重跑
        if not is_state_empty(args.user_id, args.character_id):
            print(f'⚠️ {args.user_id}/{args.character_id} 的 rel_state 已有数据。')
            ans = input('   仍然要 backfill 吗？会在原基础上叠加。[y/N] ').strip().lower()
            if ans != 'y':
                print('取消')
                return
    else:
        candidates = list_candidates(user_id=args.user_id, character_id=args.character_id)

    print(f'\n共发现 {len(candidates)} 个待 backfill 的 (user_id, character_id) 对')
    if args.dry_run:
        print('[DRY-RUN 模式：不会写入任何数据]')

    if not candidates:
        print('没有需要处理的对，退出')
        return

    ok = fail = 0
    for uid, cid in candidates:
        try:
            if process_one(uid, cid, dry_run=args.dry_run):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            print(f'  [异常] {e}')
            fail += 1

    print(f'\n=== 完成：成功 {ok} / 跳过或失败 {fail} ===')


if __name__ == '__main__':
    main()
