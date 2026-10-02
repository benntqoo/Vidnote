"""Transcribe audio with faster-whisper, emitting txt / srt / json.

Usage:
    python transcribe.py audio.wav --model large-v3 --device cuda --compute-type float16 \
        --out transcript --language zh
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


# os.add_dll_directory() hands back a handle that MUST stay alive: once it is
# garbage-collected the directory is dropped from the search path again. Keep
# every handle referenced for the lifetime of the process.
_DLL_DIR_HANDLES: list = []


def add_nvidia_dll_dirs() -> list[str]:
    """Make pip-installed NVIDIA runtime DLLs discoverable on Windows.

    ctranslate2 loads cublas/cudnn through the OS loader at runtime, but the
    nvidia-* wheels drop them in site-packages/nvidia/<pkg>/bin, which is not
    on PATH. Without this, GPU inference dies with
    "Library cublas64_12.dll is not found or cannot be loaded".
    """
    added: list[str] = []
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return added
    try:
        import nvidia
    except ImportError:
        return added

    # nvidia is a namespace package: no __file__, only __path__
    paths = list(getattr(nvidia, "__path__", []) or [])
    if not paths:
        return added
    base = Path(paths[0])
    for pkg in sorted(base.iterdir()):
        bin_dir = pkg / "bin"
        if bin_dir.is_dir():
            try:
                handle = os.add_dll_directory(str(bin_dir))
            except OSError:
                continue
            _DLL_DIR_HANDLES.append(handle)  # must stay alive
            added.append(str(bin_dir))

    # add_dll_directory only helps LoadLibraryEx with USER_DIRS. ctranslate2
    # calls plain LoadLibrary("cublas64_12.dll"), which resolves against the
    # process PATH, so prepend the dirs there too.
    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")

    return added


# MUST run at import time, before faster_whisper/ctranslate2 load their native
# libs — os.add_dll_directory only affects subsequent DLL resolution.
NVIDIA_DLL_DIRS = add_nvidia_dll_dirs()

from faster_whisper import WhisperModel  # noqa: E402


def fmt_srt(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_short(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def load_wav_16k_mono(path: str):
    """Read a 16 kHz mono PCM wav into float32 ndarray.

    Passing the ndarray straight to faster-whisper bypasses its PyAV-based
    decoder, which breaks on newer `av` releases (metadata_errors removed).
    """
    import wave

    import numpy as np

    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
            raise ValueError(
                f"expected 16kHz/mono/s16 wav, got {w.getframerate()}Hz "
                f"{w.getnchannels()}ch {w.getsampwidth() * 8}bit"
            )
        raw = w.readframes(w.getnframes())

    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--out", default="transcript")
    ap.add_argument("--initial-prompt", default=None)
    args = ap.parse_args()

    if NVIDIA_DLL_DIRS and args.device == "cuda":
        print(f"nvidia runtime dirs: {len(NVIDIA_DLL_DIRS)}", flush=True)

    print(f"loading model={args.model} device={args.device} compute={args.compute_type}", flush=True)
    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        download_root=str(Path("models").resolve()),
    )

    audio_input = args.audio
    if Path(args.audio).suffix.lower() == ".wav":
        audio_input = load_wav_16k_mono(args.audio)
        print(f"audio: {len(audio_input) / 16000:.1f}s decoded from wav", flush=True)

    segments, info = model.transcribe(
        audio_input,
        language=args.language,
        beam_size=args.beam_size,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        initial_prompt=args.initial_prompt,
        condition_on_previous_text=False,
    )

    print(f"detected language={info.language} prob={info.language_probability:.2f}", flush=True)

    rows = []
    for seg in segments:
        text = seg.text.strip()
        rows.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
        print(f"[{fmt_short(seg.start)}] {text}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "model": args.model,
        "language": info.language,
        "duration": info.duration,
        "segments": rows,
    }
    out.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with out.with_suffix(".txt").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(f"[{fmt_short(r['start'])}] {r['text']}\n")

    with out.with_suffix(".srt").open("w", encoding="utf-8") as fh:
        for i, r in enumerate(rows, 1):
            fh.write(f"{i}\n{fmt_srt(r['start'])} --> {fmt_srt(r['end'])}\n{r['text']}\n\n")

    print(f"\nsegments={len(rows)} -> {out}.txt / .srt / .json")


if __name__ == "__main__":
    main()
