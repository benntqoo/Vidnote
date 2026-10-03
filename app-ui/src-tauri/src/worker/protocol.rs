//! IPC 信封编解码 —— 对齐 `docs/IPC协议规格.md` §2。
//!
//! 协议是两端唯一的接口依据，本文件的字段名必须与规格**逐字一致**。
//! 改这里之前先改规格文档。

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// 协议主版本。与 `docs/IPC协议规格.md` §2 的 `v` 字段对应，当前恒为 1。
pub const PROTOCOL_VERSION: u32 = 1;

/// Rust → Python 命令信封（规格 §2.1）。
///
/// ```json
/// {"v": 1, "type": "cmd", "id": "c-0001", "name": "add_tasks", "args": {"urls": ["..."]}}
/// ```
#[derive(Debug, Serialize)]
pub struct Command<'a> {
    pub v: u32,
    #[serde(rename = "type")]
    pub kind: &'static str,
    /// 请求标识。**是字符串不是数字**（规格 §2.1 的示例为 `"c-0001"`）。
    pub id: &'a str,
    pub name: &'a str,
    /// 无参数时传 `{}`，不可省略（规格 §2.1 明确要求）。
    pub args: Value,
}

impl<'a> Command<'a> {
    pub fn new(id: &'a str, name: &'a str, args: Value) -> Self {
        Self {
            v: PROTOCOL_VERSION,
            kind: "cmd",
            id,
            name,
            args,
        }
    }
}

/// Python → Rust 事件信封（规格 §2.2）。
///
/// ```json
/// {"v":1,"type":"evt","name":"task_update","id":null,"seq":42,"ts":1759413000.123,"data":{}}
/// ```
///
/// `Serialize` 不是可选项：转发给前端要用 `emit(&ev)`，而 `emit` 要求 `Serialize + Clone`。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    #[serde(default)]
    pub v: u32,
    /// 规格里恒为 `"evt"`。保留为 String 是为了在版本不匹配时能记日志而不是解析失败。
    #[serde(rename = "type")]
    pub kind: String,
    pub name: String,
    /// 对某条命令的响应会回填命令 `id`；主动推送时为 `null`。
    #[serde(default)]
    pub id: Option<String>,
    /// 自增序号，从 1 开始。用于检测丢帧（规格 §2.2）。
    #[serde(default)]
    pub seq: u64,
    #[serde(default)]
    pub ts: f64,
    #[serde(default)]
    pub data: Value,
}

/// 生成命令 id：`c-0001`，对齐规格 §2.1 的示例格式。
pub fn make_command_id(serial: u64) -> String {
    format!("c-{serial:04}")
}

/// `hello` 握手命令的客户端版本号（规格 §3）。
pub const CLIENT_VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn command_id_format_matches_spec_example() {
        // 规格 §2.1 的示例是 "c-0001"
        assert_eq!(make_command_id(1), "c-0001");
        assert_eq!(make_command_id(1234), "c-1234");
    }

    #[test]
    fn command_serializes_with_type_field_not_kind() {
        let cmd = Command::new("c-0001", "hello", json!({"client_version": "0.1.0"}));
        let s = serde_json::to_string(&cmd).unwrap();
        assert!(s.contains(r#""type":"cmd""#), "必须是 type 不是 kind：{s}");
        assert!(s.contains(r#""id":"c-0001""#), "id 必须是字符串：{s}");
        assert!(s.contains(r#""v":1"#), "{s}");
        // 不得出现裸换行（JSON Lines 依赖 \n 分行，规格 §1）
        assert!(!s.contains('\n'));
    }

    #[test]
    fn event_parses_real_ready_frame() {
        // 这条是从真实 worker 抓下来的 ready 帧（2026-10-03 实测）
        let raw = r#"{"v": 1, "type": "evt", "name": "ready", "id": null, "seq": 2,
            "ts": 1791028239.367, "data": {"server_version": "0.2.0", "protocol": 1,
            "gpu_usable": true, "gpu_verified": false,
            "concurrency": {"n_asr": 1, "n_dl": 8, "n_frame": 4, "n_sum": 4},
            "db_path": "state.db", "config_is_default": false, "pipeline_state": "idle"}}"#
            .replace('\n', "");
        let ev: Event = serde_json::from_str(&raw).unwrap();
        assert_eq!(ev.kind, "evt");
        assert_eq!(ev.name, "ready");
        assert_eq!(ev.seq, 2);
        assert_eq!(ev.id, None);
        assert_eq!(ev.data["gpu_usable"], json!(true));
        assert_eq!(ev.data["concurrency"]["n_asr"], json!(1));
    }

    #[test]
    fn event_tolerates_missing_optional_fields() {
        // 缺 id / seq / ts 也不该解析失败（宁可收到事件，也不要因字段缺失丢帧）
        let ev: Event = serde_json::from_str(r#"{"type":"evt","name":"pong"}"#).unwrap();
        assert_eq!(ev.name, "pong");
        assert_eq!(ev.id, None);
        assert_eq!(ev.seq, 0);
    }
}
