"""调度器自测：用假阶段函数验证调度语义，**不需要 GPU，几秒跑完**。

为什么要有它：阶段 2 的正式验收（10 条批量 + 杀进程 + 坏链接）跑一次是分钟级、
且必须占用 GPU。而调度器最容易出错的恰恰是低频路径——暂停、取消、致命故障中止、
内存护栏。这些用真流水线测一次要几十秒，还容易因机器状态而不稳定；换成假阶段
后每次都是毫秒级确定性。

**做法**：在构造 `Dispatcher` 之前替换掉 `pipeline` 模块里那三个阶段函数。
Dispatcher 在 `__init__` 里用 `functools.partial` 绑定它们，所以替换模块属性
即可生效，不需要给生产代码开测试后门。

用法：`python tools/dispatcher_selftest.py`
"""

from __future__ import annotations

import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import dispatcher as dispatcher_mod  # noqa: E402
from app.core import pipeline  # noqa: E402
from app.core.concurrency import Concurrency  # noqa: E402
from app.core.dispatcher import Dispatcher, State  # noqa: E402
from app.core.errors import FatalEnvironmentError, StageError, TaskCancelled  # noqa: E402
from app.core.store import Store  # noqa: E402
from app.models.config import Config  # noqa: E402
from app.models.task import Task, TaskStatus  # noqa: E402

#: 假阶段耗时（秒）。足够长以暴露竞态，足够短以让整套跑完在 10 秒内。
DELAY_DL = 0.05
DELAY_ASR = 0.15
DELAY_FRAMES = 0.02

CONC = Concurrency(n_asr=1, n_dl=4, n_frame=2, n_sum=1)

#: 用例失败时若没走到 store.close()，Windows 会因文件被占用而删不掉临时目录。
#: 注册表 + main() 的 finally 兜底，避免一个判据失败就把后面 10 个用例带崩。
_OPEN_STORES: list[Store] = []


def tempdir() -> tempfile.TemporaryDirectory:
    # ignore_cleanup_errors：真的删不掉也别让用例挂掉，主判据比清理重要
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  PASS  {name}" + (f"  — {detail}" if detail else ""))
        else:
            self.failed.append(name)
            print(f"  FAIL  {name}  — {detail}")
        return ok

    def report(self, title: str) -> None:
        total = self.passed + len(self.failed)
        print(f"\n{title}: {self.passed}/{total} 通过")
        for name in self.failed:
            print(f"  ✗ {name}")


# ---------------------------------------------------------------- 假阶段


class FakeWorld:
    """假阶段的共享状态：哪些 task_id 要失败、实际执行过的阶段。"""

    def __init__(self) -> None:
        self.fail_download: set[int] = set()
        self.fatal_download: set[int] = set()
        self.cancelled_seen: list[int] = []
        self.lock = threading.Lock()


def _sleep_interruptible(seconds: float, cancel_event: threading.Event | None) -> None:
    """按 10 ms 粒度睡，期间响应取消——模拟阶段函数里的真实检查点。"""
    deadline = time.monotonic() + seconds
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.01, remaining))


def build_fakes(world: FakeWorld, store: Store):
    """构造三个假阶段函数。签名与真阶段一致（关键字参数）。

    `**_ignored` 用来吞掉 Dispatcher 用 `functools.partial` 预绑定的
    `cfg` / `store` / `device`——真实阶段的签名里有它们，假阶段不需要。
    """

    def fake_download(task, *, on_progress=None, on_stage=None, cancel_event=None, **_ignored):
        _set(task, store, TaskStatus.DOWNLOADING, on_stage)
        if on_progress:
            on_progress("downloading", 0.5)
        _sleep_interruptible(DELAY_DL, cancel_event)
        with world.lock:
            fatal = task.id in world.fatal_download
            fail = task.id in world.fail_download
        if fatal:
            raise FatalEnvironmentError("download", "假的 CUDA 故障")
        if fail:
            raise StageError("download", "假的坏链接")
        _set(task, store, TaskStatus.DOWNLOADED, on_stage)

    def fake_asr(task, *, on_progress=None, on_stage=None, cancel_event=None, **_ignored):
        _set(task, store, TaskStatus.TRANSCRIBING, on_stage)
        try:
            _sleep_interruptible(DELAY_ASR, cancel_event)
        except TaskCancelled:
            with world.lock:
                world.cancelled_seen.append(task.id)
            raise
        if on_progress:
            on_progress("transcribing", 1.0)
        _set(task, store, TaskStatus.TRANSCRIBED, on_stage)

    def fake_frames(task, *, on_progress=None, on_stage=None, cancel_event=None, **_ignored):
        _set(task, store, TaskStatus.FRAMING, on_stage)
        _sleep_interruptible(DELAY_FRAMES, cancel_event)
        _set(task, store, TaskStatus.DONE, on_stage)

    return fake_download, fake_asr, fake_frames


