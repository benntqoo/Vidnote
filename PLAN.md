# Vidnote — 开发计划书

> **项目**：Vidnote（Video → Note）批量视频内容提取与结构化汇总工具
> **仓库**：`D:/Code/SideProject/Vidnote`
> **阶段**：阶段 1 进行中（Python 功能层基础设施已完成，见 §4 目录树的 ✓ 标记）
>
> **交付声明**
> 本文件为**规划产物**，未编写任何程序实现代码。
> 为完成「本机能力上限」实测（你在 2026-10-02 明确要求动态侦测本地配置取极限值），做了两件环境层的事，如实记录：
> 1. 安装了 `nvidia-cublas-cu12 12.9.2.10` + `nvidia-cudnn-cu12 9.27.0.42` + `nvidia-cuda-nvrtc-cu12 12.9.86`（腾讯云源，1分48秒）
> 2. 修改了临时验证脚本（现位于 `prototypes/transcribe.py`），补上 CUDA 运行库加载逻辑（§9.1 记录了为何必须这样做）
>
> 你的项目代码、配置、`~/.workbuddy` 下任何内容均未改动。
>
> **项目迁移说明**（2026-10-02）：本计划书原写于临时工作缓存目录，现已迁入正式仓库。
> 迁移时同步做了：路径引用更新、原型脚本归入 `prototypes/`、实测数据独立成 `docs/实测记录.md`、
> 测试基线归入 `samples/`。**计划内容本身未改动。**

---

## 1. 目标与范围

| 项 | 内容 |
|---|---|
| 输入 | ① 视频链接列表（抖音 / B站 / YouTube，混平台）② 本地视频文件 ③ 链接文本文件（txt/csv 每行一条） |
| 输出 | 每条视频一个独立目录：转写稿（txt/srt/json）、画面拼版（可选）、结构化汇总（可选） |
| 形态 | **桌面 GUI**（按你的选择） |
| 批量规模 | **无数量上限**；并发度由程序启动时探测硬件动态决定 |
| 运行环境 | 本机 Windows，离线可跑（汇总环节除外） |

**明确不做的事**：不做视频转码/剪辑，不做多语言（先只支持中文视频），不做云端同步。

---

## 2. 本机能力实测（本计划书所有并发数字的依据）

数据来源：`tools/probe_env.py` 实跑输出（完整原始记录见 `docs/实测记录.md`）。

| 部件 | 实测值 | 对设计的影响 |
|---|---|---|
| CPU | AMD Ryzen 9 9950X，**32 逻辑核**，4.29 GHz | 抽帧/抽音频可并行 |
| 内存 | 总 **61.6 GB**，可用**在 12.6 ~ 26.2 GB 之间波动**（同日两次测量差一倍） | ⚠️ 内存是**第一约束**，并发数必须运行时探测，绝不能写死 |
| GPU | RTX 4060 Ti，**16 GB 显存**（已用 1.97 GB），驱动 610.88，算力 8.9 | 转写主力 |
| C 盘 | 511 GB 总 / **150 GB 可用** | 放程序与模型 |
| D 盘 | 3214 GB 总 / **988 GB 可用** | ✅ **工作目录必须放 D 盘** |
| Python | 3.13.14（托管环境） | — |

### 2.1 转写性能实测（最关键的数据）

同一条 20 分 04 秒音频（1204 秒），两种后端对比：

| 后端 | 配置 | 实测耗时 | 实时倍率 | 20 分钟视频折算 |
|---|---|---|---|---|
| **CPU** | `int8`，32 线程 | **13 分 54 秒** | 1.44× | 约 14 分钟 |
| **GPU** | `float16`，large-v3 | **2 分 18 秒** | **8.7×** | 约 2.3 分钟 |

**⚠️ GPU 比 CPU 快 6.0 倍。CPU 方案在批量场景下完全不可用**：100 条 20 分钟视频在 CPU 上需要约 **23 小时**，在 GPU 上约 **3.8 小时**。因此 **GPU 支持是本工具的前置硬要求，不是可选项。**

### 2.2 GPU 端到端实测详情

环境：RTX 4060 Ti 16GB（驱动 610.88）、`large-v3` + `float16`、`beam_size=5`、开启 VAD 过滤、音频 1204 秒 16kHz mono。

| 指标 | 实测值 |
|---|---|
| 端到端耗时 | **137.7 秒**（含约 5 秒模型加载 + CUDA 上下文初始化） |
| 纯推理耗时 | 约 133 秒 |
| 实时倍率 | **8.7×**（纯推理约 9.05×） |
| 输出段数 | 649 段（CPU 版为 646 段，差异来自分段边界而非内容） |
| 显存占用 | 模型加载后约 3 GB |

**吞吐量换算（用于估算批量耗时）**：

| 批量规模 | 假设平均时长 | 纯转写耗时 | 含下载+抽帧的总耗时预估 |
|---|---|---|---|
| 10 条 | 20 分钟 | 约 23 分钟 | 约 25–30 分钟 |
| 50 条 | 20 分钟 | 约 1.9 小时 | 约 2–2.4 小时 |
| 100 条 | 20 分钟 | 约 3.8 小时 | 约 4–4.8 小时 |

> 总耗时预估已计入流水线并行：下载（4~8 并发）与抽帧（4 并发）应在转写进行时同步完成，所以总时长约等于纯转写时长 × 1.05~1.25。**这个系数是设计推断，阶段 2 的批量验收会实测校验。**

---

## 3. 架构设计

