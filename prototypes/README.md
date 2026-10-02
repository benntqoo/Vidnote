# prototypes/ — 已验证原型脚本

这三个脚本**不是正式实现**，而是 2026-10-02 那轮真实任务中跑通的产物。

**它们的价值**：整套系统里最可靠的部分。CUDA DLL 加载、PyAV 规避、cookies 格式转换这些坑，
已经在这些脚本里解决并实测过。阶段 1 的任务就是把它们重构成 `app/core/` 下的正式模块，
**重写逻辑而不是照抄结构**——照抄会把命令行参数解析也带进核心模块。

## 对应关系

| 原型 | 目标模块 | 重构要点 |
|---|---|---|
| `transcribe.py` | `app/core/transcriber.py` | 抽出 `add_nvidia_dll_dirs()` 到独立位置（**必须在 `import ctranslate2` 之前执行**）；`load_wav_16k_mono()` 保留 |
| `state2cookie.py` | `app/core/cookies.py` | 输入从文件改为 playwright CLI 输出；补 cookie 有效性检测与自动重取 |
| `make_sheets.py` | `app/core/frames.py` | 去掉「去重阈值」参数（实测对动态口播帧无效，212 帧全留）；改为场景检测 + 拼版 |

`../tools/probe_env.py` 同理 → `app/core/env_probe.py`。

## 各脚本的实测环境假设

这些脚本硬编码了本机路径，重构时必须改为配置驱动：

| 脚本 | 硬编码项 | 实际值 |
|---|---|---|
| `transcribe.py` | ffmpeg、模型路径（命令行传入） | 无硬编码，已通用 |
| `state2cookie.py` | 无 | 输入输出均为命令行参数，已通用 |
| `make_sheets.py` | 无 | 已通用 |
| `probe_env.py` | 无 | 已通用，但 `cuda.usable` 字段需补真实推理验证 |

## 直接可用的调用方式

```bash
PY="C:/Users/Ben/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

# 转写（GPU，实测 8.7× 实时）
"$PY" prototypes/transcribe.py <audio.wav> \
    --model ../models/large-v3 --device cuda --compute-type float16 \
    --language zh --out <输出前缀>

# cookies 转换（playwright state.json → Netscape cookies.txt）
"$PY" prototypes/state2cookie.py <state.json> <cookies.txt>

# 抽帧 + 拼版（拼接模式：ffmpeg 先抽帧，本脚本做拼版）
"$PY" prototypes/make_sheets.py <frames_dir> <ffmpeg_showinfo_log> <out_dir> \
    --threshold 8 --cols 6 --rows 4
```

## 关键实现细节（重构时不要丢）

**`transcribe.py` 中 CUDA DLL 加载的三层要求**（这是本机耗时最长的坑，重踩一次要 1 小时）：

```python
_DLL_DIR_HANDLES: list = []          # ① 句柄必须保活，否则 GC 后目录失效

def add_nvidia_dll_dirs() -> list[str]:
    ...
    for pkg in sorted(Path(paths[0]).iterdir()):
        bin_dir = pkg / "bin"
        if bin_dir.is_dir():
            handle = os.add_dll_directory(str(bin_dir))
            _DLL_DIR_HANDLES.append(handle)          # ← 不能省
            added.append(str(bin_dir))
    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")
        #                                                       ↑ 不能省
    return added

add_nvidia_dll_dirs()    # ② 必须在 import faster_whisper / ctranslate2 之前
```

③ `ctranslate2.get_cuda_device_count() == 1` **不代表 GPU 可用**，只代表硬件存在。真实验证必须跑一次推理。

## 已知的历史局限

- `make_sheets.py` 的 `--threshold` 感知哈希去重对动态口播帧**无效**（212 帧全保留）。重构时改场景检测方案。
- `transcribe.py` 曾因 `av 19` 与 `faster-whisper 1.2.1` 不兼容而失败，现用 `wave` + `numpy` 直读 wav 规避。
- 两者的命令行参数是为单次调试设计的，不适合作为 `app/core/` 的接口。**接口契约以 PLAN.md §4.1 为准**。
