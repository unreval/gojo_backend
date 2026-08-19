# tts.py
"""TTS 合成 + STT 转录（含繁转简）"""
import os
import json
import base64
import time
import threading
import requests
import tempfile

from config import FISH_KEY, FISH_VOICE_ID, GROQ_KEY


# ═══════════════════════════════════════
#  繁体转简体
# ═══════════════════════════════════════

def traditional_to_simplified(text: str) -> str:
    """繁体中文转简体中文"""
    try:
        from opencc import OpenCC
        cc = OpenCC('t2s')
        return cc.convert(text)
    except ImportError:
        # opencc 未安装时，用简单替换处理常见繁体字
        replacements = {
            '騙': '骗', '說': '说', '話': '话', '過': '过', '後': '后',
            '嗎': '吗', '爲': '为', '這': '这', '裡': '里', '點': '点',
            '問': '问', '時': '时', '書': '书', '開': '开', '關': '关',
            '還': '还', '對': '对', '從': '从', '個': '个', '們': '们',
            '愛': '爱', '來': '来', '認': '认', '識': '识', '記': '记',
            '覺得': '觉得', '應該': '应该', '覺得': '觉得', '為什麼': '为什么',
            '什麼': '什么', '喜歡': '喜欢', '想要': '想要', '不知道': '不知道',
            '沒關係': '没关系', '沒事': '没事', '對不起': '对不起', '謝謝': '谢谢',
            '難道': '难道', '總是': '总是', '一直': '一直', '真的': '真的',
            '腦子': '脑子', '陌生': '陌生', '難過': '难过', '開心': '开心',
        }
        for trad, simp in sorted(replacements.items(), key=lambda x: -len(x[0])):
            text = text.replace(trad, simp)
        return text


# ═══════════════════════════════════════
#  TTS: Fish Audio
# ═══════════════════════════════════════
#
# ★ 声音稳定性参数（解决"偶尔变成陌生声音 / 念出怪声"）：
#   - TTS_TEMPERATURE 越低 → 越贴克隆源、越稳定（但太低会偏平淡）
#   - 0.5 偏高，长句容易漂走；0.4 更稳；还漂就降到 0.3
TTS_TEMPERATURE = 0.4
TTS_TOP_P       = 0.7

# ★ Fish Audio 模型选择(通过 HTTP header 传)
#   常见 model ID(以 Fish 官方文档为准,不同账号可能不同):
#   - speech-1.5 / speech-1.6:老一代,便宜
#   - s1:v2 主力,质量好
#   - s1-mini:s1 的低成本版
#   - s2.1-pro / speech-2.1-pro:新的 pro 档
#
#   ★ 如果你的账号支持的 model ID 是"s2.1-pro"/"speech-2.1-pro"或者别的写法,
#     去 Fish Audio 后台 Playground 里看下具体字符串,替换到这里。
FISH_MODEL = 's2.1-pro'


# ★ 情绪 → Fish Audio 内联标签
#   S2.1 Pro 支持 [angry] 这种方括号标签写在文本里。
#
#   ⚠️ 只放【在 Fish 后台实测确认过】的标签!
#      Fish 不认的标签会被【当成普通文本念出来】(「サーカスティック」混在日语里 = 抽风)。
#      没验证过的一律留空,退回纯 prosody(语速+音量),至少不会念错东西。
#
#   验证方法:Fish 后台 Playground → 模型选 S2.1 Pro →
#            文本写 "[tag] テスト用の文章です。" → 听有没有把标签念出来。
#            没念出来且语气有变化 = 支持,填进下面;否则留空。
FISH_EMOTION_TAG = {
    '平静': '',
    '愤怒': '[angry]',      # ✅ 已实测确认

    # ── 以下待验证,先留空(纯 prosody) ──
    '温柔': '',   # 待测: [gentle] / [tender] / [soft]
    '悲伤': '',   # 待测: [sad]
    '认真': '',   # 待测: [serious]
    '疑惑': '',   # 待测: [confused] / [puzzled]
    '开心': '',   # 待测: [happy] / [delighted]
    '调皮': '',   # 待测: [playful]
    '自信': '',   # 待测: [confident]
    '嘲讽': '',   # 待测: [sarcastic]
    '激动': '',   # 待测: [excited]
}

