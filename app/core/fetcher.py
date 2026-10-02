"""视频获取：在线链接走 yt-dlp，本地文件直接入库。

接口契约见 PLAN.md §4.1 / §4.2：

    download(task, cfg, out_dir, on_progress) -> DownloadResult

失败时抛 `StageError("download", ...)`，消息里带 yt-dlp 输出的**尾 20 行**——
本模块不写任务状态，状态流转由调用方（cli / dispatcher）负责。这样 core 层
不必知道「任务」这个概念的状态机，也就不会与 store 耦合。

两条实测约束（PLAN.md §9.5）：

- `yt-dlp --cookies-from-browser` 在 Windows 新版浏览器上基本必然失败，
  只能用预先生成的 Netscape `cookies.txt`；
- 平台风控敏感，需要限速（`download.rate_limit_per_min`）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.core.errors import StageError
from app.core.ffmpeg_locator import find_ffmpeg
from app.models.config import Config
from app.models.task import Task
from app.core.urls import LOCAL_PLATFORM

#: 失败时保留的输出行数（PLAN.md §4.1）。
TAIL_LINES = 20

#: yt-dlp 下载进度行：`[download]  45.2% of   44.11MiB at ...`
_PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")

#: yt-dlp 完成行（用来把进度顶到 100%）
_PROGRESS_DONE_RE = re.compile(r"\[download\]\s+100(?:\.0)?%")

INSTALL_HINT = "pip install -U yt-dlp   （或 winget install yt-dlp.yt-dlp）"

#: 探测下载耗时的超时。yt-dlp 偶发卡在网络握手，不能让它挂死整个流水线。
_DEFAULT_SOCKET_TIMEOUT = 30


# ---------------------------------------------------------------- 定位


def find_yt_dlp(explicit: str | None = None) -> str:
    """按 `config.paths.yt_dlp` → `PATH` 的顺序定位 yt-dlp 可执行文件。

    yt-dlp **没有 Rust 等价物**（社区方案均为单平台或个人项目），所以这里必然
    是外部进程调用，见 PLAN.md §7.1。
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("yt-dlp")
    if found:
        return found

    raise StageError(
        "download",
        "未找到 yt-dlp。\n"
        f"  安装：{INSTALL_HINT}\n"
        "  或在 config.yaml 的 paths.yt_dlp 中显式指定完整路径。",
    )


def _ffprobe_of(ffmpeg: str) -> str | None:
    """ffmpeg 同目录下的 ffprobe。用于本地文件取时长（失败则返回 None）。"""
    probe = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    return str(probe) if probe.is_file() else None


def probe_duration_sec(video: str | Path, ffprobe: str | None) -> int | None:
    """读容器时长（秒，取整）。**任何失败都返回 None**，不抛异常。

    本地文件没有 yt-dlp 的 info.json，只能靠 ffprobe；拿不到就不显示时长，
    这属于展示信息缺失，不该阻断流水线。
    """
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        return int(float(proc.stdout.strip()))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- 结果


@dataclass
class DownloadResult:
    """一次获取的产物。调用方据此回填 `Task` 的字段。"""

    video_path: Path
    title: str | None = None
    duration_sec: int | None = None
    video_id: str | None = None
    platform: str = ""
    info_json: Path | None = None
    from_local: bool = False


# ---------------------------------------------------------------- 主流程


