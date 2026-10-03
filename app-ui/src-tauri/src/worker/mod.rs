//! Python worker 的监督器。
//!
//! 职责：spawn 常驻 Python 进程 → 握手 → 双向转发 JSON Lines → 心跳 → 崩溃上报。
//! 协议细节见 `docs/IPC协议规格.md`，本模块不自行发明约定。
//!
//! 线程模型（与 Python 侧对称）：
//! ```text
//!   stdout 读线程  → 逐行解析 Event → 更新状态 + emit 到前端
//!   stderr 读线程  → 逐行转发（Python 日志全在这里，规格 §1.1）
//!   心跳线程       → 每 5s 发 ping，连续 2 次无 pong 判定死亡
//!   命令发送       → 加锁写 stdin（来自 Tauri 命令，任意线程）
//! ```

pub mod jobobject;
pub mod locator;
pub mod protocol;

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};

// 注意：`protocol::Command` 是**协议信封**（会被 serde 序列化后写进 stdin），
// 与上面 `std::process::Command`（用来 spawn 进程）同名但完全不同。
// 这里必须起别名，否则本模块里 `Command::new` 会解析到 std 的那个，
// 报出「参数个数不符」+「Command 未实现 Serialize」一串连锁错误。
// 用 `WireCommand` 这个显式名字，让「这是要写进管道的东西」在读代码时一眼可见。
use protocol::{make_command_id, Command as WireCommand, Event, CLIENT_VERSION};

/// 心跳间隔（规格 §6）
const PING_INTERVAL: Duration = Duration::from_secs(5);
/// 连续多少次无 pong 判定 worker 死亡（规格 §6）
const MISSED_PONG_LIMIT: u32 = 2;

/// 前端监听的事件通道
pub const EVENT_CHANNEL: &str = "worker://event";
/// 前端监听的连接状态通道
pub const STATE_CHANNEL: &str = "worker://state";

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 给前端的连接状态快照。字段扁平，前端不必再解嵌套。
#[derive(Debug, Clone, Serialize)]
pub struct StateSnapshot {
    /// `starting` | `ready` | `failed` | `exited`
    pub state: String,
    pub detail: String,
    pub server_version: Option<String>,
    pub gpu_usable: Option<bool>,
    pub gpu_verified: Option<bool>,
    pub concurrency: Option<Value>,
    /// worker 进程是否仍在运行
    pub alive: bool,
    /// 已重启次数（规格 §7：自动重启一次）
    pub restart_count: u32,
}

impl StateSnapshot {
    fn starting() -> Self {
        Self {
            state: "starting".into(),
            detail: "正在启动 Python worker…".into(),
            server_version: None,
            gpu_usable: None,
            gpu_verified: None,
            concurrency: None,
            alive: false,
            restart_count: 0,
        }
    }

    fn failed(detail: impl Into<String>) -> Self {
        Self {
            state: "failed".into(),
            detail: detail.into(),
            ..Self::starting()
        }
    }
}

