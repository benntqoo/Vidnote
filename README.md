# Vidnote

> Video → Note。批量把在线视频（抖音 / B站 / YouTube）转成本地结构化文稿。
>
> 下载 → 抽音频 → **GPU 转写** → 抽帧拼版 → （可选）汇总

## 状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| — | 链路验证 + 环境实测 | ✅ 完成（见 `docs/实测记录.md`） |
| — | 开发计划书 | ✅ 完成（见 `PLAN.md`） |
| 1 | 核心流水线（CLI，无 GUI） | ✅ 完成（验收通过） |
| 2 | 并发调度 + 断点续跑 | ✅ 完成（验收 23/23 通过） |
| 3 | GUI（Rust + Tauri，Python sidecar） | ⬜ 待开发 |
| 4 | cookie 自动化 + 汇总模块 | ⬜ 待开发 |
| 5 | 打包（PyInstaller + Tauri，两层） | ⬜ 待开发 |

**阶段 1 完成**：全链路跑通（下载 → 抽音频 → GPU 转写 → 落库，实测 2 分 36 秒 / 20 分钟视频），
产出与基线 `samples/transcript_gpu.txt` **逐字节一致**（662 段），复现性经两次独立运行验证。

**阶段 2 完成**：`dispatcher.py` 三级流水线（下载池 → 转写池 ×1 → 抽帧池）+ 有界队列背压 +
内存护栏 + 断点续跑。验收手段 `tools/phase2_acceptance.py`（23 条判据，真跑 GPU）：

| 判据（引自 `PLAN.md` §8） | 实测 |
|---|---|
| 10 条任务全部完成 | ✅ 10 done（+1 条故意混入的坏链接 failed） |
| 坏链接失败不影响其余 | ✅ 坏链接停在下载阶段，其余 10 条正常 done |
| 杀进程后重启，已完成的不重跑 | ✅ 强杀后 3 done / 1 transcribing / 6 downloaded / 1 failed；续跑时 3 条 done 的状态、`updated_at`、转写稿 mtime **三者均未被改动** |
| 流水线架构（`PLAN.md` §11.2 的设计推断） | ✅ 重叠系数 **1.10×**（> 1.0 即阶段确实重叠），转写峰值并发 **1** |

> 重叠系数只有 1.10× 是**如实结果**，不是问题：60 秒片段的转写占全部阶段耗时约 90%，
> 可与它重叠的只有下载（毫秒）与抽帧（约 1 秒）。真实 20 分钟视频的转写占比更高，
> 重叠收益只会更小——这恰好印证 `PLAN.md` §3.1「转写是串行瓶颈」的判断。

> 基线在 2026-10-03 **换代过**：v1 因产生它的 `initial_prompt` 未记录而作废，v2 附带**完整参数快照**
> （`samples/README.md`）。判据始终是「逐字符严格相等」，**从未放宽**。证据链见 `docs/实测记录.md` §2.4。
>
> **流程教训**：验收基线必须与产生它的完整参数一起存档。只存产出不存参数，"金标准"不可验证。

## 仓库内容边界

本仓库只放**源码与技术文档**——clone 下来装好依赖、下好模型即可运行，不需要仓库外的素材。

| 类型 | 内容 | 入库 |
|---|---|---|
| 源码 | `app/`、`prototypes/`、`tools/` | ✅ |
| 技术文档 | `PLAN.md`、`README.md`、`docs/实测记录.md`、`docs/IPC协议规格.md`、`prototypes/README.md`、`samples/README.md` | ✅ |
| 配置模板 | `config.example.yaml`、`requirements.txt` | ✅ |
| 模型权重 | `models/large-v3/`（2.87 GB） | ❌ 自行下载，见「快速开始」第 3 步 |
| 用户配置 | `config.yaml` | ❌ 含本机路径，自行生成 |
| 运行时数据 | `work/`、`state.db`、`cookies.txt`、日志 | ❌ |
| 测试材料 | `samples/` 下的视频、元数据、转写稿、拼版图 | ❌ 派生自第三方视频 |
| Agent 工作数据 | `.workbuddy/` | ❌ |

