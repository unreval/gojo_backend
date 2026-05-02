import json
import os
from pydub import AudioSegment
from collections import defaultdict

TRANSCRIPT = r"D:\gojo_game_transcript.json"
AUDIO_DIR  = r"E:\Download"
OUTPUT_DIR = r"D:\gojo_emotions"  # 追加到已有的情感文件里

os.makedirs(OUTPUT_DIR, exist_ok=True)

with open(TRANSCRIPT, "r", encoding="utf-8") as f:
    lines = json.load(f)

# 按文件名分组，避免重复加载同一个音频
from collections import defaultdict
file_segs = defaultdict(list)
for seg in lines:
    if seg.get("emotion"):
        file_segs[seg["file"]].append(seg)

# 收集每个情感的所有片段
emotion_clips = defaultdict(list)
silence = AudioSegment.silent(duration=300)

for filename, segs in file_segs.items():
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(path):
        print(f"⚠️  找不到：{filename}，跳过")
        continue
    print(f"📂 加载：{filename}")
    path = path.replace('.mp3', '.wav')
    audio = AudioSegment.from_wav(path)
    for seg in segs:
        start_ms = int(seg["start"] * 1000)
        end_ms   = int(seg["end"]   * 1000)
        clip = audio[start_ms:end_ms]
        emotion_clips[seg["emotion"]].append(clip)

# 追加到已有的情感 wav 文件
print("\n=== 切片统计 ===")
for emotion, clips in emotion_clips.items():
    out_path = os.path.join(OUTPUT_DIR, f"{emotion}.wav")

    # 如果已有旧文件就追加，没有就新建
    if os.path.exists(out_path):
        existing = AudioSegment.from_wav(out_path)
        combined = existing
        print(f"  {emotion}：已有 {len(existing)/1000:.1f}s，追加 {len(clips)} 段")
    else:
        combined = AudioSegment.silent(duration=300)
        print(f"  {emotion}：新建，共 {len(clips)} 段")

    for clip in clips:
        combined += clip + silence

    combined.export(out_path, format="wav")
    print(f"    → 总时长 {len(combined)/1000:.1f}s  保存到 {out_path}")

print(f"\n✅ 完成！文件保存在 {OUTPUT_DIR}")