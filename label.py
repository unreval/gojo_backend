import json
import os
import subprocess
from collections import Counter

INPUT_FILE = r"D:\gojo_transcript_old.json"
VOCALS_DIR = r"D:\gojo_vocals_old"
EMOTIONS   = ["平静", "自信", "嘲讽", "开心", "激动", "温柔", "认真", "疑惑", "调皮", "悲伤", "愤怒"]


def play_audio(source, start, end):
    """直接从对应的源文件播放对应时间段"""
    wav_path = os.path.join(VOCALS_DIR, source)
    if not os.path.exists(wav_path):
        print(f"  找不到文件：{wav_path}")
        return
    duration = end - start
    cmd = [
        "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
        "-ss", str(start), "-t", str(duration), wav_path
    ]
    try:
        subprocess.run(cmd, timeout=duration + 3)
    except Exception as e:
        print(f"  播放失败: {e}")


with open(INPUT_FILE, "r", encoding="utf-8") as f:
    lines = json.load(f)

total   = len(lines)
labeled = sum(1 for s in lines if s.get("emotion"))
print(f"共 {total} 段，已标注 {labeled} 段，待标注 {total - labeled} 段\n")


def show_menu():
    print("\n情感选项：")
    for i, e in enumerate(EMOTIONS):
        print(f"  {i+1}.{e}", end="   ")
        if (i + 1) % 4 == 0:
            print()
    print()
    print("  r=重新播放  s=跳过  q=保存退出")

show_menu()

i = 0
while i < len(lines):
    seg     = lines[i]
    current = seg.get("emotion", "")
    source  = seg.get("source", "")

    print(f"\n─── [{i+1}/{total}]  {seg['start']}s → {seg['end']}s ───")
    print(f"来源：{source}")
    print(f"台词：{seg['text']}")
    print(f"当前：{current or '未标注'}")

    play_audio(source, seg["start"], seg["end"])

    while True:
        choice = input("选择情感 (1-11 / r重播 / s跳过 / q退出): ").strip().lower()
        if choice == "q":
            with open(INPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(lines, f, ensure_ascii=False, indent=2)
            print(f"已保存！进度：{i+1}/{total}")
            exit()
        elif choice == "r":
            play_audio(source, seg["start"], seg["end"])
        elif choice == "s":
            i += 1
            break
        elif choice.isdigit() and 1 <= int(choice) <= 11:
            seg["emotion"] = EMOTIONS[int(choice) - 1]
            print(f"  标注为：{seg['emotion']}")
            if (i + 1) % 10 == 0:
                with open(INPUT_FILE, "w", encoding="utf-8") as f:
                    json.dump(lines, f, ensure_ascii=False, indent=2)
                print("  自动保存")
            i += 1
            break
        else:
            print("  请输入 1-11、r、s 或 q")

with open(INPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(lines, f, ensure_ascii=False, indent=2)

print("全部标注完成！\n=== 情感分布 ===")
ec = Counter(s["emotion"] for s in lines if s.get("emotion"))
for e, c in sorted(ec.items(), key=lambda x: -x[1]):
    print(f"  {e}：{c} 段")