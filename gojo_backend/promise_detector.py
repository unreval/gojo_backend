"""promise_detector.py —— 检测角色回复里的"承诺",自动写入 proactive_promise

当角色说了"我会主动找你" "答应你了" "到时候提醒你" 这类话,
自动在 proactive_promise 表里创建一条记录,让 proactive_scheduler 真的去执行。

★ 由 route_chat.py 在后台线程调用(和 diary_engine / grumble_engine 一样)
★ 完全 fail-safe:出错静默吞掉,不影响主对话
"""
import re
from datetime import datetime, timedelta
from config import CN_TZ
import db_promise


# 承诺关键词:角色回复的中文翻译(zh)里包含这些就可能是承诺
_PROMISE_PATTERNS = [
    # 主动联系类
    (r'(会|要)(主动|先)?(找你|联系你|发消息|发信息|打给你|给你发)', 'contact'),
    (r'(答应|保证|约定|承诺).{0,10}(找你|联系|发消息|提醒|叫你)', 'contact'),
    (r'(我来|我去)(提醒|叫|喊|催)(你)', 'remind'),
    (r'到时候.{0,6}(说一句|提醒|叫你|告诉你)', 'remind'),
    (r'(不会忘|记着|记住了).{0,8}(提醒|告诉|叫你)', 'remind'),
    # 连续发消息类(像你截图里的那种)
    (r'(连续|一直|不停)(发|发消息|找你|吵你|烦你)', 'spam'),
    (r'(收不到|不回).{0,6}(就|就会|我就)(发|吵|找|烦)', 'spam'),
    (r'轰炸', 'spam'),
    # 时间约定类
    (r'(明天|后天|晚上|早上|下午).{0,6}(找你|叫你|提醒你|说一声)', 'timed'),
    (r'(\d{1,2})(点|時).{0,6}(找你|叫你|提醒|告诉)', 'timed'),
    # 定期类
    (r'每天.{0,6}(找你|提醒|叫你|问你|发)', 'daily'),
    (r'每(早|晚|天早上|天晚上).{0,6}(找你|提醒|叫)', 'daily'),
    # ★ 频率调整类:角色同意多发消息
    (r'(好|行|可以|没问题|答应).{0,6}(多发|多找|经常|随时|常联系)', 'freq_up'),
    (r'(会|要)(多|经常|随时).{0,4}(发|找|联系|消息)', 'freq_up'),
]

# 不是真的承诺:否定句/疑问句/条件句
_NEGATION = ['不会', '才不', '谁要', '怎么可能', '别想', '你想多了', '做梦']


def detect_and_save(character_id, user_id, user_text, reply_zh):
    """检测角色回复里的承诺,有就写入 proactive_promise。

    Args:
        character_id: 角色ID
        user_id: 用户ID
        user_text: 用户说的话(存 origin_text 参考)
        reply_zh: 角色回复的中文翻译

    Returns:
        promise_id 或 None
    """
    try:
        if not reply_zh or len(reply_zh) < 4:
            return None

        # 检查否定:如果是"才不会找你"这种,不算承诺
        for neg in _NEGATION:
            if neg in reply_zh:
                return None

        # 匹配承诺模式
        matched_type = None
        matched_text = None
        for pattern, ptype in _PROMISE_PATTERNS:
            m = re.search(pattern, reply_zh)
            if m:
                matched_type = ptype
                matched_text = m.group(0)
                break

        if not matched_type:
            return None

        # 判断触发方式和时间
        now = datetime.now(CN_TZ)

        if matched_type == 'daily':
            # 每天类:默认每天 09:00 触发
            trigger_kind = 'daily'
            trigger_time = '09:00'
            # 看看有没有具体时间
            time_match = re.search(r'(\d{1,2})(点|時)', reply_zh)
            if time_match:
                h = int(time_match.group(1))
                if 0 <= h <= 23:
                    trigger_time = f'{h:02d}:00'
            context = f'角色答应每天{trigger_time}主动联系。原话:「{matched_text}」'
            return _save_promise(character_id, user_id, trigger_kind,
                                trigger_time=trigger_time,
                                context=context, origin=user_text)

        elif matched_type == 'timed':
            # 有具体时间:提取时间,创建一次性承诺
            trigger_kind = 'once'
            trigger_at = now + timedelta(hours=2)  # 默认 2 小时后

            # 看有没有明天/后天
            if '明天' in reply_zh:
                trigger_at = now + timedelta(days=1)
            elif '后天' in reply_zh:
                trigger_at = now + timedelta(days=2)

            # 看有没有具体小时
            time_match = re.search(r'(\d{1,2})(点|時)', reply_zh)
            if time_match:
                h = int(time_match.group(1))
                if 0 <= h <= 23:
                    trigger_at = trigger_at.replace(hour=h, minute=0)

            context = f'角色答应在特定时间联系。原话:「{matched_text}」'
            return _save_promise(character_id, user_id, trigger_kind,
                                trigger_at=trigger_at,
                                context=context, origin=user_text)

        elif matched_type in ('contact', 'remind', 'spam'):
            # 主动联系/提醒/连续发消息:创建一次性承诺,1-3 小时后触发
            trigger_kind = 'once'
            delay_hours = 1 if matched_type == 'spam' else 2
            trigger_at = now + timedelta(hours=delay_hours)

            type_desc = {
                'contact': '主动找她',
                'remind': '提醒她',
                'spam': '连续发消息',
            }
            context = f'角色答应会{type_desc.get(matched_type, "主动联系")}。原话:「{matched_text}」'
            return _save_promise(character_id, user_id, trigger_kind,
                                trigger_at=trigger_at,
                                context=context, origin=user_text)

        elif matched_type == 'freq_up':
            # ★ 频率调整:角色同意多发消息,调高每日上限
            _adjust_frequency(character_id, user_id, 'up')
            return None

        return None

    except Exception as e:
        print(f'[promise_detect] 出错(不影响主流程): {e}')
        return None


