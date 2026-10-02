"""人用入口 + 阶段 1 验收入口。

    python -m app.cli env [--verify]        环境自检
    python -m app.cli run <输入...>          跑流水线（URL / 本地文件）
    python -m app.cli list                   查看任务表状态

定位：阶段 1 的临时入口。阶段 3 的 GUI 走 `app/rpc.py`，两者共用 `app/core/`。
**本文件只做参数解析与输出编排，不含任何业务逻辑**——业务逻辑进 core，
否则 Rust 侧接入时又要复制一遍（PLAN.md §4.2）。

进度与日志全部走 **stderr**，stdout 只输出最终结果（产出路径），
这样可以安全地 `python -m app.cli run ... > paths.txt`。
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from app.core import env_probe
from app.core.ffmpeg_locator import INSTALL_HINT as FFMPEG_HINT
from app.core.ffmpeg_locator import describe as describe_ffmpeg
from app.core.ffmpeg_locator import ffmpeg_available
from app.core.fetcher import find_yt_dlp
from app.core.pipeline import run_one
from app.core.store import Store
from app.core.urls import UNKNOWN_PLATFORM, extract_file, extract_many
from app.models.config import EXAMPLE_CONFIG_NAME, Config, ConfigError
from app.models.task import Task, TaskStatus

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
EPILOG = f"""示例：
  python -m app.cli env --verify
  python -m app.cli run "https://v.douyin.com/AbCdEf12345/"
  python -m app.cli run samples/video.mp4 --no-frames
  python -m app.cli run --file urls.txt --limit 10

ffmpeg 未安装时：{FFMPEG_HINT}
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Vidnote — 批量视频转结构化文稿",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c", "--config", default=None, help=f"配置文件路径（默认 {EXAMPLE_CONFIG_NAME} 的副本）"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 debug 日志")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出 warning 及以上")

    sub = parser.add_subparsers(dest="command", required=True)

    p_env = sub.add_parser("env", help="环境自检")
    p_env.add_argument(
        "--verify",
        action="store_true",
        help="额外跑一次真实推理验证 CUDA（约 5 秒，会占用 3 GB 显存）",
    )

    p_run = sub.add_parser("run", help="跑流水线")
    p_run.add_argument("inputs", nargs="*", help="URL 或本地视频文件路径（可多个）")
    p_run.add_argument("--file", help="从文本文件批量读取（每行一条）")
    p_run.add_argument("--work-dir", help="覆盖 paths.work_dir")
    p_run.add_argument("--device", choices=("auto", "cuda", "cpu"), help="覆盖 runtime.device")
    p_run.add_argument("--compute-type", help="覆盖 runtime.compute_type")
    p_run.add_argument("--model", help="覆盖 runtime.model")
    p_run.add_argument("--beam-size", type=int, help="覆盖 transcribe.beam_size")
    p_run.add_argument("--language", help="覆盖 transcribe.language")
    p_run.add_argument("--initial-prompt", help="覆盖 transcribe.initial_prompt（领域术语提示）")
    p_run.add_argument("--cookies", help="Netscape 格式 cookies.txt（抖音多数内容需要）")
    p_run.add_argument(
        "--frames", action=argparse.BooleanOptionalAction, default=None, help="是否抽帧拼版"
    )
    p_run.add_argument(
        "--keep-intermediate",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是否保留 video/audio（默认保留）",
    )
    p_run.add_argument("--retry", action="store_true", help="重跑已完成的任务")
    p_run.add_argument(
        "--force",
        action="store_true",
        help="强制重跑：删除已有产出目录并清空该任务的路径字段（验收重跑用）",
    )
    p_run.add_argument("--limit", type=int, help="最多处理前 N 条")
    p_run.add_argument("--dry-run", action="store_true", help="只入队不执行")

    sub.add_parser("list", help="列出任务表（状态 / 产出目录）")
    return parser


# ---------------------------------------------------------------- env


