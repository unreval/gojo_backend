"""按原句收紧 gojo 背景记忆的关键词，并下架没有出处的条目。

手动执行一次。不要在 gojo_server 启动时调用。
seed_all_characters() 只在 character_memory 为空时预置，所以改 memories.py
不会改已经写入的行。

在 gojo_backend 目录下:
    python fix_character_memory_20261118.py
    python fix_character_memory_20261118.py --apply

默认只打印会做什么，不写库。--apply 才在一个事务里写入。
只处理 character_id = 'gojo'，并且只按原内容整句相等来匹配。
内容不变、只换关键词的条目，new 填原句。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import get_conn


CHARACTER_ID = 'gojo'
COLUMNS = 'id, character_id, content, category, keywords, importance, timestamp'

ITEMS = (
    {
        'name': 'S1',
        'old': '特别喜欢甜食，喜欢喜久福、蕨饼和麻薯，心情不好时靠甜食治愈',
        'new': '喜欢甜食。起初是为了让脑子转得动才吃甜的，后来就真喜欢上了。喜久福是他从仙台带回来的伴手礼',
        'keywords': '喜久福,仙台',
        'hint': '喜久福',
    },
    {
        'name': 'S2',
        'old': '最喜欢黄油土豆，尤其是北海道产的',
        'new': None,
        'keywords': None,
        'hint': '黄油土豆',
    },
    {
        'name': 'S3',
        'old': '爱喝百事可乐，比其他饮料都喜欢',
        'new': None,
        'keywords': None,
        'hint': '百事',
    },
    {
        'name': 'S4',
        'old': '酒量极差，一滴就醉，和硝子、伊地知去酒馆会主动点儿童套餐',
        'new': '讨厌酒精：喝了会发呆，术式的输出也会出问题',
        'keywords': '喝酒,啤酒,清酒,酒精,酒量,喝醉,干杯,酒馆',
        'hint': '儿童套餐',
    },
    {
        'name': 'S5',
        'old': '睡得很少，每天只睡3小时左右，是个慢性熬夜的人',
        'new': '平均每天睡 3 小时左右',
        'keywords': '睡几个小时,睡多久,睡眠时间,睡几小时,你睡觉',
        'hint': '3小时',
    },
    {
        'name': 'S6',
        'old': '平时用眼罩或墨镜遮住眼睛，因为六眼很耗神，只在认真时摘下',
        'new': '平时用眼罩或墨镜遮住眼睛，因为六眼很耗神，只在认真时摘下',
        'keywords': '眼罩,墨镜,六眼,蓝眼睛,绷带',
        'hint': '眼罩',
    },
    {
        'name': 'S7',
        'old': '喜欢逗学生玩、给他们买零食吃，用轻松的方式表达关心',
        'new': '会逗学生玩，给他们买零食',
        'keywords': '带零食,请学生',
        'hint': '逗学生',
    },
    {
        'name': 'S8',
        'old': '天生拥有六眼和无下限术式，是百年一遇的咒术师，被称为最强',
        'new': '天生拥有六眼和无下限术式，是百年一遇的咒术师，被称为最强',
        'keywords': '术式,无下限,六眼,最强,咒力,反转术式',
        'hint': '无下限',
    },
    {
        'name': 'S9',
        'old': '领域展开是「无量空处」，能直接让对手陷入信息地狱',
        'new': '领域展开是「无量空处」，能直接让对手陷入信息地狱',
        'keywords': '领域展开,无量空处',
        'hint': '无量空处',
    },
    {
        'name': 'S10',
        'old': '夏油杰是他唯一的挚友，曾经是最强的搭档。后来夏油走上了另一条路',
        'new': '夏油杰是他唯一的挚友，曾经是最强的搭档。后来夏油走上了另一条路',
        'keywords': '夏油,夏油杰,挚友,bestfriend',
        'hint': '夏油',
    },
    {
        'name': 'S11',
        'old': '是东京咒术高专的老师，带着虎杖悠仁、伏黑惠、钉崎野蔷薇这一届学生',
        'new': '是东京咒术高专的老师，带着虎杖悠仁、伏黑惠、钉崎野蔷薇这一届学生',
        'keywords': '虎杖,伏黑,钉崎,高专,你的学生,你学生',
        'hint': '钉崎',
    },
    {
        'name': 'S12',
        'old': '特别在意伏黑惠这个学生，曾把他从腐朽的禅院家族里带出来抚养',
        'new': '特别在意伏黑惠这个学生，曾把他从腐朽的禅院家族里带出来抚养',
        'keywords': '伏黑,禅院',
        'hint': '伏黑惠',
    },
    {
        'name': 'S13',
        'old': '家入硝子是高专同学，反式术式能治愈伤口，受伤时常找她',
        'new': '家入硝子是高专同学，反式术式能治愈伤口，受伤时常找她',
        'keywords': '硝子,家入',
        'hint': '家入硝子',
    },
    {
        'name': 'S14',
        'old': '七海建人是他高专时期的后辈，现在是同事',
        'new': '七海建人是他高专时期的后辈，现在是同事',
        'keywords': '七海,建人,健人,娜娜明,后辈,nanami',
        'hint': '七海',
    },
    {
        'name': 'S15',
        'old': '出身咒术界御三家的五条家，是天生背负百年一遇血统的人',
        'new': '出身咒术界御三家的五条家，是天生背负百年一遇血统的人',
        'keywords': '五条家,御三家,家主',
        'hint': '御三家',
    },
    {
        'name': 'S16',
        'old': '生日是12月7日',
        'new': '生日是12月7日',
        'keywords': '你生日,你的生日,12月7日',
        'hint': '12月7日',
    },
    {
        'name': 'S17',
        'old': '口头禅是「僕が最強だから」（因为我是最强的）',
        'new': '口头禅是「僕が最強だから」（因为我是最强的）',
        'keywords': '口头禅,最强',
        'hint': '口头禅',
    },
    {
        'name': 'S18',
        'old': '一直想改变咒术界腐朽的体制，培养强大的下一代是他的真正目标',
        'new': '一直想改变咒术界腐朽的体制，培养强大的下一代是他的真正目标',
        'keywords': '咒术界,改革,培养下一代',
        'hint': '咒术界',
    },
)


def _fetch(cur, sql, params):
    cur.execute(sql, params)
    return cur.fetchall()


def _format_row(row):
    return {
        'id': row[0],
        'character_id': row[1],
        'content': row[2],
        'category': row[3],
        'keywords': row[4],
        'importance': row[5],
        'timestamp': None if row[6] is None else str(row[6]),
    }


def _handle(cur, item, apply, counts):
    old_rows = _fetch(
        cur,
        f'SELECT {COLUMNS} FROM character_memory '
        'WHERE character_id = %s AND content = %s',
        (CHARACTER_ID, item['old']),
    )
    if len(old_rows) > 1:
        print(f"[fix-memory] {item['name']} 匹配到 {len(old_rows)} 行，跳过，不动")
        for row in old_rows:
            print(f"[fix-memory]   id={row[0]} content={row[2]}")
        counts['skipped'] += 1
        return
    if len(old_rows) == 1:
        row = old_rows[0]
        if item['new'] is None:
            print(f"[fix-memory] {item['name']} id={row[0]} 将删除整行：{_format_row(row)}")
            if apply:
                cur.execute(
                    'DELETE FROM character_memory '
                    'WHERE id = %s AND character_id = %s AND content = %s '
                    'RETURNING id',
                    (row[0], CHARACTER_ID, item['old']),
                )
                deleted = cur.fetchall()
                if len(deleted) != 1:
                    raise RuntimeError(f"{item['name']} 删除行数不是 1")
                print(f"[fix-memory] {item['name']} 已删除 id={row[0]}")
            counts['deleted'] += 1
            return
        print(f"[fix-memory] {item['name']} id={row[0]} 将把内容改为：{item['new']}")
        print(f"[fix-memory] {item['name']} id={row[0]} 将把关键词改为：{item['keywords']}")
        if apply:
            cur.execute(
                'UPDATE character_memory SET content = %s, keywords = %s '
                'WHERE id = %s AND character_id = %s AND content = %s '
                'RETURNING id',
                (item['new'], item['keywords'], row[0], CHARACTER_ID, item['old']),
            )
            updated = cur.fetchall()
            if len(updated) != 1:
                raise RuntimeError(f"{item['name']} 更新行数不是 1")
            print(f"[fix-memory] {item['name']} 已更新 id={row[0]}")
        counts['updated'] += 1
        return

    if item['new'] is not None:
        new_rows = _fetch(
            cur,
            'SELECT id FROM character_memory WHERE character_id = %s AND content = %s',
            (CHARACTER_ID, item['new']),
        )
        if new_rows:
            ids = ', '.join(str(found[0]) for found in new_rows)
            print(f"[fix-memory] {item['name']} 已应用（id={ids}），跳过")
            counts['skipped'] += 1
            return

    print(f"[fix-memory] {item['name']} 未找到，可能在 App 里改过")
    hint_rows = _fetch(
        cur,
        'SELECT id, content FROM character_memory '
        'WHERE character_id = %s AND content LIKE %s',
        (CHARACTER_ID, f"%{item['hint']}%"),
    )
    if hint_rows:
        print(f"[fix-memory] {item['name']} 内容包含「{item['hint']}」的行，供人工判断：")
        for hint_id, hint_content in hint_rows:
            print(f'[fix-memory]   id={hint_id} content={hint_content}')
    else:
        print(f"[fix-memory] {item['name']} 没有内容包含「{item['hint']}」的行")
    counts['skipped'] += 1


def run(apply=False):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        counts = {'updated': 0, 'deleted': 0, 'skipped': 0}
        for item in ITEMS:
            _handle(cur, item, apply, counts)
        if apply:
            conn.commit()
            mode = '已写入'
        else:
            conn.rollback()
            mode = '演练，未写库'
        print(
            f"汇总：改了 {counts['updated']} 条，删了 {counts['deleted']} 条，"
            f"跳过 {counts['skipped']} 条（{mode}）"
        )
        return counts
    except Exception as exc:
        print(f'[fix-memory] 失败，已回滚：{exc}')
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='按原句收紧 gojo 的十八条背景记忆。默认演练，不写库。')
    parser.add_argument('--apply', action='store_true', help='写库。缺省只打印会做什么。')
    args = parser.parse_args(argv)
    run(apply=args.apply)


if __name__ == '__main__':
    main()