def _save_promise(character_id, user_id, trigger_kind,
                  trigger_at=None, trigger_time=None,
                  context='', origin=''):
    """写入 proactive_promise 表。检查重复:同一 context 不重复创建。"""
    try:
        # 防重复:最近 24 小时内有类似的就不再建
        existing = db_promise.get_active_promises(character_id, user_id)
        for p in existing:
            if p.get('context', '')[:20] == context[:20]:
                print(f'[promise_detect] 跳过重复承诺: {context[:30]}')
                return None

        pid = db_promise.add_promise(
            character_id=character_id,
            user_id=user_id,
            trigger_kind=trigger_kind,
            trigger_at=trigger_at,
            trigger_time=trigger_time,
            context=context,
            origin_text=(origin or '')[:200],
        )
        print(f'[promise_detect] ✅ 检测到承诺并创建 #{pid}: {context[:40]}')
        return pid
    except Exception as e:
        print(f'[promise_detect] 保存失败: {e}')
        return None


def _ensure_app_config(cur):
    """查询前确保表存在。init_db() 也会建，这里再兜一层，避免旧进程没跑过初始化。"""
    cur.execute('''CREATE TABLE IF NOT EXISTS app_config (
        key TEXT PRIMARY KEY,
        value TEXT)''')


def _adjust_frequency(character_id, user_id, direction='up'):
    """调整角色的主动消息每日上限，写入 app_config。
    schedule_share.py 读取这个值覆盖默认的 MAX_PER_DAY。
    """
    try:
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        _ensure_app_config(cur)

        config_key = f'msg_limit_{character_id}_{user_id}'
        cur.execute("SELECT value FROM app_config WHERE key=%s", (config_key,))
        row = cur.fetchone()

        current = int(row[0]) if row else 4  # 默认 4

        if direction == 'up':
            new_val = min(current + 3, 15)  # 每次 +3,上限 15
        else:
            new_val = max(current - 2, 2)   # 每次 -2,下限 2

        cur.execute(
            '''INSERT INTO app_config (key, value) VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value''',
            (config_key, str(new_val)),
        )

        conn.commit()
        cur.close()
        conn.close()

        print(f'[promise_detect] 📈 {character_id} 主动消息上限: {current} → {new_val}')
        return new_val
    except Exception as e:
        print(f'[promise_detect] 调频率失败(用内存兜底): {e}')
        try:
            import schedule_share
            if direction == 'up':
                schedule_share.MAX_PER_DAY = min(schedule_share.MAX_PER_DAY + 3, 15)
            else:
                schedule_share.MAX_PER_DAY = max(schedule_share.MAX_PER_DAY - 2, 2)
            print(f'[promise_detect] 📈 内存调整 MAX_PER_DAY → {schedule_share.MAX_PER_DAY}')
        except Exception:
            pass
        return None


def get_msg_limit(character_id, user_id, default=4):
    """给 schedule_share.py 用:读取这个角色的每日主动消息上限。"""
    try:
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        _ensure_app_config(cur)
        config_key = f'msg_limit_{character_id}_{user_id}'
        cur.execute("SELECT value FROM app_config WHERE key=%s", (config_key,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return int(row[0]) if row else default
    except Exception:
        return default