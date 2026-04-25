import json
from collections import defaultdict

with open(r"D:\gojo_transcript.json", "r", encoding="utf-8") as f:
    lines = json.load(f)

index = defaultdict(list)
for seg in lines:
    if seg["emotion"]:
        index[seg["emotion"]].append({
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"]
        })

with open(r"D:\gojo_index.json", "w", encoding="utf-8") as f:
    json.dump(index, f, ensure_ascii=False, indent=2)

print("=== 片段索引 ===")
for emotion, clips in index.items():
    print(f"  {emotion}：{len(clips)} 个片段")