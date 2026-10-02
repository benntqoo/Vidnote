"""ffmpeg 二进制定位。

搜索顺序（PLAN.md §9.4）：

1. `config.yaml` 里显式指定的路径
2. `PATH` 中的 `ffmpeg`
3. WinGet 安装的 `Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe`
4. 报错并给出 `winget install` 提示

**明确排除** `%LOCALAPPDATA%/ms-playwright/` —— 那里的 ffmpeg 是
`--disable-everything` 构建，只编进了 mjpeg/vp8 与 image2/matroska，
**没有 mp4 demuxer**，用它抽音频必然失败。

**还有一个必须踩过的坑：WinGet 的 `Links/` 目录不能直接用。**
WinGet 会把 `%LOCALAPPDATA%/Microsoft/WinGet/Links/ffmpeg.exe` 放进 PATH，
但那是一个 **0 字节的占位文件**（app execution alias），既不是 symlink 也不是
reparse point，`shutil.which()` 会命中它，而 `subprocess` 调它会直接抛
`OSError: [WinError 193] 不是有效的 Win32 应用程序`。
所以候选必须做**有效性校验**（非空 + PE 头 `MZ`），无效就继续往下找；
真实可执行在 `WinGet/Packages/Gyan.FFmpeg_*/ffmpeg-*/bin/`。
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


def _is_usable_executable(path: Path) -> bool:
    """判断候选是不是**真的能执行**。

    三条校验缺一不可：

    1. 存在；
    2. 非空 —— WinGet `Links/` 里的占位文件大小为 0，`is_file()` 仍为 True；
    3. 有 PE 头 —— Windows 可执行文件以 `MZ` 开头。这能一次挡掉占位文件、
       被截断的下载、以及误指向 `.cmd`/`.bat`/目录的情况。
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open("rb") as fh:
            return fh.read(2) == b"MZ"
    except OSError:
        return False


def _winget_candidates() -> list[Path]:
    """WinGet 安装目录下的候选 ffmpeg（真实路径，不是 Links 转发目录）。"""
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
        if _is_usable_executable(candidate) and not _is_blocked(candidate):
            return candidate

    # ② PATH —— 命中的可能是 WinGet Links 的 0 字节占位文件，校验后再用
    which = shutil.which("ffmpeg")
    if which:
        candidate = Path(which)
        if _is_usable_executable(candidate) and not _is_blocked(candidate):
            return candidate

    # ③ WinGet 真实安装目录
    for candidate in _winget_candidates():
        if _is_usable_executable(candidate):
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
    which = shutil.which("ffmpeg")
    if which and Path(which) == found:
        return f"{found}（来自 PATH）"
    return f"{found}（来自 WinGet Packages）"
