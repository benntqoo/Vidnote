"""转写：faster-whisper + large-v3（GPU float16）。

接口契约见 PLAN.md §4.1 / §4.2：

    run(wav, model_path, out_prefix, ...) -> TranscriptOutput

**唯一「失败即整批中止」的阶段**：模型加载不出来说明环境坏了（CUDA 运行库
缺失 / 模型文件损坏），继续跑只会浪费一整夜。故此处抛
`FatalEnvironmentError`，由调用方中止整批（PLAN.md §4.1）。

三个不可改的实现细节：

1. **CUDA DLL 注入必须早于 `faster_whisper` 导入**。本模块在顶部导入
   `app.core.cuda_dll`（副作用即目的），因此 `faster_whisper` 只能在
   **函数内部**导入——写成模块级 import 会让注入失效（PLAN.md §9.1）。
2. **`av 19` 与 faster-whisper 1.2.1 不兼容**（`metadata_errors` 参数被移除）。
   所以用 `wave` + `numpy` 直读 wav，绝不把文件路径交给 faster-whisper 的解码器。
3. **分段边界由 `beam_size` 与 `vad_min_silence_ms` 决定**。基线
   `samples/transcript_gpu.txt`（649 段）产生于 `beam_size=5` +
   `min_silence_duration_ms=400` + `condition_on_previous_text=False`。
   改这三个值会改变分段，进而使逐字符验收失败——那不是 bug，是参数变了。
"""

from __future__ import annotations

import json
import logging
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.core import cuda_dll  # noqa: F401 — 副作用：注入 nvidia DLL 目录，必须最先执行
from app.core.errors import FatalEnvironmentError, StageError

if TYPE_CHECKING:  # 仅为类型标注，运行时不导入（导入时机受 cuda_dll 约束）
    from faster_whisper import WhisperModel

#: 注入结果，供日志与自检展示。
NVIDIA_DLL_DIRS: list[str] = cuda_dll.NVIDIA_DLL_DIRS

#: 音频参数必须与 audio.py 一致，否则读完直接报错（见 load_wav_16k_mono）。
EXPECTED_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_WIDTH = 2

#: 基线口径，**不要改**（见模块 docstring 第 3 条）。
DEFAULT_BEAM_SIZE = 5
DEFAULT_VAD_MIN_SILENCE_MS = 400
DEFAULT_CONDITION_ON_PREVIOUS_TEXT = False

#: 换行符。基线产出是 CRLF（Windows 文本模式写出），逐字符比对要求复现同样的
#: 行尾，因此这里**显式指定**而不是依赖平台默认——这样在任何平台上产出都一样。
NEWLINE = "\r\n"


# ---------------------------------------------------------------- 模型缓存

#: 常驻 worker 下模型只加载一次（5 秒 + 3 GB 显存，见 PLAN.md §2.2）。
#: 键为 (模型目录, device, compute_type)——换任一参数都要重新加载。
_MODEL_CACHE: dict[tuple[str, str, str], "WhisperModel"] = {}
_MODEL_LOCK = threading.Lock()


def load_model(
    model_path: str | Path,
    device: str = "cuda",
    compute_type: str = "float16",
) -> "WhisperModel":
    """加载并缓存模型。失败抛 `FatalEnvironmentError`（整批中止）。

    多线程安全：用锁包住加载过程，避免两个 worker 同时载入同一份权重。
    """
    from faster_whisper import WhisperModel  # 延迟导入：必须在 cuda_dll 之后

    path = Path(model_path)
    if not path.exists():
        raise FatalEnvironmentError(
            "transcribe",
            f"模型不存在：{path}（按 README 第 3 步下载 large-v3）",
        )

    key = (str(path), device, compute_type)
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached

        logging.info(
            "加载模型 %s（device=%s compute=%s，nvidia DLL 目录 %d 个）",
            path,
            device,
            compute_type,
            len(NVIDIA_DLL_DIRS),
        )
        try:
            model = WhisperModel(str(path), device=device, compute_type=compute_type)
        except Exception as exc:  # noqa: BLE001 — 任何加载失败都是环境故障
            raise FatalEnvironmentError(
                "transcribe",
                f"模型加载失败（CUDA 运行库不可用？）：{type(exc).__name__}: {exc}",
            ) from exc

        _MODEL_CACHE[key] = model
        return model


def clear_model_cache() -> None:
    """释放缓存的模型（测试用；也会让下一次 run() 重新加载）。"""
    with _MODEL_LOCK:
        _MODEL_CACHE.clear()


# ---------------------------------------------------------------- 音频读取


def load_wav_16k_mono(path: str | Path) -> Any:
    """读 16 kHz 单声道 s16 wav 成 float32 ndarray（值域 [-1, 1)）。

    绕开 PyAV：把 ndarray 直接交给 faster-whisper，就不会走它基于 `av` 的
    解码路径（PLAN.md §9.2）。
    """
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if (
            w.getnchannels() != EXPECTED_CHANNELS
            or w.getframerate() != EXPECTED_RATE
            or w.getsampwidth() != EXPECTED_WIDTH
        ):
            raise StageError(
                "transcribe",
                f"音频格式不符：期望 {EXPECTED_RATE}Hz/{EXPECTED_CHANNELS}ch/"
                f"{EXPECTED_WIDTH * 8}bit，实际 {w.getframerate()}Hz/"
                f"{w.getnchannels()}ch/{w.getsampwidth() * 8}bit（见 audio.py）",
            )
        raw = w.readframes(w.getnframes())

    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------- 时间格式