def _set(task: Task, store: Store, status: TaskStatus, on_stage) -> None:
    store.set_status(task, status)
    if on_stage is not None:
        on_stage(task)


# ---------------------------------------------------------------- 环境


def make_env(tmp: Path, n: int, world: FakeWorld):
    cfg = Config.from_dict({}, root=tmp)
    cfg.paths.work_dir = str(tmp / "work")
    store = Store(tmp / "state.db")
    _OPEN_STORES.append(store)
    tasks = []
    for i in range(n):
        task = Task(url=f"file:///fake/clip_{i:02d}.mp4", platform="local", title=f"clip{i:02d}")
        stored, _ = store.add(task)
        tasks.append(stored)
    return cfg, store, tasks


def install_fakes(world: FakeWorld, store: Store) -> None:
    pipeline.run_download, pipeline.run_asr, pipeline.run_frames = build_fakes(world, store)


def counts(store: Store) -> dict[str, int]:
    return store.count_by_status()


# ---------------------------------------------------------------- 判据


def cross_stage_overlap(trace, a: str, b: str) -> bool:
    """是否存在 a、b 两个阶段来自**不同任务**且时间区间重叠。

    这才是「流水线」的证据。只看总耗时证不了——总耗时短也可能只是每条都短。
    """
    spans_a = [s for s in trace if s.stage == a]
    spans_b = [s for s in trace if s.stage == b]
    for x in spans_a:
        for y in spans_b:
            if x.task_id == y.task_id:
                continue
            if x.started < y.ended and y.started < x.ended:
                return True
    return False


def stage_serial_max(trace, stage: str) -> int:
    events: list[tuple[float, int]] = []
    for span in trace:
        if span.stage == stage:
            events.append((span.started, 1))
            events.append((span.ended, -1))
    events.sort(key=lambda item: (item[0], item[1]))
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


# ---------------------------------------------------------------- 用例


def case_batch(c: Checker) -> None:
    """判据 1：批量跑完 + 阶段真并行 + 转写恒串行 + 跨阶段重叠。"""
    print("\n[1] 批量完成 / 并行度 / 重叠（20 条）")
    with tempdir() as tmp:
        tmp_path = Path(tmp)
        world = FakeWorld()
        cfg, store, tasks = make_env(tmp_path, 20, world)
        install_fakes(world, store)

        d = Dispatcher(cfg, store, CONC, memory_floor_gb=0.0)
        d.submit(tasks)
        d.start()
        done = d.wait(timeout=60)
        d.stop(timeout=10)

        got = counts(store)
        c.check("20 条全部 done", done and got.get("done") == 20, f"counts={got}")
        peaks = d.peak_concurrency()
        c.check(
            "转写峰值并发 == 1（GPU 单卡串行）",
            peaks[dispatcher_mod.STAGE_ASR] == 1,
            f"peak={peaks}",
        )
        c.check(
            "下载峰值并发 >= 2（池子真并行）",
            peaks[dispatcher_mod.STAGE_DOWNLOAD] >= 2,
            f"peak={peaks}",
        )
        c.check(
            "下载 与 转写 跨任务重叠（证明是流水线而非串行）",
            cross_stage_overlap(d.trace, dispatcher_mod.STAGE_DOWNLOAD, dispatcher_mod.STAGE_ASR),
        )
        c.check(
            "转写 与 抽帧 跨任务重叠",
            cross_stage_overlap(d.trace, dispatcher_mod.STAGE_ASR, dispatcher_mod.STAGE_FRAMES),
        )
        if d.trace:
            serial = sum(s.seconds for s in d.trace)
            wall = max(s.ended for s in d.trace) - min(s.started for s in d.trace)
            print(f"        阶段总耗时 {serial:.2f}s / 墙钟 {wall:.2f}s（比值 {serial / wall:.2f}×，越大越说明重叠）")
        store.close()


