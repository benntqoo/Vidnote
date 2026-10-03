# Vidnote IPC 协议规格（Rust 宿主 ↔ Python Worker）

> **版本**：v1.1
> **状态**：已定稿（v1 2026-10-02 / v1.1 2026-10-03），`app/rpc.py` 已按本规格实现**全部**命令与事件
> **定位**：本文件是 Rust 侧与 Python 侧之间**唯一的接口依据**。两端任何改动必须先改本文件。

---

## 1. 传输层约定

| 项 | 约定 |
|---|---|
| 通道 | 子进程的 **stdin**（Rust → Python）与 **stdout**（Python → Rust） |
| 帧格式 | **JSON Lines** —— 每行一个完整 JSON 对象，以 `\n` 结束，UTF-8 无 BOM |
| 无长度前缀 | 依赖 `\n` 分行。**JSON 内不得出现裸换行**（序列化时默认转义） |
| 端口 | **不使用**。零网络依赖，不触发防火墙 |

### 1.1 硬性约定：stdout 与 stderr 严格分离

> **stdout 只承载协议帧。任何日志、警告、进度打印一律走 stderr。**

违反此约定的后果是协议流被污染，Rust 端 JSON 解析失败。

Python 侧的实现责任（`app/rpc.py` 启动时立即执行）：

```python
_real_stdout = sys.stdout      # 协议唯一出口，先保存引用
sys.stdout = sys.stderr        # 之后任何 print 自动落到 stderr，无法污染协议
logging.basicConfig(stream=sys.stderr, ...)
```

**校验方法**：全程 stdout 的每一行都必须能被 `json.loads` 解析。这是一条可自动化的验收判据（见 §9）。

---

## 2. 消息信封

### 2.1 Rust → Python（命令）

```json
{"v": 1, "type": "cmd", "id": "c-0001", "name": "add_tasks", "args": {"urls": ["..."]}}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `v` | int | ✅ | 协议主版本，当前恒为 `1` |
| `type` | string | ✅ | 恒为 `"cmd"` |
| `id` | string | ⭕ | 请求标识。若提供，Python 的对应响应会带回同一个 `id` |
| `name` | string | ✅ | 命令名，见 §3 |
| `args` | object | ✅ | 命令参数，无参数时传 `{}`（不可省略） |

### 2.2 Python → Rust（事件）

```json
{"v": 1, "type": "evt", "name": "task_update", "id": null, "seq": 42, "ts": 1759413000.123, "data": {"...": "..."}}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `v` | int | ✅ | 协议主版本 |
| `type` | string | ✅ | 恒为 `"evt"` |
| `name` | string | ✅ | 事件名，见 §4 |
| `id` | string \| null | ✅ | 若该事件是对某条命令的响应，回填命令的 `id`；**主动推送时为 `null`** |
| `seq` | int | ✅ | 自增序号，从 1 开始。Rust 侧可用于检测丢帧 |
| `ts` | float | ✅ | Unix 时间戳（秒，含小数） |
| `data` | object | ✅ | 事件负载，无内容时传 `{}` |

---

## 3. 命令清单（Rust → Python）

| `name` | `args` | 对应响应 | 说明 |
|---|---|---|---|
| `hello` | `{"client_version": "0.1.0"}` | `ready` | 握手。**必须是第一条命令** |
| `ping` | `{}` | `pong` | 心跳，见 §6 |
| `add_tasks` | `{"urls": ["<url 或本地路径>", ...]}` | `tasks_added` | 批量入队，内部按 `PLAN §7.3` 正则抽 URL；`url` 字段有 UNIQUE 约束，重复项跳过 |
| `remove_task` | `{"task_id": 7}` | `task_removed` | 从队列删除（不影响已完成） |
| `start` | `{}` | `started` | 启动流水线（幂等，重复调用无副作用）。**只入队 `resumable()` 的任务**——即「非终态且非 failed」，`failed` 需走 `retry_task`，否则一条永久失效的链接每次点「开始」都会被自动重试一遍 |
| `pause` | `{}` | `paused` | 暂停取新任务；**已在跑的任务跑完当前阶段为止** |
| `resume` | `{}` | `resumed` | 继续 |
| `cancel_task` | `{"task_id": 7}` | `task_update` | 取消单条。运行中则置其 `cancel_event`，由阶段函数在**下一个检查点**中断并杀掉子进程 |
| `retry_task` | `{"task_id": 7}` | `task_update` | 重置失败项到上一稳定态并重新入队 |
| `list_tasks` | `{}` | `task_list` | 拉取全量任务快照（Rust 启动时同步一次） |
| `get_concurrency` | `{}` | `concurrency` | 查询当前实际并发数（用于状态栏展示） |
| `shutdown` | `{}` | `bye` | 优雅退出：停止取新任务 → 等待在跑的完成（最长 30s）→ 退出 |