### 3.1 核心判断：转写是串行瓶颈，其余环节必须围绕它设计

一个常见的错误设计是"开 N 个线程，每个线程跑完整流程"。在本机上行不通：

- **GPU 只有 1 块**。large-v3 float16 约占 3 GB 显存，理论上 16 GB 可塞 4 个实例，但 GPU 算力是分时复用的——**2 个实例并行不等于 2 倍吞吐，通常反而更慢**（上下文切换 + 显存带宽争抢）。
- **内存仅剩 12.6 GB**。每个 CPU 转写实例约占 3 GB，最多并行 4 个，但那样会挤爆系统。
- 而**下载、抽帧**是 I/O 或轻 CPU 任务，单任务只需几百 MB，完全可以高并发。

所以正确的模型是**流水线**，而不是"并行 N 个全流程"：

```
                 ┌──────────────────────────────────────────────┐
   URL 列表 ───▶ │  任务队列 (SQLite 持久化)                     │
                 └──────────────────────────────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │ 下载池  × N_dl  (4~8)   │  网络密集，可高并发
                    └────────────┬────────────┘
                                 │ video.mp4
                    ┌────────────▼────────────┐
                    │ 抽音频池 × N_a  (2~4)   │  ffmpeg，快
                    └────────────┬────────────┘
                                 │ audio.wav (16k mono)
                    ┌────────────▼────────────┐
                    │ 转写器  × 1  ★瓶颈★      │  GPU 串行，独占
                    └────────────┬────────────┘
                                 │ transcript.{txt,srt,json}
                    ┌────────────▼────────────┐
                    │ 抽帧池  × N_f  (2~4)    │  ffmpeg 场景检测 + 拼版
                    └────────────┬────────────┘
                                 │ sheet_*.jpg
                    ┌────────────▼────────────┐
                    │ 汇总池  × N_s  (2~4)    │  仅 API 模式，网络等待
                    └────────────┬────────────┘
                                 │ 汇总.md
                                 ▼
                              完成

  所有阶段之间用**有界队列**（默认容量 2）连接，防止下载跑太快把磁盘/内存撑爆。
```

### 3.2 并发数计算规则（动态探测，不写死）

程序启动时按下面的顺序算，每个数字都有依据：

```python
def plan_concurrency(cfg, env):
    mem = env.memory.avail_gb          # 实测 12.6，随系统负载变化

    # ① 转写：GPU 可用则强制 1（单卡串行是吞吐最优解）
    if env.gpu_usable:
        n_asr, mem_per_asr = 1, 1.5     # GPU 模式下主机内存占用低
    else:
        n_asr = max(1, min(2, int(mem // 3)))   # CPU 模式每个约 3GB
        mem_per_asr = 3.0

    # ② 下载：网络密集，内存占用小，但受内存总量与带宽约束
    n_dl = max(1, min(cfg.max_download, int(mem // 0.5), 8))

    # ③ 抽帧：CPU 密集且单任务短；32 核 → 4
    n_frame = max(1, min(cfg.max_frame, (env.cpu.logical or 4) // 8))

    # ④ 汇总：纯网络等待，可放宽
    n_sum = max(1, min(cfg.max_summarize, 4))

    # ⑤ 总内存预算校验，超了就按比例回退
    budget = n_asr * mem_per_asr + n_dl * 0.5 + n_frame * 0.6 + 2.0  # 2.0 为常驻开销
    if budget > mem * 0.8:
        scale = (mem * 0.8) / budget
        n_dl, n_frame = max(1, int(n_dl * scale)), max(1, int(n_frame * scale))

    return Concurrency(n_asr=n_asr, n_dl=n_dl, n_frame=n_frame, n_sum=n_sum)
```

**本机预期取值**：`n_asr=1, n_dl=8, n_frame=4, n_sum=4`。

**运行时保护**：每启动一个新任务前重新读一次可用内存；低于 2 GB 时暂停取新任务，已在跑的跑完为止。**不要用固定 sleep 轮询，用事件驱动。**

### 3.3 为什么不做"多 GPU 并行"

本机只有一块卡，方案里预留 `n_asr = gpu_count`（多卡机器上自动变成并行），但在本机恒为 1。写代码时留这个口子即可，不要为它做额外设计。

---

## 4. 模块划分

