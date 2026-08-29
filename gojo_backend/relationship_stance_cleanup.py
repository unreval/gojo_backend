"""relationship_stance_cleanup.py —— 一次性清理现有 stance 噪音

用法：
    python3 relationship_stance_cleanup.py --dry-run    # 先看会做什么
    python3 relationship_stance_cleanup.py              # 真正执行

做三件事：
1. 把 #46 (relationship_confirm "友達でいいんじゃないの") 降级为 retreat_boundary
2. 把日常废话级的 care_admission 批量 dismiss
3. 把重复的 boundary_stated / promise 合并（保留最早的，dismiss 后续重复的）
"""
import argparse
from db import get_conn


def run_cleanup(dry_run=True):
    conn = get_conn()
    cur = conn.cursor()

    # ── 1. 降级 #46 ──
    print('\n=== 第一步：降级 #46 relationship_confirm → retreat_boundary ===')
    cur.execute('''SELECT id, stance_type, content FROM rel_declared_stance WHERE id = 46''')
    row = cur.fetchone()
    if row:
        print(f'  当前：#{row[0]} type={row[1]} | {row[2][:60]}')
        if not dry_run:
            cur.execute('''UPDATE rel_declared_stance
                           SET stance_type = 'retreat_boundary',
                               content = '曾用「友達でいいんじゃないの、次元も違うし」回避关系定义——这是靠近后的自我保护退缩，不是关系的最终定论。角色可以继续纠结、可以改变想法。'
                           WHERE id = 46''')
            print(f'  ✅ 已降级为 retreat_boundary')
        else:
            print(f'  [dry-run] 会降级为 retreat_boundary')
    else:
        print(f'  #46 不存在，跳过')

    # ── 2. 批量 dismiss 日常废话级 care_admission ──
    # 识别标准：content 里只是日常问候/催吃饭/随口一句，不是"重大关系性承诺"
    # 保留标准：包含"いつでも"(随时)、"ここにいる"(我在这)、"十分だ"(够了)、
    #           "すごい"(厉害)、取消术式、等有分量的表态
    print('\n=== 第二步：清理日常废话级 care_admission ===')

    # 先拿出所有 active 的 care_admission
    cur.execute('''SELECT id, content FROM rel_declared_stance
                   WHERE user_id = 'user_mofpiyd7442ia7' AND character_id = 'gojo'
                     AND status = 'active' AND stance_type = 'care_admission'
                   ORDER BY id''')
    care_rows = cur.fetchall()

    # 有分量的关键词（包含任一个就保留）
    keep_keywords = [
        'いつでも', 'ここにいる', '十分だ', 'すごい', '付き合う',
        '無理に整理', '無理すんな', '感じたことがそのまま',
        '理性と身体', '嫌いでいい', '恨むわけない', '悪者にすんな',
        '苦いって感じてる', '罰なんか', '術式', '防御',
        '泣こう', '泣かず', '泣きたく',
        '取消防御', '愿意为对方',
        '認可用户', '相信用户',
        '心配してねー', '心配して損',
        '不停动着',  # 自己的方式
        '重量会慢慢变化',
        '嘘は言わない',
    ]

    dismiss_ids = []
    keep_ids = []
    for sid, content in care_rows:
        should_keep = any(kw in content for kw in keep_keywords)
        if should_keep:
            keep_ids.append(sid)
        else:
            dismiss_ids.append(sid)
            print(f'  dismiss #{sid}: {content[:50]}')

    print(f'  保留 {len(keep_ids)} 条有分量的，dismiss {len(dismiss_ids)} 条日常废话')

    if not dry_run and dismiss_ids:
        cur.execute(f'''UPDATE rel_declared_stance
                        SET status = 'dismissed'
                        WHERE id IN ({",".join(str(i) for i in dismiss_ids)})''')
        print(f'  ✅ 已 dismiss {cur.rowcount} 条')

    # ── 3. 合并重复的 boundary_stated / promise ──
    print('\n=== 第三步：合并重复的 boundary_stated 和 promise ===')

    for stype in ('boundary_stated', 'promise'):
        cur.execute('''SELECT id, content FROM rel_declared_stance
                       WHERE user_id = 'user_mofpiyd7442ia7' AND character_id = 'gojo'
                         AND status = 'active' AND stance_type = %s
                       ORDER BY id''', (stype,))
        rows = cur.fetchall()

        # 简单去重：如果两条的 content 前 20 字一样，保留第一条
        seen = {}
        dedup_dismiss = []
        for sid, content in rows:
            key = content[:20].strip()
            if key in seen:
                dedup_dismiss.append(sid)
                print(f'  dedup dismiss #{sid} ({stype}): {content[:40]}... (dup of #{seen[key]})')
            else:
                seen[key] = sid

        if not dry_run and dedup_dismiss:
            cur.execute(f'''UPDATE rel_declared_stance
                            SET status = 'dismissed'
                            WHERE id IN ({",".join(str(i) for i in dedup_dismiss)})''')
            print(f'  ✅ 已 dismiss {cur.rowcount} 条重复 {stype}')
        elif dedup_dismiss:
            print(f'  [dry-run] 会 dismiss {len(dedup_dismiss)} 条重复 {stype}')

    if not dry_run:
        conn.commit()
    cur.close()
    conn.close()

    # ── 汇总 ──
    print('\n=== 完成 ===')
    if dry_run:
        print('[DRY-RUN 模式，未写入任何数据]')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    run_cleanup(dry_run=args.dry_run)