**未知命令**：回 `error`，`code = "E_UNKNOWN_CMD"`，**不退出进程**。
**参数校验失败**：回 `error`，`code = "E_BAD_ARGS"`，**不退出进程**。
`task_id` 不存在（含 `cancel_task` / `retry_task` / `remove_task`）同样回 `E_BAD_ARGS`。

### 3.1 取消语义（v1.1 新增，实现约定）

取消是**协作式**的，不是强杀。`cancel_task` 只做到「置一个 `threading.Event`」，
真正的中断发生在阶段函数的内建检查点上：

| 阶段 | 检查点位置 | 取消延迟 | 中断后行为 |
|---|---|---|---|
| `downloading` | yt-dlp 每输出一行进度 | < 1 s | 杀 yt-dlp 进程树，抛 `TaskCancelled` |
| `audio` | ffmpeg 每输出一行 `-progress` | ≈ 0.5 s | 杀 ffmpeg，抛 `TaskCancelled` |
| `transcribing` | **每个 segment 边界** | 一条 segment 的时长 | 抛 `TaskCancelled`，**不写产出文件** |
| `framing` | 每检出一帧 | < 1 s | 抛 `TaskCancelled` |

> ⚠️ **转写只能在 segment 边界中断**。`CTranslate2` 是进程内的 C++ 调用，**没有中断接口**——
> 进程内取消不可能做成「立即返回」。这是硬约束，Rust 侧 UI 应把取消渲染成「正在取消…」而非瞬时完成。

两条实现上的硬要求（都是踩过的坑）：

1. **`cancel_event` 必须在阶段开始前就存在**。若在阶段启动后才创建，`cancel()` 会给一条
   正在跑的任务新建一个事件，而那个阶段永远看不到它——表现为「点了取消，任务照跑到结束」。
   正确做法是在投递任务的锁内先建好事件，阶段函数从字典里取。
2. **`TaskCancelled` 刻意不是 `StageError` 的子类**。取消是用户意图，不是故障：
   它不计入失败数，且**不写入 `INTERRUPTED_ROLLBACK`**——被取消的任务重启后是 `cancelled`
   终态，不会自己又跑起来。

---

## 4. 事件清单（Python → Rust）

| `name` | `data` 字段 | 说明 |
|---|---|---|
| `ready` | `{"server_version", "protocol", "gpu_usable", "gpu_verified", "concurrency": {"n_asr","n_dl","n_frame","n_sum"}, "pipeline_state", "db_path", "config_is_default"}` | 握手完成。**快速返回，不阻塞**（GPU 真实验证是异步的，见 §4.2） |
| `env_verified` | `{"gpu_usable", "detail", "concurrency"}` | GPU **真实推理验证**结果。异步推送，见 §4.2 |
| `pong` | `{}` | 心跳响应 |
| `tasks_added` | `{"added": [{"task_id","url","title"}], "skipped": [{"url","reason"}]}` | `reason`: `duplicate` \| `bad_url` \| `unsupported` |
| `task_removed` | `{"task_id": 7}` | — |
| `started` / `paused` / `resumed` | `{}` | — |
| `pipeline_state` | `{"state": "idle" \| "running" \| "paused" \| "aborted" \| "stopped"}` | 调度器状态跃迁。与 `started`/`paused`/`resumed` 是**两层**：后者是命令回执，本事件是调度器的实际状态 |
| `task_list` | `{"tasks": [<task 快照>, ...]}` | task 快照结构见 §5 |
| `concurrency` | `{"n_asr","n_dl","n_frame","n_sum"}` | — |
| `task_update` | `{"task_id","status","stage","progress","elapsed_sec","out_dir","error"}` | **核心事件**。状态或进度变化时推送，见 §5 |
| `resource_warning` | `{"reason","avail_gb"}` | 可用内存低于阈值，取新任务闸门已关闭。UI 建议提示「已暂停取新任务」 |
| `batch_aborted` | `{"reason"}` | 环境级故障（CUDA/模型）导致整批中止，剩余任务保持 `pending`。**UI 应弹可操作提示，不是「失败了」** |
| `batch_done` | `{"counts": {"done": 10, "failed": 1, ...}}` | 批次收敛 |
| `log` | `{"level","msg"}` | 转发 Python `logging` 记录。`level`: `debug`\|`info`\|`warning`\|`error` |
| `error` | `{"code","msg","fatal"}` | `fatal=true` 表示 Python 即将退出 |
| `bye` | `{}` | 退出前**最后一条**。发出后 Python 立即结束进程 |

