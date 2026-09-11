import os, subprocess, tempfile, wave
import numpy as np, torch
print("torch          ", torch.__version__, "| cuda build", torch.version.cuda)
print("cuda available ", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device         ", torch.cuda.get_device_name(0))
    p = torch.cuda.get_device_properties(0)
    print("vram           ", round(p.total_memory / 2**30, 2), "GiB | sm_%d%d" % (p.major, p.minor))
    a = torch.randn(4096, 4096, device="cuda", dtype=torch.bfloat16)
    b = a @ a
    print("matmul ok      ", tuple(b.shape), b.dtype)
import torchaudio, torchcodec
print("torchaudio     ", torchaudio.__version__, "| torchcodec", torchcodec.__version__)
p = os.path.join(tempfile.mkdtemp(), "t.wav")
with wave.open(p, "wb") as w:
    w.setnchannels(1); w.setsampwidth(4); w.setframerate(16000)
    w.writeframes(np.zeros(16000, dtype="<f4").tobytes())
try:
    x, sr = torchaudio.load(p)
    print("torchaudio.load", tuple(x.shape), sr, "<- FFmpeg shared libs found")
except Exception as e:
    print("torchaudio.load FAILED:", type(e).__name__, str(e)[:200])
try:
    from diffsynth.utils.data.audio import read_audio
    print("diffsynth read_audio ok")
except Exception as e:
    print("diffsynth read_audio FAILED:", type(e).__name__, str(e)[:200])
print("ffprobe        ", subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0])