测试材料**保留在本地磁盘**（阶段 1 验收依赖它们），只是不进版本库。原因与代价见 `samples/README.md`。

## 目录导航

```
Vidnote/
├── PLAN.md                  # ★开发计划书：架构、接口契约、并发公式、验收清单
├── README.md                # 本文件
├── requirements.txt         # Python 依赖（含 CUDA 运行库的安装注意事项）
├── config.example.yaml      # 配置模板，复制为 config.yaml 使用
├── app/                     # ★Python 功能层
│   ├── core/                #   env_probe / cuda_dll / ffmpeg_locator / store / urls
│   │                        #   concurrency / fetcher / audio / transcriber / frames
│   │                        #   pipeline（单条执行器）+ dispatcher（流水线调度）
│   │                        #   proc（子进程回收）—— cli 与 rpc 共用
│   ├── models/              #   Task / Config 数据类
│   ├── cli.py               #   人用入口 + 验收入口（--jobs 1 走串行黄金路径）
│   └── rpc.py               #   Rust 宿主入口（sidecar 常驻 worker）
├── app-ui/                  # ★Rust + Tauri 工程（阶段 3）
├── prototypes/              # ★已验证原型脚本，阶段 1 重构的直接输入
│   ├── transcribe.py        #   → app/core/transcriber.py（含 CUDA DLL 加载的正确实现）
│   ├── state2cookie.py      #   → app/core/cookies.py
│   ├── make_sheets.py       #   → app/core/frames.py
│   └── README.md            #   各脚本的接口与环境假设
├── tools/
│   ├── probe_env.py         #   → app/core/env_probe.py（硬件探测，已实跑）
│   ├── rpc_smoke.py         #   IPC 协议冒烟测试（19 条判据，不依赖 Rust 端）
│   ├── dispatcher_selftest.py   # 调度器语义自测（53 条判据，假阶段函数，秒级跑完）
│   └── phase2_acceptance.py     # 阶段 2 正式验收（23 条判据，真跑 GPU）
├── models/                  # 模型权重目录（内容不入库，需自行下载）
├── samples/                 # 测试基线（内容不入库，仅 README.md 入库）
└── docs/
    ├── 实测记录.md           # ★事实源：本机性能实测与环境快照
    ├── IPC协议规格.md         # ★Rust 宿主 ↔ Python Worker 的接口契约
    └── 样例产出_参考.md       # 产出形态参考（本地留存，不入库）
```

## 快速开始

### 1. 前置要求

| 组件 | 要求 | 安装方式 |
|---|---|---|
| Python | 3.13（实测 3.13.14） | — |
| ffmpeg | **9.x full build**（`-vsync` 已移除，见「已知坑」） | `winget install --id Gyan.FFmpeg -e` |
| yt-dlp | 最新版 | `pip install -U yt-dlp` |
| Node.js | 仅 cookie 采集需要 | `npm i -g @playwright/cli` |
| NVIDIA GPU | 显存 ≥ 8 GB（实测 RTX 4060 Ti 16 GB / 驱动 610.88） | 显卡驱动 |
| Rust 工具链 | **仅阶段 3 起需要**（UI 层用 Tauri），本机尚未安装 | `winget install Rustlang.Rustup` + VS Build Tools |
| CUDA 运行库 | cublas / cudnn / nvrtc 12.x | 见第 2 步 |

> ⚠️ **GPU 转写是硬要求，不是优化项。**
> 实测同一条 20 分钟音频：GPU（float16）**2 分 18 秒**，CPU（int8）**13 分 54 秒**，差 6.0 倍。
> 100 条视频在 CPU 上要 23 小时，GPU 上 3.8 小时。CPU 方案批量场景不可用。依据见 `docs/实测记录.md` §2。

### 2. 安装依赖

```bash
python -m venv .venv
.venv/Scripts/activate

pip install -r requirements.txt

# CUDA 运行库：Windows 必需，DLL 不在 PATH 上
# 注意：PyPI 直连会卡死，必须用国内镜像**且**禁用代理（否则 pip 绕道系统代理反而更慢）
pip install -i https://mirrors.cloud.tencent.com/pypi/simple --proxy "" \
    nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-nvrtc-cu12
```

