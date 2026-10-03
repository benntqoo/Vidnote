"""阶段 2 验收：并发调度 + 断点续跑（PLAN.md §8 阶段 2 的三条判据）。

设计要点（为什么这么造输入）：

1. **输入用本地短片段，不用真实 URL。** 验收要验的是**调度**，不是转写质量
   （那是阶段 1 的事，判据是逐字符比对基线）。用长视频会让每次验收跑半小时，
   而调度错误与视频长度无关。片段从 `samples/video.mp4` 用 `-c copy` 切，秒级完成。
2. **故意混入 1 条坏链接**（指向不存在的文件），用来验「单条失败不影响其余」。
3. **硬杀进程用 `taskkill /F /T`**，不是优雅退出——要模拟的是「进程被强杀」，
   这才是断点续跑真正的使用场景（断电、任务管理器结束进程）。
4. **抽帧打开**。本地文件输入下下载是硬链接（毫秒级），如果关掉抽帧，整条链路上
   就没有任何可以与转写重叠的阶段，`trace` 里的重叠系数必然 ≈ 1.0 而毫无信息量。
   打开抽帧后，第 N 条的抽帧会与第 N+1 条的转写重叠——这才是对「流水线架构」
   这个设计推断的有效证伪手段（PLAN.md §11.2）。

用法：
    python tools/phase2_acceptance.py all      # 全流程（约 6 分钟，占用 GPU）
    python tools/phase2_acceptance.py prep     # 只切片段
    python tools/phase2_acceptance.py batch    # 只跑满批（判据 1、3）
    python tools/phase2_acceptance.py crash    # 只做硬杀（判据 2 第一步）
    python tools/phase2_acceptance.py resume   # 只做续跑（判据 2 第二步）
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE = ROOT / "samples" / "video.mp4"
ACC = ROOT / "work" / "acc"
INPUTS = ACC / "inputs"
CFG = ACC / "acceptance.yaml"
URLS = ACC / "urls.txt"
WORK = ACC / "work"
DB = ACC / "state.db"
TRACE = ACC / "trace.json"
MANIFEST = ACC / "inputs.json"

CLIP_COUNT = 10
CLIP_SECONDS = 60
#: 视频总长 1204 秒；起点间隔 100 秒，最远一条到 960 秒，留足余量
CLIP_STRIDE = 100

#: 坏链接：扩展名合法（会被识别成「本地文件」）但文件不存在
BAD_LINK = str(INPUTS / "definitely_missing_clip.mp4")

#: 硬杀发生在启动后多少秒。太短则还没有 done 可供验证「不重跑」，太长则浪费时间
CRASH_AFTER_SEC = 30.0

INITIAL_PROMPT = "这是一段关于AI量化交易的讲解，涉及回测、过拟合、夏普比率、特征工程、样本外验证等概念。"


# ---------------------------------------------------------------- 输出


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
        return ok

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")

    def summary(self) -> int:
        failed = [r for r in self.rows if not r[1]]
        print(f"\n阶段 2 验收：{len(self.rows) - len(failed)}/{len(self.rows)} 通过")
        for name, _, detail in failed:
            print(f"  ✗ {name}  — {detail}")
        return 1 if failed else 0


# ---------------------------------------------------------------- 准备


def cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "app.cli", "-c", str(CFG), *args]


#: 子进程输出里全是中文日志。Windows 上默认按本地代码页（cp936）编码，
#: 而本脚本按 UTF-8 解码 → 直接 UnicodeDecodeError 崩在读取线程里。
#: 两端都钉死 UTF-8：子进程用 PYTHONIOENCODING/PYTHONUTF8，父进程用 encoding=。
CHILD_ENV = {
    "PYTHONPATH": str(ROOT),
    "PYTHONUNBUFFERED": "1",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
}


def _env() -> dict[str, str]:
    return {**os.environ, **CHILD_ENV}


def run_cli(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd(*args),
        cwd=str(ROOT),
        env=_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def preflight(report: Report) -> bool:
    """先确认**当前解释器**就是装了依赖的那个。

    这一步存在的理由：验收脚本用 `sys.executable` 拉起 CLI，如果脚本本身被
    「干净的解释器」跑（比如系统 Python），10 条任务会在转写阶段整齐地全部
    failed，看起来像流水线坏了，其实只是环境选错。花 0.5 秒做一次显式检查，
    比事后从 15 条 FAIL 里反推要划算得多。
    """
    report.section("预检：解释器与依赖")
    report.check("解释器", True, sys.executable)
    probe = subprocess.run(
        [sys.executable, "-c", "import faster_whisper, ctranslate2; print('ok')"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ok = probe.returncode == 0 and "ok" in probe.stdout
    report.check(
        "faster-whisper / ctranslate2 可导入",
        ok,
        "OK" if ok else f"缺失。请用 C:/Users/Ben/.workbuddy/binaries/python/envs/default/Scripts/python.exe 运行本脚本",
    )
    try:
        report.check("ffmpeg 可用", True, _ffmpeg())
    except Exception as exc:  # noqa: BLE001
        report.check("ffmpeg 可用", False, str(exc)[:160])
        ok = False
    return ok


def write_config() -> None:
    ACC.mkdir(parents=True, exist_ok=True)
    CFG.write_text(
        f"""# 阶段 2 验收专用配置（由 tools/phase2_acceptance.py 生成）
