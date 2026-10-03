//! Vidnote 桌面端宿主（Tauri 2）。
//!
//! 架构：本进程是 **UI + 监督器**，全部重活由常驻 Python worker 承担（`PLAN §7.1`）。
//! 两者之间是 stdin/stdout 的 JSON Lines 协议，零端口，契约见 `docs/IPC协议规格.md`。

mod commands;
mod worker;

use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            // worker 在 setup 里就启动，不等前端 ready —— 窗口渲染与 Python 冷启动并行，
            // 端到端少等几百毫秒。前端挂载后用 `worker_state` 拉一次快照补齐状态。
            let handle = app.handle().clone();
            app.manage(worker::Worker::start(handle));
            Ok(())
        })
        .on_window_event(|window, event| {
            // 窗口关闭时**先请** worker 优雅退出：发 `shutdown`，Python 侧自行收尾。
            // 这里不阻塞等待——真正的兜底在下面 `RunEvent::Exit` 与 Job Object。
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if let Some(w) = window.app_handle().try_state::<worker::Worker>() {
                    w.request_shutdown();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::worker_state,
            commands::worker_send,
            commands::worker_shutdown,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        // 应用退出的最后一道回收。**不能省**：`jobobject::bind_child` 绑定失败时只
        // 降级为警告，那时 Job Object 这道防线是空的（详见 `Worker::force_kill`）。
        // 也不能挪到 `CloseRequested` 里——那时只是在「请求关闭」，应用还没退，
        // 提前杀会截断 Python 的收尾；挪到 `Exit` 则既晚于优雅 `shutdown`，
        // 又早于进程真正消失，位置正好。
        if let tauri::RunEvent::Exit = event {
            if let Some(w) = app_handle.try_state::<worker::Worker>() {
                w.force_kill();
            }
        }
    });
}
