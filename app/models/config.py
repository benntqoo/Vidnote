"""配置读取与校验。

字段语义与默认值依据见 PLAN.md §5。
构造完成后 `paths` 内的路径已解析为绝对路径。

设计取舍：默认值一律用**相对路径**（`work` / `models`），这样仓库 clone 到任何
位置都能直接跑；`config.example.yaml` 里给的是本机推荐值（绝对 D 盘路径）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

import yaml

T = TypeVar("T")

DEFAULT_CONFIG_NAME = "config.yaml"
EXAMPLE_CONFIG_NAME = "config.example.yaml"

VALID_DEVICES = ("auto", "cuda", "cpu")
VALID_COMPUTE_TYPES = ("auto", "float16", "float32", "int8", "int8_float16")


class ConfigError(Exception):
    """配置文件缺失或字段非法。"""


def _section(cls: type[T], data: Any) -> T:
    """从 dict 构造配置段，忽略未知键与显式 null（用 dataclass 默认值兜底）。"""
    if not isinstance(data, dict):
        return cls()
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known and v is not None})


@dataclass
class PathsConfig:
    """路径配置。

    `work_dir` 是本机第一约束相关项：C 盘仅剩 150 GB，工作目录必须放 D 盘
    （理由见 PLAN.md §2 / docs/实测记录.md §1）。
    """

    work_dir: str = "work"
    model_dir: str = "models"
    # None 表示按 ffmpeg_locator 的四级顺序自动定位（PLAN.md §9.4）
    ffmpeg: str | None = None
    # None 表示按 yt_dlp_locator 的顺序自动定位（config → PATH）
    yt_dlp: str | None = None
    # Netscape 格式 cookies.txt。None 表示不带 cookie 下载（公开视频可用；
    # 抖音多数内容需要，见 PLAN.md §9.5 —— ttwid 约 24 小时过期）
    cookies_file: str | None = None

    def resolve(self, root: Path) -> PathsConfig:
        """把相对路径锚定到项目根目录。"""
        work = Path(self.work_dir).expanduser()
        model = Path(self.model_dir).expanduser()
        cookies = Path(self.cookies_file).expanduser() if self.cookies_file else None
        return PathsConfig(
            work_dir=str(work if work.is_absolute() else root / work),
            model_dir=str(model if model.is_absolute() else root / model),
            ffmpeg=self.ffmpeg,
            yt_dlp=self.yt_dlp,
            cookies_file=(
                str(cookies if cookies.is_absolute() else root / cookies) if cookies else None
            ),
        )


@dataclass
class RuntimeConfig:
    """运行期设备与并发上限。

    `device` / `compute_type` 为 auto 时由 `effective_*` 结合环境探测结果解析，
    与原型实测口径一致：cuda → float16，cpu → int8。
    """

    device: str = "auto"
    compute_type: str = "auto"
    model: str = "large-v3"
    auto_concurrency: bool = True
    max_download: int = 8
    max_frame: int = 4
    max_summarize: int = 4

    def effective_device(self, cuda_usable: bool) -> str:
        if self.device != "auto":
            return self.device
        return "cuda" if cuda_usable else "cpu"

    def effective_compute_type(self, device: str) -> str:
        if self.compute_type != "auto":
            return self.compute_type
        return "float16" if device == "cuda" else "int8"


@dataclass
class TranscribeConfig:
    """转写参数。

    ⚠️ `vad_min_silence_ms` 与 `beam_size` 直接决定**分段边界**。
    samples/transcript_gpu.txt 这条金标准基线就是在
    `beam_size=5` + `vad_min_silence_ms=400` 下产生的，
    改动这两个值会导致验收的逐字符比对失败——那不是 bug，是参数变了。
    """

    language: str = "zh"
    beam_size: int = 5
    vad_filter: bool = True
    vad_min_silence_ms: int = 400
    initial_prompt: str = ""


@dataclass
class FramesConfig:
    """抽帧与拼版。默认关闭——实测多数口播视频画面无独立信息量（实测记录 §4）。"""

    enabled: bool = False
    scene_threshold: float = 0.3
    max_frames: int = 400
    sheet_cols: int = 6
    sheet_rows: int = 4


@dataclass
class SummarizeConfig:
    """汇总（可选，走 API）。密钥不进配置文件，只留环境变量名。"""

    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "VIDNOTE_API_KEY"
    model: str = "gpt-4o"
    template: str = "摘要+章节大纲+要点"


@dataclass
class CookieConfig:
    browser: str = "chromium"
    auto_refresh: bool = True


@dataclass
class DownloadConfig:
    rate_limit_per_min: int = 10
    keep_intermediate: bool = True


@dataclass
class Config:
    """顶层配置。`root` 是配置文件所在目录，用于锚定相对路径，不写回 yaml。"""

    paths: PathsConfig = field(default_factory=PathsConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    frames: FramesConfig = field(default_factory=FramesConfig)
    summarize: SummarizeConfig = field(default_factory=SummarizeConfig)
    cookie: CookieConfig = field(default_factory=CookieConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    root: Path = field(default_factory=Path.cwd, repr=False, compare=False)

    # ---- 构造 ----

    @classmethod
    def from_dict(cls, data: Any, root: str | Path | None = None) -> Config:
        d = data if isinstance(data, dict) else {}
        cfg = cls(
            paths=_section(PathsConfig, d.get("paths")),
            runtime=_section(RuntimeConfig, d.get("runtime")),
            transcribe=_section(TranscribeConfig, d.get("transcribe")),
            frames=_section(FramesConfig, d.get("frames")),
            summarize=_section(SummarizeConfig, d.get("summarize")),
            cookie=_section(CookieConfig, d.get("cookie")),
            download=_section(DownloadConfig, d.get("download")),
            root=Path(root) if root else Path.cwd(),
        )
        cfg.paths = cfg.paths.resolve(cfg.root)
        return cfg

    @classmethod
    def from_file(cls, path: str | Path) -> Config:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ConfigError(f"配置文件不存在：{p}")
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML 解析失败：{p}\n{exc}") from exc
        cfg = cls.from_dict(raw, root=p.parent)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """加载指定配置；未指定则用项目根下的 config.yaml。文件缺失即报错。"""
        p = Path(path) if path else Path(DEFAULT_CONFIG_NAME)
        if not p.is_file():
            raise ConfigError(
                f"配置文件不存在：{p}\n"
                f"先执行：cp {EXAMPLE_CONFIG_NAME} {DEFAULT_CONFIG_NAME}"
            )
        return cls.from_file(p)

    @classmethod
    def load_or_default(cls, path: str | Path | None = None) -> tuple[Config, bool]:
        """配置缺失时回退到默认值，返回 (配置, 是否使用了默认值)。

        这样 clone 下来没有 config.yaml 也能直接跑（用相对路径默认值），
        符合「下载即可运行」的目标。
        """
        p = Path(path) if path else Path(DEFAULT_CONFIG_NAME)
        if p.is_file():
            return cls.from_file(p), False
        return cls.from_dict({}, root=Path.cwd()), True

    # ---- 输出 ----

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("root", None)
        return data

    def save(self, path: str | Path) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    # ---- 派生 ----

    def model_path(self) -> Path:
        """模型本地目录。不存在时由调用方给出下载指引，不做隐式联网下载。"""
        return Path(self.paths.model_dir) / self.runtime.model

    def work_path(self) -> Path:
        return Path(self.paths.work_dir)

    # ---- 校验 ----

    def validate(self) -> None:
        if self.runtime.device not in VALID_DEVICES:
            raise ConfigError(
                f"runtime.device 非法：{self.runtime.device!r}，可选 {VALID_DEVICES}"
            )
        if self.runtime.compute_type not in VALID_COMPUTE_TYPES:
            raise ConfigError(
                f"runtime.compute_type 非法：{self.runtime.compute_type!r}，"
                f"可选 {VALID_COMPUTE_TYPES}"
            )
        if self.transcribe.beam_size < 1:
            raise ConfigError(f"transcribe.beam_size 必须 >= 1，当前 {self.transcribe.beam_size}")
        if self.transcribe.vad_min_silence_ms < 0:
            raise ConfigError(
                f"transcribe.vad_min_silence_ms 必须 >= 0，当前 {self.transcribe.vad_min_silence_ms}"
            )
        if self.frames.sheet_cols < 1 or self.frames.sheet_rows < 1:
            raise ConfigError("frames.sheet_cols / sheet_rows 必须 >= 1")
        if self.download.rate_limit_per_min < 1:
            raise ConfigError("download.rate_limit_per_min 必须 >= 1")
