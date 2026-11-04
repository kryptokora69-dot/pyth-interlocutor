"""Local speech-to-text for PYTH via faster-whisper. Lazy-loads once, then stays loaded.
GPU (cuda, int8_float16) first; falls back to CPU int8 if CUDA fails.
Override model with PYTH_WHISPER_MODEL (default base.en), device with PYTH_WHISPER_DEVICE (cuda|cpu)."""
import os, threading

MODEL_NAME = os.environ.get("PYTH_WHISPER_MODEL", "base.en")
_model = None
_device = None
_lock = threading.Lock()


def _load():
    global _model, _device
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel
        want = os.environ.get("PYTH_WHISPER_DEVICE", "cuda")
        if want == "cuda":
            try:
                m = WhisperModel(MODEL_NAME, device="cuda", compute_type="int8_float16")
                _probe(m)
                _model, _device = m, "cuda"
                print(f"[whisper] {MODEL_NAME} loaded on cuda int8_float16")
                return _model
            except Exception as e:
                print(f"[whisper] cuda failed ({type(e).__name__}: {e}); using cpu int8")
        _model, _device = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8"), "cpu"
        print(f"[whisper] {MODEL_NAME} loaded on cpu int8")
        return _model


def _probe(m):
    """CUDA libs (cuBLAS/cuDNN) only fail at first inference, so run 1s of silence."""
    import numpy as np
    list(m.transcribe(np.zeros(16000, dtype="float32"), beam_size=1)[0])


def _decode(data: bytes):
    """Decode any browser audio (webm/opus, ogg, wav...) to 16 kHz mono float32 with PyAV.
    Done here (not faster-whisper's decode_audio) so it works across PyAV versions; no system ffmpeg needed."""
    import io, av, numpy as np
    out = []
    with av.open(io.BytesIO(data), mode="r") as c:
        rs = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in c.decode(audio=0):
            for f in rs.resample(frame) or []:
                out.append(f.to_ndarray().reshape(-1))
        for f in rs.resample(None) or []:
            out.append(f.to_ndarray().reshape(-1))
    if not out:
        return np.zeros(0, dtype="float32")
    return (np.concatenate(out).astype("float32") / 32768.0)


def transcribe_bytes(data: bytes):
    """Returns (text, info)."""
    m = _load()
    audio = _decode(data)
    if True:
        segs, info = m.transcribe(audio, beam_size=5, language="en" if MODEL_NAME.endswith(".en") else None, vad_filter=True)
        text = " ".join(s.text.strip() for s in segs).strip()
    return text, {"device": _device, "model": MODEL_NAME, "seconds": round(info.duration, 1)}