```
Vidnote/                                  # 仓库根
├── app/                                  # ★Python 功能层（阶段 1 起填充）
│   ├── core/
│   │   ├── env_probe.py                  #   ✓已完成  硬件探测 ← tools/probe_env.py
│   │   ├── cuda_dll.py                   #   ✓已完成  CUDA DLL 注入（§9.1 三层要求）
│   │   ├── ffmpeg_locator.py             #   ✓已完成  ffmpeg 定位（§9.4 四级顺序）
│   │   ├── errors.py                     #   ✓已完成  异常层次
│   │   ├── store.py                      #   ✓已完成  SQLite 任务状态（断点续跑）
│   │   ├── urls.py                       #   ✓已完成  分享文案抽 URL + 平台识别（§7.3）
│   │   ├── concurrency.py                #   ✓已完成  §3.2 的并发计算逻辑
│   │   ├── urls.py                       #   ✓已完成  分享文案抽 URL + 平台识别（§7.3）
│   │   ├── fetcher.py                    #   ✓已完成  下载（yt-dlp 封装 + 本地文件接入）
│   │   ├── audio.py                      #   ✓已完成  抽音频（ffmpeg → 16k/mono/s16）
│   │   ├── transcriber.py                #   ✓已完成  转写（faster-whisper）← prototypes/transcribe.py
│   │   ├── frames.py                     #   ✓已完成  抽帧 + 拼版 ← prototypes/make_sheets.py
│   │   ├── pipeline.py                   #   ✓已完成  单条执行器（cli / rpc / 阶段2 dispatcher 共用）
│   │   ├── dispatcher.py                 #   阶段 2  流水线调度器（队列 + worker 管理）
│   │   ├── cookies.py                    #   阶段 4  cookie 获取/缓存/刷新 ← prototypes/state2cookie.py
│   │   └── summarizer.py                 #   阶段 4  汇总（可选，API 模式）
│   ├── models/
│   │   ├── task.py                       #   ✓已完成  Task / TaskStatus 数据类
│   │   └── config.py                     #   ✓已完成  配置读写（dataclass + yaml）
│   ├── cli.py                            #   ✓已完成  人用入口 + 阶段 1 验收入口
│   └── rpc.py                            #   ✓协议层已完成  ★Rust 宿主入口（sidecar 常驻 worker）
├── app-ui/                               # ★Rust + Tauri 工程（阶段 3）
│   ├── src-tauri/                        #   Rust 宿主：进程监督器 + 事件分发
│   └── src/                              #   前端：任务表格 / 进度 / 日志
├── prototypes/                           # ★已验证原型，阶段 1 重构的直接输入
│   ├── transcribe.py  state2cookie.py  make_sheets.py
│   └── README.md                         #   对应关系、环境假设、不可丢的实现细节
├── tools/
│   └── probe_env.py                      #   硬件探测（已实跑，可直接并入 app/core/）
├── models/large-v3/                      # Whisper large-v3（2.87 GB，已就位）
├── samples/                              # ★回归测试基线（本地留存，不入 git，见 samples/README.md）
├── docs/
│   └── 实测记录.md                        #   本机性能实测与环境快照（事实源）
├── config.example.yaml                   # 配置模板（复制为 config.yaml）
├── config.yaml                           # 用户配置（.gitignore 已排除）
├── state.db                              # 任务状态（自动生成）
├── requirements.txt
├── .gitignore
└── README.md
```

**迁移已完成的部分**：`app/` 目录已建好空骨架，`prototypes/` `models/` `samples/` `docs/` 内容就位。
阶段 1 只需往 `app/core/` 里填实现，**不需要再动目录结构**。

### 4.1 各模块接口契约

| 模块 | 签名 | 输出 | 失败行为 |
|---|---|---|---|
| `env_probe.probe()` | `(work_dir=None)` | `EnvReport`（cpu/mem/disk/gpu/cuda） | 探测项失败返回 `None`，不抛异常 |
| `env_probe.verify_cuda()` | `(model_path, device, compute_type)` | `(bool, str)` | 真实推理验证，绝不抛异常 |
| `concurrency.plan()` | `(cfg, env, gpu_usable=None)` | `Concurrency` | 内存读取失败时回退到保守值 `(1,2,1,1)` |
| `fetcher.download()` | `(task, cfg, out_dir, on_progress=None)` | `DownloadResult` | 抛 `StageError`，消息含 yt-dlp 输出尾 20 行 |
| `audio.extract()` | `(video, out_path, ffmpeg_path=None, total_sec=None, on_progress=None)` | `Path`（wav） | ffmpeg 非 0 退出则抛 `StageError` |
| `transcriber.run()` | `(wav, model_path, out_prefix, *, language, beam_size, ...)` | `TranscriptOutput`(txt/srt/json) | 模型加载失败抛 `FatalEnvironmentError`，**中止整批** |
| `frames.run()` | `(video, out_dir, *, scene_threshold, ...)` | `FramesResult`（拼版图 + 索引） | 抛 `StageError`，调用方决定降级 |
| `pipeline.run_one()` | `(task, cfg, store, *, on_progress=None, on_stage=None)` | `Task`（状态已更新） | 捕获单条错误并写库；环境级故障继续上抛 |
| `cookies.ensure(platform)` | 阶段 4 | `Path`（cookies.txt） | 无效则抛 `CookieExpired` |
| `summarizer.run(transcript)` | 阶段 4 | md 路径 | API 失败重试 3 次后标记 `failed` |
| `rpc.serve()` | `()` | stdout 事件流 | 协议见 `docs/IPC协议规格.md`；worker 崩溃由宿主重启一次 |

**关键设计原则：转写是唯一"失败即整批中止"的阶段，其余阶段失败都只影响单条任务。** 因为转写失败通常意味着环境问题（CUDA/模型损坏），继续跑只会浪费时间和配额。

**状态归属**：`fetcher` / `audio` / `transcriber` / `frames` **都不写任务状态、不碰 `store`**，
它们只抛异常；状态流转（`downloading → downloaded → transcribing → …`）由
`pipeline.run_one()` 统一负责。这样每个阶段模块都是纯功能函数，可以独立测试，
也避免四个模块各自解释状态机。

### 4.2 进度上报契约（双入口共用）

所有耗时函数（`fetcher` / `audio` / `transcriber` / `frames` / `summarizer`）签名统一附加一个可选参数：

```python
def run(..., on_progress: Callable[[str, float], None] | None = None) -> ...:
    # on_progress(stage: str, progress: float)
    #   stage    —— 阶段标识，**用 TaskStatus 的英文值**：
    #               "downloading" | "audio" | "transcribing" | "framing" | "summarizing"
    #               （"audio" 是 downloaded→transcribing 之间的过渡阶段，无独立 status）
    #   progress —— 0.0~1.0；-1.0 表示"进行中但进度不可知"
```

