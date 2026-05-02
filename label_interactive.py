import json
import os
import subprocess

INPUT_FILE = r"D:\gojo_game_transcript.json"

EMOTIONS = ["平静", "自信", "嘲讽", "开心", "激动", "温柔", "认真", "疑惑", "调皮", "悲伤", "愤怒"]

def play_audio(filepath, start, end):
    """用 ffplay 播放指定片段"""
    duration = end - start
    cmd = [
        "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
        "-ss", str(start), "-t", str(duration), filepath
    ]
    try:
        subprocess.run(cmd, timeout=duration + 3)
    except Exception as e:
        print(f"  ⚠️  播放失败: {e}")

def get_audio_path(filename):
    return os.path.join(r"E:\Download", filename)

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    lines = json.load(f)

# 找出还没标注或标注为平静需要确认的段落
total = len(lines)
labeled = sum(1 for s in lines if s.get("emotion") and s["emotion"] != "平静")
print(f"📊 共 {total} 段，已标注非平静 {labeled} 段")
print()

# 显示情感选项
def show_menu():
    print("\n情感选项：")
    for i, e in enumerate(EMOTIONS):
        print(f"  {i+1}. {e}", end="   ")
        if (i+1) % 4 == 0:
            print()
    print()
    print("  r = 重新播放  |  s = 跳过  |  q = 保存退出")

show_menu()

i = 0
while i < len(lines):
    seg = lines[i]
    filepath = get_audio_path(seg["file"])
    current = seg.get("emotion", "")

    print(f"\n─── [{i+1}/{total}] {seg['file']}  {seg['start']}s → {seg['end']}s ───")
    print(f"📝 台词：{seg['text']}")
    print(f"🏷️  当前：{current or '未标注'}")

    # 自动播放
    play_audio(filepath, seg["start"], seg["end"])

    while True:
        choice = input("选择情感 (1-11 / r重播 / s跳过 / q退出): ").strip().lower()

        if choice == 'q':
            with open(INPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(lines, f, ensure_ascii=False, indent=2)
            print(f"\n✅ 已保存！标注进度：{i+1}/{total}")
            exit()

        elif choice == 'r':
            play_audio(filepath, seg["start"], seg["end"])

        elif choice == 's':
            i += 1
            break

        elif choice.isdigit() and 1 <= int(choice) <= 11:
            seg["emotion"] = EMOTIONS[int(choice) - 1]
            print(f"  ✓ 标注为：{seg['emotion']}")
            # 每10段自动保存
            if (i + 1) % 10 == 0:
                with open(INPUT_FILE, "w", encoding="utf-8") as f:
                    json.dump(lines, f, ensure_ascii=False, indent=2)
                print("  💾 自动保存")
            i += 1
            break
        else:
            print("  请输入 1-11、r、s 或 q")

# 最终保存
with open(INPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(lines, f, ensure_ascii=False, indent=2)

from collections import Counter
print(f"\n✅ 全部标注完成！")
print("\n=== 最终情感分布 ===")
emotion_count = Counter(s["emotion"] for s in lines if s.get("emotion"))
for emotion, cnt in sorted(emotion_count.items(), key=lambda x: -x[1]):
    print(f"  {emotion}：{cnt} 段")