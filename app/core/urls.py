"""输入解析：从用户粘贴的文本中抽出可下载目标（PLAN.md §7.3）。

用户实际粘贴的往往是**分享文案**而不是干净链接，典型形态：

    8.25 复制打开抖音，看看【某创作者的作品】【标题…】…
    https://v.douyin.com/AbCdEf12345/ vFh:/ 03/30 :3pm b@n.qR

所以先按平台正则抽取，**全部失败再把整行原样交给 yt-dlp**——它是通用
extractor，可能能处理我们没覆盖的站内形态。

本模块是纯函数、无外部依赖，`cli.py` / `rpc.py` / `fetcher.py` 共用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

#: 平台 → 正则。按顺序尝试，**先匹配到的胜出**。
#: 每个平台都要覆盖短链与站内长链两种形态，否则用户从 App 分享和从网页复制的链接
#: 会走两条不同路径。
URL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        (
            "douyin",
            r"https?://(?:v\.douyin\.com/[A-Za-z0-9_\-]+"
            r"|(?:www\.)?douyin\.com/video/\d+"
            r"|(?:www\.)?iesdouyin\.com/share/video/\d+)",
        ),
        (
            "bilibili",
            r"https?://(?:b23\.tv/[A-Za-z0-9]+"
            r"|(?:www\.|m\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]+|av\d+))",
        ),
        (
            "youtube",
            r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)"
            r"[A-Za-z0-9_\-]+",
        ),
    )
)

#: 本地视频文件的常见扩展名。
VIDEO_SUFFIXES = frozenset(
    {".mp4", ".mkv", ".mov", ".webm", ".flv", ".avi", ".m4v", ".ts", ".wmv"}
)

LOCAL_PLATFORM = "local"
#: 正则全部失败时的标记。**不代表失败**——调用方应把整行交给 yt-dlp 再试一次。
UNKNOWN_PLATFORM = "unknown"


@dataclass(frozen=True)
class InputItem:
    """一条解析结果。"""

    raw: str
    """用户原始输入行，用于错误提示时回显。"""

    target: str
    """抽取出的 URL，或本地文件的绝对路径。"""

    platform: str
    """`douyin` | `bilibili` | `youtube` | `local` | `unknown`。"""


def detect_platform(text: str) -> tuple[str, str] | None:
    """从任意文本中抽取第一个平台 URL，返回 `(平台, url)`；无匹配返回 None。"""
    for name, pattern in URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return name, match.group(0)
    return None


def looks_like_local_file(text: str) -> bool:
    """判断一行是否指向本地视频文件。

    先看扩展名，再看文件是否真的存在——两者任一成立即认为「用户想传本地文件」，
    这样即便文件已被移走，也能给出「文件不存在」而不是「链接无法识别」。
    """
    if not text or "://" in text:
        return False
    try:
        path = Path(text.strip().strip('"').strip("'")).expanduser()
    except (OSError, ValueError):
        return False
    if path.suffix.lower() in VIDEO_SUFFIXES:
        return True
    try:
        return path.is_file()
    except OSError:
        return False


def extract(line: str) -> InputItem | None:
    """解析一行输入。空行返回 None。"""
    raw = line.strip()
    if not raw:
        return None

    # 本地文件优先：路径里通常不会有 `https://`，但先判可以少走正则
    if looks_like_local_file(raw):
        return InputItem(raw=raw, target=str(Path(raw).expanduser()), platform=LOCAL_PLATFORM)

    hit = detect_platform(raw)
    if hit:
        platform, url = hit
        return InputItem(raw=raw, target=url, platform=platform)

    # 兜底：整行原样交出，由 yt-dlp 的通用 extractor 尝试
    return InputItem(raw=raw, target=raw, platform=UNKNOWN_PLATFORM)


def extract_many(lines: Iterable[str], dedupe: bool = True) -> list[InputItem]:
    """批量解析。`dedupe=True` 时按 `target` 去重（批内重复没必要入队两次）。

    跨批次的重复由 `store` 的 `url UNIQUE` 约束负责。
    """
    items: list[InputItem] = []
    seen: set[str] = set()
    for line in lines:
        item = extract(line)
        if item is None:
            continue
        if dedupe:
            if item.target in seen:
                continue
            seen.add(item.target)
        items.append(item)
    return items


def extract_file(path: str | Path) -> list[InputItem]:
    """从文本文件（每行一条）批量解析。PLAN.md §1 的输入形态③。"""
    p = Path(path).expanduser()
    text = p.read_text(encoding="utf-8", errors="replace")
    return extract_many(text.splitlines())