**stage 用英文标识而不是中文**：中文是**展示层**的事，`rpc.py` 用 `STAGE_LABELS`
把 status 映射成「下载中 / 转写中…」，UI 侧只消费映射结果。core 层不该内嵌 UI 文案。

**`on_stage` 与 `on_progress` 是两个回调**：前者只在**状态跃迁**时触发（低频率，
供 UI 刷新表格行），后者是高频进度（需限流，见 `docs/IPC协议规格.md` §4.1）。
合并成一个会让调用方不得不自己判断"这次是状态还是进度"。

**为何现在就要加**：`cli.py` 与 `rpc.py` 是两个入口，唯一的共同钩子就是这个回调。
若等阶段 3 再补，需要改动全部调用点及其调用方。

- `cli.py` 传一个打到 stderr 的打印函数（每 5% 一行，避免 649 段刷屏）
- `rpc.py` 传一个把 `(stage, progress)` 转成 `task_update` 事件的函数（带 200 ms 限流）

**`app/core/` 不得感知传输方式**：不 `print`、不 `sys.exit`、不读 `argv`、不直接写 stdout。
所有输出经回调或异常向上传递——这是「同一套核心既能跑 CLI 又能被 Rust 驱动」的前提。

---

## 5. 配置规格（`config.yaml`）

```yaml
paths:
  work_dir: "D:/Code/SideProject/Vidnote/work"   # 工作根目录（D 盘，988GB 可用）
  model_dir: "D:/Code/SideProject/Vidnote/models"
  ffmpeg: null                        # 留空则自动定位

runtime:
  device: "auto"                      # auto | cuda | cpu
  compute_type: "auto"                # cuda→float16, cpu→int8
  model: "large-v3"                   # large-v3 | medium | small
  auto_concurrency: true              # false 时用下面的手写值
  max_download: 8
  max_frame: 4
  max_summarize: 4

transcribe:
  language: "zh"
  beam_size: 5
  vad_filter: true
  vad_min_silence_ms: 400             # ⚠️ 与 beam_size 一同决定分段边界，改动即破坏验收
  initial_prompt: ""                  # ⚠️ 强参数：会改变分段与标点风格，见 §8 阶段 1

frames:
  enabled: false                      # 默认关：多数口播视频画面无信息量
  scene_threshold: 0.3
  max_frames: 400
  sheet_cols: 6
  sheet_rows: 4

summarize:
  enabled: false                      # true 则走 API 模式
  base_url: "https://api.openai.com/v1"
  api_key: ""                         # 建议改读环境变量，不要写死在文件里
  model: "gpt-4o"
  template: "摘要+章节大纲+要点"        # 对应你选的产出形态

cookie:
  browser: "chromium"                 # playwright 取 cookie 用的浏览器
  auto_refresh: true
```

**`summarize.api_key` 不进 yaml**：实现时改为读环境变量 `VIDNOTE_API_KEY`，yaml 里只留 `api_key_env: "VIDNOTE_API_KEY"`。避免密钥随配置文件被误提交。

> 可运行的配置模板见仓库根目录 `config.example.yaml`（含实测有效值与逐项注释）。

---

## 6. 断点续跑设计

`state.db`（SQLite）单表：

```sql
CREATE TABLE tasks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  url           TEXT    NOT NULL UNIQUE,      -- 去重键
  platform      TEXT,                        -- douyin | bilibili | youtube | local
  video_id      TEXT,
  title         TEXT,
  duration_sec  INTEGER,
  status        TEXT NOT NULL,               -- 见下方状态机
  stage_error   TEXT,
  retry_count   INTEGER DEFAULT 0,
  out_dir       TEXT,
  video_path    TEXT,
  audio_path    TEXT,
  transcript_json TEXT,
  created_at    TEXT,
  updated_at    TEXT
);
```

**状态机**（单向推进，失败可回退到上一稳定态）：

```
pending ──▶ downloading ──▶ downloaded ──▶ transcribing ──▶ transcribed
                                                              │
                                              ┌───────────────┼───────────────┐
                                              ▼               ▼               ▼
                                          framing        summarizing       done
                                              │               │
                                              └───────┬───────┘
                                                      ▼
                                                    done

  任意阶段 ──失败──▶ failed（记录 stage_error，可手动重试）
```

**启动时行为**：扫 `state.db`，把 `downloading/transcribing/framing` 这些"中途态"重置为对应的上一稳定态（因为它们可能是在进程被杀时中断的），然后继续。`done` 的直接跳过。

**`url` 上的 UNIQUE 约束**就是去重机制——重复添加同一个链接会被忽略并提示"已处理过"。

---

## 7. GUI 设计

### 7.1 技术选型：Tauri（Rust）+ Python sidecar

> **2026-10-02 修订**：原选型为 PySide6，因头头要求「高性能桌面端」而重新评估。
> 关键结论：**转写吞吐换语言换不来**——瓶颈在 GPU + CTranslate2（C++/CUDA），Python 只是那层胶水。
> 但 **UI 层与分发形态** Rust 确有优势。故分层：**UI 用 Rust，功能层保留 Python**。
>
> 佐证（2026 多源实测）：`whisper.cpp`（Rust 侧唯一的 Whisper 绑定 `whisper-rs` 的底层）在 N 卡上
> **比 faster-whisper 慢**——large-v3 CUDA 23× vs 15×、RTX 4070 上 12× vs 8×。
> 本机现为 8.7×，换 Rust 是**负收益**。

