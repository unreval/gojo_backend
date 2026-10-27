"""环境变量、常量"""
import os
from datetime import timezone, timedelta
# ═══════════════════════════════════════
#  模型分配(可通过环境变量覆盖)
# ═══════════════════════════════════════
# MODEL_MAIN: 角色扮演主体(chat/voice/story/group/image)—— Anthropic 家族,保留 prompt cache
MODEL_MAIN = os.getenv('MODEL_MAIN', 'claude-opus-4-6')

# MODEL_JP_AUX: 日语辅助任务(流式语音/提醒/scheduler 反应/主动消息)—— Haiku 便宜快
MODEL_JP_AUX = os.getenv('MODEL_JP_AUX', 'claude-haiku-4-5-20251001')

MODEL_MID = os.getenv('MODEL_MID', 'claude-sonnet-4-6')

# Translation diagnostics do not veto a reply; true restores strict checks.
TRANSLATION_CHECKS_BLOCKING = os.getenv(
    'TRANSLATION_CHECKS_BLOCKING', 'false').strip().lower() in ('1', 'true')

# Schedule claim diagnostics do not veto a reply; true restores strict checks.
# These guards compare the reply's wording against the canonical world. They
# carry no authority: commit_schedule_transition is the only path to schedule
# facts and revalidates in its own transaction, so a lexical miss must not cost
# the user a reply. Intent preflight stays blocking and is not covered here.
SCHEDULE_CLAIM_CHECKS_BLOCKING = os.getenv(
    'SCHEDULE_CLAIM_CHECKS_BLOCKING', 'false').strip().lower() in ('1', 'true')

# 候选产生独立于聊天；默认关闭，启用后也默认只记录影子结果。
CANDIDATE_PRODUCER_ENABLED = os.getenv(
    'CANDIDATE_PRODUCER_ENABLED', 'false').strip().lower() in ('1', 'true')
CANDIDATE_PRODUCER_SHADOW = os.getenv(
    'CANDIDATE_PRODUCER_SHADOW', 'true').strip().lower() not in ('0', 'false')

# MODEL_CN_AUX: 中文辅助任务(记忆提取/日记生成/纠错)—— DeepSeek 便宜、中文好
MODEL_CN_AUX = os.getenv('MODEL_CN_AUX', 'deepseek-v4-flash')

# DeepSeek 配置(MODEL_CN_AUX 走 DS 时使用)
DEEPSEEK_KEY = os.getenv('DEEPSEEK_KEY', '')
DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')

# 对指定的临时 HTTP 故障及连接建立失败做有界重试；5xx 不保证上游未生成/计费。
# PROVIDER_TRANSPORT_ATTEMPTS 是每个语义候选的外层总尝试次数（1 = 关闭外层重试）。
# 普通路径的 HTTP 次数上限为 attempts * (1 + SDK max_retries)；
# 默认刻意保持 2，与当前 SDK max_retries=1 合计最多 4 次，schema 回退另计。
PROVIDER_TRANSPORT_ATTEMPTS = max(1, min(4, int(
    os.getenv('PROVIDER_TRANSPORT_ATTEMPTS', '2'))))
PROVIDER_TRANSPORT_BACKOFF_SECONDS = max(0.0, min(5.0, float(
    os.getenv('PROVIDER_TRANSPORT_BACKOFF_SECONDS', '0.4'))))

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