struct Inner {
    app: AppHandle,
    /// 写端。None 表示 worker 未运行。
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    /// worker 是否存活（stdout 线程结束即置 false）
    alive: AtomicBool,
    /// 命令 id 自增序号
    serial: AtomicU64,
    /// 期望收到的下一个 seq（丢帧检测，规格 §2.2）
    expected_seq: AtomicU64,
    /// 最近一次收到 pong 的时刻
    last_pong: Mutex<Instant>,
    /// 连续未收到 pong 的次数
    missed_pongs: AtomicU64,
    /// 当前状态快照
    snapshot: Mutex<StateSnapshot>,
    /// 是否已主动关闭（区分「正常退出」与「崩溃」）
    shutting_down: AtomicBool,
    /// 心跳线程退出信号
    stop_heartbeat: AtomicBool,
    /// 绑定 worker 的 Job Object 句柄。
    ///
    /// **必须存在这里，不能是 `spawn_worker` 的局部变量。** 本作业设了
    /// `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，语义是「关闭本作业的**最后一个句柄**
    /// 时终止作业内所有进程」。所以句柄一旦被 drop，作业里的 Python 立刻被杀。
    /// 之前这里写的是 `let _job = jobobject::bind_child(&child);` —— 局部绑定在
    /// `spawn_worker` 返回时就 drop 了，于是**防孤儿的机制反过来把刚 spawn 的
    /// worker 当场杀死**：终端里能看到「已 spawn pid=NNNN」，紧接着进程就没了，
    /// 而且一句 stderr 都没有（进程被 TerminateProcess，来不及写）。
    ///
    /// 放进 `Inner` 后，生命周期就与监督器一致：句柄只在 `Inner` 被 drop
    /// （进程退出路径）时关闭，那时连带终止 worker 正是想要的语义。
    ///
    /// 完整排查过程见 `docs/阶段3_打通记录.md` §3。
    job: Mutex<Option<jobobject::JobObject>>,
}

/// 监督器句柄。内部是 `Arc<Inner>`，可作为 Tauri State 跨命令/跨线程共享。
#[derive(Clone)]
pub struct Worker {
    inner: Arc<Inner>,
}

impl Worker {
    /// 创建并启动。**不阻塞**——握手是异步的，前端先渲染「启动中」，
    /// 收到 `ready` 后再覆写（规格 §4.2 的同一思路）。
    pub fn start(app: AppHandle) -> Self {
        let inner = Arc::new(Inner {
            app,
            stdin: Mutex::new(None),
            child: Mutex::new(None),
            alive: AtomicBool::new(false),
            serial: AtomicU64::new(0),
            expected_seq: AtomicU64::new(1),
            last_pong: Mutex::new(Instant::now()),
            missed_pongs: AtomicU64::new(0),
            snapshot: Mutex::new(StateSnapshot::starting()),
            shutting_down: AtomicBool::new(false),
            stop_heartbeat: AtomicBool::new(false),
            job: Mutex::new(None),
        });

        let worker = Self { inner };
        worker.spawn_worker();
        worker.start_heartbeat();
        worker
    }

    /// 当前状态快照。前端首次挂载时主动拉一次，避免错过启动期的事件。
    pub fn snapshot(&self) -> StateSnapshot {
        self.inner.snapshot.lock().unwrap().clone()
    }

    /// 向 worker 发送命令。返回命令 id，便于与响应对应。
    pub fn send(&self, name: &str, args: Value) -> Result<String, String> {
        let serial = self.inner.serial.fetch_add(1, Ordering::SeqCst) + 1;
        let id = make_command_id(serial);
        let cmd = WireCommand::new(&id, name, args);
        let mut line = serde_json::to_string(&cmd)
            .map_err(|e| format!("命令序列化失败：{e}"))?;
        debug_assert!(!line.contains('\n'), "JSON Lines 不允许裸换行");

        let mut guard = self.inner.stdin.lock().unwrap();
        let writer = guard
            .as_mut()
            .ok_or_else(|| "worker 未运行，命令被拒绝".to_string())?;

        line.push('\n');
        writer
            .write_all(line.as_bytes())
            .and_then(|_| writer.flush())
            .map_err(|e| {
                // 写失败通常意味着 worker 已死。置 alive=false 让 UI 立即反映。
                self.inner.alive.store(false, Ordering::SeqCst);
                format!("写入 stdin 失败（worker 可能已退出）：{e}")
            })?;

        Ok(id)
    }

    /// 优雅关闭：发 `shutdown`。worker 会回 `bye` 后自行退出（规格 §7）。
    pub fn request_shutdown(&self) {
        self.inner.shutting_down.store(true, Ordering::SeqCst);
        let _ = self.send("shutdown", json!({}));
    }

