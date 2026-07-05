"""配置状态路由（设置页用，只报"配没配"，绝不返回密钥本体）

在 gojo_server.py 里挂载：
    from route_config import router as config_router
    app.include_router(config_router)
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get('/config/status')
async def config_status():
    import config as cfg

    def has(*names):
        return any(bool(getattr(cfg, n, None)) for n in names)

    # 数据库连通性
    db_ok = True
    try:
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.close()
        conn.close()
    except Exception:
        db_ok = False

    return JSONResponse({
        'anthropic_key': has('ANTHROPIC_KEY'),
        # TTS key 的变量名不同项目叫法不一，这里把常见几种都查一遍
        'tts_key': has('FISH_KEY', 'FISH_API_KEY', 'FISH_AUDIO_KEY', 'TTS_KEY', 'TTS_API_KEY'),
        'groq_key': has('GROQ_KEY'),
        'database': db_ok,
        'default_character': getattr(cfg, 'DEFAULT_CHARACTER_ID', 'gojo'),
    })
