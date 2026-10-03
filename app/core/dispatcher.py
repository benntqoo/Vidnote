"""流水线调度器：有界队列 + 阶段线程池 + 并发/内存护栏。

拓扑（PLAN.md §3.1）：

    _pending ──▶ q_dl ──▶ q_asr ──▶ q_frame ──▶ 完成
               n_dl 线程   n_asr 线程   n_frame 线程
                 下载     抽音频+转写   抽帧+拼版
                          ★瓶颈，GPU 恒为 1★

**为什么不是「N 个线程各跑一条完整流程」**：那样下载会被转写拖住——一个 worker
在转写时，它这条链路上的下载与抽帧也停了。而转写独占 GPU 单卡（`n_asr=1`），
所以「N 个全流程 worker」实际只有 1 个在真正推进，其余全在等 GPU。
拆成阶段池后，下载/抽帧可以在转写进行时同步推进。

**为什么用有界队列（容量 2）**：防下载跑太快。若不限，100 条链接会在几秒内全部
落地，而转写要跑 4 小时——期间任何一次 OOM 或断电都得重来，磁盘空间也被吃光。
有界队列让下载自然地被转写的消费速度反向压住。

三个必须说清楚的语义（都对得上 `docs/IPC协议规格.md`）：

| 操作 | 语义 |
|---|---|
| `pause()` | **停止取新任务**。已在跑的阶段跑完为止；已排队但未开始的任务原地不动 |
| `cancel(id)` | 单条终止。排队中的直接摘除（不占名额）；运行中的 kill 子进程（转写只能在 segment 边界中断） |
| `stop()` | 优雅退出：停止取新任务 → 等在跑的完成（默认最长 30 s）→ 线程退出 |

失败分级（PLAN.md §4.1）与 `pipeline.run_one` 完全一致：

- `StageError` → 该条 `failed`，**其余任务照常**
- `TaskCancelled` → 该条 `cancelled`，**不计入失败数**
- `FatalEnvironmentError` → 该条 `failed` 且**中止整批**（环境坏了，继续跑只是耗一整夜）。
  剩余未开始的条目**保持 pending 不动**——它们没失败，重启后应当继续。
"""

from __future__ import annotations

import functools
import logging
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from app.core import env_probe, pipeline
from app.core.concurrency import Concurrency
from app.core.errors import FatalEnvironmentError, TaskCancelled, VidnoteError
from app.core.store import Store
from app.models.config import Config
from app.models.task import Task, TaskStatus

#: 有界队列容量（PLAN.md §3.1）。
QUEUE_CAPACITY = 2

#: 内存护栏阈值（PLAN.md §3.2「低于 2 GB 时暂停取新任务」）。
DEFAULT_MEMORY_FLOOR_GB = 2.0

#: 内存采样最小间隔。13 个线程各查一次就是 52 次/秒的系统调用，没必要。
MEMORY_SAMPLE_INTERVAL_SEC = 1.0

#: 进度限流（docs/IPC协议规格.md §4.1）：同一条最快 200 ms 一次，
#: 或进度绝对增量 ≥ 1% 时提前放行。
PROGRESS_MIN_INTERVAL_SEC = 0.2
PROGRESS_MIN_DELTA = 0.01

#: 闸门轮询间隔。**只在「内存不足」分支用**（需要定期复采样）。
#: 暂停与待命都是纯事件驱动：`resume()` / `submit()` / `stop()` / `_abort()` 都会 notify。
GATE_POLL_SEC = 0.25

#: 进度默认值：刚进入某阶段、真实进度还不知道（对应协议的 `-1.0`）。
PROGRESS_UNKNOWN = -1.0

#: 阶段名（用于 trace 与峰值并发统计）。取 TaskStatus 的英文值以便与协议对齐。
STAGE_DOWNLOAD = TaskStatus.DOWNLOADING.value
STAGE_ASR = TaskStatus.TRANSCRIBING.value
STAGE_FRAMES = TaskStatus.FRAMING.value


class State(str, Enum):
    """调度器状态。与 IPC 的 `pipeline_state` 事件取值一致。"""

    IDLE = "idle"
    """没有待取任务、没有在跑的任务（未启动，或本批已跑完，线程仍存活待命）。"""

    RUNNING = "running"
    PAUSED = "paused"
    ABORTED = "aborted"
    """因环境级故障中止整批（`FatalEnvironmentError`）。"""

    STOPPED = "stopped"


