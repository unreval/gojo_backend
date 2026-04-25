from pydub import AudioSegment
import os

input_dir = r"D:\gojo_emotions"
output_dir = r"D:\gojo_emotions_90s"
os.makedirs(output_dir, exist_ok=True)

for fname in os.listdir(input_dir):
    if not fname.endswith(".wav"):
        continue
    audio = AudioSegment.from_wav(os.path.join(input_dir, fname))
    trimmed = audio[:90000]  # 裁到90秒
    out_path = os.path.join(output_dir, fname)
    trimmed.export(out_path, format="wav")
    print(f"{fname}：{len(audio)/1000:.1f}s → {len(trimmed)/1000:.1f}s")

print("\n完成！裁剪后文件在 D:\\gojo_emotions_90s\\")