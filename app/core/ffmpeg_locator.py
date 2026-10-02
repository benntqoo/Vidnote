"""ffmpeg 二进制定位。

搜索顺序（PLAN.md §9.4）：

1. `config.yaml` 里显式指定的路径
2. `PATH` 中的 `ffmpeg`
3. WinGet 安装的 `Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe`
4. 报错并给出 `winget install` 提示

**明确排除** `%LOCALAPPDATA%/ms-playwright/` —— 那里的 ffmpeg 是
`--disable-everything` 构建，只编进了 mjpeg/vp8 与 image2/matroska，
**没有 mp4 demuxer**，用它抽音频必然失败。
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from app.core.errors import StageError

INSTALL_HINT = "winget install --id Gyan.FFmpeg -e"

#: 绝不使用的目录（保留原因见模块 docstring）
_BLOCKED_PARTS = ("ms-playwright",)


def _is_blocked(path: Path) -> bool:
    lowered = str(path).lower()
    return any(part in lowered for part in _BLOCKED_PARTS)


def _winget_candidates() -> list[Path]:
    """WinGet 安装目录下的候选 ffmpeg。"""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
    if not packages.is_dir():
        return []
    found: list[Path] = []
    for pkg in sorted(packages.glob("Gyan.FFmpeg_*")):
        found.extend(sorted(pkg.glob("ffmpeg-*/bin/ffmpeg.exe")))
    return found


@lru_cache(maxsize=1)
def _locate_cached(explicit: str | None) -> Path | None:
    # ① 配置里显式指定的路径
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file() and not _is_blocked(candidate):
            return candidate

    # ② PATH
    which = shutil.which("ffmpeg")
    if which:
        candidate = Path(which)
        if not _is_blocked(candidate):
            return candidate

    # ③ WinGet
    for candidate in _winget_candidates():
        if candidate.is_file():
            return candidate

    # ④ 放弃
    return None


def find_ffmpeg(explicit: str | None = None) -> Path:
    """返回可用的 ffmpeg 绝对路径；找不到时抛 StageError（带安装命令）。"""
    found = _locate_cached(explicit)
    if found is None:
        raise StageError(
            "ffmpeg",
            "未找到可用的 ffmpeg。\n"
            f"  安装：{INSTALL_HINT}\n"
            "  或在 config.yaml 的 paths.ffmpeg 中显式指定完整路径。",
        )
    return found


def ffmpeg_available(explicit: str | None = None) -> bool:
    """只判断可用性，不抛异常（供 env_probe / GUI 状态栏使用）。"""
    return _locate_cached(explicit) is not None


def describe(explicit: str | None = None) -> str:
    """人类可读的来源描述，用于日志与自检输出。"""
    found = _locate_cached(explicit)
    if found is None:
        return "未找到"
    if explicit and Path(explicit).expanduser() == found:
        return f"{found}（来自 config）"
    if _is_blocked(found):
        return f"{found}（⚠️ 来自 playwright，不可用）"
    return str(found)