### 4.1 推送频率约束

`task_update` 是高频事件，必须限流，否则会淹没 Rust 侧的事件循环：

- **状态变化**：立即推送
- **进度变化**：同一任务最快 **每 200 ms 一次**；或进度绝对增量 ≥ 1% 时提前推送
- **`log`**：沿用 `logging` 级别过滤，默认只发 `info` 及以上

> 落地常量在 `app/core/dispatcher.py`：`PROGRESS_MIN_INTERVAL_SEC = 0.2`、
> `PROGRESS_MIN_DELTA = 0.01`。限流在**调度器**里做，不在 `rpc.py` 里做——
> 因为 `cli.py` 也要吃同一份回调，重复限流会让两个入口的行为不一致。

**`batch_aborted` 会额外附一条 `error`**（`code = "E_ENV_FATAL"`，`fatal = false`）：
整批停了但 worker 仍能响应命令（`get_concurrency` / `shutdown` 照常），所以不是进程级致命。
这样 Rust 侧不用专门解析 `batch_aborted` 就能走统一的错误通道。

### 4.2 为什么 GPU 验证是异步的

`PLAN §9.1` 明确：`ctranslate2.get_cuda_device_count()` **返回 1 不代表 GPU 可用**——
它只枚举硬件，即使 cuBLAS/cuDNN 缺失也返回 1。唯一可信的判据是跑一次真实推理。

但真实推理要**加载模型（约 5 秒 + 3 GB 显存）**。若放在 `hello` 的同步路径上，
UI 要白屏等 5 秒才能拿到 `ready`。因此拆成两步：

| 阶段 | 事件 | `gpu_usable` 语义 |
|---|---|---|
| 握手（立即） | `ready` | 基于**快速探测**（`device_count` + ctranslate2 可导入性）。`gpu_verified = false` |
| 后台线程完成后 | `env_verified` | 基于**真实推理**。`verified = true` |

**Rust 侧的处理建议**：
1. 先按 `ready.gpu_usable` 渲染状态栏（此时可标「检测中…」）
2. 收到 `env_verified` 后覆写状态栏与并发数
3. `env_verified.gpu_usable = false` 且配置要求 GPU 时，给出**可操作**提示（不是一句"失败了"）

`env_verified` 与 `ready` 都携带 `concurrency`——因为 GPU 可用性变了，并发计划必须重算
（`app/core/concurrency.py::plan` 在 GPU 可用时把 `n_asr` 钳为 1）。

---

## 5. 状态与进度语义

`status` 取值与 `PLAN §6` 状态机**完全一致**，不得新增：

```
pending → downloading → downloaded → transcribing → transcribed
        → framing → summarizing → done
任意阶段 → failed
任意非终态 → cancelled          # v1.1 新增：取消是用户意图，不是故障
```

`cancelled` 与 `failed` 的区别（**Rust 侧不要合并渲染**）：

| | `failed` | `cancelled` |
|---|---|---|
| 触发 | `StageError` / 未预期异常 | 用户调用 `cancel_task` |
| 计入失败数 | 是 | **否** |
| 重启后 | 保持 `failed`，需 `retry_task` | 保持 `cancelled` 终态，**不会自己重跑** |
| 批次收尾 | 继续跑其余任务 | 继续跑其余任务 |

`stage` 是给 UI 显示的中文阶段名，与 `status` 一一对应：

| `status` | `stage`（UI 显示） | `progress` 语义 |
|---|---|---|
| `pending` | 等待中 | `0.0` |
| `downloading` | 下载中 | yt-dlp 已下载字节 / 总字节 |
| `downloaded` | 待抽音频 | `1.0` |
| `transcribing` | 转写中 | **当前 segment 结束时间 / 音频总时长**（来自 segment 时间戳，非估计值） |
| `transcribed` | 待抽帧 / 汇总 | `1.0` |
| `framing` | 抽帧中 | 已处理帧数 / 预计总帧数（预计值未知时传 `-1.0`） |
| `summarizing` | 汇总中 | `-1.0`（API 无进度） |
| `done` | 完成 | `1.0` |
| `failed` | 失败 | 保持失败时的值 |
| `cancelled` | 已取消 | 保持取消时的值 |

**`progress = -1.0` 约定**：表示「进行中但进度不可知」。UI 应显示不确定态进度条，**不要显示 0%**。

### 5.1 task 快照结构（`task_list` 与 `task_update` 共用字段）

