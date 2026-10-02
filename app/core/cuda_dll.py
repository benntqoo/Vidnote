"""Windows 下把 pip 安装的 NVIDIA 运行库 DLL 暴露给 ctranslate2。

⚠️ **必须在 `import ctranslate2` / `import faster_whisper` 之前导入本模块。**

三层要求缺一不可（PLAN.md §9.1 / docs/实测记录.md §6，实测踩坑约 1 小时）：

1. `os.add_dll_directory()` 返回的句柄**必须被引用住**。Python 文档明确：句柄被
   GC 之后该目录会从搜索路径中移除——只调用不保存返回值等于没调用。
2. 还要注入 `os.environ["PATH"]`。`add_dll_directory` 只对
   `LoadLibraryEx(..., LOAD_LIBRARY_SEARCH_USER_DIRS)` 生效，而 ctranslate2 用的是
   裸名 `LoadLibrary("cublas64_12.dll")`，解析走**进程 PATH**。
3. 时机必须在原生库加载之前——`add_dll_directory` 只影响其后的 DLL 解析。

另注：`ctranslate2.get_cuda_device_count() == 1` **不代表 GPU 可用**，只代表检测到
硬件。真正的验证见 `env_probe.verify_cuda()`——必须跑一次真实推理。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 句柄保活容器。模块级引用，进程存活期间不会被回收。
_DLL_DIR_HANDLES: list = []


def add_nvidia_dll_dirs() -> list[str]:
    """把 site-packages/nvidia/<pkg>/bin 加入 DLL 搜索路径，返回加入的目录列表。

    非 Windows 或未安装 nvidia-* 包时返回空列表（不抛异常）。
    """
    added: list[str] = []
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return added

    try:
        import nvidia
    except ImportError:
        return added

    # nvidia 是命名空间包：没有 __file__，只有 __path__
    paths = list(getattr(nvidia, "__path__", []) or [])
    if not paths:
        return added

    base = Path(paths[0])
    if not base.is_dir():
        return added

    for pkg in sorted(base.iterdir()):
        bin_dir = pkg / "bin"
        if not bin_dir.is_dir():
            continue
        try:
            handle = os.add_dll_directory(str(bin_dir))
        except OSError:
            # 目录已加入或权限不足，跳过而不是让整个流程挂掉
            continue
        _DLL_DIR_HANDLES.append(handle)  # ← 不能省：句柄被 GC 则目录失效
        added.append(str(bin_dir))

    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")

    return added


#: 导入本模块即完成注入（副作用即目的）。
NVIDIA_DLL_DIRS: list[str] = add_nvidia_dll_dirs()