| 候选 | 判断 |
|---|---|
| **Tauri (Rust)** ✅ | 官方支持 sidecar（`bundle.externalBin`）spawn 常驻 Python 进程；UI 用 HTML/CSS 写批量表格成本最低；产物为原生安装包，用户侧无需 Python 运行时 |
| egui / iced（纯 Rust） | 无 WebView 依赖、体积更小。但表格控件需自建，生态远弱于 Web/Qt，而本项目核心 UI 恰是批量任务表格 |
| PySide6 | 原选型。原生 Qt + `QTableView`，开发最快。**代价是 UI 层与分发形态拿不到 Rust 的收益** |
| Streamlit / Flet / Electron | 为这个规模引入过重构建链，或不是你要的"桌面窗口" |

**为何不用 PyO3 内嵌 Python 解释器**：`§9.1` 的 CUDA DLL 注入是**进程级全局状态**
（句柄保活 + `PATH` 注入 + 必须在 `import ctranslate2` 之前执行）。一旦 Python 被嵌进 Rust 进程，
这些操作会改写宿主自身的 DLL 搜索路径，行为不可控且打包后极易失败。

**为何 Python 侧仍需 yt-dlp**：yt-dlp 无 Rust 等价物（社区 Rust 方案均为单平台或个人项目），
且「支持 1900+ 平台」的同类工具本身就是 yt-dlp 套壳。Rust 侧同样只能 subprocess 调它。

**代价（已知并接受）**：
- 本机 `rustc` / `cargo` / `rustup` **均未安装**，需先装 Rust 工具链 + MSVC build tools
- 分发包 = Tauri 安装包 + PyInstaller 目录（**两层打包**，见 §8 阶段 5）
- 内存与启动收益仅**部分**——Python 进程的运行时开销并未消除

**接口契约**：Rust 与 Python 之间的一切约定见 **`docs/IPC协议规格.md`**
（stdin/stdout + JSON Lines，零端口；stdout 只走协议、日志全走 stderr）。

### 7.2 窗口布局（Rust 前端实现，信息架构不变）

```
┌───────────────────────────────────────────────────────────────┐
│ [添加链接] [导入txt/csv] [开始] [暂停] [重试失败项] [打开目录]   │
├───────────────────────────────────────────────────────────────┤
│ 状态栏: GPU 可用 ✓ | 并发: 转写1 下载8 抽帧4 | 内存 12.6G 可用  │
├───────────────────────────────────────────────────────────────┤
│ 序号│ 标题          │ 时长  │ 状态      │ 进度 │ 耗时 │ 备注    │
│  1  │ 示例视频标题…  │ 20:04 │ ✓ 完成    │ 100% │ 3:12 │         │
│  2  │              │ 12:30 │ ⟳ 转写中  │  45% │ 1:20 │         │
│  3  │              │  --   │ ⟳ 下载中  │  30% │ 0:15 │         │
│  4  │              │  --   │ ✗ 失败    │  --  │ 0:02 │ cookie  │
├───────────────────────────────────────────────────────────────┤
│ 日志区（可折叠）                                                │
└───────────────────────────────────────────────────────────────┘
```

**关键交互要求**：
1. 添加链接支持**一次粘贴多条**（抖音分享文案里混着中文和链接，要做正则抽取）
2. 任务表格支持**右键**：打开输出目录 / 查看转写稿 / 重试 / 删除
3. 关闭窗口时若仍有任务在跑，弹确认并支持"后台继续"（最小化到托盘）
4. 进度条不能是假的——每个阶段上报真实进度（下载看 yt-dlp 输出，转写看 segment 时间戳）

### 7.3 抖音分享文案的正则抽取（这个必须做对）

用户实际粘贴的输入是这种形态（示例已脱敏）：

```
8.25 复制打开抖音，看看【某创作者的作品】【标题...】...
https://v.douyin.com/AbCdEf12345/ vFh:/ 03/30 :3pm b@n.qR
```

需要从中抽出 `https://v.douyin.com/AbCdEf12345/`，丢弃干扰码。规则：

```python
URL_PATTERNS = {
    "douyin":    r"https?://v\.douyin\.com/[A-Za-z0-9_\-]+/?",
    "bilibili":  r"https?://(?:www\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]+|av\d+)",
    "youtube":   r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[A-Za-z0-9_\-]+",
}
```

逐平台匹配，全部失败则把整行原样交给 yt-dlp 试一次（它是通用 extractor，可能能处理）。

---

## 8. 分阶段实施步骤

每个阶段结束都有**可独立验收**的产物，不要一口气写完再测。

### 阶段 1：核心流水线（CLI，无 GUI）

- 目标：`python -m app.cli run <url|本地文件>` 能跑通单条，产出目录结构正确
- 交付：`fetcher / audio / transcriber / frames` + `pipeline`（单条执行器）+ `cli` 临时入口
- 验收：产出转写稿与 `samples/transcript_gpu.txt` **逐字符比对**
  - ⚠️ 该基线**保留在本地磁盘但不入库**（派生自第三方视频），新机器 clone 后需自备，见 `samples/README.md`
  - **实测结论：同配置下转写是确定性的** —— 同模型/同参数/同硬件的多次独立运行，产出逐字节一致
    （2026-10-03 又跑了三次 `--force` 重跑验证，仍逐字节一致，见 `docs/实测记录.md` §2.4）
  - 因此判据就用**严格相等**。出现差异即说明环境或参数变了，是有意义的信号，**不要放宽判据**
  - 注意跨配置不复现：GPU 与 CPU 两版基线差 3 段（649 vs 646），比对前先确认用的是哪个基线
  - 🔴 **2026-10-03 发现的阻塞：现有基线不可复现**。基线产生时用过 `initial_prompt`
    （领域术语提示），而该字符串未记录在任何文件中。实测证据：正式实现与原型脚本在同参数下
    产出**逐字节一致**，说明实现无误；差异全部归结于 `initial_prompt` 的取值。
    处置方案待定，详见 `docs/实测记录.md` §2.4
