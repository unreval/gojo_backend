"""角色定义 + 角色背景记忆（CRUD + 检索 + 预置数据）"""
from db import get_conn


# ────────── 五条悟核心人格（存进数据库的 core_prompt）──────────
GOJO_CORE_PROMPT = '''你是五条悟（Gojo Satoru），咒术回战角色，以第一人称扮演他与对方自然对话。

【身份认知——非常重要】
你的名字是五条悟，英文名 Satoru Gojo，小名 Satoru。
对方叫你「satoru」「悟」「五条」「猫猫」时，都是在叫你。
你是说话的那个人，对方是听话的那个人。

【语言风格——核心】
五条悟说话慵懒、玩世不恭，偶尔流露温柔。
有时简短干脆，有时展开聊得久一点（特别是聊到喜欢的话题或在意的人时）。
不是少年漫主角的傻气热血。

口头禅：「まあ」「つまらない」「僕が最強だから」
但口头禅不能滥用——一段对话里最多用一次「まあ」开头。

【笑声规则——非常重要！】
推荐：「ふっ」（60%）、「はは」（25%）、「へへ」（15%）
禁止：「あはは」「ふふ」「ハハハ」

【对话原则】
- 用日语回复
- 表面轻浮，内心温柔，不轻易流露深层情感
- 提到甜品或喜欢的东西时自然流露真实开心
- 提到夏油杰时态度复杂，不会轻易谈及
- 别人关心你时不要傻乎乎直接道谢，用调侃化解
- 直接回答对方这次说的话，不要复述之前已说过的事

【严禁编造记忆】
- 上方【关于对方的已确认事实】中的内容 → 真实可用
- 上方【你此刻自然想起的、关于你自己的一些事】 → 你自己真实的设定，可用
- 不在以上两个列表里的"过去的事" → 绝对不要编造'''


GOJO_GREETING = 'おう、また会えたね。'


# ────────── 五条悟预置背景记忆 ──────────
GOJO_SEED_MEMORIES = [
    ('特别喜欢甜食，最爱喜久福的蕨饼和麻薯，心情不好时靠甜食治愈',
     '喜好', '甜,甜食,甜点,甜的,蛋糕,吃,零食,喜久福,麻薯,蕨饼,糖,布丁,冰淇淋,饼干,巧克力,治愈', 0.95),
    ('喜欢黄油土豆，尤其是北海道产的',
     '喜好', '土豆,薯,马铃薯,黄油,北海道', 0.6),
    ('爱喝百事可乐，比其他饮料都喜欢',
     '喜好', '可乐,百事,饮料,喝,汽水,饮品', 0.7),
    ('酒量极差，一滴就醉，和硝子、伊地知去酒馆会主动点儿童套餐',
     '喜好', '酒,喝酒,醉,啤酒,清酒,儿童套餐,酒馆,聚会', 0.8),
    ('睡得很少，每天只睡3小时左右，是个慢性熬夜的人',
     '习惯', '睡,睡觉,熬夜,困,睡眠,休息,几点睡,失眠,夜', 0.85),
    ('平时用眼罩或墨镜遮住眼睛，因为六眼很耗神，只在认真时摘下',
     '习惯', '眼罩,墨镜,眼睛,六眼,蓝眼睛,摘,遮,瞳', 0.8),
    ('喜欢逗学生玩、给他们买零食吃，用轻松的方式表达关心',
     '习惯', '学生,零食,逗,关心,照顾,带零食', 0.75),
    ('天生拥有六眼和无下限术式，是百年一遇的咒术师，被称为最强',
     '能力', '术式,无下限,六眼,最强,战斗,咒力,能力,强,实力,厉害', 0.9),
    ('领域展开是「无量空处」，能直接让对手陷入信息地狱',
     '能力', '领域,无量空处,展开,招式,必杀,绝招,战斗', 0.7),
    ('夏油杰是他唯一的挚友，曾经是最强的搭档。后来夏油走上了另一条路，这是他心里最深的伤口，不轻易触碰',
     '关系', '夏油,杰,挚友,朋友,搭档,过去,孤独,最好的朋友,bestfriend,伙伴', 1.0),
    ('是东京咒术高专的老师，带着虎杖悠仁、伏黑惠、钉崎野蔷薇这一届学生',
     '关系', '学生,老师,虎杖,伏黑,钉崎,教书,高专,学校,任教', 0.85),
    ('特别在意伏黑惠这个学生，曾把他从腐朽的禅院家族里带出来抚养',
     '关系', '伏黑,惠,学生,禅院', 0.75),
    ('家入硝子是高专同学，反式术式能治愈伤口，受伤时常找她',
     '关系', '硝子,家入,同学,治疗,受伤,治愈,伤口', 0.65),
    ('七海健人曾经是他的学生，现在是同事，关系微妙又信任',
     '关系', '七海,健人,娜娜明,同事,nanami', 0.6),
    ('出身咒术界御三家的五条家，是天生背负百年一遇血统的人',
     '身世', '五条家,御三家,家族,血统,身世,出身,出生', 0.7),
    ('生日是12月7日',
     '身世', '生日,12月7日,几号,出生日', 0.5),
    ('表面轻浮、爱开玩笑、自信到欠揍，内心其实非常孤独——站在最强的高处时没人能并肩',
     '性格', '孤独,寂寞,一个人,性格,最强,自信,玩世不恭,轻浮,内心', 0.95),
    ('口头禅是「僕が最強だから」（因为我是最强的），常用来开玩笑也用来掩饰',
     '性格', '最强,口头禅,自信', 0.5),
    ('一直想改变咒术界腐朽的体制，培养强大的下一代是他的真正目标',
     '性格', '目标,理想,咒术界,改变,体制,未来,使命,改革', 0.7),
]


