"""单条任务的流水线执行器。

**为什么单独成模块**：阶段 1 的 CLI 和阶段 3 的 `rpc.py` 都要跑「同一条链路」。
若把它写在 `cli.py` 里，Rust 侧接入时就得复制一份——这正是 PLAN.md §4.2
「双入口共用 core」想要避免的返工。阶段 2 的 `dispatcher.py`（队列 + 并发）
调用的也是本模块，它只负责**调度**，不负责**单条怎么跑**。

链路（PLAN.md §3.1，单条视角）：

    downloading → downloaded → [抽音频] → transcribing → transcribed
                → [framing]（可选）→ done

本模块导出**三个阶段函数**（`run_download` / `run_asr` / `run_frames`）与它们的
串行组合 `run_one`。两种调用方式共用同一份实现，因此不会出现「并行路径与串行
路径行为不一致」这类最难查的 bug：

| 调用方 | 用法 | 场景 |
|---|---|---|
| `cli.py --jobs 1` / `rpc.py` 单条 | `run_one(...)` | 串行，阶段 1 验收走的路径 |
| `dispatcher.py` | 三个阶段函数分别扔进各自的池 | 批量，阶段 2 |

失败策略（PLAN.md §4.1）：

- `StageError`（下载/抽音频/抽帧）→ 标记该条 `failed`，**不影响其他任务**
- `TaskCancelled` → 标记该条 `failed`（文案「已取消」），**不影响其他任务**
- `FatalEnvironmentError`（模型加载、CUDA）→ 标记该条 `failed` 后**继续向上抛**，
  由调用方中止整批——环境坏了继续跑只会浪费一整夜

产出目录：`<work_dir>/<id 四位补零>_<安全标题>/`，内含
`video.* / audio.wav / transcript.{txt,srt,json} / frames/ / sheets/`。
"""

from __future__ import annotations

import logging
import shutil
import threading
from functools import lru_cache
from pathlib import Path
from typing import Callable

from app.core import audio as audio_mod
from app.core import fetcher, frames as frames_mod, transcriber
from app.core.errors import (
    FatalEnvironmentError,
    StageError,
    TaskCancelled,
    VidnoteError,
)
from app.core.store import Store
from app.models.config import Config
from app.models.task import Task, TaskStatus

#: 下载先落到暂存区，拿到标题后再定名——避免「半个视频」出现在正式目录里。
STAGING_DIR = ".staging"

ProgressFn = Callable[[str, float], None]
StageFn = Callable[[Task], None]


def current_device(cfg: Config) -> str:
    """解析 `device: auto` 的落地值。

    真实可用性由 `env_probe.verify_cuda()` 决定；这里只让 auto 落向 cuda，
    避免在支持 GPU 的机器上悄悄退化成 CPU（8.7× → 1.44×）。
    `dispatcher` 起线程前调用一次并复用——`ctranslate2` 的导入开销不值得每条付一次。
    """
    return cfg.runtime.effective_device(_cuda_device_present())


# ---------------------------------------------------------------- 串行组合


def run_one(
    task: Task,
    cfg: Config,
    store: Store,
    *,
    on_progress: ProgressFn | None = None,
    on_stage: StageFn | None = None,
    cancel_event: threading.Event | None = None,
) -> Task:
    """跑完一条任务的完整链路。返回更新后的 `task`。

    调用前 `task` 必须已在 `store` 中（有 `id`），否则产出目录无法编号。
    """
    if task.id is None:
        raise ValueError("run_one 需要 task.id——先用 store.add() 插入")

    cfg.work_path().mkdir(parents=True, exist_ok=True)
    device = current_device(cfg)
    hooks = {"on_progress": on_progress, "on_stage": on_stage, "cancel_event": cancel_event}

    try:
        run_download(task, cfg, store, **hooks)
        run_asr(task, cfg, store, device, **hooks)
        run_frames(task, cfg, store, **hooks)
    except FatalEnvironmentError as exc:
        _fail(task, store, str(exc), on_stage)
        raise
    except TaskCancelled:
        _fail(task, store, "已取消", on_stage)
    except VidnoteError as exc:
        # 下载 / 抽音频 / 抽帧 —— 只影响当前这条
        logging.warning("任务 %s 失败：%s", task.id, exc)
        _fail(task, store, str(exc), on_stage)
    except Exception as exc:  # noqa: BLE001 — 未预期异常同样只影响单条
        logging.exception("任务 %s 未预期失败", task.id)
        _fail(task, store, f"{type(exc).__name__}: {exc}", on_stage)

    return task