    /// 强制关闭。挂在应用退出路径上，保证进程内**一定**有人动手杀子进程。
    ///
    /// 回收顺序是三道，这里是最后一道：
    ///   1. 优雅 `shutdown`（Python 侧 30s 上限）
    ///   2. Job Object（父进程被强杀时连带终止）
    ///   3. 本函数
    ///
    /// **为什么第 2 道不足以替代第 3 道**：`jobobject::bind_child` 在绑定失败时只
    /// 打印一句警告就返回 `None`（例如 worker 已被别的 Job 收编时
    /// `AssignProcessToJobObject` 会失败）。那种情况下第 2 道防线是**空的**，
    /// 只剩这里能保证不留孤儿 Python 进程。
    ///
    /// 对已退出的子进程幂等：先 `try_wait` 拿到结果，已退出就不再 kill。
    pub fn force_kill(&self) {
        self.inner.shutting_down.store(true, Ordering::SeqCst);
        self.inner.stop_heartbeat.store(true, Ordering::SeqCst);

        // 先丢 stdin：这一下就让 Python 侧的 EOF 自杀防线生效，
        // 于是「kill 失败」也还有退路，同时避免有心跳线程还在往里写。
        *self.inner.stdin.lock().unwrap() = None;

        if let Some(mut child) = self.inner.child.lock().unwrap().take() {
            match child.try_wait() {
                Ok(Some(_)) => {} // 已自行退出，不必杀
                _ => {
                    let _ = child.kill();
                }
            }
            // 必须 wait 回收句柄；否则子进程成僵尸，且本函数返回时它还没真正结束。
            let _ = child.wait();
        }

        self.inner.alive.store(false, Ordering::SeqCst);
    }

    // ---------------------------------------------------------------- spawn

    fn spawn_worker(&self) {
        let target = match locator::locate() {
            Ok(t) => t,
            Err(msg) => {
                self.set_state(StateSnapshot::failed(msg));
                return;
            }
        };

        let mut child = match spawn_child(&target) {
            Ok(c) => c,
            Err(msg) => {
                self.set_state(StateSnapshot::failed(msg));
                return;
            }
        };

        // Job Object 必须在 spawn 后尽快绑定，否则期间派生的后代不在作业内。
        //
        // **存进 `inner.job`，不要用局部变量接。** 作业设了
        // `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，句柄被 drop = 作业内进程全被杀。
        // 写成 `let _job = ...` 的话，本函数一返回就 drop，等于刚 spawn 就杀掉
        // worker（详见 `Inner::job` 字段的注释）。重启时这里会替换掉旧句柄，
        // 顺带把上一代作业里的残留进程清掉。
        let job = jobobject::bind_child(&child);
        *self.inner.job.lock().unwrap() = job;

        // 成功路径同样要留痕：否则「窗口开了但 Python 进程不在」时，
        // 终端里既没有失败原因、也没有成功证据，只能靠猜。
        eprintln!(
            "[worker] 已 spawn pid={} program={} cwd={}（来源于 {}）",
            child.id(),
            target.program.display(),
            target.cwd.display(),
            target.source
        );

        let stdout = child.stdout.take().expect("stdout 已设为 piped");
        let stderr = child.stderr.take().expect("stderr 已设为 piped");
        let stdin = child.stdin.take().expect("stdin 已设为 piped");

        *self.inner.stdin.lock().unwrap() = Some(stdin);
        *self.inner.child.lock().unwrap() = Some(child);
        self.inner.alive.store(true, Ordering::SeqCst);
        self.inner.expected_seq.store(1, Ordering::SeqCst);
        *self.inner.last_pong.lock().unwrap() = Instant::now();
        self.inner.missed_pongs.store(0, Ordering::SeqCst);

        {
            let mut snap = self.inner.snapshot.lock().unwrap();
            snap.state = "starting".into();
            snap.detail = format!("已启动：{}", target.source);
            snap.alive = true;
        }
        self.inner.emit_state();

        // stdout 读线程：协议帧唯一来源
        {
            let inner = self.inner.clone();
            std::thread::spawn(move || {
                let reader = BufReader::new(stdout);
                for line in reader.lines() {
                    match line {
                        Ok(l) => {
                            if l.trim().is_empty() {
                                continue;
                            }
                            inner.handle_frame(&l);
                        }
                        Err(e) => {
                            eprintln!("[worker] stdout 读取失败：{e}");
                            break;
                        }
                    }
                }
                inner.on_worker_exit();
            });
        }

        // stderr 读线程：**必须消费**，否则缓冲区满会让 Python 阻塞（规格 §7）
        {
            let inner = self.inner.clone();
            std::thread::spawn(move || {
                let reader = BufReader::new(stderr);
                for line in reader.lines() {
                    match line {
                        Ok(l) => {
                            if l.trim().is_empty() {
                                continue;
                            }
                            eprintln!("[py] {l}");
                            // 同时转给前端日志区，`source` 让前端区分来源
                            inner.emit_log("debug", "stderr", l);
                        }
                        Err(_) => break,
                    }
                }
            });
        }

        // 握手：必须是第一条命令（规格 §3）
        if let Err(e) = self.send("hello", json!({"client_version": CLIENT_VERSION})) {
            self.set_state(StateSnapshot::failed(format!("握手发送失败：{e}")));
        }
    }