def cmd_env(args: argparse.Namespace) -> int:
    cfg, used_default = _load_config(args)
    if used_default:
        logging.info("未找到 config.yaml，使用默认配置（相对路径锚定到当前目录）")

    report = env_probe.probe(cfg.work_path())
    print("=" * 62)
    print("Vidnote 环境自检")
    print("=" * 62)

    cpu = report.cpu
    print(f"CPU      : {cpu.name or '未知'}  物理 {cpu.physical or '?'} 核 / 逻辑 {cpu.logical or '?'} 核")
    mem = report.memory
    print(
        f"内存     : 总 {mem.total_gb or '?'} GB，"
        f"可用 {mem.avail_gb or '?'} GB（占用 {mem.load_pct or '?'}%）"
        "   ← 并发数的第一约束"
    )
    for drive, info in report.disk.items():
        print(f"磁盘 {drive:3s} : 总 {info.total_gb} GB，可用 {info.free_gb} GB")
    for gpu in report.gpu:
        print(
            f"GPU      : {gpu.name}，{gpu.vram_total_mb} MB（已用 {gpu.vram_used_mb} MB），"
            f"驱动 {gpu.driver}，算力 {gpu.compute_cap}"
        )
    if not report.gpu:
        print("GPU      : 未检测到（nvidia-smi 不可用）")

    cuda = report.cuda
    print(
        f"CUDA     : ctranslate2 {cuda.ctranslate2 or '未导入'}，"
        f"设备数 {cuda.device_count if cuda.device_count is not None else '?'}"
        "   ← 仅代表检测到硬件，不代表可用"
    )
    if cuda.detail:
        print(f"           {cuda.detail}")

    print(f"ffmpeg   : {describe_ffmpeg(cfg.paths.ffmpeg)}")
    try:
        print(f"yt-dlp   : {find_yt_dlp(cfg.paths.yt_dlp)}")
    except Exception as exc:  # noqa: BLE001 — 自检不该因单项失败而中断
        print(f"yt-dlp   : 不可用（{exc}）")

    model = cfg.model_path()
    size_gb = _dir_size_gb(model)
    print(f"模型     : {model}  {'存在，' + f'{size_gb:.2f} GB' if size_gb else '⚠️ 不存在（见 README 第 3 步）'}")
    print(f"工作目录 : {cfg.work_path()}")

    if args.verify:
        print("-" * 62)
        device = cfg.runtime.effective_device(True)
        compute = cfg.runtime.effective_compute_type(device)
        print(f"真实推理验证：device={device} compute_type={compute}（约 5 秒）")
        ok, detail = env_probe.verify_cuda(cfg.model_path(), device=device, compute_type=compute)
        print(f"结果     : {'✅ GPU 可用' if ok else '❌ 不可用'} —— {detail}")
    else:
        print("-" * 62)
        print("提示：加 --verify 可跑一次真实推理确认 CUDA 真的能用（约 5 秒）。")
        print("      `设备数 = 1` 只说明检测到硬件，cuBLAS/cuDNN 缺失时同样返回 1。")

    print("=" * 62)
    return 0


# ---------------------------------------------------------------- run


def cmd_run(args: argparse.Namespace) -> int:
    cfg, used_default = _load_config(args)
    if used_default:
        logging.info("未找到 config.yaml，使用默认配置")

    if not ffmpeg_available(cfg.paths.ffmpeg):
        logging.error("未找到可用的 ffmpeg。\n  安装：%s", FFMPEG_HINT)
        return 2

    items = _collect_inputs(args)
    if not items:
        logging.error("没有可处理的输入。用法见 python -m app.cli run --help")
        return 2

    store = Store(cfg.root / "state.db")
    try:
        changed = store.reset_interrupted()
        if changed:
            logging.info("启动重置：%d 条中途态任务已回退（上次进程可能被强杀）", len(changed))

        queued: list[Task] = []
        skipped = 0
        for item in items:
            if item.platform == UNKNOWN_PLATFORM:
                logging.warning("无法识别的输入，将原样交给 yt-dlp 尝试：%s", item.raw[:80])
            task, created = store.add(Task(url=item.target, platform=item.platform))
            # --force 隐含 --retry：强制重跑当然包括「已存在」的任务
            if not created and not (args.retry or args.force):
                skipped += 1
                logging.info("已存在，跳过（--retry 可强制重跑）：%s", item.target)
                continue
            queued.append(task)

        if args.limit:
            queued = queued[: args.limit]
        if args.dry_run:
            print(f"待处理 {len(queued)} 条（dry-run，未执行）")
            return 0

        ok = fail = 0
        for index, task in enumerate(queued, 1):
            logging.info("─" * 60)
            logging.info("[%d/%d] 开始处理 task_id=%s：%s", index, len(queued), task.id, task.url)
            task.set_status(TaskStatus.PENDING)
            store.update(task)
            if args.force:
                _clear_outputs(task, cfg, store)
            try:
                run_one(
                    task,
                    cfg,
                    store,
                    on_progress=_progress_printer(task.id),
                    on_stage=_stage_printer(task.id),
                )
            except Exception:  # noqa: BLE001 — run_one 内已记录状态，这里只负责中止整批
                logging.error("环境级故障，中止整批。修复后可用 --retry 续跑。")
                fail += 1
                break
            if task.status == TaskStatus.DONE.value:
                ok += 1
                print(task.out_dir or "")
            else:
                fail += 1

        logging.info(
            "完成：成功 %d，失败 %d，跳过 %d（重复 %d）", ok, fail, len(queued) - ok - fail, skipped
        )
        return 0 if fail == 0 else 1
    finally:
        store.close()


