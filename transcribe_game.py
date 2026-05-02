"""
transcribe.py —— 批量转录 gojo_vocals 文件夹里所有人声文件
依赖：pip install faster-whisper
用法：直接运行，自动处理所有 wav，合并输出到 OUTPUT_PATH
"""

import json
import os
import glob
from faster_whisper import WhisperModel

# ─────────────── 配置区 ───────────────
VOCALS_DIR  = r"D:\gojo_vocals"          # UVR5 输出的人声文件夹
OUTPUT_PATH = r"D:\gojo_transcript.json" # 合并后的转录结果
MODEL_SIZE  = "medium"
LANGUAGE    = "ja"

VAD_PARAMS = {
    "min_silence_duration_ms": 400,
    "speech_pad_ms": 100,
    "threshold": 0.35,
}
# ──────────────────────────────────────


def transcribe_file(model, audio_path):
    print(f"\n  处理：{os.path.basename(audio_path)}")
    segments, _ = model.transcribe(
        audio_path,
        language=LANGUAGE,
        vad_filter=True,
        vad_parameters=VAD_PARAMS,
        word_timestamps=False,
        beam_size=5,
        best_of=5,
        temperature=0.0,
        condition_on_previous_text=True,
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,
    )
    results = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        results.append({
            "source": os.path.basename(audio_path),
            "start": round(seg.start, 2),
            "end":   round(seg.end,   2),
            "text":  text,
        })
        print(f"    [{seg.start:.1f}s → {seg.end:.1f}s]  {text}")
    print(f"  ✓ {len(results)} 段")
    return results


def main():
    wav_files = sorted(glob.glob(os.path.join(VOCALS_DIR, "*.wav")))
    if not wav_files:
        print(f"❌ 在 {VOCALS_DIR} 找不到任何 wav 文件")
        return

    print(f"找到 {len(wav_files)} 个文件，开始转录...\n")
    print(f"加载模型：{MODEL_SIZE}（首次运行会自动下载）")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

    all_results = []
    for wav in wav_files:
        segs = transcribe_file(model, wav)
        all_results.extend(segs)

    print(f"\n{'='*50}")
    print(f"全部完成！共 {len(all_results)} 段，保存到 {OUTPUT_PATH}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    # 统计
    short_segs = [r for r in all_results if (r["end"] - r["start"]) < 0.5]
    print(f"超短段（<0.5s，可能噪音）：{len(short_segs)} 段")
    print(f"\n✅ 转录完成，可以运行 label.py 开始标注了")


if __name__ == "__main__":
    main()