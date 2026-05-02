"""
build_eleven_ref.py —— 把多种格式的音视频文件合并成长音频，给 ElevenLabs PVC 用
支持格式：wav, mp3, mp4, m4a, flac, ogg, aac
依赖：pip install pydub  +  系统装了 ffmpeg

用法：
1. 把所有音频/视频文件放到 INPUT_DIR
2. 运行脚本，自动提取音频、合并、导出成 wav
"""

import os
from pydub import AudioSegment

# ─────────────── 配置区 ───────────────
INPUT_DIRS = [
    r"D:\gojo_emotions",       # 已经切好的情感数据
    r"D:\gojo_extra",          # 新下载的视频/音频放这里（可以是 mp4 mp3 等）
]
OUTPUT     = r"D:\gojo_eleven_clone.wav"
SILENCE_MS = 500

# 支持的扩展名
SUPPORTED = (".wav", ".mp3", ".mp4", ".m4a", ".flac", ".ogg", ".aac", ".mkv", ".webm")
# ──────────────────────────────────────


def load_audio(path):
    """根据后缀自动选择加载方式"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wav":
        return AudioSegment.from_wav(path)
    elif ext == ".mp3":
        return AudioSegment.from_mp3(path)
    elif ext in (".mp4", ".m4a", ".mkv", ".webm"):
        return AudioSegment.from_file(path, "mp4" if ext == ".mp4" else None)
    else:
        return AudioSegment.from_file(path)


def main():
    silence = AudioSegment.silent(duration=SILENCE_MS)
    output  = AudioSegment.empty()
    found   = 0
    skipped = 0

    for in_dir in INPUT_DIRS:
        if not os.path.exists(in_dir):
            print(f"⚠ 跳过不存在：{in_dir}")
            continue

        files = sorted([f for f in os.listdir(in_dir) if f.lower().endswith(SUPPORTED)])
        if not files:
            print(f"⚠ {in_dir} 里没有支持的音视频")
            continue

        print(f"\n📁 {in_dir}（{len(files)} 个文件）")
        for fname in files:
            path = os.path.join(in_dir, fname)
            try:
                audio = load_audio(path)
                duration = len(audio) / 1000
                output += audio + silence
                found += 1
                print(f"  ✓ {fname}  ({duration:.1f}s)")
            except Exception as e:
                print(f"  ✗ {fname}  失败：{e}")
                skipped += 1

    if found == 0:
        print("\n❌ 没有任何文件被加载")
        return

    # ElevenLabs PVC 推荐 44.1kHz 单声道
    output = output.set_frame_rate(44100).set_channels(1)
    output.export(OUTPUT, format="wav")

    total = len(output) / 1000
    print(f"\n{'='*50}")
    print(f"✅ 完成！")
    print(f"   总时长：{total:.1f}s ({total/60:.1f}分钟)")
    print(f"   合并：{found} 个文件，跳过 {skipped} 个")
    print(f"   保存到：{OUTPUT}")

    if total < 1800:
        print(f"\n⚠️ 不到 30 分钟（差 {(1800-total)/60:.1f} 分钟）")
        print(f"   建议继续补充数据再上传 PVC")
    else:
        print(f"\n✅ 满足 30 分钟门槛！可以上传 ElevenLabs Professional Voice Cloning")


if __name__ == "__main__":
    main()