def case_pause(c: Checker) -> None:
    """判据 2：暂停后不再开始新阶段；在跑的跑完当前阶段；恢复后继续。"""
    print("\n[2] 暂停 / 恢复")
    with tempdir() as tmp:
        tmp_path = Path(tmp)
        world = FakeWorld()
        cfg, store, tasks = make_env(tmp_path, 8, world)
        install_fakes(world, store)

        d = Dispatcher(cfg, store, CONC, memory_floor_gb=0.0)
        d.submit(tasks)
        d.start()
        time.sleep(0.08)  # 让第一批进入阶段
        c.check("pause() 被接受", d.pause())
        c.check("重复 pause() 返回 False（状态机不回绕）", not d.pause())
        c.check("状态为 paused", d.state is State.PAUSED)

        # 「在跑的跑完当前阶段为止」：spans 会继续增加一会儿，然后停住
        time.sleep(DELAY_ASR * 3)
        with d._trace_lock:
            after_settle = len(d.trace)
        time.sleep(DELAY_ASR * 2)
        with d._trace_lock:
            later = len(d.trace)
        c.check(
            "暂停后阶段数不再增长（在跑的已跑完）",
            later == after_settle,
            f"{after_settle} → {later}",
        )
        pending_left = d.snapshot()["pending"] + d.snapshot()["inflight"]
        c.check("仍有任务未处理（说明确实停住了）", pending_left > 0, f"剩余 {pending_left}")

        c.check("resume() 被接受", d.resume())
        c.check("恢复后跑完", d.wait(timeout=60) and counts(store).get("done") == 8, f"{counts(store)}")
        d.stop(timeout=10)
        store.close()


def case_cancel_queued(c: Checker) -> None:
    """判据 3：取消**排队中**的任务 —— 立即生效、不产生阶段执行。"""
    print("\n[3] 取消排队中的任务")
    with tempdir() as tmp:
        tmp_path = Path(tmp)
        world = FakeWorld()
        cfg, store, tasks = make_env(tmp_path, 12, world)
        install_fakes(world, store)

        d = Dispatcher(cfg, store, CONC, memory_floor_gb=0.0)
        d.submit(tasks)
        d.start()
        target = tasks[-1].id  # 队尾，n_dl=4 时短时间轮不到它
        c.check("cancel(排队中) 被接受", d.cancel(target))
        c.check(
            "cancel() 幂等",
            d.cancel(target),
        )
        d.wait(timeout=60)
        d.stop(timeout=10)

        got = counts(store)
        c.check(
            "该条状态为 cancelled",
            store.get(target).status == TaskStatus.CANCELLED.value,
            store.get(target).status,
        )
        c.check(
            "取消不计入失败数",
            got.get("failed", 0) == 0,
            f"counts={got}",
        )
        c.check("其余 11 条正常完成", got.get("done") == 11, f"counts={got}")
        spans = [s for s in d.trace if s.task_id == target]
        c.check("被取消的任务没有产生任何阶段执行", not spans, f"spans={len(spans)}")
        store.close()


