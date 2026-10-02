# Vidnote

> Video → Note。批量把在线视频（抖音 / B站 / YouTube）转成本地结构化文稿。
>
> 下载 → 抽音频 → **GPU 转写** → 抽帧拼版 → （可选）汇总

## 状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| — | 链路验证 + 环境实测 | ✅ 完成（见 `docs/实测记录.md`） |
| — | 开发计划书 | ✅ 完成（见 `PLAN.md`） |
| 1 | 核心流水线（CLI，无 GUI） | ⬜ 待开发 |
| 2 | 并发调度 + 断点续跑 | ⬜ 待开发 |
| 3 | GUI（PySide6） | ⬜ 待开发 |
| 4 | cookie 自动化 + 汇总模块 | ⬜ 待开发 |
| 5 | 打包（PyInstaller） | ⬜ 待开发 |

**下一步是阶段 1**，验收判据见 `PLAN.md` §8。不要跳阶段。

## 仓库内容边界

本仓库只放**源码与技术文档**——clone 下来装好依赖、下好模型即可运行，不需要仓库外的素材。

| 类型 | 内容 | 入库 |
|---|---|---|
| 源码 | `app/`、`prototypes/`、`tools/` | ✅ |
| 技术文档 | `PLAN.md`、`README.md`、`docs/实测记录.md`、`prototypes/README.md`、`samples/README.md` | ✅ |
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
├── app/                     # 正式实现（阶段 1 起填充，按 PLAN.md §4 布局）
│   ├── core/                #   env_probe / concurrency / dispatcher / store / fetcher / ...
│   ├── models/              #   Task / Config 数据类
│   └── ui/                  #   GUI（阶段 3）
├── prototypes/              # ★已验证原型脚本，阶段 1 重构的直接输入
│   ├── transcribe.py        #   → app/core/transcriber.py（含 CUDA DLL 加载的正确实现）
│   ├── state2cookie.py      #   → app/core/cookies.py
│   ├── make_sheets.py       #   → app/core/frames.py
│   └── README.md            #   各脚本的接口与环境假设
├── tools/
│   └── probe_env.py         #   → app/core/env_probe.py（硬件探测，已实跑）
├── models/                  # 模型权重目录（内容不入库，需自行下载）
├── samples/                 # 测试基线（内容不入库，仅 README.md 入库）
└── docs/
    ├── 实测记录.md           # ★事实源：本机性能实测与环境快照
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
python tools/probe_env.py
```

确认输出中 GPU 可用。

> ⚠️ `ctranslate2.get_cuda_device_count() == 1` **不代表 GPU 可用**，只代表检测到硬件。
> 真正的验证是跑一次真实推理。

### 6. 运行（阶段 1 完成后）

```bash
python -m app.cli --url "https://v.douyin.com/xxxxx/"
```

## samples/ — 测试基线（本地留存，不入库）

阶段 1 的验收方式是「重跑同一段视频，产出与基线逐字符比对」。基线文件**不进 git**（派生自第三方视频），但在本地必须保留，**不要删**。原因、代价与重建方式见 `samples/README.md`。

| 文件 | 用途 |
|---|---|
| `video.mp4` | 测试输入，20 分 04 秒，1280×720，HEVC，44 MB |
| `video.info.json` | yt-dlp 元数据（标题、作者、时长、发布日） |
| `transcript_gpu.txt` / `.srt` / `.json` | **金标准**：GPU float16 转写结果（137.7 秒跑完，649 段） |
| `transcript_cpu_int8.txt` / `.srt` / `.json` | 对照：CPU int8 转写结果（13 分 54 秒，646 段，分段边界略异） |
| `frames_index.json` | 场景抽帧索引（212 帧，含每帧时间戳） |
| `sheets/` | 抽帧拼版产出样例（9 张 6×4 网格） |

**基线比对建议**：同配置下转写**可逐字符复现**（实测 100% 一致，见 `docs/实测记录.md` §2.3），所以阶段 1 验收直接用严格相等即可。跨配置（GPU ↔ CPU）**不复现**——两版基线本身差 3 段（649 vs 646），比对前先确认用哪个基线。

## 已知坑（实现时必读 PLAN.md §9）

1. **CUDA DLL 加载**：`os.add_dll_directory()` 的返回值**必须引用住**，且还要注入 `PATH`，且必须在 `import ctranslate2` 之前 —— 三层缺一不可，否则报 `cublas64_12.dll not found`。
2. `ctranslate2.get_cuda_device_count() == 1` **不代表 GPU 可用**，只代表检测到硬件。真正的验证是跑一次真实推理。
3. **faster-whisper 1.2.1 与 av 19 不兼容**（`metadata_errors` 已移除）—— 绕开 PyAV，用 `wave` + `numpy` 直读 wav。
4. **ffmpeg 9.0 移除了 `-vsync`** —— 改用 `-fps_mode passthrough`。
5. **yt-dlp 读浏览器 cookie 在 Windows 新版浏览器上必失败** —— 走 playwright-cli 导出 storage-state 再转 Netscape 格式。
6. Playwright 自带的 ffmpeg 是 `--disable-everything` 构建，**没有 mp4 demuxer**，不能用。

## 内存约束（第一约束，不是 CPU 也不是显存）

总 61.6 GB，**实测可用内存波动区间 12.6 ~ 26.2 GB**（同日 1 小时内两次测量差一倍）。

这一个数字就说明了为什么**并发数绝不能写死**——`PLAN.md` §3.2 的动态探测公式是本机唯一可行的方案。跑批前建议关掉浏览器等占内存程序。