def download(
    task: Task,
    cfg: Config,
    out_dir: str | Path,
    on_progress: Callable[[str, float], None] | None = None,
) -> DownloadResult:
    """把 `task` 指向的视频取到 `out_dir/video.<ext>`。

    `task.platform == "local"` 时走本地文件分支（硬链接优先），否则调 yt-dlp。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _report(on_progress, "downloading", 0.0)

    if task.platform == LOCAL_PLATFORM:
        return _take_local(task, out, cfg, on_progress)
    return _download_remote(task, out, cfg, on_progress)


def _take_local(
    task: Task,
    out: Path,
    cfg: Config,
    on_progress: Callable[[str, float], None] | None,
) -> DownloadResult:
    """本地文件：**硬链接优先**，失败回退复制。

    硬链接让 `out_dir` 自包含（符合「每个视频一个目录」的产出约定），
    同时零额外磁盘占用；由于是链接而非移动，删除 `out_dir` 不会碰到源文件。
    """
    src = Path(task.url).expanduser()
    if not src.is_file():
        raise StageError("download", f"本地文件不存在：{src}")

    dst = out / f"video{src.suffix.lower() or '.mp4'}"
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        # 跨盘 / 非 NTFS / 权限不足 → 退化为复制
        shutil.copy2(src, dst)
    logging.info("本地文件已接入：%s → %s", src.name, dst.name)

    ffmpeg = find_ffmpeg(cfg.paths.ffmpeg)
    duration = probe_duration_sec(dst, _ffprobe_of(str(ffmpeg)))
    _report(on_progress, "downloading", 1.0)

    return DownloadResult(
        video_path=dst,
        title=src.stem,
        duration_sec=duration,
        video_id=None,
        platform=LOCAL_PLATFORM,
        from_local=True,
    )


def _download_remote(
    task: Task,
    out: Path,
    cfg: Config,
    on_progress: Callable[[str, float], None] | None,
) -> DownloadResult:
    yt_dlp = find_yt_dlp(cfg.paths.yt_dlp)

    # 限速：`--limit-rate` 只接字节单位，按 10 条/分钟的大致估算给出软上限。
    rate_kib = max(1, int(cfg.download.rate_limit_per_min * 512))
    cmd: list[str] = [
        yt_dlp,
        "--no-playlist",  # 分享链接常带 list 参数，避免误抓整个合集
        "--newline",  # 进度按行输出，否则 \r 覆盖无法逐行解析
        "--no-color",
        "--no-warnings",
        "--progress",
        "--merge-output-format",
        "mp4",
        "-f",
        "bv*+ba/b",  # 优先合并最佳视频+音频；退化到单文件
        "--socket-timeout",
        str(_DEFAULT_SOCKET_TIMEOUT),
        "--retries",
        "3",
        "--limit-rate",
        f"{rate_kib}K",
        "--write-info-json",
        "-o",
        str(out / "video.%(ext)s"),
    ]

    cookies = cfg.paths.cookies_file
    if cookies:
        if not Path(cookies).is_file():
            raise StageError("download", f"cookies 文件不存在：{cookies}（见 PLAN.md §9.5）")
        cmd += ["--cookies", cookies]

    cmd.append(task.url)

    logging.info("yt-dlp 开始下载：%s", task.url)
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    last_pct = -1.0

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # 合并：进度在 stdout、错误在 stderr，统一读取
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            match = _PROGRESS_RE.search(line)
            if match:
                try:
                    pct = float(match.group(1)) / 100.0
                except ValueError:
                    continue
                if pct - last_pct >= 0.01 or _PROGRESS_DONE_RE.search(line):
                    last_pct = pct
                    _report(on_progress, "downloading", min(pct, 1.0))
    finally:
        proc.stdout.close()
        code = proc.wait()

    if code != 0:
        detail = "\n".join(f"  {l}" for l in tail)
        raise StageError(
            "download",
            f"yt-dlp 退出码 {code}，输出尾 {len(tail)} 行：\n{detail}",
        )

    video = _find_output_video(out)
    if video is None:
        raise StageError(
            "download",
            f"yt-dlp 成功退出但未找到视频文件（期望 {out}/video.*）：\n"
            + "\n".join(f"  {l}" for l in tail),
        )

    info = _read_info_json(out)
    _report(on_progress, "downloading", 1.0)
    logging.info("下载完成：%s（%.1f MB）", video.name, video.stat().st_size / 1024**2)

    return DownloadResult(
        video_path=video,
        title=info.get("title"),
        duration_sec=_as_int(info.get("duration")),
        video_id=info.get("id"),
        platform=task.platform or "",
        info_json=(out / f"{video.stem}.info.json") if (out / f"{video.stem}.info.json").is_file() else None,
    )


def _find_output_video(out: Path) -> Path | None:
    """定位 yt-dlp 的产出。优先 `video.mp4`，否则取最大的 video.* 文件。

    合并格式时中途会有 `.f137.mp4` / `.f140.m4a` 之类的临时分片，用体积排序
    可以稳定选到合并后的成品。
    """
    exact = out / "video.mp4"
    if exact.is_file():
        return exact
    candidates = [
        p
        for p in out.glob("video.*")
        if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".flv"}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _read_info_json(out: Path) -> dict:
    """读 `--write-info-json` 的产物。读不到就返回空 dict（非致命）。"""
    for path in out.glob("video.info.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("info.json 解析失败：%s", exc)
            return {}
    return {}


def _as_int(value: object) -> int | None:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _report(
    on_progress: Callable[[str, float], None] | None, stage: str, ratio: float
) -> None:
    if on_progress is not None:
        on_progress(stage, ratio)
