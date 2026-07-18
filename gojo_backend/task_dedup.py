"""task_dedup.py —— 日程模糊去重

为什么需要它：
  聊天里说"9点上课，老师点名"，隔天又说"上课点名，9点要到"——
  同一件事换个说法而已，但老的 find_duplicate_task 只认【一模一样的标题】，
  于是同一个时段被塞进三四条几乎相同的提醒（05/30 08:30 那三条就是这么来的）。

判定规则（必须【三条全中】才算重复，宁可漏判不可错杀）：
  1. 同一个用户，且任务还没完成
  2. 同一天 + 同一时刻（date 和 time 都相等，两者都空也算相等）
  3. 标题说的是同一件事：
     · 先把标点和【数字】剥掉（"9点上课"→"点上课"，防止"9点"这种时间词造成误判）
     · 一方包含另一方 → 是
     · 最长公共子串 ≥ 2 个汉字 → 是（"上课点名" ∩ "上课…点名"）

实测：
  ✅「9点上课，老师点名」≈「上课点名，9点要到」
  ✅「9点课要点名，提醒起床」≈「上课点名，9点开始，不能迟到」
  ✅「计科课设2」≈「计科课设」
  ❌「吃药」≠「吃晚饭」   ❌「9点上课」≠「9点交作业」   ❌「买牛奶」≠「交作业」
"""
import re
from db import get_conn

_PUNCT = re.compile(r'[\s，,。.、！!？?；;：:~～\-—_（）()【】\[\]"\'`]')


def _norm(s: str) -> str:
    """剥掉标点和数字，只留下"这件事本身"的字。"""
    s = _PUNCT.sub('', s or '')
    return re.sub(r'\d+', '', s)


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（滚动数组，标题都很短，开销可忽略）。"""
    if not a or not b:
        return 0
    dp = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        prev = 0
        for j in range(1, len(b) + 1):
            cur = dp[j]
            dp[j] = prev + 1 if a[i - 1] == b[j - 1] else 0
            if dp[j] > best:
                best = dp[j]
            prev = cur
    return best


def title_similar(a: str, b: str) -> bool:
    """两个任务标题是不是在说同一件事。"""
    a0, b0 = (a or '').strip(), (b or '').strip()
    if not a0 or not b0:
        return False
    if a0 == b0:
        return True
    na, nb = _norm(a0), _norm(b0)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return _lcs_len(na, nb) >= 2


def find_similar_task(user_id: str, title: str, due_date, due_time):
    """找同一时段、意思相近的【未完成】任务。
    返回 (task_id, notification_id, existing_title)，没有就返回 None。
    已完成的历史任务不参与——那些是过去的事，不该拦住新提醒。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, title, notification_id FROM tasks
           WHERE user_id = %s AND completed = FALSE
             AND COALESCE(due_date, '') = COALESCE(%s, '')
             AND COALESCE(due_time, '') = COALESCE(%s, '')
           ORDER BY id DESC LIMIT 30''',
        (user_id, due_date, due_time)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    for tid, existing_title, notif in rows:
        if title_similar(title, existing_title):
            return (tid, notif, existing_title)
    return None
