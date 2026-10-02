"""核心流水线模块。

依赖方向（避免循环导入）：
    env_probe / ffmpeg_locator  ← 无内部依赖
    store / config             ← 仅依赖 models
    audio / transcriber        ← 依赖 ffmpeg_locator
    fetcher                    ← 依赖 store
    dispatcher（阶段 2）        ← 组织以上全部
"""

from app.core.errors import StageError

__all__ = ["StageError"]