    // ----------------------------------------------------------- 事件分发

    fn start_heartbeat(&self) {
        let inner = self.inner.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(PING_INTERVAL);
            if inner.stop_heartbeat.load(Ordering::SeqCst) || !inner.alive.load(Ordering::SeqCst) {
                break;
            }
            // 先判断上一轮是否超时，再发新的 ping
            let last = *inner.last_pong.lock().unwrap();
            if last.elapsed() > PING_INTERVAL + Duration::from_secs(2) {
                let missed = inner.missed_pongs.fetch_add(1, Ordering::SeqCst) + 1;
                inner.emit_log("warning", "supervisor", format!("心跳超时（第 {missed} 次未收到 pong）"));
                if missed >= MISSED_PONG_LIMIT as u64 {
                    inner.alive.store(false, Ordering::SeqCst);
                    // 必须经由统一发射口。这里原来直接 `app.emit(STATE_CHANNEL, ..)`，
                    // 绕开了日志，于是「心跳判死」这条最重要的异常路径在终端里查不到。
                    *inner.snapshot.lock().unwrap() = StateSnapshot {
                        state: "exited".into(),
                        detail: format!("心跳中断：连续 {missed} 次未收到 pong"),
                        alive: false,
                        ..StateSnapshot::starting()
                    };
                    inner.emit_state();
                    break;
                }
            }
            if inner.send_raw("ping", json!({})).is_err() {
                break;
            }
        });
    }

    fn set_state(&self, snap: StateSnapshot) {
        *self.inner.snapshot.lock().unwrap() = snap;
        self.inner.emit_state();
    }
}

