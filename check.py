from pydub import AudioSegment
audio = AudioSegment.from_wav(r"D:\gojo_audio\htdemucs\[BraveDown.Com] [咒术回战第一季五条悟语音纯享版] [1773467622]\vocals.wav")
print("声道数:", audio.channels)
print("采样率:", audio.frame_rate)
print("位深:", audio.sample_width * 8, "bit")