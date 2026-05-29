"""Fish Audio TTS + Groq Whisper STT（带噪声过滤）"""
import base64
import requests
import os
import tempfile
from config import FISH_KEY, FISH_VOICE_ID, GROQ_KEY, EMOTION_TAGS


def fish_tts(text, emotion='平静', voice_id=None):
    tag = EMOTION_TAGS.get(emotion, '')
    prefix = '。 '
    final_text = f'{prefix}{tag} {text}' if tag else f'{prefix}{text}'

    text_len = len(text)
    if text_len < 15:
        chunk_length = 100
    elif text_len < 30:
        chunk_length = 150
    else:
        chunk_length = 200

    response = requests.post(
        'https://api.fish.audio/v1/tts',
        headers={'Authorization': f'Bearer {FISH_KEY}', 'Content-Type': 'application/json'},
        json={
            'text': final_text,
            'reference_id': voice_id or FISH_VOICE_ID,
            'format': 'mp3',
            'latency': 'normal',
            'chunk_length': chunk_length,
            'temperature': 0.5,
            'top_p': 0.7,
            'mp3_bitrate': 128,
            'prosody': {'speed': 1.15, 'volume': 0},
        },
        stream=True
    )
    if response.status_code != 200:
        raise Exception(f'Fish Audio error: {response.status_code}')
    return b''.join(response.iter_content(chunk_size=4096))


def tts_to_b64(text, emotion, voice_id=None):
    try:
        audio_bytes = fish_tts(text, emotion, voice_id)
        return base64.b64encode(audio_bytes).decode()
    except Exception as e:
        print(f'[TTS fail] {text[:30]} | {e}')
        return ''


# ★ 噪声词白名单：Whisper 在噪音情况下经常输出这些短词
NOISE_WORDS = {
    '谢谢', '感谢', '请', '您好', '你好', '嗯', '啊',
    '哦', '额', '这', '那', '这个', '那个', '什么',
    '不知道', '对', '好的', 'OK', 'ok', '哈哈', '哈',
    '是', '是的', '嗯嗯', '嗯哼',
}


def transcribe_audio_b64(audio_b64: str):
    """Groq Whisper 中文转录 + 多层噪声过滤"""
    if not GROQ_KEY:
        return {'error': 'GROQ_KEY not configured', 'text': ''}
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_KEY)
        audio_bytes = base64.b64decode(audio_b64)

        # ★ 过滤1：音频太小直接跳过（噪声片段）
        if len(audio_bytes) < 2000:
            print(f'[transcribe] 音频太小({len(audio_bytes)}B)，疑似噪音')
            return {'text': '', 'filtered': True, 'reason': 'too_small'}

        with tempfile.NamedTemporaryFile(suffix='.m4a', delete=False) as f:
            f.write(audio_bytes)
            temp_path = f.name

        try:
            with open(temp_path, 'rb') as f:
                transcript = client.audio.transcriptions.create(
                    model='whisper-large-v3-turbo',
                    file=f,
                    language='zh',
                    response_format='text',
                )
            text = transcript if isinstance(transcript, str) else transcript.text
            text = text.strip()

            # ★ 过滤2：太短直接丢弃
            if len(text) < 3:
                print(f'[transcribe] 太短丢弃："{text}"')
                return {'text': '', 'filtered': True, 'reason': 'too_short'}

            # ★ 过滤3：清掉标点后的纯文本
            text_clean = text.strip('。.，,！!？?…~ ').strip()

            # ★ 过滤4：清完只剩 1-2 字 + 在噪声词里 = 极可能是噪音误识别
            if len(text_clean) <= 2 and text_clean in NOISE_WORDS:
                print(f'[transcribe] 噪声词误识别："{text}"')
                return {'text': '', 'filtered': True, 'reason': 'noise_word'}

            # ★ 过滤5：仅由 1-2 种字符组成的短重复（如"哦哦哦"）
            if len(text_clean) > 1 and len(set(text_clean)) <= 2 and len(text_clean) <= 4:
                print(f'[transcribe] 重复噪声："{text}"')
                return {'text': '', 'filtered': True, 'reason': 'repeated'}

            print(f'[transcribe] {text}')
            return {'text': text}
        finally:
            try: os.unlink(temp_path)
            except: pass
    except Exception as e:
        print(f'转录失败：{e}')
        return {'error': str(e), 'text': ''}