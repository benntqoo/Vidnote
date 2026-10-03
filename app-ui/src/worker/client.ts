// Rust 宿主的调用封装。前端的唯一出口，组件不直接 import `@tauri-apps/api`。
//
// 命名与 `docs/IPC协议规格.md` §3 的命令表一一对应，方便对照查找。

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

import type { WorkerEvent, WorkerState } from "./types";

/** Rust 侧 emit 的事件通道（与 `worker/mod.rs` 的常量一致） */
export const EVENT_CHANNEL = "worker://event";
export const STATE_CHANNEL = "worker://state";

// ---------------------------------------------------------------- 连接状态

/** 拉一次连接状态快照。前端首次挂载时必须调一次，补齐启动期错过的事件。 */
export function getWorkerState(): Promise<WorkerState> {
  return invoke<WorkerState>("worker_state");
}

export function onState(cb: (s: WorkerState) => void): Promise<UnlistenFn> {
  return listen<WorkerState>(STATE_CHANNEL, (e) => cb(e.payload));
}

export function onEvent(cb: (ev: WorkerEvent) => void): Promise<UnlistenFn> {
  return listen<WorkerEvent>(EVENT_CHANNEL, (e) => cb(e.payload));
}

// ------------------------------------------------------------------ 命令层

/** 透传一条命令。`name` 必须是规格 §3 里的命令 —— Rust 侧有白名单校验。 */
export function sendCommand(
  name: string,
  args: Record<string, unknown> = {},
): Promise<string> {
  return invoke<string>("worker_send", { name, args });
}

export function shutdownWorker(): Promise<void> {
  return invoke<void>("worker_shutdown");
}

// ---------------------------------------------------------------- 业务语义

/** 批量入队。支持一次粘贴多条（含抖音分享文案，Python 侧负责抽 URL）。 */
export function addTasks(urls: string[]): Promise<string> {
  return sendCommand("add_tasks", { urls });
}

export function removeTask(taskId: number): Promise<string> {
  return sendCommand("remove_task", { task_id: taskId });
}

/** 启动流水线。幂等；只入队非终态且非 failed 的任务。 */
export function startPipeline(): Promise<string> {
  return sendCommand("start");
}

/**
 * 暂停取新任务。**已在跑的任务会跑完当前阶段为止**（规格 §3），
 * 所以界面应显示「正在暂停…」而不是瞬时完成。
 */
export function pausePipeline(): Promise<string> {
  return sendCommand("pause");
}

export function resumePipeline(): Promise<string> {
  return sendCommand("resume");
}

/**
 * 取消单条。**协作式**：转写只能在 segment 边界中断（规格 §3.1），
 * 界面要渲染成「正在取消…」。
 */
export function cancelTask(taskId: number): Promise<string> {
  return sendCommand("cancel_task", { task_id: taskId });
}

/** 重试失败项。`failed` 不是终态，必须显式重试才会重跑。 */
export function retryTask(taskId: number): Promise<string> {
  return sendCommand("retry_task", { task_id: taskId });
}

export function listTasks(): Promise<string> {
  return sendCommand("list_tasks");
}

export function getConcurrency(): Promise<string> {
  return sendCommand("get_concurrency");
}
