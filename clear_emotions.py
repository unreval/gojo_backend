import json

with open(r"D:\gojo_game_transcript.json", encoding="utf-8") as f:
    data = json.load(f)

for s in data:
    s["emotion"] = ""

with open(r"D:\gojo_game_transcript.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"✅ 已清空所有情感标注，共 {len(data)} 段")