"""角色数据包。每个角色是一个独立的子文件夹（gojo/, geto/, ...）。
要加新角色：
1. 在本文件夹下新建 <角色id>/，里面放 core.py、memories.py、canon_lock.py、lore.json
2. 在 REGISTRY 里登记该角色 id
3. 重启服务，seed 函数会自动跑

不需要动 characters.py / prompt.py / 任何路由文件。
"""

# 已注册的角色 id 列表。新增角色就往这里加。
REGISTRY = [
    'gojo',
    'geto',
]