paths:
  work_dir: {WORK.as_posix()}
  model_dir: {(ROOT / 'models').as_posix()}
runtime:
  device: auto
  compute_type: auto
  model: large-v3
transcribe:
  language: zh
  beam_size: 5
  vad_filter: true
  vad_min_silence_ms: 400
  initial_prompt: "{INITIAL_PROMPT}"
frames:
  # 打开：本地输入的下载阶段是硬链接（毫秒级），关掉抽帧就没有任何阶段可与转写重叠，
  # trace 的重叠系数将恒为 1.0、失去证伪意义（见模块 docstring 第 4 条）
  enabled: true
  scene_threshold: 0.3
  max_frames: 60
  sheet_cols: 6
  sheet_rows: 4
download:
  keep_intermediate: true
""",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wipe(path: Path) -> None:
    """清空运行时目录——**靠改名旁置，不靠删除**。

    原因（2026-10-03 实测）：一整批任务的产出目录有 200+ 个文件，`shutil.rmtree`
    在受管环境里会命中批量删除确认（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`，
    阈值 50），验收脚本会在这一步直接中断，而已通过的前半段白跑。

    改名没有任何删除动作，因此不受该规则影响，还顺带保留了上一轮的现场（要排查
    「上一次为什么失败」时它就是证据）。代价是 `work/acc/` 下会堆积
    `work.prev-<时间戳>` 目录，属于可容忍的垃圾——它本来就在 `.gitignore` 的
    `work/` 之下，人工清一次即可。
    """
    if not path.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside = path.with_name(f"{path.name}.prev-{stamp}")
    serial = 1
    while aside.exists():
        serial += 1
        aside = path.with_name(f"{path.name}.prev-{stamp}-{serial}")
    path.rename(aside)


def _reset_run() -> None:
    """把一次运行的现场清干净：产出目录旁置 + 删库（单文件）。"""
    _wipe(WORK)
    DB.unlink(missing_ok=True)
    for suffix in ("-journal", "-wal", "-shm"):
        Path(str(DB) + suffix).unlink(missing_ok=True)


def cmd_prep(report: Report, force: bool = False) -> bool:
    report.section("准备：切片段 + 造坏链接")
    if not SOURCE.is_file():
        report.check("源视频存在", False, f"缺少 {SOURCE}（见 samples/README.md）")
        return False
    report.check("源视频存在", True, f"{SOURCE.name} {SOURCE.stat().st_size / 1024**2:.1f} MB")

    INPUTS.mkdir(parents=True, exist_ok=True)
    clips: list[dict] = []
    ffmpeg = _ffmpeg()
    for index in range(CLIP_COUNT):
        start = index * CLIP_STRIDE
        out = INPUTS / f"clip_{index + 1:02d}.mp4"
        if force or not out.is_file():
            proc = subprocess.run(
                [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-ss", str(start), "-t", str(CLIP_SECONDS), "-i", str(SOURCE),
                    "-c", "copy", str(out),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode != 0:
                report.check(f"切片段 clip_{index + 1:02d}", False, proc.stderr.strip()[:200])
                return False
        with __import__("contextlib").suppress(Exception):
            clips.append({"file": out.name, "start_sec": start, "sha256": sha256(out)})

    sizes = [c for c in clips if c.get("sha256")]
    report.check(
        f"切出 {CLIP_COUNT} 条 {CLIP_SECONDS} 秒片段",
        len(clips) == CLIP_COUNT and len(sizes) == CLIP_COUNT,
        f"共 {len(clips)} 条，合计 {sum((INPUTS / c['file']).stat().st_size for c in clips) / 1024**2:.1f} MB",
    )
    MANIFEST.write_text(json.dumps(clips, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [str((INPUTS / c["file"]).as_posix()) for c in clips] + [Path(BAD_LINK).as_posix()]
    URLS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report.check(
        "输入清单含 10 条片段 + 1 条坏链接",
        len(lines) == CLIP_COUNT + 1,
        f"{URLS.name} 共 {len(lines)} 行",
    )
    return True


def _ffmpeg() -> str:
    from app.core.ffmpeg_locator import find_ffmpeg

    return str(find_ffmpeg())


# ---------------------------------------------------------------- DB


def read_tasks() -> dict[int, dict]:
    if not DB.is_file():
        return {}
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        return {r["id"]: dict(r) for r in rows}
    finally:
        conn.close()


def counts(tasks: dict[int, dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for task in tasks.values():
        out[task["status"]] = out.get(task["status"], 0) + 1
    return out


# ---------------------------------------------------------------- 判据 1 / 3


def cmd_batch(report: Report) -> bool:
    report.section("判据 1、3：10 条跑满 + 坏链接只影响自己")
    _reset_run()

    started = time.monotonic()
    proc = run_cli(
        ["run", "--file", str(URLS), "--trace", str(TRACE)],
        timeout=60 * 20,
    )
    wall = time.monotonic() - started
    print(f"        CLI 退出码 {proc.returncode}，墙钟 {wall:.1f}s")
    tail = [line for line in proc.stderr.splitlines() if "失败" in line or "完成" in line]
    for line in tail[-4:]:
        print(f"        {line.strip()}")

    tasks = read_tasks()
    got = counts(tasks)
    report.check(
        "10 条片段全部 done",
        got.get("done") == CLIP_COUNT,
        f"counts={got}",
    )
    report.check("1 条坏链接 failed", got.get("failed") == 1, f"counts={got}")

    bad = [t for t in tasks.values() if t["status"] == "failed"]
    if bad:
        err = (bad[0]["stage_error"] or "").splitlines()[0]
        report.check("坏链接的错误信息可读", "不存在" in err, err[:120])
    else:
        report.check("坏链接的错误信息可读", False, "没有 failed 任务")

    report.check(
        "批次里没有停留中途态的任务",
        not {k for k in got if k in {"downloading", "transcribing", "framing"}},
        f"counts={got}",
    )

    # ---- trace：阶段是否真的重叠 ----
    if TRACE.is_file():
        trace = json.loads(TRACE.read_text(encoding="utf-8"))
        spans = trace["spans"]
        total = trace["total_stage_seconds"]
        wall_trace = trace["wall_seconds"]
        ratio = total / wall_trace if wall_trace else 0.0
        peaks = trace["peak_concurrency"]
        print(f"        阶段总耗时 {total:.1f}s / 墙钟 {wall_trace:.1f}s → 重叠系数 {ratio:.2f}×")
        print(f"        峰值并发 {peaks}")
        # 坏链接也会跑下载阶段（它在下载阶段失败），所以下载 span 是 11 条，
        # 而不是 10 条——期望值按「下载 (CLIP_COUNT+1) + 转写 CLIP_COUNT + 抽帧 CLIP_COUNT」算
        stage_counts: dict[str, int] = {}
        for span in spans:
            stage_counts[span["stage"]] = stage_counts.get(span["stage"], 0) + 1
        expected = {
            "downloading": CLIP_COUNT + 1,
            "transcribing": CLIP_COUNT,
            "framing": CLIP_COUNT,
        }
        report.check(
            "各阶段执行记录完整（坏链接只到下载，其余各自跑满三个阶段）",
            stage_counts == expected,
            f"实际 {stage_counts}（期望 {expected}，共 {sum(expected.values())} 条）",
        )
        report.check(
            "转写峰值并发 == 1（GPU 单卡串行，§3.1 的设计前提）",
            peaks.get("transcribing") == 1,
            f"peak={peaks}",
        )
        report.check(
            "抽帧与转写确实重叠（重叠系数 > 1.0）",
            ratio > 1.0,
            f"重叠系数 {ratio:.2f}×（1.0 即完全串行）",
        )
    else:
        report.check("trace 已生成", False, f"缺少 {TRACE}")

    return proc.returncode == 1  # 有 1 条失败 → CLI 应返回 1


# ---------------------------------------------------------------- 判据 2


def cmd_crash(report: Report) -> dict[int, dict]:
    report.section("判据 2 第一步：硬杀进程（模拟断电/任务管理器结束进程）")
    _reset_run()

    env = _env()
    # 不加 --force：产出目录与库都已清空，任务必然是新建的，没有东西要清。
    # 加了反而会让 11 条任务各删一次旧产出目录，命中批量删除确认（见 _wipe）。
    proc = subprocess.Popen(
        cmd("run", "--file", str(URLS)),
        cwd=str(ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"        启动 PID={proc.pid}，{CRASH_AFTER_SEC:.0f} 秒后强杀进程树")
    try:
        proc.wait(timeout=CRASH_AFTER_SEC)
        report.check("进程在强杀前仍在运行", False, f"它自己退出了（exit={proc.returncode}）")
    except subprocess.TimeoutExpired:
        report.check("强杀前进程仍在运行", True, f"已跑 {CRASH_AFTER_SEC:.0f}s")

    _kill_tree(proc.pid)
    time.sleep(2.0)
    report.check("进程已被强杀", proc.poll() is not None, f"exit={proc.poll()}")

    tasks = read_tasks()
    got = counts(tasks)
    done = [t for t in tasks.values() if t["status"] == "done"]
    mid = [t for t in tasks.values() if t["status"] in {"downloading", "transcribing", "framing"}]
    print(f"        强杀后 DB：{got}")
    report.check(
        "强杀时已完成了一部分（否则「不重跑」无从验证）",
        len(done) >= 1,
        f"done={len(done)}",
    )
    report.check(
        "留下了中途态（证明是被硬杀而非优雅退出）",
        len(mid) >= 1,
        f"中途态 {[t['status'] for t in mid]}",
    )
    snapshot = {
        t["id"]: {
            "status": t["status"],
            "updated_at": t["updated_at"],
            "transcript": _transcript_mtime(t),
        }
        for t in done
    }
    (ACC / "pre_crash.json").write_text(
        json.dumps({"tasks": snapshot, "counts": got}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return snapshot


def cmd_resume(report: Report, snapshot: dict[int, dict]) -> bool:
    report.section("判据 2 第二步：重启续跑，已完成的不重跑")
    proc = run_cli(
        ["run", "--file", str(URLS), "--trace", str(ACC / "trace_resume.json")],
        timeout=60 * 20,
    )
    print(f"        CLI 退出码 {proc.returncode}")
    for line in proc.stderr.splitlines():
        if "断点续跑" in line or "已完成，跳过" in line or "启动重置" in line:
            print(f"        {line.strip()}")

    report.check(
        "启动时重置了中途态",
        "启动重置" in proc.stderr or "回退" in proc.stderr,
        "（上次被强杀留下的中途态已回退到上一稳定态）",
    )
    report.check(
        "日志中出现「从断点续跑」",
        "断点续跑" in proc.stderr,
        "未完成的条目被续跑，而不是被当作已完成跳过",
    )

    tasks = read_tasks()
    got = counts(tasks)
    report.check(
        "续跑后 10 条 done + 1 条 failed",
        got.get("done") == CLIP_COUNT and got.get("failed") == 1,
        f"counts={got}",
    )

    # 核心判据：强杀前已完成的条目没有被碰过
    changed_status = []
    changed_stamp = []
    changed_file = []
    for task_id, before in snapshot.items():
        after = tasks.get(task_id)
        if after is None:
            changed_status.append(task_id)
            continue
        if after["status"] != before["status"]:
            changed_status.append(task_id)
        if after["updated_at"] != before["updated_at"]:
            changed_stamp.append(task_id)
        if _transcript_mtime(after) != before["transcript"]:
            changed_file.append(task_id)

    report.check(
        f"强杀前已完成的 {len(snapshot)} 条：状态未被改动",
        not changed_status,
        f"被改动的 {changed_status}",
    )
    report.check(
        f"强杀前已完成的 {len(snapshot)} 条：updated_at 未变（=数据库层没被重写）",
        not changed_stamp,
        f"被重写的 {changed_stamp}",
    )
    report.check(
        f"强杀前已完成的 {len(snapshot)} 条：转写稿文件未被重写（mtime 未变）",
        not changed_file,
        f"被重写的 {changed_file}",
    )
    return got.get("done") == CLIP_COUNT


def _transcript_mtime(task: dict) -> float | None:
    if not task.get("out_dir"):
        return None
    path = Path(task["out_dir"]) / "transcript.txt"
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _kill_tree(pid: int) -> None:
    """在 Windows 上强杀整棵进程树（python + 它 spawn 的 ffmpeg）。

    `errors="replace"` 不是可选项：`taskkill` 在进程树里已有成员先行退出时会往
    stderr 写一段本地代码页（cp936）的中文提示，按 UTF-8 解码会在读取线程里抛
    `UnicodeDecodeError`。那个异常不影响主线程，但会在输出里留下无关的 traceback，
    看起来像验收失败了。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    else:
        subprocess.run(
            ["kill", "-9", str(pid)], capture_output=True, text=True, errors="replace"
        )


# ---------------------------------------------------------------- 入口


def main(argv: list[str]) -> int:
    what = (argv[1] if len(argv) > 1 else "all").lower()
    report = Report()
    write_config()

    if not preflight(report):
        return report.summary()

    if what in ("all", "prep") and not cmd_prep(report, force=(what == "prep")):
        return report.summary()

    if what in ("all", "batch"):
        cmd_batch(report)

    if what in ("all", "crash"):
        snapshot = cmd_crash(report)
    else:
        pre = ACC / "pre_crash.json"
        snapshot = json.loads(pre.read_text(encoding="utf-8"))["tasks"] if pre.is_file() else {}
        snapshot = {int(k): v for k, v in snapshot.items()}

    if what in ("all", "resume"):
        if not snapshot:
            report.check("有强杀快照可供比对", False, "先跑 crash")
        else:
            cmd_resume(report, snapshot)

    return report.summary()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
