"""GojoAssistant 后端入口

★ v8：注册 voice_stream_router —— B档半流式语音通话 /chat/voice_stream
   (老的 /chat/voice_text 保留,前端可以自由切换或做灰度)
★ 角色日程：char_schedule 表 + /schedule 路由 + 日程驱动的主动分享
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
from migrate_two_level_recall import migrate_two_level   # ★ 两级召回迁移
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
from db_promise import init_promise_table
from push_notify import init_push_table
from route_accounting import router as accounting_router
from route_stats import router as stats_router
from route_voice_stream import router as voice_stream_router   # ★ B档：流式语音通话
from route_chatlog import router as chatlog_router              # ★ 聊天记录云端同步
from route_schedule import router as schedule_router            # ★ 角色自己的日程
from db_schedule import init_schedule_table
from db_chatlog import init_chatlog_table
from schedule_share import start_schedule_share                 # ★ 日程驱动的主动分享

# ★ 新模块用 try/except 包住：缺文件不会搞崩整个后端
try:
    from route_grumble import router as grumble_router
    from db_grumble import init_grumble_table
    _HAS_GRUMBLE = True
except ImportError as e:
    _HAS_GRUMBLE = False
    print(f'[init] ⚠️ 便利贴模块未找到({e}),该功能不可用,其他功能不受影响')

try:
    from route_game import router as game_router
    _HAS_GAME = True
except ImportError as e:
    _HAS_GAME = False
    print(f'[init] ⚠️ 游戏模块未找到({e}),该功能不可用,其他功能不受影响')

try:
    from route_explore import router as explore_router
    from db_visited_places import init_visited_places_table
    _HAS_EXPLORE = True
except ImportError as e:
    _HAS_EXPLORE = False
    print(f'[init] ⚠️ 探店地图模块未找到({e}),该功能不可用,其他功能不受影响')


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
migrate_two_level()   # ★ 两级召回：给 long_memory / bond_memory 加评分列
init_period_table()
init_diary_tables()
init_proactive_table()
init_promise_table()   # ★ 承诺表（承诺驱动的主动消息）
init_chatlog_table()   # ★ 聊天记录表（卸载重装/换手机都不丢）
init_schedule_table()  # ★ 角色日程表（忙的时候只已读不回）
init_push_table()
if _HAS_GRUMBLE:
    try:
        init_grumble_table()
    except Exception as e:
        print(f'[init] ⚠️ 便利贴表初始化失败({e}),不影响其他功能')
        _HAS_GRUMBLE = False
if _HAS_EXPLORE:
    try:
        init_visited_places_table()
    except Exception as e:
        print(f'[init] ⚠️ 探店记录表初始化失败({e}),不影响其他功能')
        _HAS_EXPLORE = False
memory_search.init_vector_support()
memory_search.start_auto_backfill()   # ★ 后台自动补 embedding,不用手动跑 /rag/backfill
seed_gojo_character()

# ── 后台常驻线程 ──
group_bubbler.start_bubbler()
diary_scheduler.start_diary_scheduler()
proactive_scheduler.start_proactive_scheduler()
start_schedule_share()   # ★ 他在探店/翘班时,可能顺手发条消息（发不发由他按关系判断）

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
app.include_router(chatlog_router)        # ★ 聊天记录同步
app.include_router(schedule_router)       # ★ 角色日程
if _HAS_GRUMBLE:
    app.include_router(grumble_router)
if _HAS_GAME:
    app.include_router(game_router)
if _HAS_EXPLORE:
    app.include_router(explore_router)


@app.get('/health')
async def health():
    return {
        'status': 'ok',
        'tts_provider': TTS_PROVIDER,
        'db': 'postgresql',
        'arch': 'modular-v8-voice-stream',
        'vector_ready': memory_search.is_vector_ready(),
        'game': _HAS_GAME,
        'grumble': _HAS_GRUMBLE,
        'explore': _HAS_EXPLORE,
    }


if __name__ == '__main__':
    print(f'GojoAssistant starting... TTS: {TTS_PROVIDER} | DB: PostgreSQL | Voice Stream')
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))