# 超过这个字数就要在句子之间补一次标签。
# 实测:长句只在开头放一个标签,后半段情绪会衰减回参考声的平淡。
TAG_REPEAT_EVERY = 22


def _inject_emotion_tags(text: str, tag: str) -> str:
    """把情绪标签插进文本,长文本按句子重复插。

    为什么要重复:
      Fish 的情绪标签作用范围有限,一段长文本只在开头放一个,
      念到后半段就衰减回平淡了。实测在句子边界补第二、第三个,
      整段的情绪才能维持住。

    做法:开头放一个,之后每累计约 TAG_REPEAT_EVERY 字、
    且正好在句子边界时,再补一个。
    """
    if not tag or not text:
        return text

    # 按日语/中文句末标点切分,保留标点
    import re as _re
    parts = _re.split(r'(?<=[。！？!?…])', text)
    parts = [p for p in parts if p.strip()]
    if not parts:
        return f'{tag} {text}'

    out = [f'{tag} {parts[0]}']
    acc = len(parts[0])
    for p in parts[1:]:
        if acc >= TAG_REPEAT_EVERY:
            out.append(f' {tag} {p}')     # 攒够长度了,补一个标签
            acc = len(p)
        else:
            out.append(p)
            acc += len(p)
    return ''.join(out)


# ★ 情绪 → 语速 + 音量映射(和标签配合使用,不是替代)
#   speed:  越大越快(1.0 = 慢, 1.15 = 参考声, 1.5 = 快)
#   volume: 相对分贝(0 = 参考声,正数更响,负数更轻)
#
#   ★ v2 修正:愤怒/激动之类高唤醒情绪必须【快+响】,不是【慢+响】——
#     慢+响在人耳里是"严肃/凝重/温柔",不是"愤怒"。
EMOTION_PROSODY = {
    '平静': (1.15, 0),
    '温柔': (1.10, -2),
    '悲伤': (1.10, -4),
    '认真': (1.15, 1),
    '疑惑': (1.15, 0),
    '开心': (1.30, 2),
    '调皮': (1.30, 2),
    '自信': (1.20, 2),
    '嘲讽': (1.25, 2),
    '激动': (1.45, 5),
    '愤怒': (1.30, 6),   # 明显快 + 明显响,和"温柔"形成 30% 语速差 + 8dB 音量差
}


def fish_tts(text: str, emotion: str = '平静', voice_id: str = None) -> bytes:
    # ★ 修:去掉开头的省略号/连续句号,Fish 会把它读成怪声
    #   开头的 "。。。。。。" / "……" / "..." / "・・・" → 静音处理
    #   长句中间的省略号保留(Fish 会自然停顿,不会发怪声)
    import re as _re
    text = _re.sub(r'^\s*[。．\.…・]{2,}\s*', '', text).strip()
    if not text:
        raise Exception('TTS 内容为空(全是省略号)')

    # ★ 情绪标签 + prosody 双管齐下:
    #   标签负责音色/语气的情绪,prosody 负责语速音量,两者叠加效果最明显。
    tag = FISH_EMOTION_TAG.get(emotion, '')
    tagged = _inject_emotion_tags(text, tag)
    final_text = f'。 {tagged}'   # 开头的"。"是暖场,避开首字被切掉

    text_len = len(text)
    if text_len < 15:
        chunk_length = 100
    elif text_len < 30:
        chunk_length = 150
    else:
        chunk_length = 200

    actual_voice_id = voice_id or FISH_VOICE_ID
    speed, volume = EMOTION_PROSODY.get(emotion, (1.15, 0))

    response = requests.post(
        'https://api.fish.audio/v1/tts',
        headers={
            'Authorization': f'Bearer {FISH_KEY}',
            'Content-Type': 'application/json',
            'model': FISH_MODEL,   # ★ 指定用哪个模型
        },
        json={
            'text': final_text,
            'reference_id': actual_voice_id,
            'format': 'mp3',
            'latency': 'normal',
            'chunk_length': chunk_length,
            'temperature': TTS_TEMPERATURE,
            'top_p': TTS_TOP_P,
            'mp3_bitrate': 128,
            'prosody': {
                'speed': speed,
                'volume': volume,
            },
        },
        stream=True,
    )
    if response.status_code != 200:
        raise Exception(f'Fish Audio error: {response.status_code} {response.text[:200]}')
    return b''.join(response.iter_content(chunk_size=4096))


