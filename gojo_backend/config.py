"""环境变量、常量"""
import os
from datetime import timezone, timedelta

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