#: `(task, progress, elapsed_sec)` → 由调用方推 task_update。
TaskUpdateFn = Callable[[Task, float, "float | None"], None]
#: `(event_name, data)` → 批级事件（state / batch_done / batch_aborted / resource_warning）。
EventFn = Callable[[str, dict[str, Any]], None]


@dataclass
class StageSpan:
    """一次阶段执行的起止记录。

    存在的理由只有一个：**能证明「阶段确实重叠」而不是串行**。
    PLAN.md §11.2 把「流水线架构」列为设计推断、未经实测，验收要求就是证伪它。
    光看总耗时证不了——总耗时短也可能只是「每条都很短」。看区间重叠才成立。
    """

    task_id: int
    stage: str
    started: float
    ended: float
    ok: bool

    @property
    def seconds(self) -> float:
        return self.ended - self.started

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "stage": self.stage,
            "started": round(self.started, 3),
            "ended": round(self.ended, 3),
            "seconds": round(self.seconds, 3),
            "ok": self.ok,
        }


class Dispatcher:
    """任务调度器。线程安全，CLI 与 RPC 两个入口共用。"""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        conc: Concurrency,
        *,
        on_task_update: TaskUpdateFn | None = None,
        on_event: EventFn | None = None,
        memory_floor_gb: float = DEFAULT_MEMORY_FLOOR_GB,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.conc = conc
        self.on_task_update = on_task_update
        self.on_event = on_event
        self.memory_floor_gb = memory_floor_gb

        #: 设备只解析一次：`current_device()` 会导入 ctranslate2，不该每条付一次。
        self._device = pipeline.current_device(cfg)

        self._cv = threading.Condition()
        self._pending: list[Task] = []
        self._inflight = 0
        self._active: set[int] = set()
        self._state = State.IDLE
        self._stop = False
        self._abort_reason: str | None = None

        self._cancel_events: dict[int, threading.Event] = {}

        #: 阶段间有界队列。**下载阶段的输入队列就是 `_pending` 本身**——
        #: 它在 `_cv` 保护下由下载线程直接取，不需要再多一个队列加一个搬运线程。
        #: 背压仍然成立：下载线程取到任务后要往 `_q_asr` 放，队列满就在这里阻塞，
        #: 于是 `_pending` 的消费速度被转写速度反向钳住。
        self._q_asr: queue.Queue[Task] = queue.Queue(maxsize=QUEUE_CAPACITY)
        self._q_frame: queue.Queue[Task] = queue.Queue(maxsize=QUEUE_CAPACITY)

        self._threads: list[threading.Thread] = []

        # 进度限流与耗时统计
        self._progress_lock = threading.Lock()
        self._progress: dict[int, tuple[float, float]] = {}
        self._started_at: dict[int, float] = {}

        # 内存采样缓存（见 _memory_ok）
        self._mem_lock = threading.Lock()
        self._mem_sampled_at = 0.0
        self._mem_ok = True
        self._mem_blocked = False

        #: 阶段起止记录，验收用（见 StageSpan docstring）。
        self.trace: list[StageSpan] = []
        self._trace_lock = threading.Lock()

        self._fn_dl = functools.partial(pipeline.run_download, cfg=cfg, store=store)
        self._fn_asr = functools.partial(
            pipeline.run_asr, cfg=cfg, store=store, device=self._device
        )
        self._fn_frame = functools.partial(pipeline.run_frames, cfg=cfg, store=store)

    # ------------------------------------------------------------------ 控制

    def submit(self, tasks: Iterable[Task]) -> int:
        """把任务加入待取队列。返回实际入队条数。

        跳过三类：已完成、已在跑、已排队。**不跳过 failed / cancelled**——
        调用方要重跑就显式把它们放进来（或走 `retry()`）。
        """
        added = 0
        with self._cv:
            if self._abort_reason is not None:
                logging.error("调度器已因环境故障中止，拒绝接收新任务：%s", self._abort_reason)
                return 0
            for task in tasks:
                if task.id is None or task.status == TaskStatus.DONE.value:
                    continue
                if task.id in self._active or any(t.id == task.id for t in self._pending):
                    continue
                task.set_status(TaskStatus.PENDING)
                self.store.update(task)
                self._pending.append(task)
                added += 1
            if added:
                self._state = State.RUNNING
                self._cv.notify_all()
        if added:
            logging.info("调度器接收 %d 条任务（待取队列现 %d 条）", added, len(self._pending))
        return added

    def start(self) -> bool:
        """启动线程池。幂等：已在跑则只保证状态为 RUNNING，返回 False。"""
        with self._cv:
            if any(t.is_alive() for t in self._threads):
                if self._abort_reason is None and self._state is not State.STOPPED:
                    self._state = State.RUNNING
                    self._cv.notify_all()
                return False

            self._stop = False
            self._abort_reason = None
            self._state = State.RUNNING
            self._threads = []
            for index in range(max(1, self.conc.n_dl)):
                self._threads.append(
                    self._spawn(
                        f"dl-{index}",
                        STAGE_DOWNLOAD,
                        self._take_pending,
                        self._fn_dl,
                        self._q_asr,
                    )
                )
            for index in range(max(1, self.conc.n_asr)):
                self._threads.append(
                    self._spawn(
                        f"asr-{index}",
                        STAGE_ASR,
                        functools.partial(self._take_queue, self._q_asr),
                        self._fn_asr,
                        self._q_frame,
                    )
                )
            for index in range(max(1, self.conc.n_frame)):
                self._threads.append(
                    self._spawn(
                        f"frame-{index}",
                        STAGE_FRAMES,
                        functools.partial(self._take_queue, self._q_frame),
                        self._fn_frame,
                        None,
                    )
                )
            self._cv.notify_all()

        logging.info("调度器启动：%d 个线程，并发 %s", len(self._threads), self.conc.to_dict())
        self._emit_event("state", {"state": self._state.value})
        return True

    def pause(self) -> bool:
        """暂停取新任务。已在跑的阶段跑完为止（`docs/IPC协议规格.md` §3）。"""
        with self._cv:
            if self._state is not State.RUNNING:
                return False
            self._state = State.PAUSED
            self._cv.notify_all()
        logging.info("调度器已暂停（在跑的任务跑完当前阶段为止）")
        self._emit_event("state", {"state": self._state.value})
        return True

    def resume(self) -> bool:
        with self._cv:
            if self._state is not State.PAUSED:
                return False
            # 恢复时若其实没活可干，直接回到 IDLE，不留下一个假的「运行中」
            self._state = State.RUNNING if self._pending or self._inflight else State.IDLE
            self._cv.notify_all()
        logging.info("调度器已恢复（%s）", self._state.value)
        self._emit_event("state", {"state": self._state.value})
        return True

    def cancel(self, task_id: int) -> bool:
        """取消单条。返回是否被接受。

        - **排队中**（还没被任何阶段取走）→ 直接摘除并置 `cancelled`，不占名额
        - **运行中** → 置位该任务的 `cancel_event`，阶段函数会 kill 子进程并抛
          `TaskCancelled`（转写只能在 segment 边界中断，见 `transcriber.run`）
        - **已完成 / 已取消** → 幂等返回
        - **已失败且不在队列里** → 拒绝（没有可取消的东西）
        """
        with self._cv:
            task = self.store.get(task_id)
            if task is None:
                return False
            if task.status == TaskStatus.CANCELLED.value:
                return True  # 幂等
            if task.status == TaskStatus.DONE.value:
                return False

            before = len(self._pending)
            self._pending = [t for t in self._pending if t.id != task_id]
            removed_from_queue = len(self._pending) != before
            active = task_id in self._active

            if not removed_from_queue and not active:
                return False  # 例如 failed 且早已离队，无可取消

            event = self._cancel_events.get(task_id)
            if event is None:
                event = threading.Event()
                self._cancel_events[task_id] = event
            event.set()
            self._cv.notify_all()

        if removed_from_queue:
            # 还没进任何阶段：不必等阶段函数自己发现，直接标记。
            # 注意不释放 inflight——排队中的任务从未计入。
            self._mark_cancelled(task)
        else:
            logging.info("任务 %s 已请求取消（等待当前阶段中断）", task_id)
        return True

    def retry(self, task_id: int) -> bool:
        """重置失败/取消项并重新入队。断点续跑由 pipeline 各阶段自己判断。"""
        with self._cv:
            task = self.store.get(task_id)
            if task is None or task.status == TaskStatus.DONE.value:
                return False
            if task.id in self._active or any(t.id == task.id for t in self._pending):
                return False
            self._cancel_events.pop(task_id, None)
            task.retry_count += 1
            task.set_status(TaskStatus.PENDING)
            self.store.update(task)
            self._pending.append(task)
            if self._abort_reason is None:
                self._state = State.RUNNING
            self._cv.notify_all()
        logging.info("任务 %s 重新入队（第 %d 次重试）", task_id, task.retry_count)
        return True

    def stop(self, timeout: float = 30.0) -> bool:
        """优雅退出：停止取新任务 → 等在跑的完成 → 线程退出。返回是否全部退出。

        线程在循环顶部才检查 `_stop`，所以**在跑的阶段会先跑完**——这正是
        「优雅」的含义，不需要额外的 drain 逻辑。
        """
        with self._cv:
            self._stop = True
            self._state = State.STOPPED
            self._cv.notify_all()

        deadline = time.monotonic() + timeout
        for thread in self._threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

        alive = [t.name for t in self._threads if t.is_alive()]
        self._threads = []
        if alive:
            logging.warning("调度器停止超时，仍有线程存活：%s", alive)
        else:
            logging.info("调度器已停止")
        return not alive

    def wait(self, timeout: float | None = None) -> bool:
        """等本批跑完。返回 True=跑完；False=超时、被中止，或没有线程能消费队列。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._cv:
                if self._abort_reason is not None:
                    return False
                if not self._pending and self._inflight == 0:
                    return True
                if not any(t.is_alive() for t in self._threads):
                    return False  # 未 start，或线程已退出——没人会消费了
                self._cv.wait(0.5)
            if deadline is not None and time.monotonic() >= deadline:
                return False

    # ------------------------------------------------------------------ 查询

    @property
    def state(self) -> State:
        return self._state

    @property
    def abort_reason(self) -> str | None:
        return self._abort_reason

    def snapshot(self) -> dict[str, Any]:
        with self._cv:
            return {
                "state": self._state.value,
                "pending": len(self._pending),
                "inflight": self._inflight,
                "queued": {
                    # 下载阶段的输入队列就是 _pending（见 __init__ 注释）
                    "download": len(self._pending),
                    "asr": self._q_asr.qsize(),
                    "frames": self._q_frame.qsize(),
                },
                "counts": self.store.count_by_status(),
                "concurrency": self.conc.to_dict(),
                "memory_blocked": self._mem_blocked,
                "abort_reason": self._abort_reason,
            }

    def peak_concurrency(self) -> dict[str, int]:
        """各阶段出现过的最大并行数。验收用：能证明池子真的在并行。"""
        peaks: dict[str, int] = {}
        for stage in (STAGE_DOWNLOAD, STAGE_ASR, STAGE_FRAMES):
            events: list[tuple[float, int]] = []
            for span in list(self.trace):
                if span.stage != stage:
                    continue
                events.append((span.started, 1))
                events.append((span.ended, -1))
            # 同一时刻先算结束再算开始，避免把「刚结束」与「刚开始」算成重叠
            events.sort(key=lambda item: (item[0], item[1]))
            current = peak = 0
            for _, delta in events:
                current += delta
                peak = max(peak, current)
            peaks[stage] = peak
        return peaks

    # ------------------------------------------------------------------ 内部：线程

    def _spawn(
        self,
        name: str,
        stage: str,
        take_fn: Callable[[float], "Task | None"],
        stage_fn: Callable[..., None],
        q_out: "queue.Queue[Task] | None",
    ) -> threading.Thread:
        thread = threading.Thread(
            target=self._worker_loop,
            args=(name, stage, take_fn, stage_fn, q_out),
            name=name,
            daemon=True,
        )
        thread.start()
        return thread

    def _worker_loop(
        self,
        name: str,
        stage: str,
        take_fn: Callable[[float], "Task | None"],
        stage_fn: Callable[..., None],
        q_out: "queue.Queue[Task] | None",
    ) -> None:
        while not self._stop and self._state is not State.ABORTED:
            if not self._intake_open():
                # 暂停 / 内存不足：不取新任务，留在循环里等闸门重开。
                # 暂停与待命是纯事件驱动（resume/submit/stop/_abort 都会 notify）；
                # 只有内存分支需要超时，好定期复采样。
                with self._cv:
                    self._cv.wait(GATE_POLL_SEC if self._mem_blocked else None)
                continue

            task = take_fn(GATE_POLL_SEC)
            if task is None:
                continue

            ok = self._execute(task, stage, stage_fn)
            if not ok:
                self._release(task)  # 失败 / 取消：这条到此为止
            elif q_out is None:
                self._release(task)  # 终端阶段成功
            elif not self._put(q_out, task):
                self._release(task)  # 正在停止，交不出去

        logging.debug("线程 %s 退出", name)

    def _take_pending(self, timeout: float) -> Task | None:
        """下载阶段的取任务：从 `_pending` 摘一条并计入在手数。

        「摘除 + 计数」必须在同一个锁里——否则 `cancel()` 可能刚判定「还在队列里」
        而这里已经把它取走，出现「标记成 cancelled 了却还在跑」。
        """
        with self._cv:
            if not self._pending:
                # 有事件会唤醒（submit / stop / _release），这里是待命而非忙等
                self._cv.wait(timeout)
                return None
            task = self._pending.pop(0)
            self._inflight += 1
            return task

    @staticmethod
    def _take_queue(q: "queue.Queue[Task]", timeout: float) -> Task | None:
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _intake_open(self) -> bool:
        """是否允许取新任务。

        ⚠️ 这里**不能写成 `if self._mem_blocked: return False`**——那样一旦被挡住
        就再也走不到 `_memory_ok()`，采样停止 → 永久卡死。
        记忆点是「上次采样结论」，复检必须每次都真的调用采样函数（它自带 1 秒缓存）。
        这是自测用例 7 抓出来的。
        """
        if self._state is not State.RUNNING:
            return False
        return self._memory_ok()

    def _put(self, q: "queue.Queue[Task]", task: Task) -> bool:
        """带停止感知的入队（有界队列满时阻塞）。"""
        while not self._stop:
            try:
                q.put(task, timeout=GATE_POLL_SEC)
                return True
            except queue.Full:
                continue
        return False

    def _release(self, task: Task) -> None:
        finished = False
        with self._cv:
            if task.id is not None:
                self._active.discard(task.id)
                self._cancel_events.pop(task.id, None)
                self._started_at.pop(task.id, None)
            self._inflight = max(0, self._inflight - 1)
            if self._inflight == 0 and not self._pending and self._state is State.RUNNING:
                self._state = State.IDLE
                finished = True
            self._cv.notify_all()
        if finished:
            self._emit_event("batch_done", {"counts": self.store.count_by_status()})

    # ------------------------------------------------------------------ 内部：执行

    def _execute(self, task: Task, stage: str, stage_fn: Callable[..., None]) -> bool:
        """跑一个阶段。返回 True=成功（应交给下一阶段），False=终止该任务。"""
        if task.id is None:
            return False

        with self._cv:
            self._active.add(task.id)
            self._started_at.setdefault(task.id, time.monotonic())
            cancel_event = self._cancel_events.get(task.id)
            if cancel_event is None:
                # ⚠️ 事件必须在**阶段开始前**就存在。
                # 阶段函数一旦启动，它持有的事件对象就固定了；若此处给 None，
                # 那么「阶段跑到一半时调用 cancel()」只会新建一个事件，正在跑的那个
                # 阶段永远看不到它——表现就是「点了取消，却一直等到阶段自然跑完」。
                # 这正是自测用例 4 抓出来的（elapsed 1.019s == 阶段全长）。
                cancel_event = threading.Event()
                self._cancel_events[task.id] = cancel_event

        if cancel_event.is_set():
            # 排队期间就被取消了：不必真跑一遍（否则白起一次 ffmpeg / yt-dlp）
            self._mark_cancelled(task)
            return False

        started = time.monotonic()
        ok = True
        try:
            stage_fn(
                task,
                on_progress=self._progress_cb(task),
                on_stage=self._stage_cb(task),
                cancel_event=cancel_event,
            )
        except TaskCancelled:
            self._mark_cancelled(task)
            ok = False
        except FatalEnvironmentError as exc:
            self._mark_failed(task, str(exc))
            self._abort(str(exc))
            ok = False
        except VidnoteError as exc:
            logging.warning("任务 %s 失败：%s", task.id, exc)
            self._mark_failed(task, str(exc))
            ok = False
        except Exception as exc:  # noqa: BLE001 — 未预期异常同样只影响单条
            logging.exception("任务 %s 未预期失败", task.id)
            self._mark_failed(task, f"{type(exc).__name__}: {exc}")
            ok = False
        finally:
            with self._trace_lock:
                self.trace.append(
                    StageSpan(
                        task_id=task.id,
                        stage=stage,
                        started=started,
                        ended=time.monotonic(),
                        ok=ok,
                    )
                )

        self._emit_update(task)
        return ok

    def _abort(self, reason: str) -> None:
        """环境级故障 → 中止整批。剩余未开始的条目保持 pending 不动。"""
        with self._cv:
            if self._abort_reason is not None:
                return
            self._abort_reason = reason
            self._state = State.ABORTED
            self._cv.notify_all()
        logging.error("整批中止：%s", reason)
        self._emit_event("batch_aborted", {"reason": reason})

    # ------------------------------------------------------------------ 内部：状态与回调

    def _mark_failed(self, task: Task, message: str) -> None:
        self.store.set_status(task, TaskStatus.FAILED, message)

    def _mark_cancelled(self, task: Task) -> None:
        self.store.set_status(task, TaskStatus.CANCELLED, "已取消")
        logging.info("任务 %s 已取消", task.id)
        self._emit_update(task)

    def _progress_cb(self, task: Task) -> Callable[[str, float], None]:
        def _callback(stage: str, ratio: float) -> None:
            if self._should_emit(task, ratio):
                self._emit_update(task, progress=ratio)

        return _callback

    def _stage_cb(self, task: Task) -> Callable[[Task], None]:
        def _callback(updated: Task) -> None:
            # 状态跃迁立即推送（不限流）。进度退回「未知」，等第一个 on_progress 修正。
            self._emit_update(updated, progress=self._default_progress(updated))

        return _callback

    def _should_emit(self, task: Task, ratio: float) -> bool:
        """进度限流（docs/IPC协议规格.md §4.1）。"""
        key = task.id if task.id is not None else -1
        now = time.monotonic()
        with self._progress_lock:
            last_time, last_ratio = self._progress.get(key, (0.0, -2.0))
            if (
                now - last_time < PROGRESS_MIN_INTERVAL_SEC
                and abs(ratio - last_ratio) < PROGRESS_MIN_DELTA
            ):
                return False
            self._progress[key] = (now, ratio)
            return True

    def _emit_update(self, task: Task, progress: float | None = None) -> None:
        if self.on_task_update is None:
            return
        ratio = progress if progress is not None else self._default_progress(task)
        started = self._started_at.get(task.id if task.id is not None else -1)
        elapsed = None if started is None else round(time.monotonic() - started, 3)
        try:
            self.on_task_update(task, ratio, elapsed)
        except Exception:  # noqa: BLE001 — 回调失败不能拖垮调度
            logging.exception("task_update 回调异常（task_id=%s）", task.id)

    def _emit_event(self, name: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(name, data)
        except Exception:  # noqa: BLE001
            logging.exception("事件回调异常（%s）", name)

    @staticmethod
    def _default_progress(task: Task) -> float:
        """状态跃迁时的进度默认值。语义见 docs/IPC协议规格.md §5。"""
        status = task.status
        if status in (TaskStatus.DONE.value, TaskStatus.DOWNLOADED.value, TaskStatus.TRANSCRIBED.value):
            return 1.0
        if status == TaskStatus.PENDING.value:
            return 0.0
        return PROGRESS_UNKNOWN

    # ------------------------------------------------------------------ 内部：内存护栏

    def _memory_ok(self) -> bool:
        """可用内存是否够开新任务。

        **事件驱动**而非定时轮询（PLAN.md §3.2）：采样点就是「准备取新任务」这一
        时刻，判据是当时的真实可用内存；被挡住后由 `Condition` 在任务完成时唤醒
        复检，没有独立的 sleep 轮询循环。代价是每个线程每轮都要问一次，所以加了
        一层 1 秒缓存。

        读不到内存时**放行**：`avail_gb is None` 只说明这次探测拿不到数（非
        Windows、API 失败），不等于没内存。按「未知 → 可用」处理，避免因探测失败
        把整个调度器锁死。
        """
        now = time.monotonic()
        with self._mem_lock:
            if now - self._mem_sampled_at < MEMORY_SAMPLE_INTERVAL_SEC:
                return self._mem_ok
            self._mem_sampled_at = now
            avail = env_probe.memory_info().avail_gb
            ok = avail is None or avail >= self.memory_floor_gb
            self._mem_ok = ok

            if ok and self._mem_blocked:
                self._mem_blocked = False
                logging.info("可用内存恢复至 %.1f GB，继续取新任务", avail or -1.0)
                with self._cv:
                    self._cv.notify_all()
            elif not ok and not self._mem_blocked:
                self._mem_blocked = True
                logging.warning(
                    "可用内存 %.1f GB < %.1f GB，暂停取新任务（在跑的跑完为止）",
                    avail or -1.0,
                    self.memory_floor_gb,
                )
                self._emit_event(
                    "resource_warning",
                    {"kind": "memory", "avail_gb": avail, "floor_gb": self.memory_floor_gb},
                )
            return ok