# ★ 同一时刻只放一个 TTS 请求出门 + 撞 429 自动等待重试
#   （Fish 低档套餐并发限制很小，群聊多角色说话时容易撞车）
_TTS_LOCK = threading.Semaphore(1)

def tts_to_b64(text: str, emotion: str, voice_id: str = None) -> str:
    for attempt in range(5):
        try:
            with _TTS_LOCK:
                audio_bytes = fish_tts(text, emotion, voice_id)
            return base64.b64encode(audio_bytes).decode()
        except Exception as e:
            if '429' in str(e) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))   # 等 1.5s / 3s 再试
                continue
            print(f'[TTS fail] {text[:30]} | {e}')
            return ''
    return ''


# ═══════════════════════════════════════
#  STT: Whisper via Groq（带繁转简）
# ═══════════════════════════════════════

def transcribe_audio_b64(audio_b64: str) -> dict:
    """
    接收 base64 音频，用 Groq Whisper 转文字。
    返回: {"text": "..."} 或 {"text": "", "filtered": True, "reason": "..."}
    """
    if not GROQ_KEY:
        print('[transcribe] GROQ_KEY 未配置')
        return {'text': '', 'error': 'GROQ_KEY not configured'}

    try:
        from groq import Groq

        client = Groq(api_key=GROQ_KEY)
        audio_bytes = base64.b64decode(audio_b64)

        if len(audio_bytes) < 2000:
            print(f'[transcribe] 音频太小({len(audio_bytes)}B)，疑似噪音')
            return {'text': '', 'filtered': True, 'reason': 'audio_too_small'}

        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(audio_bytes)
            temp_path = f.name

        try:
            with open(temp_path, 'rb') as f:
                transcript = client.audio.transcriptions.create(
                    model='whisper-large-v3-turbo',
                    file=f,
                    language='zh',
                    response_format='verbose_json',
                    temperature=0.0,
                )

            text = transcript.text.strip() if hasattr(transcript, 'text') else str(transcript).strip()

            # ★ 繁转简
            text = traditional_to_simplified(text)

            # 获取分段置信度
            segments = getattr(transcript, 'segments', [])
            avg_confidence = 0
            if segments:
                logprobs = []
                for seg in segments:
                    if isinstance(seg, dict):
                        lp = seg.get('avg_logprob')
                        if lp is not None:
                            logprobs.append(lp)
                if logprobs:
                    avg_confidence = sum(logprobs) / len(logprobs)

            print(f'[transcribe] text="{text}" confidence={avg_confidence:.3f} segs={len(segments)}')

            # 低置信度标记
            low_confidence = avg_confidence < -1.5 and len(text) < 6

            # 过滤太短的
            if len(text) < 2:
                print(f'[transcribe] 太短，丢弃')
                return {'text': '', 'filtered': True, 'reason': 'too_short'}

            # 噪声常见误识别过滤
            noise_words = {
                '谢谢', '感谢', '请', '您好', '你好', '嗯', '啊',
                '哦', '额', '这', '那', '这个', '那个', '什么',
                '不知道', '对', '好的',
            }
            text_clean = text.strip('。.，,！!？? ')

            if text_clean in noise_words and len(text_clean) <= 2:
                print(f'[transcribe] 疑似噪声："{text}"')
                return {'text': '', 'filtered': True, 'reason': 'noise_word'}

            # 重复字符过滤
            if len(set(text_clean)) <= 2 and len(text_clean) > 3:
                print(f'[transcribe] 疑似重复噪声："{text}"')
                return {'text': '', 'filtered': True, 'reason': 'repetitive'}

            if low_confidence:
                return {'text': text, 'low_confidence': True}

            return {'text': text}

        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass

    except Exception as e:
        print(f'[transcribe] error: {e}')
        return {'text': '', 'error': str(e)}