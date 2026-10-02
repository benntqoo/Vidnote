# samples/ — 回归测试基线

> ⚠️ **本目录下的多数基线文件有意未纳入 git。**
> 它们仍留在本地磁盘，阶段 1 的验收依赖它们；但不进版本库。

## 为什么不入库

本仓库（`github.com/benntqoo/Vidnote`）是**公开仓库**，而下列素材全部派生自一段第三方视频：
原始画面、原始音频，以及由该音频产生的转写文本，均属第三方内容。
公开发布涉及版权问题，因此只保留在本地。**2026-10-02 由头头决定。**

## 基线清单

| 文件 | 用途 | 入库 |
|---|---|---|
| `video.mp4` | 测试输入，20 分 04 秒，1280×720，HEVC，44 MB | ❌ |
| `video.info.json` | yt-dlp 元数据（标题、作者、时长、发布日、原始 URL） | ❌ |
| `transcript_gpu.txt` / `.srt` / `.json` | **金标准（v2）**：见下方「参数快照」 | ❌ |
| `transcript_cpu_int8.txt` / `.srt` / `.json` | 对照：CPU int8 转写结果（13 分 54 秒，646 段，分段边界略异） | ❌ |
| `archive/transcript_gpu_v1_649seg_promptUNRECORDED.*` | **已作废的 v1 基线**（649 段）——参数未记录，不可复现，仅作历史参考 | ❌ |
| `frames_index.json` | 场景抽帧索引（212 帧，含每帧时间戳） | ✅ |
| `sheets/sheet_01..09.jpg` | 抽帧拼版产出样例（9 张 6×4 网格） | ❌ |

`frames_index.json` 只含时间戳与帧号，不含任何内容信息，故保留入库。

## 🔴 基线参数快照（**必须与基线一起维护**）

> **这条规矩是 2026-10-03 用两小时排查换来的。**
> 只存产出不存参数，"金标准"就是不可验证的——v1 基线正是这样作废的。

`transcript_gpu.*`（v2）由下列**完整**参数产生。任一参数变化都会改变产出，
**比对失败先查这张表，不要怀疑代码**：

| 项 | 值 |
|---|---|
| 输入 | `samples/video.mp4`（1204 秒） |
| 模型 | `models/large-v3`（faster-whisper 格式，2.87 GB） |
| device / compute_type | `cuda` / `float16` |
| language | `zh` |
| beam_size | `5` |
| vad_filter | `true` |
| vad_min_silence_ms | `400` |
| condition_on_previous_text | `false`（代码内固定，不暴露为配置） |
| **initial_prompt** | `这是一段关于AI量化交易的讲解，涉及回测、过拟合、夏普比率、特征工程、样本外验证等概念。` |
| 抽音频 | `ffmpeg -vn -acodec pcm_s16le -ar 16000 -ac 1` |
| 产出 | **662 段**，`transcript.txt` = 24547 字节（CRLF 行尾） |
| 环境 | faster-whisper 1.2.1 / ctranslate2 4.8.2 / Python 3.13.14 / RTX 4060 Ti 16GB / 驱动 610.88 |

**生成与验证命令**（复现只需这两条，用时各约 2.5 分钟）：

```bash
cp config.example.yaml config.yaml          # 基线就是用模板的原样值跑的
python -m app.cli run samples/video.mp4 --force
diff work/0001_video/transcript.txt samples/transcript_gpu.txt   # 应无输出
```

**已验证**：2026-10-03 两次独立运行，`txt` / `srt` / `json` 三种格式**全部逐字节一致**。

### v1 基线作废的原因（留档）

v1（649 段，整句带标点）产生时命令行传过 `--initial-prompt`，但**那个字符串没写进任何文件**。
2026-10-03 用正式实现重跑得 681 段（碎句无标点），五步二分排查后确认：
实现与原型脚本在同参数下产出**逐字节一致**，唯一变量就是 `initial_prompt` 的取值。
完整证据链见 `docs/实测记录.md` §2.4。

教训：`initial_prompt` 不只是纠术语——它**改变 whisper 的分段与标点风格**
（prompt 本身是带标点的完整句，模型会模仿）。同一音频不加它 681 段、加它 662 段。

## 已知代价（坦诚记录）

1. **新机器 clone 后无法直接跑阶段 1 验收** —— 缺 `transcript_gpu.txt` 基准。
2. **基线失去了版本保护** —— 本地误删即永久丢失。这是本次权衡的代价，
   换取的是不把第三方内容推到公开仓库。
3. 若将来仍需要版本保护：把仓库改为 Private，然后撤销 `.gitignore` 末尾「第三方素材」
   一节，执行 `git add -f samples/` 即可（届时历史里仍不含这些 blob，需重新提交）。

## 重建方式

- `video.mp4` 的原始来源记录在本地 `video.info.json` 的 `webpage_url` 字段（该字段未入库，
  避免在公开仓库中留存第三方内容标识）。
- 转写稿基线的重建：见上方「生成与验证命令」。**参数必须与「基线参数快照」完全一致**，
  否则不复现——这不是 bug，是参数变了。
- 注意**跨配置不复现**：GPU（662 段）与 CPU int8（646 段）两版基线不同，
  比对前先确认用的是哪个。