def fmt_srt(seconds: float) -> str:
    """`HH:MM:SS,mmm`（SRT 时间格式）。"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_short(seconds: float) -> str:
    """`MM:SS`（转写稿前缀，与基线一致）。"""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


# ---------------------------------------------------------------- 结果


@dataclass
class TranscriptOutput:
    """一次转写的全部产物。"""

    txt: Path
    srt: Path
    json: Path
    segments: int
    duration_sec: float
    language: str
    model: str


# ---------------------------------------------------------------- 主流程


def run(
    wav: str | Path,
    model_path: str | Path,
    out_prefix: str | Path,
    *,
    language: str = "zh",
    beam_size: int = DEFAULT_BEAM_SIZE,
    vad_filter: bool = True,
    vad_min_silence_ms: int = DEFAULT_VAD_MIN_SILENCE_MS,
    initial_prompt: str | None = None,
    device: str = "cuda",
    compute_type: str = "float16",
    on_progress: Callable[[str, float], None] | None = None,
) -> TranscriptOutput:
    """转写 wav，产出 `<out_prefix>.txt` / `.srt` / `.json`。

    进度语义：`progress = 当前 segment 结束时间 / 音频总时长`——真实进度，
    来自 segment 时间戳而不是估计值（docs/IPC协议规格.md §5）。
    """
    src = Path(wav)
    if not src.is_file():
        raise StageError("transcribe", f"音频文件不存在：{src}")

    model = load_model(model_path, device=device, compute_type=compute_type)
    audio = load_wav_16k_mono(src)
    logging.info("音频已解码：%.1f 秒", len(audio) / EXPECTED_RATE)

    model_ref = str(model_path)
    _report(on_progress, "transcribing", 0.0)

    try:
        segments_iter, info = model.transcribe(
            audio,
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            vad_parameters={"min_silence_duration_ms": vad_min_silence_ms},
            initial_prompt=initial_prompt or None,
            condition_on_previous_text=DEFAULT_CONDITION_ON_PREVIOUS_TEXT,
        )
    except Exception as exc:  # noqa: BLE001 — 推理起步就失败通常也是环境问题
        raise FatalEnvironmentError(
            "transcribe", f"推理启动失败：{type(exc).__name__}: {exc}"
        ) from exc

    total = float(info.duration or 0.0)
    rows: list[dict[str, Any]] = []
    for seg in segments_iter:
        rows.append(
            {
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
            }
        )
        if total > 0:
            _report(on_progress, "transcribing", min(seg.end / total, 1.0))

    if not rows:
        logging.warning("转写结果为空（音频可能是纯静音）")

    out = Path(out_prefix)
    out.parent.mkdir(parents=True, exist_ok=True)
    txt_path, srt_path, json_path = _write_outputs(out, rows, model_ref, info, language)

    _report(on_progress, "transcribing", 1.0)
    logging.info("转写完成：%d 段，%.1f 秒音频 → %s", len(rows), total, txt_path.name)

    return TranscriptOutput(
        txt=txt_path,
        srt=srt_path,
        json=json_path,
        segments=len(rows),
        duration_sec=total,
        language=info.language or language,
        model=model_ref,
    )


def _write_outputs(
    out: Path,
    rows: list[dict[str, Any]],
    model_ref: str,
    info: Any,
    language: str,
) -> tuple[Path, Path, Path]:
    """写出 txt / srt / json。**三种格式全部用 CRLF**，与基线逐字节可比。"""
    txt_path = out.with_suffix(".txt")
    srt_path = out.with_suffix(".srt")
    json_path = out.with_suffix(".json")

    lines = [f"[{fmt_short(r['start'])}] {r['text']}\n" for r in rows]
    _write_text(txt_path, "".join(lines))

    srt_blocks = [
        f"{i}\n{fmt_srt(r['start'])} --> {fmt_srt(r['end'])}\n{r['text']}\n\n"
        for i, r in enumerate(rows, 1)
    ]
    _write_text(srt_path, "".join(srt_blocks))

    payload = {
        "model": model_ref,
        "language": info.language or language,
        "duration": info.duration,
        "segments": rows,
    }
    _write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    return txt_path, srt_path, json_path


def _write_text(path: Path, text: str) -> None:
    """统一以 LF→CRLF 的显式行尾写出。

    `newline=NEWLINE` 表示「写 `\\n` 时实际输出 `\\r\\n`」，因此在 Windows 与
    其他平台上产出完全一致——验收要的是字节级可比，不能依赖平台默认。
    """
    with path.open("w", encoding="utf-8", newline=NEWLINE) as fh:
        fh.write(text)


def _report(
    on_progress: Callable[[str, float], None] | None, stage: str, ratio: float
) -> None:
    if on_progress is not None:
        on_progress(stage, ratio)