- **这一阶段必须先跑通，再碰 GUI。** GUI 只是壳，壳下面没有能跑的核心是最大的返工来源。


### 阶段 2：并发调度 + 断点续跑

- 目标：批量 10 条 URL，Ctrl+C 杀掉进程，重启后能从断点继续
- 交付：`dispatcher.py` + 完整状态机
- 验收：
  - 10 条任务全部完成，耗时符合 §2 的吞吐预估
  - 杀掉进程后重启，已完成的不重跑（检查 `state.db` 的 status）
  - 故意混入 1 条坏链接，确认它失败后**其他 9 条正常完成**

### 阶段 3：GUI（Rust + Tauri）

- 目标：双击启动，粘贴链接，点开始，看进度，完成后能打开目录
- 交付：`app-ui/`（Tauri 工程：`src-tauri/` 宿主 + `src/` 前端）+ `app/rpc.py` 完整实现
- 前置：Rust 工具链 + MSVC build tools（本机未安装，需先补）
- 验收：见 §10 验收清单，**外加** `docs/IPC协议规格.md` §9 的全部 8 条协议判据

### 阶段 4：cookie 自动化 + 汇总模块

- 目标：抖音 cookie 过期时能自动重取（调 playwright）；汇总可开关
- 验收：手动把 `cookies.txt` 改坏，程序能检测到并自动重新采集

### 阶段 5：打包（两层）

- **Python 侧**：用 PyInstaller 打成**单目录**（不是单文件——单文件解压慢且模型路径处理麻烦），
  产物作为 Tauri 的 `externalBin` sidecar 随包分发
- **Rust 侧**：Tauri bundler 出平台安装包
- 验收：在**没装 Python、也没装 Rust** 的机器上双击能启动（同机模拟）
- ⚠️ **本阶段风险最高**：CUDA DLL 随 PyInstaller 分发本就未验证（§11.5），
  叠加 Tauri sidecar 与两层构建链后复杂度翻倍。建议在阶段 3 末期就做一次最小打包验证，不要拖到最后

---

## 9. 已踩过的坑（必须写进实现，否则会重踩）

这些是今天实测中真实遇到的，**不是推测**。每条都给出根因，实现时直接照做。

### 9.1 CUDA DLL 加载（最坑，浪费了 3 次尝试）

**现象**：`ctranslate2` 报 `RuntimeError: Library cublas64_12.dll is not found or cannot be loaded`，即使已 `pip install nvidia-cublas-cu12`。

**根因**（两层，缺一不可）：
1. `nvidia-*` wheel 把 DLL 放在 `site-packages/nvidia/<pkg>/bin/`，不在 PATH 上；
2. **`os.add_dll_directory()` 返回的句柄必须被引用住**。Python 文档明确：句柄被 GC 后目录会从搜索路径中移除。只调用不保存返回值 = 无效。
3. 而且 `add_dll_directory` 只对 `LoadLibraryEx` 带 `LOAD_LIBRARY_SEARCH_USER_DIRS` 生效；ctranslate2 用的是裸名 `LoadLibrary("cublas64_12.dll")`，解析走**进程 PATH**，所以 PATH 也必须注入。
4. 调用时机必须在 `import ctranslate2` **之前**。

**最终可用实现**（已写入 `prototypes/transcribe.py`，可直接搬进 `app/core/transcriber.py`）：

```python
_DLL_DIR_HANDLES: list = []          # 必须保活

def add_nvidia_dll_dirs() -> list[str]:
    added = []
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return added
    import nvidia                     # 命名空间包，没有 __file__，只有 __path__
    paths = list(getattr(nvidia, "__path__", []) or [])
    if not paths:
        return added
    for pkg in sorted(Path(paths[0]).iterdir()):
        bin_dir = pkg / "bin"
        if bin_dir.is_dir():
            handle = os.add_dll_directory(str(bin_dir))
            _DLL_DIR_HANDLES.append(handle)      # ← 这行不能省
            added.append(str(bin_dir))
    if added:                                  # ← PATH 注入也不能省
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")
    return added

# 必须在 import faster_whisper / ctranslate2 之前执行
add_nvidia_dll_dirs()
```

**验证方法**：`ctranslate2.get_cuda_device_count()` 返回 1 **不代表 GPU 可用**——它只探测硬件存在。真正的验证是跑一次真实推理。写成程序里的 `env.cuda.usable` 字段时，**必须实测一次而不是读 device_count**。

### 9.2 `faster-whisper` 与 `av 19` 不兼容

**现象**：`TypeError: open() got an unexpected keyword argument 'metadata_errors'`
**根因**：PyAV 19 移除了该参数，faster-whisper 1.2.1 仍在传。
**解法**：绕开 PyAV。音频既然是标准 16k mono s16 wav，直接用 `wave` + `numpy` 读成 float32 数组传给 `model.transcribe()`。实现见 `prototypes/transcribe.py::load_wav_16k_mono()`。

