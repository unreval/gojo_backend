"""日记常驻排程（方案A）：他自己会不定期写日记、不定期偷看你的日记。

仿 group_bubbler 的常驻线程模式：后台每隔一段时间"擲骰"，
命中才行动，所以行为不规律、像真人心情，而不是打卡。

★ 与前端无关：哪怕你没开 App，他半夜也会写、也会偷看——
   这样你早上打开才会看到"他 03:14 偷看了你的日记"。

节奏（克制，省钱）：
  · 写日记：平均每 2-3 天一篇，每天最多 1 篇。
  · 偷看你日记：你有最近新写的日记时，他才可能看；每天最多 1 次。
  · 深夜也允许（偷看尤其适合半夜发生，制造"他惦记着你"的感觉）。

★ 单用户设计：跟你现在架构一致（FIXED_USER_ID 单人自用）。
   要扩展多用户时，把 TARGET_USERS 换成从库里查活跃用户即可。
"""
import threading
import time
import random
from datetime import datetime, timedelta
from config import CN_TZ, DEFAULT_CHARACTER_ID

import db_diary
import diary_engine

# 自用阶段：就你一个人、就 gojo 一个角色写日记。以后要扩展改这里。
TARGET_USER = 'user_mofpiyd7442ia7'
DIARY_CHARACTER = 'gojo'

# 每隔多久醒来擲一次骰（秒）。3 小时醒一次。
TICK_SECONDS = 3 * 3600

# 每次醒来时，"写日记"和"偷看"各自的命中概率。
# 3 小时一次、每次约 0.12 → 一天约 8 次机会 × 0.12 ≈ 平均 1 篇/天上限内、实际因每日上限和随机性落在 2-3 天一篇。
WRITE_CHANCE = 0.12
PEEK_CHANCE  = 0.18   # 偷看比写日记更随性一点

_thread = None
_stop = False


def _now():
    return datetime.now(CN_TZ)


def _today_start():
    n = _now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def _maybe_write_diary():
    """按概率+每日上限，决定他要不要写日记。"""
    # 每天最多 1 篇
    if db_diary.count_char_diaries_since(DIARY_CHARACTER, TARGET_USER, _today_start()) >= 1:
        return
    # 距上一篇太近（<20 小时）就先不写，避免扎堆
    last = db_diary.get_last_char_diary_time(DIARY_CHARACTER, TARGET_USER)
    if last is not None:
        try:
            # last 是 naive UTC，粗略换算
            hours = (datetime.utcnow() - last.replace(tzinfo=None)).total_seconds() / 3600
            if hours < 20:
                return
        except Exception:
            pass
    if random.random() < WRITE_CHANCE:
        diary_engine.generate_char_diary(DIARY_CHARACTER, TARGET_USER)


def _maybe_peek_diary():
    """按概率+每日上限，决定他要不要偷看你的日记。"""
    # 每天最多偷看 1 次
    since_today = _today_start()
    # 用 diary_visit 当天计数
    try:
        from db import get_conn
        conn = get_conn(); cur = conn.cursor()
        cur.execute(
            'SELECT COUNT(*) FROM diary_visit WHERE character_id=%s AND user_id=%s AND visited_at >= %s',
            (DIARY_CHARACTER, TARGET_USER, since_today)
        )
        cnt = cur.fetchone()[0]
        cur.close(); conn.close()
        if cnt >= 1:
            return
    except Exception:
        pass

    if random.random() < PEEK_CHANCE:
        # 偷看的时间戳就用现在（可能正好是凌晨）——制造"半夜偷看"的真实记号
        diary_engine.peek_user_diary(DIARY_CHARACTER, TARGET_USER, visited_at=_now())


def _loop():
    global _stop
    # 启动后先等一小会儿，别和其它初始化抢
    time.sleep(60)
    while not _stop:
        try:
            _maybe_write_diary()
            _maybe_peek_diary()
        except Exception as e:
            print(f'[diary_scheduler] tick 出错：{e}')
        # 睡到下一次；加一点随机抖动，让节奏更不规律
        jitter = random.randint(-1800, 1800)  # ±30 分钟
        time.sleep(max(600, TICK_SECONDS + jitter))


def start_diary_scheduler():
    """在 gojo_server.py 启动时调用一次。"""
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    print('[diary_scheduler] 日记常驻排程已启动（他会自己写日记、偷看你的日记）')


# ══════════════════════════════════════════════════════════
#  开 App 补偿检查（保险）：万一容器休眠漏跑了排程，
#  你一打开 App，前端调 /diary/catch_up，这里补一次机会。
# ══════════════════════════════════════════════════════════

def catch_up(user_id=TARGET_USER, character_id=DIARY_CHARACTER):
    """补偿：如果他今天既没写、也没看，而距上一篇/上一次已经隔了挺久，
       给一次"补写/补看"的机会（各自仍受每日上限约束）。
       返回简单统计，方便前端/调试看。"""
    result = {'wrote': False, 'peeked': False}
    try:
        # 补写：今天还没写，且距上篇 > 30 小时，给一次较高概率
        if db_diary.count_char_diaries_since(character_id, user_id, _today_start()) < 1:
            last = db_diary.get_last_char_diary_time(character_id, user_id)
            far_enough = True
            if last is not None:
                try:
                    hours = (datetime.utcnow() - last.replace(tzinfo=None)).total_seconds() / 3600
                    far_enough = hours >= 30
                except Exception:
                    pass
            if far_enough and random.random() < 0.5:
                if diary_engine.generate_char_diary(character_id, user_id):
                    result['wrote'] = True

        # 补看：今天还没看，给一次机会
        from db import get_conn
        conn = get_conn(); cur = conn.cursor()
        cur.execute(
            'SELECT COUNT(*) FROM diary_visit WHERE character_id=%s AND user_id=%s AND visited_at >= %s',
            (character_id, user_id, _today_start())
        )
        cnt = cur.fetchone()[0]
        cur.close(); conn.close()
        if cnt < 1 and random.random() < 0.5:
            peeked = diary_engine.peek_user_diary(character_id, user_id, visited_at=_now())
            if peeked:
                result['peeked'] = True
    except Exception as e:
        print(f'[diary_scheduler] catch_up 出错：{e}')
    return result