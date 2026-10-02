"""流水线异常类型。

分级依据 PLAN.md §4.1：转写失败意味着环境坏了（CUDA / 模型），必须中止整批；
其余阶段的失败只影响单条任务。
"""

from __future__ import annotations


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


class CookieExpired(StageError):
    """cookie 失效，需要用户重新采集（阶段 4 自动化）。"""

    def __init__(self, message: str = "cookie 已失效，请重新采集") -> None:
        super().__init__("cookie", message)
