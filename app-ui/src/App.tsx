// 主界面：把协议事件流接到 UI 上。
//
// 本文件只做「事件 → 状态 → 视图」的映射，不含协议细节：
// - 通道名、命令名、类型定义全在 `worker/` 下，与 `docs/IPC协议规格.md` 对齐
// - 组件只接收普通 props，不 import `@tauri-apps/api`
//
// 两条容易写错的地方，都已按规格处理：
// 1. `task_update` 是**增量**语义（规格 §4）。这里按 task_id 合并进已有快照，
//    而不是整表替换 —— 否则并发时后到的部分更新会把别的任务抹掉。
// 2. `progress < 0` 表示「进行中但进度不可知」，不是 0%（规格 §5）。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { revealItemInDir } from "@tauri-apps/plugin-opener";

import "./App.css";
import { AddTasksBar } from "./components/AddTasksBar";
import { LogPanel, type LogEntry } from "./components/LogPanel";
import { StatusBar } from "./components/StatusBar";
import { TaskTable } from "./components/TaskTable";
import * as api from "./worker/client";
import type {
  PipelineState,
  TaskSnapshot,
  WorkerEvent,
  WorkerState,
} from "./worker/types";

/** 日志区保留的最大条数。超出后丢最旧的 —— 长跑不设上限会吃光内存。 */
const MAX_LOGS = 500;

