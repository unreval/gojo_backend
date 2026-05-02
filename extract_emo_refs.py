"""
extract_emo_refs.py —— 从 D:\gojo_emotions\ 的合并文件里自动截取情感参考片段
依赖：pip install pydub
用法：直接运行，会从每个情绪音频中段截取 8 秒干净片段
"""

import os
from pydub import AudioSegment

# ─────────────── 配置区 ───────────────
INPUT_DIR  = r"D:\gojo_emotions"      # 合并的情感大文件
OUTPUT_DIR = r"D:\gojo_emo_refs"       # 输出参考片段
CLIP_LEN   = 8 * 1000                  # 每段 8 秒（毫秒）

# 哪个情绪当 speaker（音色参考）—— 选数据最多、最干净的
SPEAKER_EMOTION = "平静"
# ──────────────────────────────────────


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".wav")]
    if not files:
        print(f"❌ {INPUT_DIR} 里没有 wav 文件")
        return

    print(f"找到 {len(files)} 个情感文件\n")

    for fname in files:
        emotion  = fname.replace(".wav", "")
        in_path  = os.path.join(INPUT_DIR, fname)
        out_path = os.path.join(OUTPUT_DIR, fname)

        audio = AudioSegment.from_wav(in_path)
        total_ms = len(audio)

        if total_ms < CLIP_LEN:
            # 文件本身不够 8 秒，全要
            clip = audio
            note = f"（原始 {total_ms/1000:.1f}s，全部使用）"
        else:
            # 从中段截取，跳过前后各 1 秒（避免边缘静音）
            start = max(1000, (total_ms - CLIP_LEN) // 2)
            clip = audio[start : start + CLIP_LEN]
            note = f"（{start/1000:.1f}s 起，8 秒）"

        # 转 24kHz 单声道（IndexTTS2 推荐格式）
        clip = clip.set_frame_rate(24000).set_channels(1)
        clip.export(out_path, format="wav")
        print(f"✓ {emotion}.wav  {note}")

    # 单独制作 speaker.wav —— 用平静里最长一段
    speaker_src = os.path.join(INPUT_DIR, f"{SPEAKER_EMOTION}.wav")
    if os.path.exists(speaker_src):
        audio = AudioSegment.from_wav(speaker_src)
        total_ms = len(audio)
        # 取 10 秒做音色参考
        spk_len = min(10 * 1000, total_ms)
        start = max(1000, (total_ms - spk_len) // 2)
        clip = audio[start : start + spk_len]
        clip = clip.set_frame_rate(24000).set_channels(1)
        clip.export(os.path.join(OUTPUT_DIR, "speaker.wav"), format="wav")
        print(f"\n✓ speaker.wav  （从 {SPEAKER_EMOTION} 中提取 10 秒，作为音色参考）")

    print(f"\n✅ 完成！全部保存在 {OUTPUT_DIR}")
    print(f"   把这个文件夹里的内容上传到云端（图床/Cloudflare R2/七牛云）")
    print(f"   IndexTTS2 需要 URL 调用，不能直接传本地文件")


if __name__ == "__main__":
    main()