impl Inner {
    /// 处理一行协议帧。任何解析失败都**不中断读循环**——丢一帧好过整个 worker 失联。
    fn handle_frame(&self, line: &str) {
        let ev: Event = match serde_json::from_str(line) {
            Ok(ev) => ev,
            Err(e) => {
                eprintln!("[worker] 协议帧解析失败，已跳过：{e}\n  原始行：{line}");
                return;
            }
        };

        // 丢帧检测（规格 §2.2：seq 从 1 开始自增）
        if ev.seq > 0 {
            let expected = self.expected_seq.load(Ordering::SeqCst);
            if ev.seq != expected {
                let missed = ev.seq.saturating_sub(expected);
                if missed > 0 {
                    eprintln!("[worker] 检测到丢帧：期望 seq={expected}，收到 {}（丢 {missed} 帧）", ev.seq);
                }
            }
            self.expected_seq.store(ev.seq + 1, Ordering::SeqCst);
        }

        if ev.name == "pong" {
            *self.last_pong.lock().unwrap() = Instant::now();
            self.missed_pongs.store(0, Ordering::SeqCst);
            return; // pong 不必转给前端
        }

        // 状态类事件：同步到快照，让 `worker_state` 命令能返回最新值
        match ev.name.as_str() {
            "ready" => {
                let mut snap = self.snapshot.lock().unwrap();
                snap.state = "ready".into();
                snap.detail = "worker 已就绪".into();
                snap.alive = true;
                snap.server_version = ev.data["server_version"].as_str().map(str::to_string);
                snap.gpu_usable = ev.data["gpu_usable"].as_bool();
                snap.gpu_verified = ev.data["gpu_verified"].as_bool();
                snap.concurrency = Some(ev.data["concurrency"].clone());
                drop(snap);
                self.emit_state();
            }
            "env_verified" => {
                let mut snap = self.snapshot.lock().unwrap();
                snap.gpu_usable = ev.data["gpu_usable"].as_bool();
                snap.gpu_verified = Some(true);
                if !ev.data["concurrency"].is_null() {
                    snap.concurrency = Some(ev.data["concurrency"].clone());
                }
                drop(snap);
                self.emit_state();
            }
            "bye" => {
                self.shutting_down.store(true, Ordering::SeqCst);
            }
            _ => {}
        }

        // 全量转发给前端。前端按 name 自行分发。
        if let Err(e) = self.app.emit(EVENT_CHANNEL, &ev) {
            eprintln!("[worker] 事件转发失败（{}）：{e}", ev.name);
        }
    }

    /// 日志事件的**唯一**发射口。
    ///
    /// 与 `emit_state` 同样的理由：手写 `app.emit(EVENT_CHANNEL, json!{..})` 的地方
    /// 一旦多起来，就会出现「有的路径记了、有的没记」。
    ///
    /// `source` 区分来源（`stderr` = Python 侧日志，`supervisor` = Rust 监督器），
    /// 前端日志区直接展示。**不要**把 Python 的 stdout/stderr 都塞成同一个 source——
    /// 排查时「这行是 Python 说的还是 Rust 说的」是第一个要回答的问题。
    fn emit_log(&self, level: &str, source: &str, msg: impl Into<String>) {
        let _ = self.app.emit(
            EVENT_CHANNEL,
            json!({
                "v": protocol::PROTOCOL_VERSION,
                "type": "evt",
                "name": "log",
                "id": null,
                "seq": null,
                "ts": 0.0,
                "data": {"level": level, "msg": msg.into(), "source": source}
            }),
        );
    }

    /// 状态快照的**唯一**发射口。所有状态变更都必须经过这里。
    ///
    /// 之前这里有两个几乎相同的函数（`Worker::emit_state` 与 `emit_state_locked`），
    /// 差别只在加锁写法；另有一条心跳路径直接 `app.emit(STATE_CHANNEL, ..)`，
    /// 把两者都绕开了。结果是「有的状态跃迁能查到、有的查不到」——排查时最容易
    /// 误判成「状态机没走到某一步」。合并成一个并把日志放在这里，就不可能漏记。
    ///
    /// 调用约定：调用方须已释放 `snapshot` 的锁（本函数内部会再取一次）。
    fn emit_state(&self) {
        let snap = self.snapshot.lock().unwrap().clone();
        // 状态跃迁必须同时落 stderr。否则失败在终端里**完全不可见**——
        // 例如 `locate()` / spawn 失败只写进 UI 快照，而那一刻前端往往还没挂载，
        // 现象就是「窗口开了、一句话没有、Python 进程也不在」，无从下手。
        // 这一行是排查这类静默失败的主要入口，不要删。
        eprintln!(
            "[worker] state → {}（{}）alive={} restart={} ver={:?} gpu={:?}/{:?}",
            snap.state,
            snap.detail,
            snap.alive,
            snap.restart_count,
            snap.server_version,
            snap.gpu_usable,
            snap.gpu_verified
        );
        if let Err(e) = self.app.emit(STATE_CHANNEL, snap) {
            eprintln!("[worker] 状态事件发送失败：{e}");
        }
    }