```json
{
  "task_id": 7,
  "url": "https://v.douyin.com/AbCdEf12345/",
  "platform": "douyin",
  "title": "示例视频标题",
  "duration_sec": 1204,
  "status": "transcribing",
  "stage": "转写中",
  "progress": 0.45,
  "elapsed_sec": 80,
  "retry_count": 0,
  "out_dir": "D:/Code/SideProject/Vidnote/work/0007_示例视频标题",
  "error": null
}
```

字段名与 `app/models/task.py::Task` 及 `PLAN §6` 的 SQL 列**一一对应**，不另起名。

---

## 6. 心跳与超时

| 参数 | 值 | 责任方 |
|---|---|---|
| Rust 发 `ping` 间隔 | **5 s** | Rust |
| Python 必须回 `pong` 时限 | **2 s** | Python |
| Rust 判定 worker 死亡 | 连续 **2 次**（即 ~10 s）无 `pong` | Rust |
| 优雅关闭等待 | **30 s** | Rust |

### 6.1 关键实现要求：独立线程读 stdin

> **Python 侧必须用独立线程读取 stdin，不能在主循环里同步读。**

原因：转写阶段单条视频要跑 **2 分 18 秒**（`PLAN §2.2`）。若主线程阻塞在转写上，`pause` / `cancel_task` / `ping` 全部无法响应，UI 会「卡死」。

实现形态：

```
主线程         → 流水线调度（core.dispatcher）
stdin 读取线程  → 逐行解析 JSON，投递到命令队列
stdout 写入     → 加锁后单线程写入（JSON Lines 天然按行独立，但要防交错）
```

**stdout 写入必须加锁**：`log` 事件可能从多个 worker 线程产生，不加锁会出现两行 JSON 交错成一行，直接毁掉协议。

---

## 7. 进程生命周期与回收（Windows）

| 环节 | 要求 |
|---|---|
| 启动 | Rust 侧 spawn 时设置 `CREATE_NO_WINDOW`（防止弹出黑框控制台） |
| 绑定 | Rust 侧用 **Job Object** 并设 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，父进程被强杀时子进程随之终止 |
| 管道 | stdin / stdout / stderr **三条都必须接管道**。stderr 若不消费，缓冲区满会让 Python 阻塞 |
| 正常退出 | Rust 发 `shutdown` → Python 回 `bye` → Python 自行 `exit(0)` |
| 异常退出 | Python 侧读到 **stdin EOF** 即认为宿主已消失，立即退出（不等待） |
| 崩溃恢复 | Rust 侧检测到 worker 退出后**自动重启一次**；再失败则提示用户并展示 stderr 尾部 |

**Python 侧必须实现 EOF 退出**：这是防止孤儿进程的最后一道防线——即使 Job Object 因权限问题失效，父进程消失也会关闭管道。

---

## 8. 错误码

| `code` | 含义 | `fatal` | Rust 侧建议动作 |
|---|---|---|---|
| `E_BAD_FRAME` | 收到的行不是合法 JSON | false | 记日志，跳过该行 |
| `E_UNKNOWN_CMD` | 未知命令名 | false | 记录，通常是版本不匹配 |
| `E_BAD_ARGS` | 参数缺失或类型错误 | false | 记录，提示用户 |
| `E_COOKIE_EXPIRED` | cookie 失效（`PLAN §9.5`） | false | **弹可操作提示**，引导用户重新采集 |
| `E_FFMPEG_MISSING` | 定位不到可用 ffmpeg | **true** | 展示安装命令，不重启 |
| `E_ENV_FATAL` | CUDA / 模型加载失败 | **true** | 展示 stderr 尾部，不重启（重启也没用） |
| `E_DISK_FULL` | 剩余空间不足 5 GB | false | 告警，暂停取新任务 |
| `E_INTERNAL` | 未预期异常 | false | 附 traceback，重启 worker |
| `E_NOT_IMPLEMENTED` | 命令已定义但尚未实现 | false | 记录。**v1.1 起无任何命令返回此码**（`start`/`pause`/`resume`/`cancel_task`/`retry_task` 已随 `dispatcher.py` 落地）；保留该项是为了阶段 4 的新命令 |

> **`E_ENV_FATAL` 会中止整批**，与 `PLAN §4.1` 的设计原则一致：转写是唯一「失败即整批中止」的阶段，因为那意味着环境坏了，继续跑只会浪费一整夜。

---

## 9. 验收判据

协议层的验收**不依赖 Rust 端**，用 Python 写一个 mock client 即可全部覆盖。
落地实现：`tools/rpc_smoke.py`，**实跑 19/19 通过**（2026-10-03，`python tools/rpc_smoke.py`）。

