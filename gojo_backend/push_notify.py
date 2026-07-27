"""推送模块：存手机 push token + 通过 Expo 推送服务发通知到手机。

链路：后端 → Expo Push API → FCM → 用户手机（app 关着也能收到）。
FCM 凭证已在 Expo 后台配好，这里只管调 Expo 的推送接口。

用法：
  - init_push_table()：建表（gojo_server 启动时调）
  - save_token(user_id, token)：前端注册后把 token 发来存下
  - push_to_user(user_id, title, body)：给某用户所有设备推一条通知
"""
import json
import urllib.request
from db import get_conn

EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send'


def init_push_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS push_token (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        token TEXT NOT NULL UNIQUE,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    cur.close()
    conn.close()
    print('[push] 推送 token 表已就绪')


def save_token(user_id, token):
    """存/更新一个设备的 push token（同 token 只留一条，换用户就更新归属）。"""
    if not token:
        return False
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO push_token (user_id, token, updated_at)
           VALUES (%s, %s, CURRENT_TIMESTAMP)
           ON CONFLICT (token) DO UPDATE SET user_id=EXCLUDED.user_id, updated_at=CURRENT_TIMESTAMP''',
        (user_id, token)
    )
    conn.commit()
    cur.close()
    conn.close()
    return True


def get_tokens(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT token FROM push_token WHERE user_id=%s', (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0] for r in rows]


def push_to_user(user_id, title, body, data=None):
    """给某用户的所有设备推一条通知。静默失败（推送不该拖垮主流程）。"""
    tokens = get_tokens(user_id)
    if not tokens:
        print(f'[push] {user_id} 没有已注册的设备，跳过推送')
        return
    for token in tokens:
        payload = {
            'to': token,
            'title': title,
            'body': body,
            'sound': 'default',
            'channelId': 'default',
        }
        if data:
            payload['data'] = data
        try:
            req = urllib.request.Request(
                EXPO_PUSH_URL,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                _ = resp.read()
            print(f'[push] ✅ 已推送给 {user_id}：{title} - {body[:20]}')
        except Exception as e:
            print(f'[push] 推送失败（{token[:20]}...）：{e}')
