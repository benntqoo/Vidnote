"""Rust 宿主 ↔ Python Worker 的 RPC 入口（sidecar 模式）。

协议契约见 `docs/IPC协议规格.md`，本文件是它在 Python 侧的**唯一实现**。

三条不可违反的约束（违反任何一条即协议损坏）：

1. **stdout 只写协议帧**。启动时把 `sys.stdout` 指向 stderr，
   此后任何 stray `print` 都落到 stderr，无法污染协议流。
2. **stdout 写入必须加锁**。`log` / `task_update` 可能来自多个线程，
   不加锁会让两行 JSON 交错成一行。
3. **必须用独立线程读 stdin**。转写一条 20 分钟视频要 2 分 18 秒
   （PLAN.md §2.2），主线程若阻塞在转写上，就无法响应 `ping` / `pause` / `cancel`。

`app/core/` 不感知传输方式；本模块负责把 core 的 `on_progress` 回调
翻译成 `task_update` 事件。

由 Rust 侧以 `python -m app.rpc` 方式启动，cwd 应为项目根目录。
可用环境变量 `VIDNOTE_CONFIG` 指定配置文件路径（不通过 argv——argv 属于 cli.py）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from typing import Any, Callable, TextIO

from app.core import concurrency as concurrency_mod
from app.core import env_probe
from app.core.dispatcher import Dispatcher
from app.core.store import Store
from app.core.urls import extract_many
from app.models.config import Config
from app.models.task import Task, TaskStatus

PROTOCOL_VERSION = 1
SERVER_VERSION = "0.2.0"

#: 内部事件名 → 协议事件名。
#: 调度器不感知协议，它只报「发生了什么」；名字翻译在这里做，与 `STAGE_LABELS` 同理。
EVENT_NAMES: dict[str, str] = {
    "state": "pipeline_state",
    "batch_done": "batch_done",
    "batch_aborted": "batch_aborted",
    "resource_warning": "resource_warning",
}

#: 协议唯一出口的原始引用。**必须在任何重定向之前拿到**，故置于模块级。
raw_stdout: TextIO = sys.stdout
raw_stdin: TextIO = sys.stdin

#: 状态 → UI 显示名。与 docs/IPC协议规格.md §5 的表一一对应。
STAGE_LABELS: dict[str, str] = {
    TaskStatus.PENDING.value: "等待中",
    TaskStatus.DOWNLOADING.value: "下载中",
    TaskStatus.DOWNLOADED.value: "待抽音频",
    TaskStatus.TRANSCRIBING.value: "转写中",
    TaskStatus.TRANSCRIBED.value: "待抽帧 / 汇总",
    TaskStatus.FRAMING.value: "抽帧中",
    TaskStatus.SUMMARIZING.value: "汇总中",
    TaskStatus.DONE.value: "完成",
    TaskStatus.FAILED.value: "失败",
    TaskStatus.CANCELLED.value: "已取消",
}


def _configure_streams() -> None:
    """把三条标准流固定为 UTF-8 + LF。

    Windows 默认用 locale 编码（GBK），中文会直接写坏；换行转换还会把 `\\n`
    变成 `\\r\\n`，干扰按行分帧。
    """
    for stream in (raw_stdout, raw_stdin, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    try:
        raw_stdout.reconfigure(newline="\n")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


# ------------------------------------------------------------------ 协议出口


class Protocol:
    """协议帧的唯一出口。所有写入经此处，保证加锁与序号连续。"""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    def emit(
        self,
        name: str,
        data: dict[str, Any] | None = None,
        req_id: str | None = None,
    ) -> None:
        """推一个事件。`req_id` 仅在响应对应命令时回填。"""
        with self._lock:
            self._seq += 1
            frame = {
                "v": PROTOCOL_VERSION,
                "type": "evt",
                "name": name,
                "id": req_id,
                "seq": self._seq,
                "ts": round(time.time(), 3),
                "data": data if data is not None else {},
            }
            self._stream.write(json.dumps(frame, ensure_ascii=False) + "\n")
            self._stream.flush()

    def error(
        self,
        code: str,
        msg: str,
        fatal: bool = False,
        req_id: str | None = None,
    ) -> None:
        self.emit("error", {"code": code, "msg": msg, "fatal": fatal}, req_id)


class _ProtocolLogHandler(logging.Handler):
    """把 logging 记录转发为 `log` 事件。

    ⚠️ 本 handler 内部不得再调用 logging，否则无限递归。
    """

    def __init__(self, proto: Protocol, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._proto = proto

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — 日志失败绝不能让进程崩
            msg = "<日志格式化失败>"
        self._proto.emit("log", {"level": record.levelname.lower(), "msg": msg})


# ------------------------------------------------------------------ Worker


class Worker:
    """命令处理与生命周期管理。

    调度职责全部委托给 `app/core/dispatcher.py`：本类只做
    「协议帧 ↔ 调度器调用」的翻译，不自己管线程与队列。
    """

    def __init__(self, cfg: Config, proto: Protocol, config_is_default: bool = False) -> None:
        self.cfg = cfg
        self.proto = proto
        self.config_is_default = config_is_default
        # state.db 放仓库根（PLAN.md §4 目录树），与 work/ 分开，避免误删工作目录丢状态
        self.store = Store(cfg.root / "state.db")
        self.env = env_probe.probe(cfg.work_path())
        # 快速判据：硬件存在即按 GPU 规划并发。真实可用性由 _verify_env 异步确认。
        # 不能直接用 env.cuda.usable——probe() 后它是 None（未验证），
        # bool(None) 会退化成「按 CPU 规划」，而 CPU 模式下 8.7× 会掉到 1.44×。
        self.quick_gpu_usable = bool(self.env.cuda.device_count)
        self.concurrency = concurrency_mod.plan(
            cfg, self.env, gpu_usable=self.quick_gpu_usable
        )

        self.dispatcher = Dispatcher(
            cfg,
            self.store,
            self.concurrency,
            on_task_update=self._on_task_update,
            on_event=self._on_dispatcher_event,
        )

        self._inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stop = threading.Event()
        self._handlers: dict[str, Callable[[dict[str, Any], str | None], None]] = {
            "hello": self._cmd_hello,
            "ping": self._cmd_ping,
            "add_tasks": self._cmd_add_tasks,
            "remove_task": self._cmd_remove_task,
            "list_tasks": self._cmd_list_tasks,
            "get_concurrency": self._cmd_get_concurrency,
            "start": self._cmd_start,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "cancel_task": self._cmd_cancel_task,
            "retry_task": self._cmd_retry_task,
            "shutdown": self._cmd_shutdown,
        }

    # ---- 启动 ----

    def serve(self) -> int:
        _configure_streams()
        self._install_logging()
        # 关键：此后任何 stray print 都落到 stderr，协议流不会被污染
        sys.stdout = sys.stderr

        self._bootstrap()

        threading.Thread(target=self._reader_loop, name="stdin-reader", daemon=True).start()
        threading.Thread(target=self._verify_env, name="env-verify", daemon=True).start()

        while not self._stop.is_set():
            try:
                frame = self._inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            if frame is None:
                # stdin EOF —— 宿主已消失。这是防孤儿进程的最后一道防线。
                logging.warning("stdin 已关闭（宿主进程消失），worker 退出")
                break
            self._dispatch(frame)

        self.store.close()
        return 0

    def _install_logging(self) -> None:
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        if not any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, _ProtocolLogHandler)
            for h in root.handlers
        ):
            stderr_handler = logging.StreamHandler(stream=sys.stderr)
            stderr_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
            root.addHandler(stderr_handler)
        root.addHandler(_ProtocolLogHandler(self.proto))

    def _bootstrap(self) -> None:
        """进程启动时的必要修复。**必须在处理任何命令前执行。**"""
        if self.config_is_default:
            logging.info("未找到 config.yaml，使用默认配置（相对路径锚定到仓库根）")
        changed = self.store.reset_interrupted()
        if changed:
            logging.info(
                "启动重置：%d 条中途态任务已回退到上一稳定态（上次进程可能被强杀）",
                len(changed),
            )
        logging.info("worker 就绪：%s", self.concurrency.to_dict())

    def _verify_env(self) -> None:
        """后台做一次**真实推理**验证 GPU，完成后推 `env_verified`。

        为什么异步：要加载模型，数秒。阻塞 `ready` 会让 UI 白屏。
        为什么不能只看 `device_count`：它只枚举硬件，即使 cuBLAS/cuDNN 缺失
        也返回 1（PLAN.md §9.1）。
        """
        device = self.cfg.runtime.effective_device(True)
        compute_type = self.cfg.runtime.effective_compute_type(device)

        if device != "cuda":
            self._apply_env_verification(False, "配置指定 device=cpu，跳过 GPU 验证")
            return

        ok, detail = env_probe.verify_cuda(
            self.cfg.model_path(), device=device, compute_type=compute_type
        )
        self._apply_env_verification(ok, detail)

    def _apply_env_verification(self, ok: bool, detail: str) -> None:
        self.env.cuda.usable = ok
        self.env.cuda.detail = detail
        self.env.cuda.verified = True
        # 验证结果可能改变 GPU 可用性 → 并发计划必须重算，并同步给调度器
        self.concurrency = concurrency_mod.plan(self.cfg, self.env, gpu_usable=ok)
        self.dispatcher.conc = self.concurrency
        logging.info("环境验证：GPU %s —— %s", "可用" if ok else "不可用", detail)
        self.proto.emit(
            "env_verified",
            {
                "gpu_usable": ok,
                "detail": detail,
                "concurrency": self.concurrency.to_dict(),
            },
        )

    def _reader_loop(self) -> None:
        """独立线程读 stdin，逐帧投递到 `_inbox`。

        用 `readline()` 而非 `for line in sys.stdin`——后者有预读缓冲，
        会导致命令延迟到达（转写中发 pause 要等很久才生效）。
        """
        while True:
            try:
                line = raw_stdin.readline()
            except (OSError, ValueError) as exc:  # 管道断了
                logging.warning("读取 stdin 失败：%s", exc)
                break
            if not line:  # EOF
                break
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                self.proto.error("E_BAD_FRAME", f"不是合法 JSON：{exc}")
                continue
            if not isinstance(frame, dict):
                self.proto.error("E_BAD_FRAME", "帧必须是 JSON 对象")
                continue
            self._inbox.put(frame)
        self._inbox.put(None)  # 哨兵：通知主循环退出

    # ---- 分发 ----

    def _dispatch(self, frame: dict[str, Any]) -> None:
        req_id = frame.get("id")
        if not isinstance(req_id, str):
            req_id = None
        name = frame.get("name")
        args = frame.get("args")
        if not isinstance(args, dict):
            args = {}

        if not isinstance(name, str):
            self.proto.error("E_BAD_ARGS", "命令缺少 name 字段", req_id=req_id)
            return

        handler = self._handlers.get(name)
        if handler is None:
            self.proto.error(
                "E_UNKNOWN_CMD", f"未知命令：{name}（协议版本不匹配？）", req_id=req_id
            )
            return

        try:
            handler(args, req_id)
        except Exception as exc:  # noqa: BLE001 — 单条命令失败不能拖垮 worker
            logging.exception("命令 %s 执行失败", name)
            self.proto.error("E_INTERNAL", f"{type(exc).__name__}: {exc}", req_id=req_id)

    # ---- 命令实现 ----

    def _cmd_hello(self, args: dict[str, Any], req_id: str | None) -> None:
        client_version = args.get("client_version")
        if isinstance(client_version, str):
            logging.info("宿主握手：client_version=%s", client_version)
        self.proto.emit(
            "ready",
            {
                "server_version": SERVER_VERSION,
                "protocol": PROTOCOL_VERSION,
                "gpu_usable": self.quick_gpu_usable,
                "gpu_verified": bool(self.env.cuda.verified),
                "concurrency": self.concurrency.to_dict(),
                "db_path": str(self.store.db_path),
                "config_is_default": self.config_is_default,
                "pipeline_state": self.dispatcher.state.value,
            },
            req_id,
        )

    def _cmd_ping(self, args: dict[str, Any], req_id: str | None) -> None:
        self.proto.emit("pong", {}, req_id)

    def _cmd_add_tasks(self, args: dict[str, Any], req_id: str | None) -> None:
        urls = args.get("urls")
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            self.proto.error(
                "E_BAD_ARGS", "add_tasks 需要 args.urls 为字符串数组", req_id=req_id
            )
            return

        added: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []

        for item in extract_many(urls):
            task = Task(url=item.target, platform=item.platform)
            stored, created = self.store.add(task)
            if created:
                added.append(
                    {
                        "task_id": stored.id,
                        "url": stored.url,
                        "title": stored.title,
                        "platform": stored.platform,
                        "status": stored.status,
                    }
                )
            else:
                skipped.append({"url": item.target, "reason": "duplicate"})

        logging.info("入队 %d 条，跳过 %d 条", len(added), len(skipped))
        self.proto.emit("tasks_added", {"added": added, "skipped": skipped}, req_id)

    def _cmd_remove_task(self, args: dict[str, Any], req_id: str | None) -> None:
        task_id = args.get("task_id")
        if not isinstance(task_id, int):
            self.proto.error("E_BAD_ARGS", "remove_task 需要整数 args.task_id", req_id=req_id)
            return
        # 先取消：删除一条正在跑的任务，不能只从表里抹掉而让线程继续跑它的产出
        self.dispatcher.cancel(task_id)
        removed = self.store.delete(task_id)
        if not removed:
            self.proto.error("E_BAD_ARGS", f"任务不存在：{task_id}", req_id=req_id)
            return
        self.proto.emit("task_removed", {"task_id": task_id}, req_id)

    def _cmd_list_tasks(self, args: dict[str, Any], req_id: str | None) -> None:
        self.proto.emit(
            "task_list",
            {"tasks": [self._task_payload(t) for t in self.store.list()]},
            req_id,
        )

    def _cmd_get_concurrency(self, args: dict[str, Any], req_id: str | None) -> None:
        self.proto.emit("concurrency", self.concurrency.to_dict(), req_id)

    # ---- 流水线控制 ----

    def _cmd_start(self, args: dict[str, Any], req_id: str | None) -> None:
        """启动流水线：把可续跑的任务入队并启动线程池。幂等。

        **只入队「可续跑」的任务**（pending / downloaded / transcribed 等非终态且
        非 failed）。failed / cancelled 要显式 `retry_task` 才会再跑——否则一条永久
        坏链会在每次点「开始」时被重试一遍，看起来像程序在空转。
        """
        resumable = self.store.resumable()
        added = self.dispatcher.submit(resumable)
        self.dispatcher.start()
        logging.info("start：续跑入队 %d 条（state=%s）", added, self.dispatcher.state.value)
        self.proto.emit(
            "started",
            {
                "added": added,
                "state": self.dispatcher.state.value,
                "concurrency": self.concurrency.to_dict(),
            },
            req_id,
        )

    def _cmd_pause(self, args: dict[str, Any], req_id: str | None) -> None:
        accepted = self.dispatcher.pause()
        logging.info("pause：%s（state=%s）", "已接受" if accepted else "忽略", self.dispatcher.state.value)
        self.proto.emit(
            "paused", {"accepted": accepted, "state": self.dispatcher.state.value}, req_id
        )

    def _cmd_resume(self, args: dict[str, Any], req_id: str | None) -> None:
        accepted = self.dispatcher.resume()
        logging.info("resume：%s（state=%s）", "已接受" if accepted else "忽略", self.dispatcher.state.value)
        self.proto.emit(
            "resumed", {"accepted": accepted, "state": self.dispatcher.state.value}, req_id
        )

    def _cmd_cancel_task(self, args: dict[str, Any], req_id: str | None) -> None:
        task_id = args.get("task_id")
        if not isinstance(task_id, int):
            self.proto.error("E_BAD_ARGS", "cancel_task 需要整数 args.task_id", req_id=req_id)
            return
        if not self.dispatcher.cancel(task_id):
            self.proto.error("E_BAD_ARGS", f"任务无法取消（不存在或已完成）：{task_id}", req_id=req_id)
            return
        task = self.store.get(task_id)
        if task is None:
            self.proto.error("E_BAD_ARGS", f"任务不存在：{task_id}", req_id=req_id)
            return
        # 响应即当前快照：排队中的任务在这里已是 cancelled；运行中的还要等阶段
        # 中断，最终态会以一次主动推送的 task_update 到达（id=null）。
        self.proto.emit("task_update", self._task_payload(task), req_id)

    def _cmd_retry_task(self, args: dict[str, Any], req_id: str | None) -> None:
        task_id = args.get("task_id")
        if not isinstance(task_id, int):
            self.proto.error("E_BAD_ARGS", "retry_task 需要整数 args.task_id", req_id=req_id)
            return
        if not self.dispatcher.retry(task_id):
            self.proto.error(
                "E_BAD_ARGS", f"任务无法重试（不存在、已完成或仍在队列中）：{task_id}", req_id=req_id
            )
            return
        self.dispatcher.start()  # 首次调用 retry 时线程池可能还没起
        task = self.store.get(task_id)
        if task is not None:
            self.proto.emit("task_update", self._task_payload(task), req_id)

    def _cmd_shutdown(self, args: dict[str, Any], req_id: str | None) -> None:
        logging.info("收到 shutdown，等待在跑的任务结束（最长 30 秒）")
        self.dispatcher.stop(timeout=30.0)
        logging.info("worker 退出")
        self.proto.emit("bye", {}, req_id)
        self._stop.set()

    # ---- 调度器回调 ----

    def _on_task_update(self, task: Task, progress: float, elapsed: float | None) -> None:
        """调度器 → `task_update` 事件。**会从多个 worker 线程调用**，故 `emit` 必须加锁。"""
        self.proto.emit(
            "task_update",
            self._task_payload(task, progress=progress, elapsed=elapsed),
        )

    def _on_dispatcher_event(self, name: str, data: dict[str, Any]) -> None:
        proto_name = EVENT_NAMES.get(name, name)
        if proto_name == "batch_aborted":
            # 环境级故障：不是进程级致命（worker 仍能响应命令），但整批停了，
            # 所以同时给一条 fatal=false 的 error，让 Rust 侧有明确的错误通道。
            self.proto.error("E_ENV_FATAL", str(data.get("reason", "")), fatal=False)
        self.proto.emit(proto_name, data)

    # ---- 序列化 ----

    @staticmethod
    def _task_payload(
        task: Task,
        progress: float | None = None,
        elapsed: float | None = None,
    ) -> dict[str, Any]:
        """Task → IPC `task_list` / `task_update` 用的快照结构。

        字段名与 `docs/IPC协议规格.md` §5.1 一致；不在协议里的字段（如
        `video_path`）刻意不外传，减少两端耦合面。
        """
        if progress is None:
            progress = 1.0 if task.status == TaskStatus.DONE.value else 0.0
        return {
            "task_id": task.id,
            "url": task.url,
            "platform": task.platform,
            "title": task.title,
            "duration_sec": task.duration_sec,
            "status": task.status,
            "stage": STAGE_LABELS.get(task.status, task.status),
            "progress": progress,
            "elapsed_sec": elapsed,
            "retry_count": task.retry_count,
            "out_dir": task.out_dir,
            "error": task.stage_error,
        }


def main() -> int:
    _configure_streams()
    cfg_path = os.environ.get("VIDNOTE_CONFIG") or None
    cfg, used_default = Config.load_or_default(cfg_path)
    proto = Protocol(raw_stdout)
    return Worker(cfg, proto, config_is_default=used_default).serve()


if __name__ == "__main__":
    sys.exit(main())
