"""流水线异常类型。

分级依据 PLAN.md §4.1：转写失败意味着环境坏了（CUDA / 模型），必须中止整批；
其余阶段的失败只影响单条任务。

`TaskCancelled` 是**用户意图**而不是故障：它单条生效、不计入重试预算，
并在调度器里走与 `StageError` 不同的分支（状态文案为「已取消」，见 `dispatcher.py`）。
"""

from __future__ import annotations

import threading


class VidnoteError(Exception):
    """本项目所有异常的基类。"""


class StageError(VidnoteError):
    """单个阶段失败，只影响当前任务。

    带上 `stage` 便于直接写入 `Task.stage_error`。
    """

    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        super().__init__(f"[{stage}] {message}")


class FatalEnvironmentError(VidnoteError):
    """环境级故障（CUDA DLL 缺失、模型损坏等），必须中止整批。

    继续跑只会浪费时间和平台配额，见 PLAN.md §4.1。
    """

    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        super().__init__(f"[{stage}] 环境故障，中止整批：{message}")


class TaskCancelled(VidnoteError):
    """当前任务被用户取消。

    ⚠️ **不是** `StageError` 的子类。两者在语义上不同：`StageError` 表示「试过了、
    失败了」，`TaskCancelled` 表示「不让它跑了」。混在一起会让「失败」计数把
    用户取消也算进去，UI 上表现为「我明明点了取消，怎么报错了」。
    """

    def __init__(self, stage: str = "", message: str = "任务已取消") -> None:
        self.stage = stage
        super().__init__(f"[{stage}] {message}" if stage else message)


class CookieExpired(StageError):
    """cookie 失效，需要用户重新采集（阶段 4 自动化）。"""

    def __init__(self, message: str = "cookie 已失效，请重新采集") -> None:
        super().__init__("cookie", message)


def raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    """在长循环里调用：被取消则抛 `TaskCancelled`。

    放在 `errors` 而非各阶段模块，是为了让四个阶段（下载 / 抽音频 / 转写 / 抽帧）
    用同一套判定，避免各写一遍 `if event and event.is_set()` 写出不一致的分支。

    `cancel_event is None` 时只做一次判空——这是阶段 1 的调用路径（CLI 单条串行），
    零行为变化。
    """
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelled()