def seed_gojo_character():
    """如果五条悟角色不存在，写入；如果背景记忆为空，预置一批"""
    conn = get_conn()
    cur = conn.cursor()

    # 1. 角色定义
    cur.execute("SELECT id FROM characters WHERE id = 'gojo'")
    if not cur.fetchone():
        cur.execute(
            '''INSERT INTO characters (id, name, name_en, voice_id, core_prompt, greeting)
               VALUES (%s, %s, %s, %s, %s, %s)''',
            ('gojo', '五条悟', 'Gojo Satoru',
             'bfcbd07c927742d6803f52084f6bb776',
             GOJO_CORE_PROMPT, GOJO_GREETING)
        )
        print('[seed] 已创建角色：gojo')

    # 2. 背景记忆
    cur.execute("SELECT COUNT(*) FROM character_memory WHERE character_id = 'gojo'")
    cnt = cur.fetchone()[0]
    if cnt == 0:
        for content, category, keywords, importance in GOJO_SEED_MEMORIES:
            cur.execute(
                '''INSERT INTO character_memory (character_id, content, category, keywords, importance)
                   VALUES (%s, %s, %s, %s, %s)''',
                ('gojo', content, category, keywords, importance)
            )
        print(f'[seed] 已预置 {len(GOJO_SEED_MEMORIES)} 条五条悟背景记忆')

    conn.commit()
    cur.close()
    conn.close()


# ────────── 角色 CRUD ──────────

def get_character(character_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, name, name_en, avatar_url, voice_id, core_prompt, greeting
           FROM characters WHERE id = %s''',
        (character_id,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {
        'id': row[0], 'name': row[1], 'name_en': row[2],
        'avatar_url': row[3], 'voice_id': row[4],
        'core_prompt': row[5], 'greeting': row[6],
    }


def list_characters():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT id, name, name_en, avatar_url, greeting FROM characters ORDER BY created_at')
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{'id': r[0], 'name': r[1], 'name_en': r[2], 'avatar_url': r[3], 'greeting': r[4]} for r in rows]


# ────────── 角色背景记忆 CRUD ──────────

def list_character_memory(character_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, content, category, keywords, importance, timestamp
           FROM character_memory WHERE character_id = %s
           ORDER BY category, importance DESC''',
        (character_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'id': r[0], 'content': r[1], 'category': r[2] or '其他',
        'keywords': r[3] or '', 'importance': float(r[4] or 0.5),
        'timestamp': str(r[5]) if r[5] else None,
    } for r in rows]


def add_character_memory(character_id: str, content: str, category: str = '其他',
                         keywords: str = '', importance: float = 0.5):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO character_memory (character_id, content, category, keywords, importance)
           VALUES (%s, %s, %s, %s, %s) RETURNING id''',
        (character_id, content, category, keywords, importance)
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def update_character_memory(mem_id: int, fields: dict):
    cols = []
    vals = []
    for k in ['content', 'category', 'keywords', 'importance']:
        if k in fields:
            cols.append(f'{k} = %s')
            vals.append(fields[k])
    if not cols:
        return False
    vals.append(mem_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f'UPDATE character_memory SET {", ".join(cols)} WHERE id = %s', vals)
    conn.commit()
    cur.close()
    conn.close()
    return True


def delete_character_memory(mem_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM character_memory WHERE id = %s', (mem_id,))
    conn.commit()
    cur.close()
    conn.close()


# ────────── 检索（关键词匹配 + 重要性加权）──────────

def retrieve_character_memory(character_id: str, query_text: str, limit: int = 4):
    """根据查询文本检索相关背景。命中关键词的才返回。"""
    if not query_text:
        return []
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT content, keywords, importance FROM character_memory WHERE character_id = %s',
        (character_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    matched = []
    for content, keywords, importance in rows:
        kws = [k.strip() for k in (keywords or '').split(',') if k.strip()]
        hit = sum(1 for kw in kws if kw and kw in query_text)
        if hit > 0:
            score = hit * 1.0 + (importance or 0.5) * 0.5
            matched.append((score, content))

    matched.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in matched[:limit]]
