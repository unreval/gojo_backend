"""GojoAssistant 后端入口

注意：Zeabur 配置认这个文件名，请保留 gojo_server.py。
所有业务逻辑都在同目录其他文件里。
"""
import os
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import TTS_PROVIDER
from db import init_db, migrate_old_gojo_memory
from db_group import init_group_tables
from characters import seed_gojo_character
from db_bond import init_bond_table


# 路由模块
from route_chat import router as chat_router
from route_memory import router as memory_router
from route_tasks import router as tasks_router
from route_character import router as character_router
from route_image import router as image_router
from route_story import router as story_router
from route_group import router as group_router
from route_avatar import router as avatar_router
from route_config import router as config_router
from route_period import router as period_router, init_period_table


app = FastAPI(title='GojoAssistant Backend')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'], allow_methods=['*'], allow_headers=['*'],
)

# ── 启动初始化 ──
init_db()
migrate_old_gojo_memory()
init_group_tables()
init_bond_table()
init_period_table()          # ★ 生理期建表（之前缺的就是这行）
seed_gojo_character()

# ── 注册路由 ──
app.include_router(chat_router)
app.include_router(memory_router)
app.include_router(tasks_router)
app.include_router(character_router)
app.include_router(image_router)
app.include_router(story_router)
app.include_router(group_router)    # 群聊路由
app.include_router(avatar_router)   # 头像路由
app.include_router(config_router)
app.include_router(period_router)   # ★ 生理期路由（之前缺的另一行）


@app.get('/health')
async def health():
    return {'status': 'ok', 'tts_provider': TTS_PROVIDER, 'db': 'postgresql', 'arch': 'modular-v4-period'}


if __name__ == '__main__':
    print(f'GojoAssistant starting... TTS: {TTS_PROVIDER} | DB: PostgreSQL | Modular + Group + Period')
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))