### 3. 下载模型（2.87 GB，不入库）

HuggingFace 直连不通，走 hf-mirror。但镜像对 LFS 文件会 302 到 xethub，而 `huggingface_hub` 默认下载超时只有 10 秒，容易失败——用 `curl -C -` 断点续传最可靠：

```bash
mkdir -p models/large-v3 && cd models/large-v3
for f in config.json preprocessor_config.json tokenizer.json vocabulary.json model.bin; do
  curl -sL --retry 5 --retry-delay 3 -C - -o "$f" \
    "https://hf-mirror.com/Systran/faster-whisper-large-v3/resolve/main/$f" &
done; wait
cd ../..
```

实测约 41 MB/s，全程约 1 分 35 秒。完成后 `model.bin` 应约 2.87 GB。

### 4. 配置

```bash
cp config.example.yaml config.yaml
```

按需修改 `paths.work_dir`（**必须在 D 盘**，C 盘仅剩 150 GB）与 `runtime` 各项。字段语义见 `PLAN.md` §5。

### 5. 环境自检

```bash
python -m app.cli env            # 秒级：CPU/内存/磁盘/GPU/ffmpeg/yt-dlp/模型
python -m app.cli env --verify   # 额外跑一次真实推理确认 CUDA（约 5 秒，占 3 GB 显存）
```

确认输出中 GPU 可用、模型存在、ffmpeg 与 yt-dlp 已定位。

> ⚠️ `ctranslate2.get_cuda_device_count() == 1` **不代表 GPU 可用**，只代表检测到硬件。
> 真正的验证是跑一次真实推理——这就是 `--verify` 做的事。

### 6. 运行

```bash
# 单条在线链接（抖音分享文案可直接粘贴，会自动抽 URL）
python -m app.cli run "https://v.douyin.com/xxxxx/" --cookies cookies.txt

# 本地文件
python -m app.cli run samples/video.mp4 --no-frames

# 批量（从文本文件，每行一条）
python -m app.cli run --file urls.txt --limit 10

# 并发调度（默认）：下载/抽帧按实测内存自动规划，转写恒为 1（GPU 单卡串行）
python -m app.cli run --file urls.txt
python -m app.cli run --file urls.txt --jobs 1     # 强制串行，阶段 1 验收走的那条路径
python -m app.cli run --file urls.txt --trace trace.json   # 落盘各阶段起止记录

# 查看任务表
python -m app.cli list
```

产出结构：`<work_dir>/<四位序号>_<安全标题>/`，内含
`video.mp4 / audio.wav / transcript.{txt,srt,json} / frames/ / sheets/`。

常用开关：`--initial-prompt`（领域术语提示，**会改变分段与标点**，见 `docs/实测记录.md` §2.4）、
`--frames`、`--jobs`、`--trace`、`--retry`、`--force`（删产出重跑）、`--dry-run`。

**中断与续跑**：Ctrl+C 会「停止取新任务，等在跑的跑完（最长 30 秒）」，已完成的进度留在 `state.db`。
下次运行同一条命令即自动续跑——已完成的不重跑，停在中间态的回退到上一稳定态。
**不需要** `--retry`（那是给 `failed` / `cancelled` 用的）。

## samples/ — 测试基线（本地留存，不入库）

阶段 1 的验收方式是「重跑同一段视频，产出与基线逐字符比对」。基线文件**不进 git**（派生自第三方视频），但在本地必须保留，**不要删**。原因、代价与重建方式见 `samples/README.md`。

| 文件 | 用途 |
|---|---|
| `video.mp4` | 测试输入，20 分 04 秒，1280×720，HEVC，44 MB |
| `video.info.json` | yt-dlp 元数据（标题、作者、时长、发布日） |
| `transcript_gpu.txt` / `.srt` / `.json` | **金标准（v2）**：GPU float16 转写，662 段。完整参数快照见 `samples/README.md` |
| `transcript_cpu_int8.txt` / `.srt` / `.json` | 对照：CPU int8 转写结果（13 分 54 秒，646 段，分段边界略异） |
| `archive/` | 已作废的 v1 基线（649 段，参数未记录） |
| `frames_index.json` | 场景抽帧索引（212 帧，含每帧时间戳） |
| `sheets/` | 抽帧拼版产出样例（9 张 6×4 网格） |