def cmd_list(args: argparse.Namespace) -> int:
    cfg, _ = _load_config(args)
    store = Store(cfg.root / "state.db")
    try:
        tasks = store.list()
        if not tasks:
            print("任务表为空。")
            return 0
        print(f"{'id':>4}  {'状态':<12} {'时长':>6}  标题 / 产出目录")
        for task in tasks:
            duration = f"{task.duration_sec // 60}:{task.duration_sec % 60:02d}" if task.duration_sec else "  --"
            label = task.title or task.url
            print(f"{task.id:>4}  {task.status:<12} {duration:>6}  {label[:48]}")
            if task.stage_error:
                print(f"{'':>4}  ⚠️ {task.stage_error.splitlines()[0][:100]}")
        print()
        for status, count in sorted(store.count_by_status().items()):
            print(f"  {status}: {count}")
        return 0
    finally:
        store.close()


# ---------------------------------------------------------------- 辅助


def _load_config(args: argparse.Namespace) -> tuple[Config, bool]:
    """加载配置。文件缺失时回退默认值——「clone 下来即可运行」的要求。"""
    try:
        cfg, used_default = Config.load_or_default(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if args.command == "run":
        _apply_overrides(cfg, args)
    return cfg, used_default


def _apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    """把命令行开关盖到配置上。只覆盖显式给出的项。"""
    if args.work_dir:
        cfg.paths.work_dir = str(Path(args.work_dir).expanduser().absolute())
    if args.device:
        cfg.runtime.device = args.device
    if args.compute_type:
        cfg.runtime.compute_type = args.compute_type
    if args.model:
        cfg.runtime.model = args.model
    if args.beam_size:
        cfg.transcribe.beam_size = args.beam_size
    if args.language:
        cfg.transcribe.language = args.language
    if args.initial_prompt is not None:
        cfg.transcribe.initial_prompt = args.initial_prompt
    if args.cookies:
        cfg.paths.cookies_file = str(Path(args.cookies).expanduser().absolute())
    if args.frames is not None:
        cfg.frames.enabled = args.frames
    if args.keep_intermediate is not None:
        cfg.download.keep_intermediate = args.keep_intermediate
    cfg.validate()


def _clear_outputs(task: Task, cfg: Config, store: Store) -> None:
    """`--force`：删掉产出目录并清空路径字段，让流水线从头再跑。

    断点续跑逻辑（`pipeline` 里「已有视频/已有转写稿就跳过」）在验收重跑时
    反而碍事——同一个输入要跑多次比对基线，必须能真正重来一遍。

    删除前校验目录确实位于 `work_dir` 之下：`out_dir` 由 `dir_name()` 生成，
    正常情况下必然如此，但「强制删除」这种动作值得多一道保险。
    """
    if not task.out_dir:
        return
    out = Path(task.out_dir)
    if out.exists():
        if out.parent.resolve() != cfg.work_path().resolve():
            logging.error("拒绝删除 work_dir 之外的目录：%s", out)
        else:
            shutil.rmtree(out)
            logging.info("已删除产出目录：%s", out)
    task.video_path = None
    task.audio_path = None
    task.transcript_json = None
    store.update(task)


def _collect_inputs(args: argparse.Namespace) -> list:
    lines: list[str] = list(args.inputs)
    if args.file:
        items = extract_file(args.file)
        logging.info("从 %s 读取 %d 条", args.file, len(items))
        return items
    return extract_many(lines)


def _progress_printer(task_id: int | None):
    """把 `(stage, ratio)` 转成 stderr 上的稀疏进度行。

    只在百分比跨过 5% 的整数倍时打印——否则 649 段转写会刷出 649 行。
    """
    state = {"last": -1}

    def fn(stage: str, ratio: float) -> None:
        if ratio < 0:
            return
        pct = int(ratio * 100)
        if pct >= state["last"] + 5 or pct == 100:
            state["last"] = pct
            print(f"  [task {task_id}] {stage:<12} {pct:3d}%", file=sys.stderr, flush=True)

    return fn


def _stage_printer(task_id: int | None):
    def fn(task: Task) -> None:
        line = f"  [task {task_id}] → {task.status}"
        if task.stage_error:
            line += f"  ⚠️ {task.stage_error.splitlines()[0][:120]}"
        print(line, file=sys.stderr, flush=True)

    return fn


def _dir_size_gb(path: Path) -> float:
    if not path.is_dir():
        return 0.0
    total = 0
    for file in path.rglob("*"):
        try:
            if file.is_file():
                total += file.stat().st_size
        except OSError:
            continue
    return total / 1024**3


def _configure_logging(args: argparse.Namespace) -> None:
    level = logging.INFO
    if args.verbose:
        level = logging.DEBUG
    if args.quiet:
        level = logging.WARNING
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args)

    handlers = {"env": cmd_env, "run": cmd_run, "list": cmd_list}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