    /// stdout 读到 EOF —— worker 已退出。
    ///
    /// 退出**必须同时进日志区**，不能只更新状态快照：状态栏只显示 detail 的首行，
    /// 而用户排查时的第一反应是去翻日志 —— 那里若还停在崩溃前的最后一条，
    /// 得到的印象会是「一切正常，然后就没了」。
    ///
    /// 实测（2026-10-03 崩溃注入）：不加这一条时，杀了 worker 之后日志区最后一行
    /// 仍是崩溃前的 `环境验证：GPU 可用`，看不出已经出事。
    fn on_worker_exit(&self) {
        let already_dead = !self.alive.swap(false, Ordering::SeqCst);
        self.stop_heartbeat.store(true, Ordering::SeqCst);
        *self.stdin.lock().unwrap() = None;

        let code = self
            .child
            .lock()
            .unwrap()
            .as_mut()
            .and_then(|c| c.wait().ok())
            .and_then(|s| s.code());

        if self.shutting_down.load(Ordering::SeqCst) {
            let detail = format!("worker 已正常退出（code={code:?}）");
            self.emit_log("info", "supervisor", detail.clone());
            let mut snap = self.snapshot.lock().unwrap();
            snap.state = "exited".into();
            snap.detail = detail;
            snap.alive = false;
            drop(snap);
            self.emit_state();
            return;
        }

        if already_dead {
            return; // 心跳线程已报过，不重复
        }

        let detail = format!(
            "worker 意外退出（code={code:?}）。规格 §7 要求自动重启一次；当前版本先如实上报，未自动重启。"
        );
        self.emit_log("error", "supervisor", detail.clone());
        let mut snap = self.snapshot.lock().unwrap();
        snap.state = "exited".into();
        snap.detail = detail;
        snap.alive = false;
        drop(snap);
        self.emit_state();
    }

    /// 心跳线程用的无锁快路径（`Worker::send` 的薄包装）。
    fn send_raw(&self, name: &str, args: Value) -> Result<String, String> {
        let serial = self.serial.fetch_add(1, Ordering::SeqCst) + 1;
        let id = make_command_id(serial);
        let cmd = WireCommand::new(&id, name, args);
        let mut line = serde_json::to_string(&cmd).map_err(|e| e.to_string())?;
        let mut guard = self.stdin.lock().unwrap();
        let writer = guard.as_mut().ok_or("worker 未运行")?;
        line.push('\n');
        writer
            .write_all(line.as_bytes())
            .and_then(|_| writer.flush())
            .map_err(|e| e.to_string())?;
        Ok(id)
    }
}

impl Drop for Inner {
    fn drop(&mut self) {
        self.stop_heartbeat.store(true, Ordering::SeqCst);
        if let Some(c) = self.child.lock().unwrap().as_mut() {
            let _ = c.kill();
        }
    }
}

/// 启动子进程。**三处细节不能省**：
/// 1. `CREATE_NO_WINDOW` —— 否则会闪一个黑色控制台窗口（规格 §7）
/// 2. `PYTHONUTF8/IOENCODING` —— 否则 Python 按 cp936 输出，父子两端编码不一致
/// 3. 三条管道全部 piped —— stderr 不接会让 Python 在缓冲区满时阻塞（规格 §7）
fn spawn_child(target: &locator::WorkerTarget) -> Result<Child, String> {
    let mut cmd = Command::new(&target.program);
    cmd.args(&target.args)
        .current_dir(&target.cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1");

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    cmd.spawn().map_err(|e| {
        format!(
            "启动 worker 失败：{}\n  工作目录：{}\n  错误：{e}",
            target.program.display(),
            target.cwd.display()
        )
    })
}