# ---------------------------------------------------------------- 各阶段
#
# 三个阶段函数是 `dispatcher.py` 的调度单位。共同约定：
#   - **不吞异常**：失败向上抛，状态标记由调用方负责（`run_one` 或 dispatcher）
#   - `cancel_event` 可选，None 时零行为差异
#   - 自带断点续跑判断（已有产出就跳过），因此重复调用是安全的


def run_download(
    task: Task,
    cfg: Config,
    store: Store,
    *,
    on_progress: ProgressFn | None = None,
    on_stage: StageFn | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """下载阶段：`downloading → downloaded`。"""
    work = cfg.work_path()
    work.mkdir(parents=True, exist_ok=True)

    if task.video_path and Path(task.video_path).is_file():
        # 断点续跑：上次已下完，直接进入下一阶段
        logging.info("任务 %s 已有视频，跳过下载", task.id)
        return

    _set(task, store, TaskStatus.DOWNLOADING, on_stage)
    staging = work / STAGING_DIR / str(task.id)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    result = fetcher.download(task, cfg, staging, on_progress, cancel_event)

    task.title = result.title or task.title or result.video_path.stem
    task.duration_sec = result.duration_sec or task.duration_sec
    task.video_id = result.video_id or task.video_id
    if result.platform:
        task.platform = result.platform

    out_dir = _promote(task, staging, work)
    task.out_dir = str(out_dir)

    # 视频可能带任何扩展名，落定后重新定位
    video = _find_video(out_dir)
    if video is None:
        raise StageError("download", f"暂存目录里没有视频文件：{staging}")
    task.video_path = str(video)

    _set(task, store, TaskStatus.DOWNLOADED, on_stage)


def run_asr(
    task: Task,
    cfg: Config,
    store: Store,
    device: str,
    *,
    on_progress: ProgressFn | None = None,
    on_stage: StageFn | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """抽音频 + 转写：`downloaded → ... → transcribed`。

    抽音频为什么要放在这里而不是单独一个池：实测 1.44 秒 / 1204 秒音频
    （docs/实测记录.md §2.1），相对转写的 2 分 18 秒只占 1%。为它加一个线程池
    并往 IPC 协议的 `concurrency` 事件里加 `n_audio` 字段，收益是「100 条视频
    省 2.5 分钟」（0.6%），代价是协议契约变更 + 一处新的并发点。
    不值当，故并入转写池——转写池本来就恒为 1（GPU 单卡串行），没有争抢。
    """
    out_dir = Path(task.out_dir)  # type: ignore[arg-type]
    video = Path(task.video_path)  # type: ignore[arg-type]

    audio_path = out_dir / "audio.wav"
    if not audio_path.is_file():
        audio_path = audio_mod.extract(
            video,
            audio_path,
            ffmpeg_path=cfg.paths.ffmpeg,
            total_sec=task.duration_sec,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
    task.audio_path = str(audio_path)

    if task.transcript_json and Path(task.transcript_json).is_file():
        logging.info("任务 %s 已有转写稿，跳过转写", task.id)
        return

    _set(task, store, TaskStatus.TRANSCRIBING, on_stage)
    output = transcriber.run(
        audio_path,
        cfg.model_path(),
        out_dir / "transcript",
        language=cfg.transcribe.language,
        beam_size=cfg.transcribe.beam_size,
        vad_filter=cfg.transcribe.vad_filter,
        vad_min_silence_ms=cfg.transcribe.vad_min_silence_ms,
        initial_prompt=cfg.transcribe.initial_prompt or None,
        device=device,
        compute_type=cfg.runtime.effective_compute_type(device),
        on_progress=on_progress,
        cancel_event=cancel_event,
    )
    task.transcript_json = str(output.json)
    task.duration_sec = task.duration_sec or int(output.duration_sec)
    _set(task, store, TaskStatus.TRANSCRIBED, on_stage)

    if not cfg.download.keep_intermediate:
        for path in (Path(task.video_path), audio_path):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # noqa: BLE001 — 删不掉不影响产出
                logging.warning("删除中间文件失败：%s（%s）", path, exc)
        task.video_path = None
        task.audio_path = None
        store.update(task)


def run_frames(
    task: Task,
    cfg: Config,
    store: Store,
    *,
    on_progress: ProgressFn | None = None,
    on_stage: StageFn | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """抽帧 + 拼版（可选）：`transcribed → framing → done`。

    **本阶段是唯一一个把状态推到 `done` 的地方**。即使 `frames.enabled=false`
    也走这里——让「谁负责置 done」只有一个答案，代价是那一次空调用（微秒级）。
    """
    if not cfg.frames.enabled:
        _set(task, store, TaskStatus.DONE, on_stage)
        return

    if not task.video_path:
        logging.warning("任务 %s 无视频文件（keep_intermediate=false？），跳过抽帧", task.id)
        _set(task, store, TaskStatus.DONE, on_stage)
        return

    _set(task, store, TaskStatus.FRAMING, on_stage)
    try:
        frames_mod.run(
            task.video_path,
            Path(task.out_dir),  # type: ignore[arg-type]
            scene_threshold=cfg.frames.scene_threshold,
            max_frames=cfg.frames.max_frames,
            sheet_cols=cfg.frames.sheet_cols,
            sheet_rows=cfg.frames.sheet_rows,
            ffmpeg_path=cfg.paths.ffmpeg,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
    except TaskCancelled:
        raise
    except StageError as exc:
        # 画面是可选产物：失败只记 warning，不阻断（PLAN.md §4.1）
        logging.warning("任务 %s 抽帧失败，已跳过：%s", task.id, exc)

    _set(task, store, TaskStatus.DONE, on_stage)


# ---------------------------------------------------------------- 辅助


def _set(
    task: Task,
    store: Store,
    status: TaskStatus,
    on_stage: StageFn | None,
) -> None:
    store.set_status(task, status)
    logging.info("任务 %s → %s", task.id, status.value)
    if on_stage is not None:
        on_stage(task)


def _fail(task: Task, store: Store, message: str, on_stage: StageFn | None) -> None:
    store.set_status(task, TaskStatus.FAILED, message)
    if on_stage is not None:
        on_stage(task)


def _promote(task: Task, staging: Path, work: Path) -> Path:
    """把暂存目录改名为最终产出目录（同盘 rename，零拷贝）。

    目标目录名依赖标题，而标题只有下载完才知道——这就是分两步的原因。
    """
    target = work / task.dir_name()
    if target.exists():
        # 重跑：清掉上次的产出。目录必然在 work 下（由 dir_name 生成），
        # 但仍做一次前缀校验，避免任何情况下误删 work 之外的东西。
        if target.parent.resolve() == work.resolve():
            shutil.rmtree(target)
        else:
            raise StageError("download", f"拒绝删除 work 之外的目录：{target}")

    staging.rename(target)
    return target


def _find_video(out_dir: Path) -> Path | None:
    """在产出目录里找视频文件。

    yt-dlp 合并后固定产出 `video.mp4`；但只有单流可用时可能是 mkv/webm，
    所以按扩展名白名单扫描，并优先取名为 `video.*` 的那个。
    """
    from app.core.urls import VIDEO_SUFFIXES

    candidates = [
        p
        for p in out_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
    ]
    if not candidates:
        return None
    for path in candidates:
        if path.stem == "video":
            return path
    return max(candidates, key=lambda p: p.stat().st_size)


@lru_cache(maxsize=1)
def _cuda_device_present() -> bool:
    """硬件层面是否有可用的 CUDA 设备（快速判据，非真实验证）。

    真实可用性由 `env_probe.verify_cuda()` 决定。结果缓存：ctranslate2 的导入
    开销不值得每个任务付一次。
    """
    from app.core import env_probe

    return bool(env_probe.cuda_info().device_count)
