"""Fish Audio TTS + Groq Whisper STT（verbose_json + 置信度 + 噪声过滤）"""
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


NOISE_WORDS = {
    '谢谢', '感谢', '请', '您好', '你好', '嗯', '啊',
    '哦', '额', '这', '那', '这个', '那个', '什么',
    '不知道', '对', '好的', 'OK', 'ok', '哈哈', '哈',
    '是', '是的', '嗯嗯', '嗯哼',
}


def transcribe_audio_b64(audio_b64: str):
    """Groq Whisper 转录：verbose_json + 置信度 + 噪声过滤"""
    if not GROQ_KEY:
        return {'error': 'GROQ_KEY not configured', 'text': ''}
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_KEY)
        audio_bytes = base64.b64decode(audio_b64)

        # 过滤1：音频太小
        if len(audio_bytes) < 2000:
            print(f'[transcribe] 音频太小({len(audio_bytes)}B)')
            return {'text': '', 'filtered': True, 'reason': 'too_small'}

        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(audio_bytes)
            temp_path = f.name

        try:
            with open(temp_path, 'rb') as f:
                # ★ verbose_json + temperature=0 提高准确度
                transcript = client.audio.transcriptions.create(
                    model='whisper-large-v3-turbo',
                    file=f,
                    language='zh',
                    response_format='verbose_json',
                    temperature=0.0,
                )

            text = transcript.text.strip() if hasattr(transcript, 'text') else ''

            # ★ 获取平均置信度
            avg_confidence = 0
            segments = getattr(transcript, 'segments', [])
            if segments:
                confidences = []
                for seg in segments:
                    if isinstance(seg, dict):
                        confidences.append(seg.get('avg_logprob', -1))
                    elif hasattr(seg, 'avg_logprob'):
                        confidences.append(seg.avg_logprob)
                if confidences:
                    avg_confidence = sum(confidences) / len(confidences)

            print(f'[transcribe] text="{text}" confidence={avg_confidence:.3f}')

            # 过滤2：太短
            if len(text) < 2:
                return {'text': '', 'filtered': True, 'reason': 'too_short'}

            text_clean = text.strip('。.，,！!？?…~ ').strip()

            # 过滤3：噪声词
            if len(text_clean) <= 2 and text_clean in NOISE_WORDS:
                print(f'[transcribe] 噪声词："{text}"')
                return {'text': '', 'filtered': True, 'reason': 'noise_word'}

            # 过滤4：重复字符
            if len(text_clean) > 1 and len(set(text_clean)) <= 2 and len(text_clean) <= 4:
                print(f'[transcribe] 重复噪声："{text}"')
                return {'text': '', 'filtered': True, 'reason': 'repeated'}

            # ★ 过滤5：置信度太低的短句标记
            if avg_confidence < -1.8 and len(text_clean) < 6:
                print(f'[transcribe] 低置信度短句："{text}" conf={avg_confidence:.3f}')
                return {'text': text, 'low_confidence': True}

            return {'text': text}
        finally:
            try: os.unlink(temp_path)
            except: pass
    except Exception as e:
        print(f'转录失败：{e}')
        return {'error': str(e), 'text': ''}