def case_cancel_running(c: Checker) -> None:
    """判据 4：取消**运行中**的任务 —— 阶段函数内中断，其余任务不受影响。"""
    print("\n[4] 取消运行中的任务")
    with tempdir() as tmp:
        tmp_path = Path(tmp)
        world = FakeWorld()
        cfg, store, tasks = make_env(tmp_path, 6, world)
        install_fakes(world, store)
        # 把转写拉长，保证取消请求到达时它还在转写中
        global DELAY_ASR
        original = DELAY_ASR
        DELAY_ASR = 1.0
        try:
            d = Dispatcher(cfg, store, CONC, memory_floor_gb=0.0)
            d.submit(tasks)
            d.start()
            # 必须等到目标**已经进入转写阶段**再取消，否则取消会落在「排队中」
            # 分支（阶段入口处直接标记），测不到阶段函数内部的检查点。
            target = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                for t in tasks:
                    if store.get(t.id).status == TaskStatus.TRANSCRIBING.value:
                        target = t.id
                        break
                if target is not None:
                    break
                time.sleep(0.02)

            c.check("有任务已进入转写阶段", target is not None, f"target={target}")
            if target is not None:
                t0 = time.monotonic()
                c.check("cancel(运行中) 被接受", d.cancel(target))
                # 等它变 cancelled（假阶段每 10 ms 检查一次取消）
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if store.get(target).status == TaskStatus.CANCELLED.value:
                        break
                    time.sleep(0.02)
                elapsed = time.monotonic() - t0
                c.check(
                    "运行中的任务被中断",
                    store.get(target).status == TaskStatus.CANCELLED.value,
                    f"status={store.get(target).status}",
                )
                c.check(
                    "中断延迟 < 1.0 秒（检查点生效，没等阶段跑完）",
                    elapsed < 1.0,
                    f"{elapsed:.3f}s（该阶段总时长 {DELAY_ASR}s）",
                )
                c.check(
                    "阶段函数内部确实感知到取消（不是靠阶段入口拦下的）",
                    target in world.cancelled_seen,
                    f"cancelled_seen={world.cancelled_seen}",
                )

            d.wait(timeout=60)
            d.stop(timeout=10)
            got = counts(store)
            c.check("其余 5 条正常完成", got.get("done") == 5, f"counts={got}")
            c.check("失败数为 0（取消不算失败）", got.get("failed", 0) == 0, f"counts={got}")
        finally:
            DELAY_ASR = original
            store.close()


def case_bad_link(c: Checker) -> None:
    """判据 5：坏链接只影响自己（PLAN §8 阶段 2 第 3 条）。"""
    print("\n[5] 坏链接不影响其他任务")
    with tempdir() as tmp:
        tmp_path = Path(tmp)
        world = FakeWorld()
        cfg, store, tasks = make_env(tmp_path, 10, world)
        bad = tasks[4].id
        world.fail_download.add(bad)
        install_fakes(world, store)

        d = Dispatcher(cfg, store, CONC, memory_floor_gb=0.0)
        d.submit(tasks)
        d.start()
        done = d.wait(timeout=60)
        d.stop(timeout=10)

        got = counts(store)
        c.check("批次正常结束（未被单条失败中止）", done, f"state={d.state}")
        c.check("坏链接为 failed", store.get(bad).status == TaskStatus.FAILED.value)
        c.check(
            "错误信息含阶段与原因",
            "假的坏链接" in (store.get(bad).stage_error or ""),
            store.get(bad).stage_error or "",
        )
        c.check("其余 9 条 done", got.get("done") == 9, f"counts={got}")
        c.check("无任务被中止在非终态", got.get("failed") == 1, f"counts={got}")
        store.close()


