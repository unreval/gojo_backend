"""记账数据层：账户 + 收支记录 + 转账 + 余额计算
和 tasks.py 平级。
"""
import uuid
from db import get_conn


# ══════════════════════════════════════════════
#  账户 CRUD
# ══════════════════════════════════════════════

def list_accounts(user_id):
    """列所有账户,并为每个账户计算实时余额。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, name, initial_balance, icon, sort_order, created_at
           FROM accounts WHERE user_id = %s
           ORDER BY sort_order ASC, id ASC''',
        (user_id,)
    )
    rows = cur.fetchall()

    # 一次性算出每个账户的净流水
    cur.execute(
        '''SELECT account_id,
                  COALESCE(SUM(CASE WHEN type='in'  THEN amount ELSE 0 END), 0),
                  COALESCE(SUM(CASE WHEN type='out' THEN amount ELSE 0 END), 0)
           FROM accounting_records
           WHERE user_id = %s
           GROUP BY account_id''',
        (user_id,)
    )
    flow = {r[0]: (float(r[1]), float(r[2])) for r in cur.fetchall()}

    cur.close()
    conn.close()

    accounts = []
    for r in rows:
        aid = r[0]
        init = float(r[2] or 0)
        income, expense = flow.get(aid, (0.0, 0.0))
        accounts.append({
            'id': aid,
            'name': r[1],
            'initial_balance': init,
            'icon': r[3] or '💰',
            'sort_order': r[4] or 0,
            'created_at': str(r[5]) if r[5] else None,
            'balance': init + income - expense,   # ★ 实时余额
            'total_income': income,
            'total_expense': expense,
        })
    return accounts


def create_account(user_id, name, initial_balance=0, icon='💰', sort_order=0):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO accounts (user_id, name, initial_balance, icon, sort_order)
           VALUES (%s, %s, %s, %s, %s) RETURNING id''',
        (user_id, name, float(initial_balance), icon, int(sort_order))
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def update_account(account_id, fields):
    cols, vals = [], []
    for k in ['name', 'initial_balance', 'icon', 'sort_order']:
        if k in fields:
            cols.append(f'{k} = %s')
            vals.append(fields[k])
    if not cols:
        return False
    vals.append(account_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f'UPDATE accounts SET {", ".join(cols)} WHERE id = %s', vals)
    conn.commit()
    cur.close()
    conn.close()
    return True


def delete_account(account_id):
    """删账户会级联删掉挂在它下面的所有 records(FK CASCADE)。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM accounts WHERE id = %s', (account_id,))
    conn.commit()
    cur.close()
    conn.close()


def find_account_by_name(user_id, name):
    """按名字模糊匹配一个账户(LLM 返回的 account_hint 可能不完全一致)。"""
    if not name:
        return None
    conn = get_conn()
    cur = conn.cursor()
    # 先精确
    cur.execute('SELECT id FROM accounts WHERE user_id = %s AND name = %s',
                (user_id, name))
    row = cur.fetchone()
    if not row:
        # 再模糊
        cur.execute('SELECT id FROM accounts WHERE user_id = %s AND name ILIKE %s LIMIT 1',
                    (user_id, f'%{name}%'))
        row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


# ══════════════════════════════════════════════
#  收支记录 CRUD
# ══════════════════════════════════════════════

