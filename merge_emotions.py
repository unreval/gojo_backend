from pydub import AudioSegment
import os

EMOTIONS_DIR = r"D:\gojo_emotions"
OUTPUT_FILE  = r"D:\gojo_upload.wav"
MAX_MS       = 210 * 1000  # 210秒

files = [f for f in os.listdir(EMOTIONS_DIR) if f.endswith('.wav')]
print(f"找到 {len(files)} 个情感文件")

# 每个情感平均分配时长
per_emotion_ms = MAX_MS // len(files)
print(f"每个情感最多取 {per_emotion_ms/1000:.1f}s")

combined = AudioSegment.empty()
for f in sorted(files):
    path = os.path.join(EMOTIONS_DIR, f)
    audio = AudioSegment.from_wav(path)
    clip = audio[:per_emotion_ms]
    combined += clip
    print(f"  {f}：取 {len(clip)/1000:.1f}s")

# 确保不超过210秒
combined = combined[:MAX_MS]
combined = combined.set_frame_rate(22050).set_channels(1)
combined.export(OUTPUT_FILE, format="wav")
print(f"\n✅ 完成！总时长 {len(combined)/1000:.1f}s")
print(f"📄 保存到 {OUTPUT_FILE}")
