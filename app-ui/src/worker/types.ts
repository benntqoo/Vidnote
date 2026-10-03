// 协议类型定义。
//
// ⚠️ 这些类型是 `docs/IPC协议规格.md` 的镜像。规格改了就改这里，不要各写各的。
// 字段名与 Rust 侧 `worker/protocol.rs`、Python 侧 `app/rpc.py` 三方一致。

/** 协议主版本（规格 §2） */
export const PROTOCOL_VERSION = 1;

/** Python → Rust → 前端 的事件信封（规格 §2.2） */
export interface WorkerEvent {
  v: number;
  type: string;
  name: string;
  /** 对某条命令的响应会回填命令 id；主动推送时为 null */
  id: string | null;
  /** 自增序号，从 1 开始 */
  seq: number;
  ts: number;
  data: Record<string, unknown>;
}

/** Rust 侧维护的连接状态快照（`worker/mod.rs::StateSnapshot`） */
export interface WorkerState {
  /** starting | ready | failed | exited */
  state: "starting" | "ready" | "failed" | "exited";
  detail: string;
  server_version: string | null;
  gpu_usable: boolean | null;
  /**
   * ⚠️ 三态布尔，**不要用 `!gpu_verified` 判断不可用**。
   * null = 尚未验证（不是「否」）——规格 §4.2。
   */
  gpu_verified: boolean | null;
  concurrency: Concurrency | null;
  alive: boolean;
  restart_count: number;
  /**
   * 调度器状态。Rust 侧从 `ready` / `pipeline_state` 事件累积而来，
   * 所以前端挂载时能一次性拿到，不必等下一次状态跃迁。
   */
  pipeline_state: PipelineState | null;
}

export interface Concurrency {
  n_asr: number;
  n_dl: number;
  n_frame: number;
  n_sum: number;
}

/** task 快照（规格 §5.1），`task_list` 与 `task_update` 共用 */
export interface TaskSnapshot {
  task_id: number;
  url: string;
  platform: string;
  title: string;
  duration_sec: number;
  status: TaskStatus;
  /** 中文阶段名，由 Python 侧 `rpc.STAGE_LABELS` 给出 */
  stage: string;
  /** -1.0 表示「进行中但进度不可知」，**不要显示成 0%**（规格 §5） */
  progress: number;
  elapsed_sec: number;
  retry_count: number;
  out_dir: string;
  error: string | null;
}

/** 状态机取值，与 `PLAN §6` 完全一致（规格 §5） */
export type TaskStatus =
  | "pending"
  | "downloading"
  | "downloaded"
  | "transcribing"
  | "transcribed"
  | "framing"
  | "summarizing"
  | "done"
  | "failed"
  | "cancelled";

/**
 * 「已停止推进」的状态集合 —— **只用于界面渲染**（决定要不要显示取消按钮、
 * 进度条要不要渲染不确定态）。
 *
 * ⚠️ 名字不是 `TERMINAL_STATUSES`：本项目的**终态只有 `done` 与 `cancelled`**
 * （`PLAN §6` / `app/models/task.py::TERMINAL_STATUSES`）。`failed` 不是终态——
 * 它必须显式 `retry` 才会重跑。这里把它算进来，只是因为「界面不再需要显示取消按钮」。
 * 两者含义不同，不要合并。
 */
export const SETTLED_STATUSES: ReadonlySet<TaskStatus> = new Set<TaskStatus>([
  "done",
  "failed",
  "cancelled",
]);

/** 调度器状态（`pipeline_state` 事件） */
export type PipelineState = "idle" | "running" | "paused" | "aborted" | "stopped";

/** 各状态的界面表现 */
export const STATUS_META: Record<TaskStatus, { label: string; tone: Tone }> = {
  pending: { label: "等待中", tone: "muted" },
  downloading: { label: "下载中", tone: "active" },
  downloaded: { label: "待抽音频", tone: "active" },
  transcribing: { label: "转写中", tone: "active" },
  transcribed: { label: "待抽帧", tone: "active" },
  framing: { label: "抽帧中", tone: "active" },
  summarizing: { label: "汇总中", tone: "active" },
  done: { label: "完成", tone: "ok" },
  // `failed` 与 `cancelled` 刻意不同色：前者是故障，后者是用户意图（规格 §5）
  failed: { label: "失败", tone: "error" },
  cancelled: { label: "已取消", tone: "warn" },
};

export type Tone = "muted" | "active" | "ok" | "warn" | "error";

/** 错误码 → 可操作提示（规格 §8） */
export const ERROR_HINTS: Record<string, string> = {
  E_COOKIE_EXPIRED: "cookie 已失效。请重新采集后重试该任务。",
  E_FFMPEG_MISSING: "定位不到可用的 ffmpeg。请安装后重启程序。",
  E_ENV_FATAL: "CUDA / 模型加载失败，整批已中止。修复环境后需手动重试。",
  E_DISK_FULL: "磁盘剩余空间不足 5 GB，已暂停取新任务。",
  E_BAD_ARGS: "命令参数有误（通常是前端与 worker 版本不一致）。",
  E_UNKNOWN_CMD: "worker 不认识这条命令，通常是版本不匹配。",
  E_INTERNAL: "worker 内部异常。请查看日志区的 traceback。",
};

/** 把秒格式化成 `m:ss` / `h:mm:ss` */
export function formatDuration(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return "--";
  const s = Math.floor(sec % 60);
  const m = Math.floor((sec / 60) % 60);
  const h = Math.floor(sec / 3600);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** 进度显示。**-1 表示不可知**，返回 null 让调用方渲染不确定态（规格 §5） */
export function formatProgress(p: number): string | null {
  if (!Number.isFinite(p) || p < 0) return null;
  return `${Math.round(p * 100)}%`;
}
