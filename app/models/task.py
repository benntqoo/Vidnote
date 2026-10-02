"""任务数据模型。

字段与 PLAN.md §6 的 `tasks` 表一一对应，便于 store 层直接映射。
路径字段统一用 `str`（DB 友好），需要 Path 时用对应的 property。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

# 文件名里不能出现的字符（Windows：\ / : * ? " < > |）
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


class TaskStatus(str, Enum):
    """任务状态机（PLAN.md §6）。单向推进，失败可回退到上一稳定态。"""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    FRAMING = "framing"
    SUMMARIZING = "summarizing"
    DONE = "done"
    FAILED = "failed"


#: 中途态 → 回退目标。进程被杀时这些状态不可信，启动时要重置。
#: 例：downloading 说明下载没跑完，回退到 pending 重来。
INTERRUPTED_ROLLBACK: dict[TaskStatus, TaskStatus] = {
    TaskStatus.DOWNLOADING: TaskStatus.PENDING,
    TaskStatus.TRANSCRIBING: TaskStatus.DOWNLOADED,
    TaskStatus.FRAMING: TaskStatus.TRANSCRIBED,
    TaskStatus.SUMMARIZING: TaskStatus.TRANSCRIBED,
}

#: 不需要重新处理的状态。
TERMINAL_STATUSES = frozenset({TaskStatus.DONE})


def now_iso() -> str:
    """本地时间 ISO 字符串（秒精度），用于 created_at / updated_at。"""
    return datetime.now().isoformat(timespec="seconds")


def safe_name(text: str | None, fallback: str = "untitled", limit: int = 60) -> str:
    """把标题转成安全的目录名。

    去掉 Windows 非法字符、压缩空白、截断长度；结果为空则用 fallback。
    """
    if not text:
        return fallback
    cleaned = _ILLEGAL_FILENAME.sub(" ", text)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:limit].rstrip(" .")


@dataclass
class Task:
    """单条视频的处理任务。"""

    url: str
    platform: str | None = None
    video_id: str | None = None
    title: str | None = None
    duration_sec: int | None = None
    status: str = TaskStatus.PENDING.value
    stage_error: str | None = None
    retry_count: int = 0
    out_dir: str | None = None
    video_path: str | None = None
    audio_path: str | None = None
    transcript_json: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    id: int | None = None

    # ---- 状态 ----

    @property
    def status_enum(self) -> TaskStatus:
        return TaskStatus(self.status)

    def set_status(self, status: TaskStatus | str, error: str | None = None) -> None:
        self.status = status.value if isinstance(status, TaskStatus) else status
        self.stage_error = error
        self.updated_at = now_iso()

    def reset_interrupted(self) -> TaskStatus | None:
        """若当前处于中途态，回退到上一稳定态并返回新状态；否则返回 None。"""
        try:
            current = self.status_enum
        except ValueError:
            # 未知状态（例如旧版本写下的），按 pending 处理最安全
            self.set_status(TaskStatus.PENDING)
            return TaskStatus.PENDING
        target = INTERRUPTED_ROLLBACK.get(current)
        if target is None:
            return None
        self.set_status(target)
        return target

    # ---- 路径 ----

    @property
    def out_path(self) -> Path | None:
        return Path(self.out_dir) if self.out_dir else None

    @property
    def video_file(self) -> Path | None:
        return Path(self.video_path) if self.video_path else None

    @property
    def audio_file(self) -> Path | None:
        return Path(self.audio_path) if self.audio_path else None

    # ---- 序列化 ----

    def to_row(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> Task:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in dict(row).items() if k in known})

    def dir_name(self) -> str:
        """输出目录名：`<id 四位补零>_<安全标题>`。

        补零是为了在文件管理器里按名称排序时与任务序号一致——IPC 协议
        `docs/IPC协议规格.md` §5.1 的示例即 `work/0007_示例视频标题`。
        无标题时退回 video_id / 占位符。
        """
        prefix = f"{self.id:04d}" if self.id is not None else "0000"
        stem = safe_name(self.title or self.video_id, fallback="video")
        return f"{prefix}_{stem}"
