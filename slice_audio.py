import json
import os
from pydub import AudioSegment
from collections import defaultdict

TRANSCRIPT  = r"D:\gojo_transcript_old.json"
VOCALS_DIR  = r"D:\gojo_vocals_old"
OUTPUT_DIR  = r"D:\gojo_emotions"

os.makedirs(OUTPUT_DIR, exist_ok=True)

with open(TRANSCRIPT, "r", encoding="utf-8") as f:
    lines = json.load(f)

# 缓存已加载的音频，避免同一个文件重复读
audio_cache = {}

def get_audio(source):
    if source not in audio_cache:
        path = os.path.join(VOCALS_DIR, source)
        print(f"  加载音频：{source}")
        audio_cache[source] = AudioSegment.from_wav(path)
    return audio_cache[source]

emotion_segments = defaultdict(list)
skipped = 0

for seg in lines:
    if not seg.get("emotion"):   # 跳过未标注或跳过的段
        skipped += 1
        continue
    source = seg.get("source", "")
    if not source:
        skipped += 1
        continue
    try:
        audio    = get_audio(source)
        start_ms = int(seg["start"] * 1000)
        end_ms   = int(seg["end"]   * 1000)
        clip     = audio[start_ms:end_ms]
        emotion_segments[seg["emotion"]].append(clip)
    except Exception as e:
        print(f"  跳过出错段：{seg.get('text','?')[:20]} | {e}")
        skipped += 1

silence = AudioSegment.silent(duration=300)

print(f"\n=== 切片统计（跳过 {skipped} 段）===")
for emotion, clips in sorted(emotion_segments.items()):
    combined = silence
    for clip in clips:
        combined += clip + silence
    duration = len(combined) / 1000
    out_path = os.path.join(OUTPUT_DIR, f"{emotion}.wav")

    # 追加模式：如果文件已存在就合并，方便多批次导入
    if os.path.exists(out_path):
        existing = AudioSegment.from_wav(out_path)
        combined = existing + combined

    combined.export(out_path, format="wav")
    print(f"  {emotion}：{len(clips)} 段，共 {duration:.1f}s → {out_path}")

print(f"\n✅ 完成！文件保存在 {OUTPUT_DIR}")