"""一次性迁移：把历史长期记忆全部迁入 shared 共享桶。跑一次就删。"""
from db import get_conn

conn = get_conn()
cur = conn.cursor()

# 先看看迁移前的分布
cur.execute("SELECT character_id, COUNT(*) FROM long_memory GROUP BY character_id")
print('迁移前：', cur.fetchall())

cur.execute("UPDATE long_memory SET character_id = 'shared'")
print(f'已迁移 {cur.rowcount} 条记忆')
conn.commit()

cur.execute("SELECT character_id, COUNT(*) FROM long_memory GROUP BY character_id")
print('迁移后：', cur.fetchall())

cur.close()
conn.close()