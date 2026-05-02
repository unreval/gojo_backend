import json
from collections import Counter

INPUT_FILE  = r"D:\gojo_game_transcript.json"
OUTPUT_FILE = r"D:\gojo_game_transcript.json"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    lines = json.load(f)

rules = [
    ("激动",  ["楽しくなってきた", "楽しい", "燃え", "テンション", "最高"]),
    ("自信",  ["最強", "僕最強", "当たってない", "僕を何だと", "俺最強", "敵わない"]),
    ("嘲讽",  ["つまらない", "弱いもん", "痛い目", "学習しろ", "殺して", "雑魚", "ザコ"]),
    ("认真",  ["領域展開", "無量空処", "順転", "反転", "青", "赤", "死ぬ時は一人", "術式"]),
    ("调皮",  ["マジ", "受ける", "デート", "おはよう", "こんばんは", "ほらほら", "お土産", "散歩", "まあ", "ね〜", "なるほど"]),
    ("疑惑",  ["どういう状況", "何者", "まだ何も", "なんで", "どうして", "え？"]),
    ("温柔",  ["めぐみ", "ゆうじ", "大丈夫", "気にせず", "豊作", "頑張", "ありがとう"]),
    ("悲伤",  ["夏油", "死んで勝った", "死んでも勝つ", "悲しい", "寂しい"]),
    ("开心",  ["良かった", "いやー", "いえーい", "面白", "やった", "嬉し"]),
    ("愤怒",  ["乱暴", "くそ", "ふざけ", "許さない"]),
]

count = 0
for seg in lines:
    if seg.get("emotion"):
        continue
    matched = False
    for emotion, keywords in rules:
        if any(kw in seg["text"] for kw in keywords):
            seg["emotion"] = emotion
            matched = True
            count += 1
            break
    if not matched:
        seg["emotion"] = "平静"
        count += 1

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(lines, f, ensure_ascii=False, indent=2)

print(f"✅ 标注完成，共补充 {count} 段")
print("\n=== 情感分布统计 ===")
emotion_count = Counter(s["emotion"] for s in lines if s.get("emotion"))
for emotion, cnt in sorted(emotion_count.items(), key=lambda x: -x[1]):
    print(f"  {emotion}：{cnt} 段")