**基线比对建议**：同配置下转写**可逐字节复现**（2026-10-03 两次独立运行验证，见 `docs/实测记录.md` §2.4），
所以阶段 1 验收直接用严格相等。跨配置（GPU ↔ CPU）**不复现**——两版基线本身不同（662 vs 646 段），
比对前先确认用哪个。

> ✅ **2026-10-03：基线已换代（v1 → v2）** 并附**完整参数快照**（`samples/README.md`）。
> v1（649 段）因产生它的 `initial_prompt` 未记录而作废。判据未放宽，只是基线换代——
> **比对失败先查参数快照表，不要先怀疑代码**（实现与原型同参数下已实测逐字节一致）。

## 已知坑（实现时必读 PLAN.md §9）

1. **CUDA DLL 加载**：`os.add_dll_directory()` 的返回值**必须引用住**，且还要注入 `PATH`，且必须在 `import ctranslate2` 之前 —— 三层缺一不可，否则报 `cublas64_12.dll not found`。
2. `ctranslate2.get_cuda_device_count() == 1` **不代表 GPU 可用**，只代表检测到硬件。真正的验证是跑一次真实推理。
3. **faster-whisper 1.2.1 与 av 19 不兼容**（`metadata_errors` 已移除）—— 绕开 PyAV，用 `wave` + `numpy` 直读 wav。
4. **ffmpeg 9.0 移除了 `-vsync`** —— 改用 `-fps_mode passthrough`。
5. **yt-dlp 读浏览器 cookie 在 Windows 新版浏览器上必失败** —— 走 playwright-cli 导出 storage-state 再转 Netscape 格式。
6. Playwright 自带的 ffmpeg 是 `--disable-everything` 构建，**没有 mp4 demuxer**，不能用。
7. **WinGet `Links/` 下的 `ffmpeg.exe` 是 0 字节占位文件**（app execution alias）——`shutil.which()` 能命中，但调用会报 `[WinError 193] 不是有效的 Win32 应用程序`。定位时必须校验「非空 + PE 头 `MZ`」，真实可执行在 `WinGet/Packages/Gyan.FFmpeg_*/`。
8. **`initial_prompt` 会改变分段与标点**，不只是纠术语——同一音频加了它从 681 段碎句变 662 段整句带标点。它是影响验收判据的强参数，必须随基线一起记录。
9. **`dispatcher.submit()` 会主动跳过 `done` 状态的任务**。`--force` / `--retry` 要跑的恰恰是 `done` / `failed` 的那几条，所以必须在入队前把状态重新盖成 `pending`——否则 `--force` 会「删完产出目录却一条都不入队」，日志里只剩一句「没有任务被入队」（2026-10-03 实测踩到）。
10. **子进程输出必须两端都钉死 UTF-8**。Windows 下子进程默认按本地代码页（cp936）编码，父进程按 UTF-8 解码 → `UnicodeDecodeError` 抛在读取线程里、日志全丢。子进程设 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`，父进程 `subprocess.run(..., encoding="utf-8", errors="replace")`。`taskkill` 同理（它会往 stderr 写 cp936 提示）。
11. **不要用 `shutil.rmtree` 删整批产出目录**。一批任务的产出有 200+ 个文件，会命中受管环境的批量删除确认（阈值 50）而中断脚本。改为「改名旁置」，既不受该规则影响，又保留了现场。

## 内存约束（第一约束，不是 CPU 也不是显存）

总 61.6 GB，**实测可用内存波动区间 12.6 ~ 26.2 GB**（同日 1 小时内两次测量差一倍）。

这一个数字就说明了为什么**并发数绝不能写死**——`PLAN.md` §3.2 的动态探测公式是本机唯一可行的方案。跑批前建议关掉浏览器等占内存程序。
