"""GojoAssistant 后端入口

★ v8:注册 voice_stream_router —— B档半流式语音通话 /chat/voice_stream
   (老的 /chat/voice_text 保留,前端可以自由切换或做灰度)
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
import memory_search
import group_bubbler
import diary_scheduler
import proactive_scheduler


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
from route_tts import router as tts_router
from route_diary import router as diary_router
from db_diary import init_diary_tables
from route_proactive import router as proactive_router
from proactive_msg import init_proactive_table
from push_notify import init_push_table
from route_accounting import router as accounting_router
from route_stats import router as stats_router
from route_voice_stream import router as voice_stream_router   # ★ B档:流式语音通话


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
init_period_table()
init_diary_tables()
init_proactive_table()
init_push_table()
memory_search.init_vector_support()
seed_gojo_character()
group_bubbler.start_bubbler()
diary_scheduler.start_diary_scheduler()
proactive_scheduler.start_proactive_scheduler()

# ── 注册路由 ──
app.include_router(chat_router)
app.include_router(memory_router)
app.include_router(tasks_router)
app.include_router(character_router)
app.include_router(image_router)
app.include_router(story_router)
app.include_router(group_router)
app.include_router(avatar_router)
app.include_router(config_router)
app.include_router(period_router)
app.include_router(tts_router)
app.include_router(diary_router)
app.include_router(proactive_router)
app.include_router(accounting_router)
app.include_router(stats_router)
app.include_router(voice_stream_router)   # ★ B档流式语音


@app.get('/health')
async def health():
    return {
        'status': 'ok',
        'tts_provider': TTS_PROVIDER,
        'db': 'postgresql',
        'arch': 'modular-v8-voice-stream',
        'vector_ready': memory_search.is_vector_ready(),
    }


if __name__ == '__main__':
    print(f'GojoAssistant starting... TTS: {TTS_PROVIDER} | DB: PostgreSQL | Voice Stream')
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))