| # | 判据（脚本内的实际名称） | 方法 / 实测 |
|---|---|---|
| 1 | **协议纯净性** | 全程 stdout 每一行都必须能 `json.loads`。**这条能抓住所有 stray print**。实测 35/35 行合法 |
| 2 | **`seq` 连续** | 严格递增且无跳号。实测 1..35 无跳号 |
| 3 | 握手 `hello→ready` | `ready.data.gpu_usable` 必须是布尔值（不是 `0/1`、不是字符串），且回填命令 `id` |
| 4 | 未知命令不崩 | 回 `E_UNKNOWN_CMD`，且**随后 `ping` 仍通**（证明进程没死） |
| 5 | 非法 JSON 不崩 | 发 `this is not json at all` → 回 `E_BAD_FRAME` |
| 6 | 心跳 <2 s | 往返实测 0 ms |
| 7 | `add_tasks` 抽 URL + 批内去重 | 3 行输入 → 入队 2 条（分享文案里的 URL 被正确抽出） |
| 8 | `add_tasks` 跨批次去重 | 重复链接以 `reason=duplicate` 出现在 `skipped` 里 |
| 9 | `list_tasks` | 返回全量快照 |
| 10 | `remove_task` | 清空队列 |
| 11 | `start` 幂等 | 空队列 `start` → `started`，`added=0` |
| 12 | `pause` → `paused` | 被接受 |
| 13 | `resume` → `resumed` | 被接受 |
| 14 | `cancel_task` 非法 id | 回 `E_BAD_ARGS`，进程存活 |
| 15 | `retry_task` 非法 id | 回 `E_BAD_ARGS`，进程存活 |
| 16 | `env_verified` 异步推送 | 真实推理验证结果异步到达：`device=cuda`、`gpu_usable=True` |
| 17 | 优雅关闭 `shutdown→bye` | 收到 `bye` |
| 18 | 退出码为 0 | worker 自行 `exit(0)` |
| 19 | **EOF 自杀（3 s 内）** | 关掉 stdin 模拟宿主消失，worker 必须自行退出，不留孤儿。实测 0.02 s |

**判据 1 是最有价值的一条**：它用一个断言覆盖了「任何地方不小心 `print` 到 stdout」这一整类
协议污染事故。新增任何代码后重跑它，成本几秒。

> ⚠️ **判据 19 曾经「存在但不被报告」**（2026-10-03 修复）。它单开一个 worker（因为要自杀，
> 不能复用主流程那个），却把结果 `record` 到自己身上，而 `main()` 只打印主 host 的判据
> → 那条判据从未出现在输出里，脚本却显示 18/18。**一个悄悄不报告的判据比没有判据更危险**：
> 它制造虚假的信心。现在结果列表改为显式传入。
> 教训可以推广成一条规则：**凡是「新开一个对象来承载结果」的测试代码，都要确认结果真的被收集了。**

> ⚠️ 判据 6 的「心跳」测的是**空闲态**往返（0 ms），**不能**据此断言「转写进行中心跳仍通」。
> 转写阻塞主循环是「UI 假死」的典型来源，真正验证它需要在转写进行中发 `ping`——
> 那需要在协议测试里塞一条真实转写任务。**本条尚未覆盖，如实标注。**

---

## 10. 变更流程

本协议是两端共同契约。任何字段增删：

1. 先改本文件，并在下方「变更记录」追加一行
2. `v` 主版本号递增（不兼容改动）或保持（兼容性新增）
3. 同步改 `app/rpc.py` 与该文档 §3 / §4 的清单
4. 重跑 §9 全部判据

### 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-02 | v1 | 首版。确立 stdio + JSON Lines、命令/事件清单、状态语义、心跳、进程回收与验收判据 |
| 2026-10-03 | v1.1 | ① 新增 `cancelled` 状态与 §3.1 取消语义（协作式取消、检查点位置、两条硬要求）；② 事件清单补齐调度器事件：`pipeline_state` / `resource_warning` / `batch_aborted` / `batch_done`，`task_update` 之外的控制面事件改名映射（`dispatcher` 只报事实，名字翻译在 `rpc.py` 的 `EVENT_NAMES`）；③ `ready` 增 `pipeline_state`；④ §4.1 补限流落地常量与「限流在调度器做，不在 rpc 做」的理由；⑤ §9 判据从 8 条扩到 19 条（含新修好的 EOF 判据），并标注两条**尚未覆盖**的项（转写进行中的心跳、控制面事件在真实批次下的时序）；⑥ `E_NOT_IMPLEMENTED` 已无命令返回 |