function App() {
  const [worker, setWorker] = useState<WorkerState | null>(null);
  const [tasks, setTasks] = useState<Record<number, TaskSnapshot>>({});
  const [pipeline, setPipeline] = useState<PipelineState>("idle");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const logSeq = useRef(0);

  const pushLog = useCallback(
    (level: LogEntry["level"], msg: string, source?: string) => {
      logSeq.current += 1;
      const entry: LogEntry = { key: logSeq.current, level, msg, source };
      setLogs((prev) => {
        const next = prev.length >= MAX_LOGS ? prev.slice(prev.length - MAX_LOGS + 1) : prev.slice();
        next.push(entry);
        return next;
      });
    },
    [],
  );

  /** 拉全量任务快照。`task_list` 事件到达后也会调用，保证与库一致。 */
  const refreshTasks = useCallback(() => {
    api.listTasks().catch(() => {
      /* worker 可能尚未 ready，属正常，静默即可 */
    });
  }, []);

  const handleEvent = useCallback(
    (ev: WorkerEvent) => {
      switch (ev.name) {
        case "ready": {
          const p = ev.data.pipeline_state;
          if (typeof p === "string") setPipeline(p as PipelineState);
          break;
        }
        case "pipeline_state":
        case "state": {
          const s = ev.data.state;
          if (typeof s === "string") setPipeline(s as PipelineState);
          break;
        }
        case "task_list": {
          const list = ev.data.tasks;
          if (Array.isArray(list)) {
            const map: Record<number, TaskSnapshot> = {};
            for (const t of list as TaskSnapshot[]) map[t.task_id] = t;
            setTasks(map);
          }
          break;
        }
        case "task_update": {
          const patch = ev.data as unknown as TaskSnapshot;
          if (typeof patch?.task_id !== "number") break;
          setTasks((prev) => ({
            ...prev,
            [patch.task_id]: { ...prev[patch.task_id], ...patch },
          }));
          break;
        }
        case "tasks_added": {
          const added = ev.data.added as unknown[] | undefined;
          const skipped = ev.data.skipped as { url?: string; reason?: string }[] | undefined;
          pushLog(
            skipped?.length ? "warning" : "info",
            `入队 ${added?.length ?? 0} 条${skipped?.length ? `，跳过 ${skipped.length} 条` : ""}`,
          );
          for (const s of skipped ?? []) {
            pushLog("warning", `跳过 ${s.url ?? "?"}（${s.reason ?? "未知原因"}）`);
          }
          refreshTasks();
          break;
        }
        case "task_removed":
          refreshTasks();
          break;
        case "log": {
          const level = (ev.data.level as LogEntry["level"]) ?? "info";
          pushLog(level, String(ev.data.msg ?? ""), ev.data.source as string | undefined);
          break;
        }
        case "error": {
          // 环境级故障会同时发 `batch_aborted` 与 `error(fatal=false)`，
          // 这里照实显示，不合并 —— 前者说「整批停了」，后者说「为什么」。
          pushLog("error", `${ev.data.code ?? "E_?"}：${ev.data.msg ?? ""}`);
          break;
        }
        case "resource_warning":
        case "batch_aborted": {
          pushLog("warning", `${ev.name}：${JSON.stringify(ev.data)}`);
          break;
        }
        case "batch_done": {
          pushLog("info", `整批结束：${JSON.stringify(ev.data)}`);
          refreshTasks();
          break;
        }
        default:
          break;
      }
    },
    [pushLog, refreshTasks],
  );

  useEffect(() => {
    let disposed = false;
    const unlisteners: (() => void)[] = [];

    // 先订阅再拉快照：反过来会漏掉「拉快照」与「订阅生效」之间的事件。
    // 若 effect 已被回收（React 严格模式会跑两次），拿到句柄后立即注销。
    api.onState((s) => {
      if (!disposed) setWorker(s);
    }).then((fn) => (disposed ? fn() : unlisteners.push(fn)));

    api.onEvent((ev) => {
      if (!disposed) handleEvent(ev);
    }).then((fn) => (disposed ? fn() : unlisteners.push(fn)));

    api
      .getWorkerState()
      .then((snap) => {
        if (disposed) return;
        setWorker(snap);
        if (snap.pipeline_state) setPipeline(snap.pipeline_state);
      })
      .catch((e) => pushLog("error", `拉取连接状态失败：${e}`));

    refreshTasks();

    return () => {
      disposed = true;
      for (const fn of unlisteners) fn();
    };
  }, [handleEvent, pushLog, refreshTasks]);

  /** 命令失败要显式落到日志区 —— Rust 侧返回的 Err 不弹窗，容易静默。 */
  const run = useCallback(
    (label: string, p: Promise<unknown>) => {
      p.catch((e) => pushLog("error", `${label}失败：${e}`));
    },
    [pushLog],
  );

  const ordered = useMemo(
    () => Object.values(tasks).sort((a, b) => a.task_id - b.task_id),
    [tasks],
  );

  const busy = worker?.state === "ready";

  return (
    <div className="app">
      <header className="toolbar">
        <span className="brand">Vidnote</span>
        <AddTasksBar
          disabled={!busy}
          onSubmit={(text) => {
            // 抽 URL 是 Python 侧的责任（`urls.extract_many`），前端不做正则，
            // 否则两处规则会打架。这里只按行传过去。
            const lines = text
              .split(/\r?\n/)
              .map((s) => s.trim())
              .filter(Boolean);
            run("加入队列", api.addTasks(lines));
          }}
        />

        <span className="divider" />

        <button
          className="primary"
          disabled={!busy || pipeline === "running"}
          onClick={() => run("开始", api.startPipeline())}
          title="只入队未完成且非失败的任务（failed / cancelled 需在该行点「重试」）"
        >
          开始
        </button>
        <button
          disabled={!busy || pipeline !== "running"}
          onClick={() => run("暂停", api.pausePipeline())}
          title="已在跑的任务会跑完当前阶段为止"
        >
          暂停
        </button>
        <button
          disabled={!busy || pipeline !== "paused"}
          onClick={() => run("继续", api.resumePipeline())}
        >
          继续
        </button>

        <span className="spacer" />

        <button className="mini" onClick={() => refreshTasks()} title="重新拉取任务表">
          刷新
        </button>
      </header>

      <StatusBar worker={worker} pipeline={pipeline} />

      <TaskTable
        tasks={ordered}
        onCancel={(id) => run(`取消 #${id}`, api.cancelTask(id))}
        onRetry={(id) => run(`重试 #${id}`, api.retryTask(id))}
        onRemove={(id) => run(`移除 #${id}`, api.removeTask(id))}
        onOpenDir={(path) =>
          revealItemInDir(path).catch((e) => pushLog("error", `打开目录失败 ${path}：${e}`))
        }
      />

      <LogPanel entries={logs} onClear={() => setLogs([])} />
    </div>
  );
}

export default App;
