import json
import os
from pydub import AudioSegment
from collections import defaultdict

with open(r"D:\gojo_transcript.json", "r", encoding="utf-8") as f:
    lines = json.load(f)

audio = AudioSegment.from_wav(
    r"D:\gojo_audio\htdemucs\[BraveDown.Com] [咒术回战第一季五条悟语音纯享版] [1773467622]\vocals.wav"
)

output_dir = r"D:\gojo_emotions"
os.makedirs(output_dir, exist_ok=True)

emotion_segments = defaultdict(list)
for seg in lines:
    if not seg["emotion"]:
        continue
    start_ms = int(seg["start"] * 1000)
    end_ms = int(seg["end"] * 1000)
    clip = audio[start_ms:end_ms]
    emotion_segments[seg["emotion"]].append(clip)

silence = AudioSegment.silent(duration=300)

print("=== 切片统计 ===")
for emotion, clips in emotion_segments.items():
    combined = silence
    for clip in clips:
        combined += clip + silence
    duration = len(combined) / 1000
    out_path = os.path.join(output_dir, f"{emotion}.wav")
    combined.export(out_path, format="wav")
    print(f"  {emotion}：{len(clips)} 段，共 {duration:.1f} 秒 → {out_path}")

print(f"\n完成！文件保存在 D:\\gojo_emotions\\")