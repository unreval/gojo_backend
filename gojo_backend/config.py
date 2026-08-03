"""环境变量、常量"""
import os
from datetime import timezone, timedelta
# ═══════════════════════════════════════
#  模型分配(可通过环境变量覆盖)
# ═══════════════════════════════════════
# MODEL_MAIN: 角色扮演主体(chat/voice/story/group/image)—— Anthropic 家族,保留 prompt cache
MODEL_MAIN = _os.getenv('MODEL_MAIN', 'claude-opus-4-6')

# MODEL_JP_AUX: 日语辅助任务(流式语音/提醒/scheduler 反应/主动消息)—— Haiku 便宜快
MODEL_JP_AUX = _os.getenv('MODEL_JP_AUX', 'claude-haiku-4-5-20251001')

# MODEL_CN_AUX: 中文辅助任务(记忆提取/日记生成/纠错)—— DeepSeek 便宜、中文好
MODEL_CN_AUX = _os.getenv('MODEL_CN_AUX', 'deepseek-v4-flash')

# DeepSeek 配置(MODEL_CN_AUX 走 DS 时使用)
DEEPSEEK_KEY = _os.getenv('DEEPSEEK_KEY', '')
DEEPSEEK_BASE_URL = _os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_KEY', '')
FISH_KEY      = os.environ.get('FISH_KEY', '')
FISH_VOICE_ID = os.environ.get('FISH_VOICE_ID', 'bfcbd07c927742d6803f52084f6bb776')
GROQ_KEY      = os.environ.get('GROQ_KEY', '')
DATABASE_URL  = os.environ.get('DATABASE_URL', '')
TTS_PROVIDER  = os.environ.get('TTS_PROVIDER', 'fish')

CN_TZ = timezone(timedelta(hours=8))

EMOTION_TAGS = {
    '平静': '(calm)',
    '自信': '(confident)',
    '嘲讽': '(sarcastic, mocking)',
    '开心': '(excited, happy)',
    '激动': '(excited)',
    '温柔': '(gentle, tender)',
    '认真': '(serious)',
    '疑惑': '(puzzled, questioning)',
    '调皮': '(playful, teasing)',
    '悲伤': '(sad)',
    '愤怒': '(angry)',
}
EMOTIONS = list(EMOTION_TAGS.keys())

# 默认角色（前端不传 character_id 时用这个）
DEFAULT_CHARACTER_ID = 'gojo'
