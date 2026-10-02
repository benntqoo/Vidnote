"""抽音频：从视频里取 16 kHz 单声道 PCM 音频。

接口契约见 PLAN.md §4.1 / §4.2：

    extract(video, out_path, ...) -> Path      # 产出 16k/mono/s16 wav

**参数口径不许改**（`-ar 16000 -ac 1 -acodec pcm_s16le`）：基线
`samples/transcript_gpu.txt` 就是在这一组参数下产生的，改任何一个都会
导致验收的逐字符比对失败。本机实测产出 38528444 字节
= 44 字节 RIFF 头 + 1204 秒 × 16000 Hz × 2 字节（见 docs/实测记录.md §2.3）。

为什么不用 PyAV 直接读 mp4：`av 19` 移除了 `metadata_errors`，而
faster-whisper 1.2.1 仍在传该参数（PLAN.md §9.2）。所以固定在 ffmpeg 侧
落地一个标准 wav，转写侧用 `wave` + `numpy` 直读。
"""

from __future__ import annotations

import logging
import subprocess
from collections import deque
from pathlib import Path
from typing import Callable

from app.core.errors import StageError
from app.core.ffmpeg_locator import find_ffmpeg

#: 目标音频参数——与基线一致，**不可调**。
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # pcm_s16le
CODEC = "pcm_s16le"

_TAIL_LINES = 20


def wav_expected_size(duration_sec: float) -> int:
    """按目标参数推算 wav 字节数（44 字节头 + PCM 数据）。用于自检与文档。"""
    return 44 + int(duration_sec * SAMPLE_RATE) * CHANNELS * SAMPLE_WIDTH


def extract(
    video: str | Path,
    out_path: str | Path,
    ffmpeg_path: str | None = None,
    total_sec: float | None = None,
    on_progress: Callable[[str, float], None] | None = None,
) -> Path:
    """抽音频到 `out_path`。

    `total_sec` 已知时用于换算百分比进度；未知则上报 `-1.0`
    （「进行中但进度不可知」，见 docs/IPC协议规格.md §5）。

    抽音频实测 1.44 秒（1204 秒音频），进度条意义有限，但接口保持一致。
    """
    src = Path(video)
    if not src.is_file():
        raise StageError("audio", f"视频文件不存在：{src}")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg(ffmpeg_path)

    cmd = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-nostdin",  # 防止 ffmpeg 抢占 stdin（常驻 worker 里 stdin 属于协议）
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vn",  # 丢视频流
        "-sn",  # 丢字幕流
        "-dn",  # 丢数据流
        "-acodec",
        CODEC,
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-progress",
        "pipe:1",  # 进度走 stdout；日志仍走 stderr
        "-nostats",
        str(out),
    ]

    _report(on_progress, "audio", -1.0 if total_sec is None else 0.0)
    logging.info("抽音频：%s → %s", src.name, out.name)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail: deque[str] = deque(maxlen=_TAIL_LINES)
    last_ratio = -1.0

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                raw = line.split("=", 1)[1]
                if raw.isdigit() and total_sec:
                    ratio = min(int(raw) / 1_000_000 / total_sec, 1.0)
                    if ratio - last_ratio >= 0.01:
                        last_ratio = ratio
                        _report(on_progress, "audio", ratio)
            elif line == "progress=end":
                break
    finally:
        proc.stdout.close()

    # stdout 已读尽，此时才能安全读 stderr（ffmpeg 未死时管道不会满）
    stderr_text = (proc.stderr.read() if proc.stderr else "") or ""
    if proc.stderr:
        proc.stderr.close()
    code = proc.wait()

    for row in stderr_text.strip().splitlines()[-_TAIL_LINES:]:
        tail.append(row)

    if code != 0:
        detail = "\n".join(f"  {l}" for l in tail) or "  （ffmpeg 无输出）"
        raise StageError("audio", f"ffmpeg 退出码 {code}：\n{detail}")

    if not out.is_file():
        raise StageError("audio", f"ffmpeg 成功退出但未产出 {out}")

    size = out.stat().st_size
    logging.info("抽音频完成：%.1f MB", size / 1024**2)
    _report(on_progress, "audio", 1.0)
    return out


def _report(
    on_progress: Callable[[str, float], None] | None, stage: str, ratio: float
) -> None:
    if on_progress is not None:
        on_progress(stage, ratio)