### 9.3 ffmpeg 9.0 移除了 `-vsync`

**现象**：`Unrecognized option 'vsync'`
**解法**：改用 `-fps_mode passthrough`。网络上 95% 的抽帧教程还是旧写法。

### 9.4 Playwright 自带的 ffmpeg 不能用

`%LOCALAPPDATA%/ms-playwright/ffmpeg-*/ffmpeg-win64.exe` 是 `--disable-everything` 构建，只编进了 mjpeg/vp8 编解码和 `image2`/`matroska` 解复用器——**没有 mp4 demuxer**。

`ffmpeg_locator.py` 的搜索顺序应为：
1. `config.yaml` 里显式指定的路径
2. `PATH` 中的 `ffmpeg`
3. `%LOCALAPPDATA%/Microsoft/WinGet/Packages/Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe`
4. 报错并给出 `winget install` 提示

**明确排除** `ms-playwright` 目录。

#### 9.4.1 更隐蔽的一个：WinGet `Links/` 里的 0 字节占位文件（2026-10-03 实测踩到）

**现象**：`shutil.which("ffmpeg")` 成功返回
`%LOCALAPPDATA%/Microsoft/WinGet/Links/ffmpeg.exe`，但 `subprocess` 调它直接抛
`OSError: [WinError 193] %1 不是有效的 Win32 应用程序`。

**根因**：WinGet 会在 `Links/` 下放 **0 字节的占位文件**（app execution alias），
它既不是 symlink 也不是 reparse point，`os.path.getsize()` 为 0、无 `MZ` 头。
它出现在 PATH 上，所以 `which()` 会命中——**"找到了"不等于"能用"**。

**解法**：候选必须过**有效性校验**——存在 + 非空 + 前两字节为 `MZ`（PE 头）。
校验不过就继续往下搜，最终落到 `WinGet/Packages/` 里的真实可执行。
`ffmpeg_locator._is_usable_executable()` 实现，`ffmpeg_available()` 因此也从"误报可用"变为可信。

### 9.5 抖音 cookie

- `yt-dlp --cookies-from-browser edge|chrome` 在 Windows 新版浏览器上**基本必然失败**（Edge：`Failed to decrypt with DPAPI`；Chrome：`Could not copy Chrome cookie database`）。不要浪费时间在这条路上。
- 可用路径：`playwright-cli open <url>` → **紧接着**`state-save`（超过几秒会话会关，报 `Browser 'default' is not open`）→ 把 storage-state JSON 转成 Netscape 格式 cookies.txt。
- 转换规则：`domain` 以 `.` 开头则 include_sub 为 `TRUE`；`expires<=0` 用 `now+1年`；`httpOnly` 加 `#HttpOnly_` 前缀；`name` 为空的行跳过。参考实现：`prototypes/state2cookie.py`。
- `ttwid` 有效期约 24 小时，程序需要能检测失效并自动重取。

### 9.6 模型下载

- HuggingFace 直连不通，必须走 `hf-mirror.com`
- 但 hf-mirror 对 LFS 文件会 302 到 `cas-bridge.xethub.hf.co`，而 `huggingface_hub` 默认下载超时只有 10 秒 → 经常失败
- **可靠做法**：用 `curl` 手动下载，带 `-C -` 断点续传：
  ```bash
  for f in config.json preprocessor_config.json tokenizer.json vocabulary.json model.bin; do
    curl -sL --retry 5 --retry-delay 3 -C - -o "models/large-v3/$f" \
      "https://hf-mirror.com/Systran/faster-whisper-large-v3/resolve/main/$f" &
  done; wait
  ```
- 实测速度约 41 MB/s，2.87 GB 模型约 1 分 35 秒下完
- 注意：pip 装 CUDA 库时若走系统代理访问国内镜像会**反而更慢**，要加 `--proxy ""`

---

## 10. 验收清单

### 功能验收

- [ ] 一次粘贴 10 条混合平台链接（含 1 条坏链接），全部处理完，坏链接被标记失败且不影响其他 9 条
- [ ] 粘贴抖音原始分享文案（含干扰码），能正确抽出 URL
- [ ] 中断进程后重启，已完成任务不重跑，未完成任务从断点继续
- [ ] GPU 被其他程序占满时，程序能提示并降级到 CPU（或等待），不直接崩溃
- [ ] 关闭窗口时若有任务在跑，弹确认框，选"后台继续"后最小化到托盘仍能完成
- [ ] 输出目录结构统一，每个视频一个文件夹，命名不含非法字符

### 性能验收

- [ ] 单条 20 分钟视频，GPU 模式下「下载+抽音频+转写」全流程耗时 **≤ 4 分钟**
- [ ] 10 条 20 分钟视频批量，总耗时 ≤ 单条耗时 × 10 的 60%（证明流水线确实并行生效，而不是串行排队）
- [ ] 内存占用峰值不超可用内存的 80%

### 健壮性验收

- [ ] cookie 失效时给出**可操作**的提示（不是一句"失败了"）
- [ ] ffmpeg 缺失时启动即报错，并给出安装命令
- [ ] 磁盘剩余空间不足 5 GB 时停止取新任务并告警
- [ ] 转写阶段失败会中止整批（避免环境坏了还空跑一整夜）

---

## 11. 风险与未验证项

