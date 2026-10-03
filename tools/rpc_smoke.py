"""IPC 协议冒烟测试（docs/IPC协议规格.md §9 的 8 条判据）。

不需要 Rust 端——本脚本扮演宿主：spawn `python -m app.rpc`，走 stdin/stdout，
逐条核对协议契约。Rust 工程建起来之前，这就是协议层的验收手段。

用法：
    python tools/rpc_smoke.py
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: `env_verified` 要真实加载模型（约 5-8 秒），给足超时
ENV_VERIFY_TIMEOUT = 90
DEFAULT_TIMEOUT = 15


class Host:
    """扮演 Rust 宿主：管理与 worker 的两条管道。"""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.rpc"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={**os.environ, **(env or {})},
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.received: list[str] = []
        self.judgements: list[tuple[str, bool, str]] = []

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # ---- 管道 ----

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                break
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)

    def _read_stderr(self) -> None:
        """必须独立消费 stderr，否则缓冲区满会让子进程阻塞。"""
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip("\n"))

    # ---- 发送 ----

    def send(self, name: str, args: dict | None = None, req_id: str | None = None) -> None:
        frame: dict = {"v": 1, "type": "cmd", "name": name, "args": args or {}}
        if req_id is not None:
            frame["id"] = req_id
        self.send_raw(json.dumps(frame, ensure_ascii=False))

    def send_raw(self, text: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    # ---- 接收 ----

    def await_event(self, name: str, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
        """等到指定事件，途中记录所有原始行（用于协议纯净性检查）。"""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                line = self.lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                return None
            self.received.append(line)
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                self.judgements.append(
                    ("协议纯净性", False, f"stdout 出现非 JSON 行：{line[:100]!r}")
                )
                continue
            if evt.get("name") == name:
                return evt

    def close_stdin(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()

    def record(self, name: str, ok: bool, detail: str) -> None:
        self.judgements.append((name, ok, detail))


def check_protocol_purity(host: Host) -> None:
    """判据 1：所有收到的行都必须是合法 JSON。

    这条能抓住一切 stray print——包括依赖库偷偷打到 stdout 的东西。
    """
    bad = []
    for line in host.received:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad.append(line[:100])
    if bad:
        host.record("协议纯净性", False, f"{len(bad)} 行非法：{bad[:2]}")
    else:
        host.record("协议纯净性", True, f"{len(host.received)} 行全部合法 JSON")


def check_seq_monotonic(host: Host) -> None:
    """判据 7：`seq` 严格递增且无跳号。"""
    seqs = []
    for line in host.received:
        try:
            seqs.append(json.loads(line)["seq"])
        except (json.JSONDecodeError, KeyError):
            return
    expected = list(range(1, len(seqs) + 1))
    if seqs == expected:
        host.record("seq 连续", True, f"1..{len(seqs)} 无跳号")
    else:
        host.record("seq 连续", False, f"实际 {seqs[:10]}… 期望 1..{len(seqs)}")


def run_main_flow(env: dict[str, str]) -> Host:
    host = Host(env)

    # ---- 判据 2：握手 ----
    host.send("hello", {"client_version": "0.0.0-smoke"}, req_id="c-1")
    ready = host.await_event("ready")
    if ready is None:
        host.record("握手 hello→ready", False, "超时未收到 ready")
        return host
    data = ready.get("data", {})
    ok = (
        isinstance(data.get("gpu_usable"), bool)
        and data.get("server_version")
        and ready.get("id") == "c-1"
    )
    host.record(
        "握手 hello→ready",
        ok,
        f"server={data.get('server_version')} gpu_usable={data.get('gpu_usable')} "
        f"verified={data.get('gpu_verified')} 并发={data.get('concurrency')}",
    )

    # ---- 判据 3：未知命令不崩 ----
    host.send("no_such_command", req_id="c-2")
    err = host.await_event("error")
    ok_unknown = err is not None and err.get("data", {}).get("code") == "E_UNKNOWN_CMD"
    host.send("ping", req_id="c-3")
    pong = host.await_event("pong", timeout=5)
    host.record(
        "未知命令不崩",
        bool(ok_unknown and pong is not None),
        f"E_UNKNOWN_CMD 已回={ok_unknown}；随后 ping 仍通={pong is not None}",
    )

    # ---- 判据 4：非法 JSON 不崩 ----
    host.send_raw("this is not json at all")
    err = host.await_event("error")
    ok_frame = err is not None and err.get("data", {}).get("code") == "E_BAD_FRAME"
    host.record("非法 JSON 不崩", ok_frame, f"code={err.get('data', {}).get('code') if err else None}")

    # ---- 判据 5：心跳 2 秒内 ----
    started = time.time()
    host.send("ping", req_id="c-4")
    pong = host.await_event("pong", timeout=5)
    elapsed = time.time() - started
    host.record(
        "心跳 <2s 响应",
        pong is not None and elapsed < 2.0,
        f"往返 {elapsed * 1000:.0f} ms",
    )

    # ---- 任务入队：分享文案抽 URL + 批内去重 ----
    host.send(
        "add_tasks",
        {
            "urls": [
                "8.25 复制打开抖音，看看【某创作者】 https://v.douyin.com/AbCdEf12345/ vFh:/ 03/30",
                "https://v.douyin.com/AbCdEf12345/",
                "https://www.bilibili.com/video/BV1xx411c7mD",
            ]
        },
        req_id="c-5",
    )
    added = host.await_event("tasks_added")
    if added is None:
        host.record("add_tasks 抽 URL", False, "超时")
    else:
        d = added.get("data", {})
        # 第 1 行是分享文案 → 抽出短链；第 2 行与之相同 → 在 urls 层就被合并
        # 所以这里应是 2 条入队、0 条 skipped（不是 1 条 duplicate）
        ok_add = len(d.get("added", [])) == 2 and len(d.get("skipped", [])) == 0
        host.record(
            "add_tasks 抽 URL + 批内去重",
            ok_add,
            f"3 行输入 → 入队 {len(d.get('added', []))} 条、跳过 {len(d.get('skipped', []))} 条",
        )

    # ---- 跨批次去重：这块由 store 的 url UNIQUE 约束负责 ----
    host.send("add_tasks", {"urls": ["https://v.douyin.com/AbCdEf12345/"]}, req_id="c-5b")
    again = host.await_event("tasks_added")
    if again is None:
        host.record("add_tasks 跨批次去重", False, "超时")
    else:
        d2 = again.get("data", {})
        skipped2 = d2.get("skipped", [])
        ok_dup = (
            len(d2.get("added", [])) == 0
            and len(skipped2) == 1
            and skipped2[0].get("reason") == "duplicate"
        )
        host.record("add_tasks 跨批次去重", ok_dup, f"重复链接被识别：{skipped2}")

    # ---- list_tasks ----
    host.send("list_tasks", req_id="c-6")
    lst = host.await_event("task_list")
    n = len(lst.get("data", {}).get("tasks", [])) if lst else -1
    host.record("list_tasks", lst is not None and n == 2, f"返回 {n} 条")

    # ---- 判据：流水线控制面（阶段 2 起由 dispatcher 提供）----
    # 先把队列清空：start 会去跑 store 里的可续跑任务，留着它们会真发网络请求。
    # 清空同时验证 remove_task。
    removed = []
    for task in lst.get("data", {}).get("tasks", []) if lst else []:
        host.send("remove_task", {"task_id": task["task_id"]}, req_id=f"c-rm-{task['task_id']}")
        removed.append(host.await_event("task_removed") is not None)
    host.record("remove_task 清空队列", all(removed) and len(removed) == 2, f"移除 {sum(removed)}/2")

    host.send("start", req_id="c-7")
    started_evt = host.await_event("started")
    sd = started_evt.get("data", {}) if started_evt else {}
    host.record(
        "start → started（空队列幂等启动）",
        started_evt is not None and sd.get("added") == 0,
        f"added={sd.get('added')} state={sd.get('state')}",
    )

    host.send("pause", req_id="c-8")
    paused_evt = host.await_event("paused")
    host.record(
        "pause → paused（被接受）",
        paused_evt is not None and paused_evt.get("data", {}).get("accepted") is True,
        f"state={paused_evt.get('data', {}).get('state') if paused_evt else None}",
    )

    host.send("resume", req_id="c-9")
    resumed_evt = host.await_event("resumed")
    host.record(
        "resume → resumed（被接受）",
        resumed_evt is not None and resumed_evt.get("data", {}).get("accepted") is True,
        f"state={resumed_evt.get('data', {}).get('state') if resumed_evt else None}",
    )

    host.send("cancel_task", {"task_id": 99999}, req_id="c-10")
    err_cancel = host.await_event("error")
    host.record(
        "cancel_task 不存在的 id → E_BAD_ARGS",
        bool(err_cancel) and err_cancel.get("data", {}).get("code") == "E_BAD_ARGS",
        f"code={err_cancel.get('data', {}).get('code') if err_cancel else None}",
    )

    host.send("retry_task", {"task_id": 99999}, req_id="c-11")
    err_retry = host.await_event("error")
    host.record(
        "retry_task 不存在的 id → E_BAD_ARGS",
        bool(err_retry) and err_retry.get("data", {}).get("code") == "E_BAD_ARGS",
        f"code={err_retry.get('data', {}).get('code') if err_retry else None}",
    )

    # ---- 等 GPU 真实验证（异步，要加载模型）----
    print("   … 等待 env_verified（真实推理验证，需加载模型 5-8 秒）")
    verified = host.await_event("env_verified", timeout=ENV_VERIFY_TIMEOUT)
    if verified is None:
        host.record("env_verified 异步推送", False, f"{ENV_VERIFY_TIMEOUT}s 内未收到")
    else:
        vd = verified.get("data", {})
        host.record(
            "env_verified 异步推送",
            "gpu_usable" in vd and "concurrency" in vd,
            f"gpu_usable={vd.get('gpu_usable')} detail={str(vd.get('detail'))[:60]}",
        )

    # ---- 判据 8：优雅关闭 ----
    host.send("shutdown", req_id="c-8")
    bye = host.await_event("bye", timeout=10)
    host.record("优雅关闭 shutdown→bye", bye is not None, f"bye={'收到' if bye else '未收到'}")

    try:
        code = host.proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        code = None
    host.record("退出码为 0", code == 0, f"exit={code}")

    return host


def run_eof_flow(env: dict[str, str], judgements: list[tuple[str, bool, str]]) -> None:
    """宿主消失（stdin EOF）时 worker 必须自行退出，不留孤儿。

    这一步必须**单开一个 worker**（它要自杀，不能复用前面那个），因此也不能把结果
    记在 `run_main_flow` 返回的 host 上。

    ⚠️ 2026-10-03 修：原先这里新建 host 后 `record` 到自己身上，而 `main()` 只打印
    第一个 host 的判据 → **这一条从未被报告**，脚本却显示「18/18 通过」。
    一个悄悄不报告的判据比没有判据更危险：它制造虚假的信心。
    所以现在把结果列表**显式传进来**（而不是返回新 host），漏接会在类型上看得见。
    """
    host = Host(env)
    host.send("hello", {"client_version": "0.0.0-smoke"})
    ready = host.await_event("ready", timeout=15)
    if ready is None:
        judgements.append(("EOF 自杀（3s 内）", False, "握手就没成功"))
        host.proc.kill()
        return

    started = time.time()
    host.close_stdin()  # 模拟宿主进程突然消失
    try:
        code = host.proc.wait(timeout=10)
        elapsed = time.time() - started
        judgements.append(
            ("EOF 自杀（3s 内）", elapsed < 3.0, f"{elapsed:.2f}s 内退出，exit={code}")
        )
    except subprocess.TimeoutExpired:
        host.proc.kill()
        judgements.append(("EOF 自杀（3s 内）", False, "10s 仍未退出，已成孤儿进程"))

    host.proc.wait(timeout=5)


def _isolated_env() -> tuple[dict[str, str], str]:
    """构造隔离环境：临时 config + 临时 work_dir。

    测试会真实入队任务，若直接写项目根的 `state.db` 会留下脏数据，
    且第二次运行会全部被判为 duplicate。
    """
    tmp = tempfile.mkdtemp(prefix="vidnote-smoke-")
    cfg = Path(tmp) / "config.yaml"
    cfg.write_text(
        f"paths:\n  work_dir: {tmp}/work\n  model_dir: {ROOT}/models\n",
        encoding="utf-8",
    )
    return {"VIDNOTE_CONFIG": str(cfg)}, tmp


def main() -> int:
    print("IPC 协议冒烟测试 —— docs/IPC协议规格.md §9\n")

    env, tmp = _isolated_env()

    host = run_main_flow(env)
    check_protocol_purity(host)
    check_seq_monotonic(host)

    run_eof_flow(env, host.judgements)

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'判据':<34} {'结果':<6} 说明")
    print("-" * 100)
    passed = 0
    for name, ok, detail in host.judgements:
        mark = "PASS" if ok else "FAIL"
        passed += 1 if ok else 0
        print(f"{name:<30} {mark:<6} {detail}")
    total = len(host.judgements)
    print("-" * 100)
    print(f"{passed}/{total} 通过")

    if passed < total:
        print("\n--- worker stderr 末尾 ---")
        for line in host.stderr_lines[-25:]:
            print("  " + line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
