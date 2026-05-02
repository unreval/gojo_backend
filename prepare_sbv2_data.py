"""
prepare_sbv2_data.py —— 把已标注的 transcript JSON 转换成 SBV2 训练格式
SBV2 要求：每句一个独立 wav + 一个 esd.list 文件（格式：wav路径|说话人|语言|文本）

依赖：pip install pydub
用法：直接运行，会从三个 JSON + 对应音频源批量生成 SBV2 训练数据
"""

import json
import os
from pydub import AudioSegment

# ─────────────── 配置区 ───────────────
# 三批数据源：(transcript_json, vocals_dir)
DATA_SOURCES = [
    (r"D:\gojo_transcript.json",            r"D:\gojo_vocals"),       # 主数据 343 段
    (r"D:\gojo_transcript_old_backup.json", r"E:\Download\gojosound"), # gojopart1
    (r"D:\gojo_transcript_old.json",        r"D:\gojo_vocals_old"),    # gojo1-10
]

# 输出根目录（SBV2 训练数据格式）
OUTPUT_ROOT = r"D:\gojo_sbv2_data"
SPEAKER     = "gojo"
LANGUAGE    = "JP"   # SBV2 日语标记
# ──────────────────────────────────────


def main():
    # 创建输出文件夹
    wavs_dir = os.path.join(OUTPUT_ROOT, "wavs")
    os.makedirs(wavs_dir, exist_ok=True)

    audio_cache  = {}  # 源音频缓存
    metadata     = []  # 每行：wav文件名|说话人|语言|文本|情感
    skipped      = 0
    saved        = 0
    emotion_count = {}

    def get_audio(path):
        if path not in audio_cache:
            audio_cache[path] = AudioSegment.from_wav(path)
        return audio_cache[path]

    for transcript_path, vocals_dir in DATA_SOURCES:
        if not os.path.exists(transcript_path):
            print(f"⚠ 跳过不存在的文件：{transcript_path}")
            continue
        print(f"\n处理：{transcript_path}")
        with open(transcript_path, "r", encoding="utf-8") as f:
            segments = json.load(f)

        for seg in segments:
            emotion = seg.get("emotion", "")
            text    = seg.get("text", "").strip()
            source  = seg.get("source", "")

            if not emotion or not text or not source:
                skipped += 1
                continue

            src_path = os.path.join(vocals_dir, source)
            if not os.path.exists(src_path):
                print(f"  跳过：找不到 {src_path}")
                skipped += 1
                continue

            try:
                audio    = get_audio(src_path)
                start_ms = int(seg["start"] * 1000)
                end_ms   = int(seg["end"]   * 1000)
                clip     = audio[start_ms:end_ms]

                # 转成 SBV2 要求的 44100Hz 单声道
                clip = clip.set_frame_rate(44100).set_channels(1)

                # 输出文件名：000001.wav, 000002.wav...
                out_name = f"{saved+1:06d}.wav"
                out_path = os.path.join(wavs_dir, out_name)
                clip.export(out_path, format="wav")

                # esd.list 格式：wavs/000001.wav|gojo|JP|文本
                # 注意：SBV2 不直接用情感字段，但我们把情感记下来供后处理
                metadata.append({
                    "filename": f"wavs/{out_name}",
                    "speaker":  SPEAKER,
                    "language": LANGUAGE,
                    "text":     text,
                    "emotion":  emotion,
                })

                emotion_count[emotion] = emotion_count.get(emotion, 0) + 1
                saved += 1
            except Exception as e:
                print(f"  跳过出错：{text[:20]} | {e}")
                skipped += 1

    # 写 esd.list（SBV2 标准格式）
    esd_path = os.path.join(OUTPUT_ROOT, "esd.list")
    with open(esd_path, "w", encoding="utf-8") as f:
        for m in metadata:
            f.write(f"{m['filename']}|{m['speaker']}|{m['language']}|{m['text']}\n")

    # 单独存情感映射，给后续 style 训练用
    emotion_map_path = os.path.join(OUTPUT_ROOT, "emotion_map.json")
    with open(emotion_map_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    # 统计输出
    print(f"\n{'='*50}")
    print(f"✅ 完成！")
    print(f"   保存：{saved} 段 → {wavs_dir}")
    print(f"   跳过：{skipped} 段")
    print(f"   esd.list：{esd_path}")
    print(f"   情感映射：{emotion_map_path}")
    print(f"\n=== 情感分布 ===")
    for emo, cnt in sorted(emotion_count.items(), key=lambda x: -x[1]):
        print(f"   {emo}: {cnt} 段")


if __name__ == "__main__":
    main()