def case_fatal(c: Checker) -> None:
    """判据 6：环境级故障中止整批，剩余条目保持 pending（不误标 failed）。"""
    print("\n[6] 环境级故障 → 中止整批")
    with tempdir() as tmp:
        tmp_path = Path(tmp)
        world = FakeWorld()
        cfg, store, tasks = make_env(tmp_path, 12, world)
        fatal = tasks[0].id
        world.fatal_download.add(fatal)
        install_fakes(world, store)

        d = Dispatcher(cfg, store, CONC, memory_floor_gb=0.0)
        d.submit(tasks)
        d.start()
        ok = d.wait(timeout=60)
        # 状态必须在 stop() 之前读——stop() 会把它改成 stopped
        state_after = d.state
        reason = d.abort_reason
        d.stop(timeout=10)

        got = counts(store)
        c.check("wait() 返回 False（被中止）", not ok)
        c.check("状态为 aborted", state_after is State.ABORTED, state_after.value)
        c.check("中止原因已记录", bool(reason), reason or "")
        c.check("故障条为 failed", store.get(fatal).status == TaskStatus.FAILED.value)
        c.check(
            "其余任务未被误标 failed",
            got.get("failed", 0) == 1,
            f"counts={got}（其余应留在 pending，重启后继续）",
        )
        c.check(
            "剩余条目保持 pending",
            got.get("pending", 0) >= 1,
            f"counts={got}",
        )
        c.check("中止后拒绝接收新任务", d.submit(tasks) == 0)
        store.close()


def case_memory_guard(c: Checker) -> None:
    """判据 7：内存低于阈值时停止取新任务，恢复后继续（事件驱动复检）。"""
    print("\n[7] 内存护栏")
    with tempdir() as tmp:
        tmp_path = Path(tmp)
        world = FakeWorld()
        cfg, store, tasks = make_env(tmp_path, 10, world)
        install_fakes(world, store)

        d = Dispatcher(cfg, store, CONC, memory_floor_gb=0.0)
        d.submit(tasks)
        d.start()
        time.sleep(0.15)

        # 把阈值抬到不可能达到的高度 → 下一秒的采样就会判定「内存不足」
        d.memory_floor_gb = 1e6
        time.sleep(dispatcher_mod.MEMORY_SAMPLE_INTERVAL_SEC + 0.4)
        with d._trace_lock:
            settled = len(d.trace)
        time.sleep(0.6)
        with d._trace_lock:
            later = len(d.trace)
        c.check(
            "内存不足后停止取新任务",
            later == settled,
            f"阶段数 {settled} → {later}",
        )
        c.check("memory_blocked 已置位", d.snapshot()["memory_blocked"])

        # 恢复阈值 → 下一个采样点应自行放行（由 store 写入 + 定时兜底触发 notify）
        d.memory_floor_gb = 0.0
        c.check(
            "恢复阈值后继续跑完",
            d.wait(timeout=60) and counts(store).get("done") == 10,
            f"counts={counts(store)}",
        )
        d.stop(timeout=10)
        store.close()


def case_idempotent_start(c: Checker) -> None:
    """判据 8：start() 幂等；线程数等于并发配置之和。"""
    print("\n[8] start() 幂等 / 线程数")
    with tempdir() as tmp:
        tmp_path = Path(tmp)
        world = FakeWorld()
        cfg, store, tasks = make_env(tmp_path, 4, world)
        install_fakes(world, store)

        d = Dispatcher(cfg, store, CONC, memory_floor_gb=0.0)
        d.submit(tasks)
        c.check("首次 start() 返回 True", d.start())
        first = len(d._threads)
        c.check("二次 start() 返回 False（幂等）", not d.start())
        c.check(
            "线程数 == n_dl + n_asr + n_frame",
            first == CONC.n_dl + CONC.n_asr + CONC.n_frame,
            f"{first} vs {CONC.to_dict()}",
        )
        d.wait(timeout=60)
        d.stop(timeout=10)
        store.close()