### 已识别风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| **内存仅剩 12.6 GB** | 并发上不去，大模型可能 OOM | 已设计动态探测 + 运行时保护；建议跑批前关掉浏览器等占内存的程序 |
| **抖音反爬策略变化** | cookie 频繁失效，批量中断 | cookie 自动重取 + 失败任务可重试；极端情况需降级为手动下载 |
| **平台限流** | 短时间大量请求可能触发风控甚至封号 | 下载池加**速率限制**（默认每分钟 ≤ 10 条），可配置 |
| 磁盘占用 | 每条视频约 44 MB 视频 + 38 MB wav；100 条约 8.2 GB | D 盘 988 GB 充足；提供"完成后删除中间文件"选项 |
| 模型下载失败 | 首次使用受阻 | 提供手动下载指引；模型只下一次 |
| GPU 被占用 | 转写变慢或失败 | 启动时检查显存余量，不足则告警 |

### 未验证项（诚实标注）

1. ~~**GPU 全量转写性能**~~ → **已验证**，见 §2.2。仍是单次测量，长跑稳定性（连续 3 小时以上是否降频/OOM）未测。
2. **并发正确性**：流水线架构是设计推断，**未经实测**。阶段 2 的验收标准（10 条批量的总耗时）就是用来证伪它的。
3. **B站 / YouTube 的实际可用性**：本机从未测过这两个平台。YouTube 在国内需代理，B站部分视频需登录。**建议阶段 1 只做抖音，跑通后再扩展。**
4. **汇总模块的 token 成本**：一条 20 分钟视频转写稿约 8000–10000 字，是否超出所选模型的上下文、单条成本多少，**未测**。
5. **两层打包**：PyInstaller（ctranslate2 + CUDA DLL，DLL 需随包分发）+ Tauri bundler，**未验证**，复杂度高于原单层方案。
6. **GPU 并发是否真的无收益**：§3.1 断言"GPU 转写开 2 个实例不会提升吞吐"，这是基于显存与算力分时复用的推断，**未实测**。若要验证，可本地跑双实例对照——但如果阶段 2 验收达标，就没有必要验证。
7. **Rust UI 与 IPC 协议**：`docs/IPC协议规格.md` 是 2026-10-02 新设计的契约，**未经端到端实测**。
   其协议层验收判据（该文档 §9，共 8 条）可用 Python mock client 独立跑通，**不必等 Rust 工程建起来**——建议在阶段 1 收尾前先跑一遍。
8. **Rust 侧产出未验证的自有风险**：本机 `rustc`/`cargo` 未安装，`app-ui/` 尚未创建。Tauri 的 sidecar 机制、Windows Job Object 进程回收、WebView2 依赖分发性，均**未在本机验证**。
9. 🔴 **基线缺参数记录 → 原基线不可复现**（2026-10-03 实测）。基线生成时用过
   `initial_prompt`（领域术语提示），该字符串未落盘。已实测证实：正式实现与原型脚本在同参数下
   **逐字节一致**、三次重跑**逐字节一致**，差异只归因于 `initial_prompt` 的取值。
   处置待定，证据见 `docs/实测记录.md` §2.4。
   **流程教训**：验收基线必须与产生它的完整参数一起存档，否则"金标准"无法验证。

---

## 12. 建议的下一步

### 12.1 架构已定（2026-10-02）

**UI 层 = Rust + Tauri；功能层 = Python（复用，不重写）。** 接口契约见 `docs/IPC协议规格.md`。

这一变更**不产生返工**：阶段 1-2 的 Python 核心本就是「功能层」，只有阶段 3 的 GUI 换了实现语言。
详细选型论证见 §7.1；理由速览：转写吞吐由 GPU + CTranslate2(C++) 决定，换语言无收益，
而 Rust 的收益（UI 流畅度、分发形态）全部落在阶段 3 之后。

### 12.2 推进顺序

按 `§8` 顺序走，**不要跳阶段**。当前处于**阶段 1**——把 `prototypes/` 下那三个已经跑通的脚本（`state2cookie.py` / `transcribe.py` / `make_sheets.py`）重构成 `app/core/` 下的正式模块。它们已经被 2026-10-02 那一轮真实任务验证过，是整套系统里最可靠的部分。

`prototypes/README.md` 列了每个脚本对应的目标模块、必须保留的实现细节（尤其是 CUDA DLL 加载那三层要求）、以及**不该照抄的部分**（命令行参数解析不该进核心模块）。

---

## 附录：本机环境当前状态（实测快照）

| 项 | 状态 |
|---|---|
| ffmpeg | ✅ `%LOCALAPPDATA%/Microsoft/WinGet/Packages/Gyan.FFmpeg_*/ffmpeg-9.0.2-full_build/bin` |
| yt-dlp | ✅ `C:/Users/Ben/.agent-reach-venv/Scripts/yt-dlp`（2026.06.09） |
| playwright-cli | ✅ `C:/Users/Ben/.workbuddy/binaries/node/versions/22.22.2-3/playwright-cli.cmd`（0.1.22） |
| faster-whisper | ✅ 1.2.1 |
| ctranslate2 | ✅ 4.8.2 |
| CUDA 运行时 | ✅ cublas 12.9.2.10 / cudnn 9.27.0.42 / nvrtc 12.9.86 |
| large-v3 模型 | ✅ `models/large-v3/`（2.87 GB） |
| **GPU 转写** | ✅ **实测可用，8.7× 实时** |

> 上面这些**已经全部就绪**，程序不需要再装一遍环境——`requirements.txt` 里只列 Python 依赖，CUDA 与模型按"已存在则复用、不存在则引导下载"处理。
