"""硬件能力探测。

回答的问题是「这台机器实际能做多少并行」——即 PLAN.md §3.2 并发公式的输入。

设计约束（PLAN.md §4.1）：

- 探测项失败返回 None / 空集合，**绝不抛异常**。自检脚本不该因为查不到显卡就崩。
- `cuda.usable` 必须由**真实推理**验证，不能读 `get_cuda_device_count()`。
  后者只说明「检测到硬件」，不代表 cuBLAS/cuDNN 能加载（PLAN.md §9.1）。
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

# 探测超时：nvidia-smi 偶发卡住，不能让它拖死整个自检
_NVIDIA_SMI_TIMEOUT = 15


@dataclass
class CpuInfo:
    name: str | None = None
    physical: int | None = None
    logical: int | None = None
    mhz: int | None = None


@dataclass
class MemoryInfo:
    total_gb: float | None = None
    avail_gb: float | None = None
    load_pct: int | None = None


@dataclass
class DiskInfo:
    total_gb: float | None = None
    free_gb: float | None = None


@dataclass
class GpuInfo:
    name: str
    vram_total_mb: int
    vram_used_mb: int
    driver: str
    compute_cap: str


@dataclass
class CudaInfo:
    """CUDA 可用性。

    `usable` 的三态语义：None = 未验证，True/False = 已由真实推理验证。
    任何基于 `device_count` 的结论都是不可信的。
    """

    ctranslate2: str | None = None
    device_count: int | None = None
    usable: bool | None = None
    detail: str | None = None
    verified: bool = False

    @property
    def needs_verification(self) -> bool:
        return not self.verified


@dataclass
class EnvReport:
    cpu: CpuInfo = field(default_factory=CpuInfo)
    memory: MemoryInfo = field(default_factory=MemoryInfo)
    disk: dict[str, DiskInfo] = field(default_factory=dict)
    gpu: list[GpuInfo] = field(default_factory=list)
    cuda: CudaInfo = field(default_factory=CudaInfo)
    python: str = ""
    executable: str = ""
    probed_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["cuda"]["needs_verification"] = self.cuda.needs_verification
        return data


# ---------------------------------------------------------------- 单机探测


def cpu_info() -> CpuInfo:
    info = CpuInfo(logical=os.cpu_count() or None)
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        try:
            info.name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
        for reg_name, attr in (("NumberOfCores", "physical"), ("~MHz", "mhz")):
            try:
                setattr(info, attr, winreg.QueryValueEx(key, reg_name)[0])
            except OSError:
                pass
        winreg.CloseKey(key)
    except Exception:  # noqa: BLE001 — 非 Windows 或注册表不可读，保留 logical 即可
        pass
    return info


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def memory_info() -> MemoryInfo:
    """可用内存是**本机第一约束**（实测在 12.6~26.2 GB 间波动，见实测记录 §1），

    因此每次启动都要重新读，不允许缓存后复用。
    """
    if os.name != "nt":
        # 本项目为 Windows 专用（CUDA DLL 注入与 WinGet 目录布局都依赖 Windows）。
        # 其他平台不做内存探测，返回空值由调用方按保守默认处理。
        return MemoryInfo()
    try:
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return MemoryInfo()
        return MemoryInfo(
            total_gb=round(stat.ullTotalPhys / 1024**3, 1),
            avail_gb=round(stat.ullAvailPhys / 1024**3, 1),
            load_pct=int(stat.dwMemoryLoad),
        )
    except Exception:  # noqa: BLE001
        return MemoryInfo()


def disk_info(extra_paths: Iterable[str | Path] = ()) -> dict[str, DiskInfo]:
    """探测各盘符余量。默认覆盖 C/D，并额外覆盖传入路径（如 work_dir）所在盘。"""
    drives = {"C:", "D:"}
    for raw in extra_paths:
        if not raw:
            continue
        drive = Path(raw).drive
        if drive:
            drives.add(drive)

    out: dict[str, DiskInfo] = {}
    for drive in sorted(drives):
        try:
            usage = shutil.disk_usage(f"{drive}\\")
        except OSError:
            continue
        out[drive] = DiskInfo(
            total_gb=round(usage.total / 1024**3, 1),
            free_gb=round(usage.free / 1024**3, 1),
        )
    return out


def gpu_info() -> list[GpuInfo]:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,driver_version,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_NVIDIA_SMI_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 — 无 nvidia-smi / 超时，按无 GPU 处理
        return []

    if proc.returncode != 0:
        return []

    gpus: list[GpuInfo] = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpus.append(
                GpuInfo(
                    name=parts[0],
                    vram_total_mb=int(float(parts[1])),
                    vram_used_mb=int(float(parts[2])),
                    driver=parts[3],
                    compute_cap=parts[4],
                )
            )
        except ValueError:
            continue
    return gpus


def cuda_info() -> CudaInfo:
    """只做快速探测：版本号 + 硬件计数。`usable` 留为 None 待真实验证。"""
    import app.core.cuda_dll  # noqa: F401 — 副作用：先注入 DLL 目录

    try:
        import ctranslate2
    except Exception as exc:  # noqa: BLE001
        return CudaInfo(detail=f"导入 ctranslate2 失败：{type(exc).__name__}: {exc}")

    info = CudaInfo(ctranslate2=getattr(ctranslate2, "__version__", None))
    try:
        info.device_count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001
        info.detail = f"get_cuda_device_count 失败：{type(exc).__name__}: {exc}"
    return info


# ---------------------------------------------------------------- 汇总入口


def probe(work_dir: str | Path | None = None) -> EnvReport:
    """快速探测（不加载模型，秒级返回）。

    要确认 GPU 真能用，必须再调 `verify_cuda()`——`probe()` 不做这件事，
    因为模型加载要数秒，不该塞进每次启动的自检里。
    """
    return EnvReport(
        cpu=cpu_info(),
        memory=memory_info(),
        disk=disk_info([work_dir] if work_dir else ()),
        gpu=gpu_info(),
        cuda=cuda_info(),
        python=sys.version.split()[0],
        executable=sys.executable,
        probed_at=datetime.now().isoformat(timespec="seconds"),
    )


def verify_cuda(
    model_path: str | Path,
    device: str = "cuda",
    compute_type: str = "float16",
) -> tuple[bool, str]:
    """跑一次**真实推理**确认 CUDA 可用，返回 (是否可用, 说明)。

    为什么不能用 `get_cuda_device_count()`：它只枚举硬件，即使 cuBLAS/cuDNN
    缺失也会返回 1。唯一可信的判据是「模型能加载 + 能出结果」。
    """
    import app.core.cuda_dll  # noqa: F401 — 必须在 faster_whisper 之前

    model_dir = Path(model_path)
    if not model_dir.exists():
        return False, f"模型不存在：{model_dir}（按 README 第 3 步下载）"

    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except Exception as exc:  # noqa: BLE001
        return False, f"导入 faster_whisper 失败：{type(exc).__name__}: {exc}"

    try:
        model = WhisperModel(str(model_dir), device=device, compute_type=compute_type)
    except Exception as exc:  # noqa: BLE001
        return False, f"模型加载失败（CUDA 运行库不可用？）：{type(exc).__name__}: {exc}"

    try:
        silence = np.zeros(16000, dtype=np.float32)  # 1 秒静音，够触发一次前向
        segments, info = model.transcribe(silence, language="zh", beam_size=1)
        list(segments)  # 生成器必须被消费，否则推理根本没执行
    except Exception as exc:  # noqa: BLE001
        return False, f"推理失败：{type(exc).__name__}: {exc}"

    return True, f"真实推理通过（输入 {info.duration:.1f}s，device={device}）"


def dumps(report: EnvReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