def case_retry(c: Checker) -> None:
    """判据 9：retry() 把失败项重新入队并跑通。"""
    print("\n[9] retry 失败项")
    with tempdir() as tmp:
        tmp_path = Path(tmp)
        world = FakeWorld()
        cfg, store, tasks = make_env(tmp_path, 6, world)
        bad = tasks[2].id
        world.fail_download.add(bad)
        install_fakes(world, store)

        d = Dispatcher(cfg, store, CONC, memory_floor_gb=0.0)
        d.submit(tasks)
        d.start()
        d.wait(timeout=60)
        c.check("首轮：该条 failed", store.get(bad).status == TaskStatus.FAILED.value)

        world.fail_download.clear()  # 模拟「链接已修好」
        c.check("retry() 被接受", d.retry(bad))
        c.check("retry 后计数 +1", store.get(bad).retry_count == 1, str(store.get(bad).retry_count))
        c.check(
            "重试后跑完（6 条全 done）",
            d.wait(timeout=60) and counts(store).get("done") == 6,
            f"counts={counts(store)}",
        )
        c.check("重复 retry 已完成项被拒绝", not d.retry(bad))
        d.stop(timeout=10)
        store.close()


def case_bounded_queue(c: Checker) -> None:
    """判据 10：有界队列生效 —— 下载不会一次性把全部任务推到磁盘。"""
    print("\n[10] 有界队列（背压）")
    with tempdir() as tmp:
        tmp_path = Path(tmp)
        world = FakeWorld()
        cfg, store, tasks = make_env(tmp_path, 30, world)
        install_fakes(world, store)

        global DELAY_ASR, DELAY_DL
        original_asr, original_dl = DELAY_ASR, DELAY_DL
        DELAY_ASR, DELAY_DL = 0.25, 0.01
        peak_inflight = 0
        try:
            d = Dispatcher(cfg, store, CONC, memory_floor_gb=0.0)
            d.submit(tasks)
            d.start()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and d.snapshot()["inflight"] < 30:
                peak_inflight = max(peak_inflight, d.snapshot()["inflight"])
                time.sleep(0.02)
            d.wait(timeout=120)
            d.stop(timeout=10)
            # 转写恒为 1，队列容量 2 → 同时在手的任务数被压得很低，
            # 远小于 30，这正说明「下载被转写的消费速度反向压住了」
            c.check(
                "同时在手的任务数被背压限制（远小于总数 30）",
                peak_inflight < 30,
                f"峰值 inflight={peak_inflight}（总 30 条）",
            )
            c.check("仍然全部跑完", counts(store).get("done") == 30, f"counts={counts(store)}")
        finally:
            DELAY_ASR, DELAY_DL = original_asr, original_dl
            store.close()


def case_store_thread_safety(c: Checker) -> None:
    """判据 11：Store 在多线程下不错不乱（并发 add / set_status）。"""
    print("\n[11] Store 线程安全")
    with tempdir() as tmp:
        store = Store(Path(tmp) / "state.db")
        created: list[Task] = []
        lock = threading.Lock()
        errors: list[str] = []

        def worker(index: int) -> None:
            try:
                for i in range(20):
                    task = Task(url=f"https://example.com/{index}/{i}", platform="douyin")
                    stored, ok = store.add(task)
                    if ok:
                        with lock:
                            created.append(stored)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        c.check("并发 add 无异常", not errors, "; ".join(errors[:3]))
        c.check("8×20 = 160 条全部插入", len(created) == 160, f"{len(created)} 条")

        # 并发改状态
        def status_worker(subset: list[Task]) -> None:
            for t in subset:
                store.set_status(t, TaskStatus.DONE)

        threads = [
            threading.Thread(target=status_worker, args=(created[i::8],)) for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        c.check("并发 set_status 后计数正确", store.count_by_status().get("done") == 160, str(store.count_by_status()))
        store.close()


def main() -> int:
    print("调度器自测 —— app/core/dispatcher.py（假阶段，不需要 GPU）")
    c = Checker()
    try:
        for case in (
            case_batch,
            case_pause,
            case_cancel_queued,
            case_cancel_running,
            case_bad_link,
            case_fatal,
            case_memory_guard,
            case_idempotent_start,
            case_retry,
            case_bounded_queue,
            case_store_thread_safety,
        ):
            try:
                case(c)
            except Exception as exc:  # noqa: BLE001 — 一个用例炸了不能带崩其余
                import traceback

                traceback.print_exc()
                c.check(f"{case.__name__} 未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        for store in _OPEN_STORES:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
    c.report("调度器自测")
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
