import whisper
import json

model = whisper.load_model("small")

audio_path = r"D:\gojo_audio\htdemucs\[BraveDown.Com] [咒术回战第一季五条悟语音纯享版] [1773467622]\vocals.wav"

print("转录中，大概等3~5分钟...")
result = model.transcribe(
    audio_path,
    language="ja",
    verbose=False
)

print("\n=== 五条悟台词清单 ===\n")
lines = []
for seg in result["segments"]:
    start = seg["start"]
    end = seg["end"]
    text = seg["text"].strip()
    mins_s = int(start // 60)
    secs_s = int(start % 60)
    mins_e = int(end // 60)
    secs_e = int(end % 60)
    line = f"[{mins_s:02d}:{secs_s:02d} → {mins_e:02d}:{secs_e:02d}]  {text}"
    print(line)
    lines.append({
        "start": round(start, 2),
        "end": round(end, 2),
        "text": text,
        "emotion": ""
    })

with open(r"D:\gojo_transcript.json", "w", encoding="utf-8") as f:
    json.dump(lines, f, ensure_ascii=False, indent=2)

print(f"\n共 {len(lines)} 段，已保存到 D:\\gojo_transcript.json")