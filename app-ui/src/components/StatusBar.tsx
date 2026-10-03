// 状态栏：连接状态 / GPU 可用性 / 实际并发 / 调度器状态。
//
// ⚠️ `gpu_verified === null` 表示**尚未验证**，不是「不可用」。
// 这里必须显示成「检测中…」，与 `PLAN §9.1` 的三态布尔约定一致。

import type { PipelineState, WorkerState } from "../worker/types";

interface Props {
  worker: WorkerState | null;
  pipeline: PipelineState;
}

const PIPELINE_LABELS: Record<PipelineState, string> = {
  idle: "空闲",
  running: "运行中",
  paused: "已暂停",
  aborted: "已中止",
  stopped: "已停止",
};

function gpuView(w: WorkerState | null): { text: string; tone: string; title: string } {
  if (!w) return { text: "连接中…", tone: "muted", title: "" };
  if (w.state === "failed")
    return { text: "worker 启动失败", tone: "error", title: w.detail };
  if (w.state === "exited")
    return { text: "worker 已退出", tone: "error", title: w.detail };

  // 三态：null 是「未知」，别用 falsy 判断
  if (w.gpu_verified === null) {
    return {
      text: "GPU 检测中…",
      tone: "warn",
      title: "快速探测已通过，正在后台跑一次真实推理做最终确认",
    };
  }
  if (w.gpu_verified && w.gpu_usable) {
    return { text: "GPU 可用", tone: "ok", title: "已通过真实推理验证" };
  }
  if (!w.gpu_usable) {
    return {
      text: "GPU 不可用",
      tone: "error",
      title: "将按 CPU 模式规划并发（吞吐下降约 6 倍），详见日志区",
    };
  }
  return { text: "GPU 状态未知", tone: "muted", title: "" };
}

export function StatusBar({ worker, pipeline }: Props) {
  const gpu = gpuView(worker);
  const conc = worker?.concurrency;
  const connTone =
    worker?.state === "ready"
      ? "ok"
      : worker?.state === "starting"
        ? "warn"
        : worker?.state === "failed" || worker?.state === "exited"
          ? "error"
          : "muted";

  return (
    <div className="statusbar">
      <span className={`chip chip-${connTone}`} title={worker?.detail ?? ""}>
        <span className="dot" />
        {worker?.state === "ready"
          ? `worker ${worker.server_version ?? ""}`
          : (worker?.state ?? "connecting")}
      </span>

      <span className={`chip chip-${gpu.tone}`} title={gpu.title}>
        {gpu.text}
      </span>

      {conc && (
        <span className="chip chip-muted" title="实际并发：转写 / 下载 / 抽帧 / 汇总">
          并发 转写{conc.n_asr} 下载{conc.n_dl} 抽帧{conc.n_frame}
        </span>
      )}

      <span className={`chip chip-${pipeline === "running" ? "active" : "muted"}`}>
        流水线 {PIPELINE_LABELS[pipeline] ?? pipeline}
      </span>

      <span className="spacer" />

      {worker?.state === "failed" && (
        <span className="statusbar-detail" title={worker.detail}>
          {worker.detail.split("\n")[0]}
        </span>
      )}
    </div>
  );
}
