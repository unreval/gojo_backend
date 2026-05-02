"""
build_fish_ref.py —— 从 D:\gojo_emotions\ 生成 Fish Audio 克隆用的最佳参考音频
策略：以"中性底色"为主（平静/温柔/自信），少量加调皮特征，避免极端情绪污染
依赖：pip install pydub
"""

import os
from pydub import AudioSegment

# ─────────────── 配置区 ───────────────
INPUT_DIR  = r"D:\gojo_emotions"
OUTPUT     = r"D:\gojo_fish_ref.wav"
TARGET_SEC = 75   # 目标总时长（秒）—— Fish Audio 最佳范围 60-90 秒

# 各情感占比（中性为主，避免极端情绪污染基础调）
WEIGHTS = {
    "平静": 0.30,   # 30% 平静做底
    "温柔": 0.25,   # 25% 温柔
    "自信": 0.15,   # 15% 自信
    "认真": 0.10,   # 10% 认真
    "调皮": 0.10,   # 10% 调皮（加点性格）
    "嘲讽": 0.10,   # 10% 嘲讽
    # 不用 愤怒/悲伤/激动/疑惑/开心 —— 情绪太强
}
# ──────────────────────────────────────


def main():
    silence = AudioSegment.silent(duration=300)  # 段间 0.3 秒静音
    output = AudioSegment.empty()

    print(f"目标时长：{TARGET_SEC} 秒\n")

    for emotion, weight in WEIGHTS.items():
        in_path = os.path.join(INPUT_DIR, f"{emotion}.wav")
        if not os.path.exists(in_path):
            print(f"⚠ 找不到 {emotion}.wav，跳过")
            continue

        audio = AudioSegment.from_wav(in_path)
        target_ms = int(TARGET_SEC * 1000 * weight)

        # 从中段截取（跳过前后 1 秒，避免边缘静音）
        if len(audio) <= target_ms:
            clip = audio
            note = "（全部使用）"
        else:
            start = max(1000, (len(audio) - target_ms) // 2)
            clip = audio[start : start + target_ms]
            note = f"（{start/1000:.1f}s 起，{target_ms/1000:.1f}s）"

        output += clip + silence
        print(f"  {emotion}：{len(clip)/1000:.1f}s  {note}")

    # 标准化采样率给 Fish Audio（44100Hz 或 48000Hz 都可以，Fish 自己会处理）
    output = output.set_frame_rate(44100).set_channels(1)
    output.export(OUTPUT, format="wav")

    total = len(output) / 1000
    print(f"\n✅ 完成！总时长 {total:.1f}s")
    print(f"   保存到：{OUTPUT}")
    print(f"\n📝 接下来：")
    print(f"   1. 上 https://fish.audio/ 登录")
    print(f"   2. 进 My Voices → Create Voice Model（或 Clone Voice）")
    print(f"   3. 上传 {OUTPUT}")
    print(f"   4. 拿到新的 Voice ID，替换到 gojo_server.py 的 FISH_VOICE_ID")


if __name__ == "__main__":
    main()