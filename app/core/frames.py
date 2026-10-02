"""抽帧与拼版：场景检测 → 缩略拼版图 + 索引。

接口契约见 PLAN.md §4.1 / §4.2：

    run(video, out_dir, ...) -> FramesResult

**失败只记 warning，不阻断流程**——画面是可选产物。模块本身抛
`StageError("frames", ...)`，是否降级由调用方决定（PLAN.md §4.1）。

与原型 `prototypes/make_sheets.py` 的两处关键差异：

1. **去掉感知哈希去重**。实测对动态口播帧完全无效：212 帧一个都没被去掉
   （docs/实测记录.md §3），该环节只是白白多读一遍全部图像。
   改为「场景检测 + 抽帧 + 拼版」，去重交给场景阈值。
2. **抽帧与拼版合并**。原型要求调用方先手工跑一条 ffmpeg 再传日志文件，
   不适合作为 core 接口。现在由本模块自己驱动 ffmpeg 并解析 `showinfo`。

`frames.enabled` 默认为 `false`：实测多数口播视频画面无独立信息量、
知识主体 100% 在语音（docs/实测记录.md §4）。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.core.errors import StageError
from app.core.ffmpeg_locator import find_ffmpeg

#: `showinfo` 输出的时间戳：`n: 12 pts: 12345 pts_time:12.345 ...`
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")

#: ffmpeg 9.0 移除了 `-vsync`，改用 `-fps_mode`（PLAN.md §9.3）
_FPS_MODE = "passthrough"

_TAIL_LINES = 20


@dataclass
class FramesResult:
    """抽帧产物。"""

    frames_dir: Path
    sheets: list[Path]
    index_json: Path
    total_frames: int
    kept: int
    sheet_cols: int
    sheet_rows: int


def fmt_short(seconds: float) -> str:
    """`MM:SS`，与转写稿前缀同一格式。"""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def run(
    video: str | Path,
    out_dir: str | Path,
    *,
    scene_threshold: float = 0.3,
    max_frames: int = 400,
    sheet_cols: int = 6,
    sheet_rows: int = 4,
    cell_width: int = 320,
    ffmpeg_path: str | None = None,
    on_progress: Callable[[str, float], None] | None = None,
) -> FramesResult:
    """场景检测抽帧并拼版。

    进度语义：`已产出帧数 / max_frames`——上限已知、实际帧数未知，
    因此它是个**单调上升但不保证到达 1.0** 的估计值。收尾时会补报 1.0。
    """
    src = Path(video)
    if not src.is_file():
        raise StageError("frames", f"视频文件不存在：{src}")

    out = Path(out_dir)
    frames_dir = out / "frames"
    sheets_dir = out / "sheets"
    frames_dir.mkdir(parents=True, exist_ok=True)
    sheets_dir.mkdir(parents=True, exist_ok=True)

    # 重跑前清掉旧帧，否则索引与图片会错位（frame_0001 可能是上一轮留下的）
    for old in frames_dir.glob("frame_*.jpg"):
        old.unlink()
    for old in sheets_dir.glob("sheet_*.jpg"):
        old.unlink()

    ffmpeg = find_ffmpeg(ffmpeg_path)
    timestamps = _detect_scene_frames(
        src, frames_dir, ffmpeg, scene_threshold, max_frames, on_progress
    )

    frames = sorted(frames_dir.glob("frame_*.jpg"))
    if not frames:
        logging.warning("场景检测未产出任何帧（阈值 %.2f 可能过高）", scene_threshold)
        index = {"total_scene_frames": 0, "sheets": [], "frames": []}
        index_path = sheets_dir / "frames_index.json"
        _write_json(index_path, index)
        _report(on_progress, "framing", 1.0)
        return FramesResult(frames_dir, [], index_path, 0, 0, sheet_cols, sheet_rows)

    kept = [
        {
            "file": f.name,
            "t": timestamps[i] if i < len(timestamps) else 0.0,
            "time": fmt_short(timestamps[i] if i < len(timestamps) else 0.0),
        }
        for i, f in enumerate(frames)
    ]

    sheets = _build_sheets(sheets_dir, frames_dir, kept, sheet_cols, sheet_rows, cell_width)
    index_path = sheets_dir / "frames_index.json"
    _write_json(
        index_path,
        {
            "scene_threshold": scene_threshold,
            "total_scene_frames": len(frames),
            "kept": len(kept),
            "sheets": [p.name for p in sheets],
            "frames": kept,
        },
    )

    logging.info("抽帧完成：%d 帧 → %d 张拼版图", len(frames), len(sheets))
    _report(on_progress, "framing", 1.0)
    return FramesResult(
        frames_dir, sheets, index_path, len(frames), len(kept), sheet_cols, sheet_rows
    )


def _detect_scene_frames(
    src: Path,
    frames_dir: Path,
    ffmpeg: Path,
    threshold: float,
    max_frames: int,
    on_progress: Callable[[str, float], None] | None,
) -> list[float]:
    """跑 ffmpeg 场景检测抽帧，返回每个输出帧的时间戳（秒）。

    `showinfo` 的时间戳走 stderr，而 `-progress` 走 stdout，两路都要读——
    若只读一路，另一路填满 64 KB 管道缓冲后 ffmpeg 会永久阻塞。
    """
    cmd = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",  # showinfo 属 info 级，压低级别会丢掉时间戳
        "-i",
        str(src),
        "-vf",
        f"select='gt(scene,{threshold})',showinfo",
        "-fps_mode",
        _FPS_MODE,
        "-q:v",
        "3",
        "-progress",
        "pipe:1",
        "-nostats",
        str(frames_dir / "frame_%04d.jpg"),
    ]

    _report(on_progress, "framing", 0.0)
    logging.info("场景检测抽帧（阈值 %.2f，上限 %d 帧）", threshold, max_frames)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    timestamps: list[float] = []
    stderr_lines: list[str] = []
    last_ratio = -1.0

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.rstrip("\n")
            stderr_lines.append(line)
            if len(stderr_lines) > _TAIL_LINES:
                del stderr_lines[0]
            match = _PTS_RE.search(line)
            if match:
                try:
                    timestamps.append(float(match.group(1)))
                except ValueError:
                    pass

    reader = threading.Thread(target=_drain_stderr, name="frames-ffmpeg-stderr", daemon=True)
    reader.start()

    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if line.startswith("frame=") and max_frames > 0:
                value = line.split("=", 1)[1]
                if value.isdigit():
                    ratio = min(int(value) / max_frames, 1.0)
                    if ratio - last_ratio >= 0.05:
                        last_ratio = ratio
                        _report(on_progress, "framing", ratio)
            elif line == "progress=end":
                break
    finally:
        proc.stdout.close()

    reader.join(timeout=10)
    if proc.stderr:
        proc.stderr.close()
    code = proc.wait()

    if code != 0:
        detail = "\n".join(f"  {l}" for l in stderr_lines) or "  （ffmpeg 无输出）"
        raise StageError("frames", f"ffmpeg 退出码 {code}：\n{detail}")

    return timestamps


def _build_sheets(
    sheets_dir: Path,
    frames_dir: Path,
    kept: list[dict],
    cols: int,
    rows: int,
    cell_width: int,
) -> list[Path]:
    """把帧按 `cols × rows` 拼成缩略图，带 `#序号 时间` 标注。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # noqa: BLE001
        raise StageError("frames", f"缺少 Pillow，无法拼版：{exc}") from exc

    per_sheet = max(1, cols * rows)
    cell_height = int(cell_width * 9 / 16)
    label_height = 18
    sheets: list[Path] = []

    for start in range(0, len(kept), per_sheet):
        chunk = kept[start : start + per_sheet]
        canvas = Image.new(
            "RGB",
            (cols * cell_width, rows * (cell_height + label_height)),
            (24, 24, 28),
        )
        draw = ImageDraw.Draw(canvas)
        for offset, item in enumerate(chunk):
            row, col = divmod(offset, cols)
            x = col * cell_width
            y = row * (cell_height + label_height)
            with Image.open(frames_dir / item["file"]) as im:
                canvas.paste(
                    im.convert("RGB").resize((cell_width, cell_height), Image.LANCZOS),
                    (x, y + label_height),
                )
            draw.text((x + 4, y + 3), f"#{start + offset + 1} {item['time']}", fill=(255, 220, 120))

        name = f"sheet_{start // per_sheet + 1:02d}.jpg"
        path = sheets_dir / name
        canvas.save(path, quality=88)
        sheets.append(path)

    return sheets


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _report(
    on_progress: Callable[[str, float], None] | None, stage: str, ratio: float
) -> None:
    if on_progress is not None:
        on_progress(stage, ratio)
