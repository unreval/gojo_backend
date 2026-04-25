import fish_audio_sdk
import os

session = fish_audio_sdk.Session(apikey="3b34f5f70bc0463c98d558e00b25e633")

reference_id = "ab84e47919264ee3bd8bb2751706531b"

text = "まあ、僕最強だから。心配しなくていいよ。"

print("合成中...")
with open(r"D:\test_output.wav", "wb") as f:
    for chunk in session.tts(
        fish_audio_sdk.TTSRequest(
            reference_id=reference_id,
            text=text,
        )
    ):
        f.write(chunk)

print("完成！文件保存在 D:\\test_output.wav")