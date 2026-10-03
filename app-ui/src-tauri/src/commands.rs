//! Tauri 命令 —— 前端 `invoke` 的入口。
//!
//! 这一层刻意做薄：只做参数校验与转发，业务逻辑在 `worker` 模块。
//! 命令白名单与 `docs/IPC协议规格.md` §3 的命令清单一字对应 —— 前端的拼写错误
//! 会在这里被挡住，而不是变成一条 Python 侧的 `E_UNKNOWN_CMD`。

use serde_json::{json, Value};
use tauri::State;

use crate::worker::{StateSnapshot, Worker};

/// 规格 §3 定义的**全部**命令。改动此表必须先改规格文档。
const ALLOWED_COMMANDS: &[&str] = &[
    "hello",
    "ping",
    "add_tasks",
    "remove_task",
    "start",
    "pause",
    "resume",
    "cancel_task",
    "retry_task",
    "list_tasks",
    "get_concurrency",
    "shutdown",
];

/// 查当前连接状态。前端首次挂载时拉一次，避免错过启动期事件（规格 §4.2 的同一思路）。
#[tauri::command]
pub fn worker_state(worker: State<'_, Worker>) -> StateSnapshot {
    worker.snapshot()
}

/// 发送一条命令给 Python worker。
///
/// 返回命令 id（形如 `c-0001`），与随后事件信封里的 `id` 字段对应。
#[tauri::command]
pub fn worker_send(
    worker: State<'_, Worker>,
    name: String,
    args: Option<Value>,
) -> Result<String, String> {
    if !ALLOWED_COMMANDS.contains(&name.as_str()) {
        return Err(format!(
            "未知命令 {name:?}。合法命令见 docs/IPC协议规格.md §3：{}",
            ALLOWED_COMMANDS.join(", ")
        ));
    }
    worker.send(&name, args.unwrap_or_else(|| json!({})))
}

/// 请求 worker 优雅退出（发 `shutdown`，等它回 `bye` 后自行 exit）。
#[tauri::command]
pub fn worker_shutdown(worker: State<'_, Worker>) -> Result<(), String> {
    worker.request_shutdown();
    Ok(())
}
