"""route_period.py —— 生理期记录 + 预测

在 gojo_server.py 里挂载（两处各加一行）：
    from route_period import router as period_router, init_period_table
    init_period_table()          # 放在其他 init_xxx() 旁边
    app.include_router(period_router)   # 放在其他 include_router 旁边

端点：
    POST   /period/record            记录一次经期 {user_id, start_date, end_date?}
    GET    /period/records           历史记录列表
    DELETE /period/record/{id}       删除一条（记错了用）
    GET    /period/status            ★ 预测：下次日期/还有几天/当前阶段/平均周期

预测算法：取最近最多 6 个周期（相邻两次开始日的间隔）的平均值，
夹在 20~45 天之间防脏数据；不足 2 条记录时用默认 28 天。
经期长度取历史平均（没记结束日就按默认 5 天）。
"""
from datetime import datetime, date, timedelta
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from config import CN_TZ
from db import get_conn

router = APIRouter()

DEFAULT_CYCLE = 28   # 没有足够历史时的默认周期
DEFAULT_LEN   = 5    # 默认经期长度（天）


def init_period_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS period_records (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        start_date DATE NOT NULL,
        end_date DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_period_uid
                   ON period_records (user_id, start_date DESC)''')
    conn.commit()
    cur.close()
    conn.close()
    print('[init] 生理期记录表已就绪：period_records')


def _today() -> date:
    return datetime.now(CN_TZ).date()


def _get_records(user_id, limit=12):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, start_date, end_date FROM period_records
           WHERE user_id = %s ORDER BY start_date DESC LIMIT %s''',
        (user_id, limit)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows  # 新→旧


def _compute(user_id):
    """核心计算：返回 dict 或 None（没有任何记录时）。"""
    rows = _get_records(user_id)
    if not rows:
        return None

    starts = [r[1] for r in rows]           # 新→旧
    # 平均周期：相邻开始日间隔，取最近最多 6 个，夹在 20~45
    cycles = []
    for i in range(len(starts) - 1):
        d = (starts[i] - starts[i + 1]).days
        if 15 <= d <= 60:                    # 离谱间隔（漏记/重复记）不参与平均
            cycles.append(d)
    cycles = cycles[:6]
    avg_cycle = round(sum(cycles) / len(cycles)) if cycles else DEFAULT_CYCLE
    avg_cycle = max(20, min(45, avg_cycle))

    # 平均经期长度
    lens = [(r[2] - r[1]).days + 1 for r in rows if r[2]]
    avg_len = round(sum(lens) / len(lens)) if lens else DEFAULT_LEN
    avg_len = max(2, min(10, avg_len))

    last_start = starts[0]
    last_end = rows[0][2]
    today = _today()

    next_predicted = last_start + timedelta(days=avg_cycle)
    # 预测日已过（这个月还没记）→ 往后滚到未来最近的一次
    while next_predicted < today - timedelta(days=3):
        next_predicted += timedelta(days=avg_cycle)
    days_until = (next_predicted - today).days

    # 当前阶段
    day_in_period = (today - last_start).days + 1
    in_period_len = ((last_end - last_start).days + 1) if last_end else avg_len
    if 1 <= day_in_period <= in_period_len and (last_end is None or today <= last_end):
        phase = f'经期第{day_in_period}天'
        phase_key = 'period'
    elif 0 <= days_until <= 3:
        phase = f'临近经期（预计{days_until}天后）'
        phase_key = 'pms'
    elif 4 <= days_until <= 7:
        phase = f'经期前一周（预计{days_until}天后）'
        phase_key = 'pre'
    else:
        phase = f'安全期（距下次约{days_until}天）'
        phase_key = 'normal'

    return {
        'last_start': str(last_start),
        'last_end': str(last_end) if last_end else None,
        'avg_cycle': avg_cycle,
        'avg_length': avg_len,
        'next_predicted': str(next_predicted),
        'days_until': days_until,
        'phase': phase,
        'phase_key': phase_key,
        'records_count': len(rows),
    }


# ────────── ★ 给 prompt.py 用：角色的"贴心情报" ──────────

def get_period_context(user_id) -> str:
    """返回注入 system prompt 的文本；无记录或处于普通日子时返回空串（不打扰）。"""
    try:
        info = _compute(user_id)
    except Exception as e:
        print(f'[period] 计算失败：{e}')
        return ''
    if not info:
        return ''
    key = info['phase_key']
    if key == 'normal':
        return ''   # 平常日子不注入，省 token 也避免角色没事提起
    if key == 'period':
        situ = f"她现在正处于生理期（{info['phase']}），可能不太舒服。"
    elif key == 'pms':
        situ = f"她的生理期预计这{max(info['days_until'],1)}天内就到了，情绪和身体可能开始不适。"
    else:
        situ = f"她的生理期预计 {info['days_until']} 天后（{info['next_predicted']} 前后）到来。"
    return f'''

【她的生理周期——只有你默默记着的贴心情报】
{situ}
分寸要求：
1. 不要主动直白地点破"我知道你姨妈来了/快来了"，除非她自己先提。
2. 用行动体现记在心里：语气自然放软一点、提醒喝热水别碰冰的、劝她早点休息、
   她烦躁时多包容少抬杠——像一个把这件事记在心里的人，而不是一个播报系统。
3. 她主动说不舒服时，你可以自然接住并照顾，这时不用装作不知道。'''


# ────────── 端点 ──────────

@router.post('/period/record')
async def add_record(data: dict):
    user_id = data.get('user_id', 'default')
    start_s = (data.get('start_date') or '').strip()
    end_s   = (data.get('end_date') or '').strip()
    if not start_s:
        return JSONResponse({'error': 'start_date 必填（YYYY-MM-DD）'}, status_code=400)
    try:
        start_d = datetime.strptime(start_s, '%Y-%m-%d').date()
        end_d = datetime.strptime(end_s, '%Y-%m-%d').date() if end_s else None
    except ValueError:
        return JSONResponse({'error': '日期格式要 YYYY-MM-DD'}, status_code=400)

    conn = get_conn()
    cur = conn.cursor()
    # 同一开始日只留一条（重复提交视为更新结束日）
    cur.execute('SELECT id FROM period_records WHERE user_id = %s AND start_date = %s',
                (user_id, start_d))
    row = cur.fetchone()
    if row:
        cur.execute('UPDATE period_records SET end_date = %s WHERE id = %s', (end_d, row[0]))
        rid = row[0]
    else:
        cur.execute(
            'INSERT INTO period_records (user_id, start_date, end_date) VALUES (%s, %s, %s) RETURNING id',
            (user_id, start_d, end_d))
        rid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True, 'id': rid})


@router.get('/period/records')
async def list_records(user_id: str = 'default'):
    rows = _get_records(user_id, limit=24)
    return JSONResponse({'records': [{
        'id': r[0], 'start_date': str(r[1]),
        'end_date': str(r[2]) if r[2] else None,
    } for r in rows]})


@router.delete('/period/record/{record_id}')
async def delete_record(record_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM period_records WHERE id = %s', (record_id,))
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True})


@router.get('/period/status')
async def period_status(user_id: str = 'default'):
    info = _compute(user_id)
    if not info:
        return JSONResponse({'has_data': False,
                             'hint': '还没有记录，先记一次开始日期，两个周期后预测就准了'})
    return JSONResponse({'has_data': True, **info})