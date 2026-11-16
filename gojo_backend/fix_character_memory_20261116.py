"""按原句修正 gojo 背景记忆里的四处结论。

手动执行一次。不要在 gojo_server 启动时调用。
seed_all_characters() 只在 character_memory 为空时预置，所以改 memories.py
不会改已经写入的行。

在 gojo_backend 目录下:
    python fix_character_memory_20261116.py
    python fix_character_memory_20261116.py --apply

默认只打印会做什么，不写库。--apply 才在一个事务里写入。
只处理 character_id = 'gojo'，并且只按原内容整句相等来匹配。
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
        'name': 'M1',
        'old': '七海健人曾经是他的学生，现在是同事，关系微妙又信任',
        'new': '七海建人是他高专时期的后辈，现在是同事',
        'keywords': '七海,建人,健人,娜娜明,同事,后辈,nanami',
        'hint': '七海',
    },
    {
        'name': 'M2',
        'old': '夏油杰是他唯一的挚友，曾经是最强的搭档。后来夏油走上了另一条路，这是他心里最深的伤口，不轻易触碰',
        'new': '夏油杰是他唯一的挚友，曾经是最强的搭档。后来夏油走上了另一条路',
        'keywords': '夏油,杰,挚友,朋友,搭档,过去,最好的朋友,bestfriend,伙伴',
        'hint': '夏油',
    },
    {
        'name': 'M3',
        'old': '口头禅是「僕が最強だから」（因为我是最强的），常用来开玩笑也用来掩饰',
        'new': '口头禅是「僕が最強だから」（因为我是最强的）',
        'keywords': '最强,口头禅,自信',
        'hint': '口头禅',
    },
    {
        'name': 'M4',
        'old': '表面轻浮、爱开玩笑、自信到欠揍，内心其实非常孤独——站在最强的高处时没人能并肩',
        'new': None,
        'keywords': None,
        'hint': '表面轻浮',
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
        description='按原句修正 gojo 的四条背景记忆。默认演练，不写库。')
    parser.add_argument('--apply', action='store_true', help='写库。缺省只打印会做什么。')
    args = parser.parse_args(argv)
    run(apply=args.apply)


if __name__ == '__main__':
    main()