def list_records(user_id, limit=200):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT r.id, r.account_id, a.name, a.icon,
                  r.type, r.category, r.description, r.amount,
                  r.record_date, r.record_time,
                  r.is_transfer, r.transfer_id, r.created_at
           FROM accounting_records r
           LEFT JOIN accounts a ON a.id = r.account_id
           WHERE r.user_id = %s
           ORDER BY r.record_date DESC, r.record_time DESC NULLS LAST, r.id DESC
           LIMIT %s''',
        (user_id, limit)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'id': r[0],
        'account_id': r[1],
        'account_name': r[2],
        'account_icon': r[3] or '💰',
        'type': r[4],
        'category': r[5],
        'desc': r[6],
        'amount': float(r[7]),
        'date': str(r[8]) if r[8] else None,
        'time': r[9],
        'is_transfer': bool(r[10]),
        'transfer_id': r[11],
        'created_at': str(r[12]) if r[12] else None,
    } for r in rows]


def create_record(user_id, account_id, type_, category, desc, amount,
                  record_date, record_time=None,
                  is_transfer=False, transfer_id=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO accounting_records
           (user_id, account_id, type, category, description, amount,
            record_date, record_time, is_transfer, transfer_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id''',
        (user_id, account_id, type_, category, desc, float(amount),
         record_date, record_time, bool(is_transfer), transfer_id)
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def delete_record(record_id):
    """删一条记录。如果是转账,联动删掉配对的另一条。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT transfer_id FROM accounting_records WHERE id = %s', (record_id,))
    row = cur.fetchone()
    if row and row[0]:
        cur.execute('DELETE FROM accounting_records WHERE transfer_id = %s', (row[0],))
    else:
        cur.execute('DELETE FROM accounting_records WHERE id = %s', (record_id,))
    conn.commit()
    cur.close()
    conn.close()


# ══════════════════════════════════════════════
#  转账(两条链接的记录)
# ══════════════════════════════════════════════

def create_transfer(user_id, from_account_id, to_account_id, amount,
                    desc='转账', record_date=None, record_time=None):
    """一次转账 = 两条记录(共享 transfer_id)。"""
    tid = str(uuid.uuid4())
    from_id = create_record(
        user_id, from_account_id, 'out', '转账', desc, amount,
        record_date, record_time, is_transfer=True, transfer_id=tid,
    )
    to_id = create_record(
        user_id, to_account_id, 'in', '转账', desc, amount,
        record_date, record_time, is_transfer=True, transfer_id=tid,
    )
    return {'transfer_id': tid, 'from_record_id': from_id, 'to_record_id': to_id}


# ══════════════════════════════════════════════
#  统计:给 LLM 或 insights 用
# ══════════════════════════════════════════════

def summary_last_n_days(user_id, days=30):
    """
    返回最近 N 天的记账摘要,用来喂给五条悟做短评。
    格式尽量精简,减少 token 开销。
    """
    conn = get_conn()
    cur = conn.cursor()

    # 总收支(不含转账)
    cur.execute(
        '''SELECT
             COALESCE(SUM(CASE WHEN type='in'  THEN amount ELSE 0 END), 0),
             COALESCE(SUM(CASE WHEN type='out' THEN amount ELSE 0 END), 0)
           FROM accounting_records
           WHERE user_id = %s
             AND is_transfer = FALSE
             AND record_date >= CURRENT_DATE - INTERVAL '%s days' ''',
        (user_id, days)
    )
    row = cur.fetchone()
    total_in = float(row[0] or 0)
    total_out = float(row[1] or 0)

    # 分类支出 top 5
    cur.execute(
        '''SELECT category, SUM(amount) as total
           FROM accounting_records
           WHERE user_id = %s
             AND type = 'out'
             AND is_transfer = FALSE
             AND record_date >= CURRENT_DATE - INTERVAL '%s days'
           GROUP BY category
           ORDER BY total DESC
           LIMIT 5''',
        (user_id, days)
    )
    by_category = [{'category': r[0], 'amount': float(r[1])} for r in cur.fetchall()]

    # 各账户当前余额
    cur.execute(
        '''SELECT a.name, a.initial_balance,
                  COALESCE(SUM(CASE WHEN r.type='in'  THEN r.amount ELSE 0 END), 0),
                  COALESCE(SUM(CASE WHEN r.type='out' THEN r.amount ELSE 0 END), 0)
           FROM accounts a
           LEFT JOIN accounting_records r ON r.account_id = a.id
           WHERE a.user_id = %s
           GROUP BY a.id, a.name, a.initial_balance
           ORDER BY a.sort_order ASC, a.id ASC''',
        (user_id,)
    )
    accounts = []
    for r in cur.fetchall():
        init = float(r[1] or 0)
        accounts.append({
            'name': r[0],
            'balance': init + float(r[2]) - float(r[3]),
        })

    # 最近 3 笔支出(给他一个具体的话头)
    cur.execute(
        '''SELECT description, amount, category, record_date
           FROM accounting_records
           WHERE user_id = %s AND type = 'out' AND is_transfer = FALSE
           ORDER BY record_date DESC, id DESC LIMIT 3''',
        (user_id,)
    )
    recent_expenses = [{
        'desc': r[0], 'amount': float(r[1]), 'category': r[2],
        'date': str(r[3]) if r[3] else None,
    } for r in cur.fetchall()]

    cur.close()
    conn.close()

    return {
        'days': days,
        'total_income': total_in,
        'total_expense': total_out,
        'by_category': by_category,
        'accounts': accounts,
        'recent_expenses': recent_expenses,
        'has_data': (total_in + total_out) > 0 or len(accounts) > 0,
    }
