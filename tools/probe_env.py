"""Probe local hardware capacity and derive batch-processing limits.

This is the prototype for the tool's `env_probe` module: it answers
"how much can this machine actually do in parallel?"

Outputs JSON on stdout.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def cpu_info() -> dict:
    info: dict = {"logical": os.cpu_count() or 0}
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        try:
            info["name"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            info["name"] = None
        for reg_name, out_name in (("NumberOfCores", "physical"), ("~MHz", "mhz")):
            try:
                info[out_name] = winreg.QueryValueEx(key, reg_name)[0]
            except OSError:
                info[out_name] = None
        winreg.CloseKey(key)
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
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


def mem_info() -> dict:
    try:
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return {
            "total_gb": round(stat.ullTotalPhys / 1024**3, 1),
            "avail_gb": round(stat.ullAvailPhys / 1024**3, 1),
            "load_pct": stat.dwMemoryLoad,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def disk_info() -> dict:
    out = {}
    for drive in ("C:\\", "D:\\"):
        try:
            usage = shutil.disk_usage(drive)
            out[drive[0]] = {
                "total_gb": round(usage.total / 1024**3, 1),
                "free_gb": round(usage.free / 1024**3, 1),
            }
        except OSError:
            continue
    return out


def gpu_info() -> list[dict]:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,driver_version,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return []
        gpus = []
        for line in proc.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append(
                    {
                        "name": parts[0],
                        "vram_total_mb": int(float(parts[1])),
                        "vram_used_mb": int(float(parts[2])),
                        "driver": parts[3],
                        "compute_cap": parts[4],
                    }
                )
        return gpus
    except Exception:  # noqa: BLE001
        return []


def cuda_runtime_available() -> dict:
    """Check whether ctranslate2 can actually use CUDA (needs cuBLAS + cuDNN 9)."""
    try:
        import ctranslate2

        return {
            "ctranslate2": ctranslate2.__version__,
            "cuda_device_count": ctranslate2.get_cuda_device_count(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    report = {
        "cpu": cpu_info(),
        "memory": mem_info(),
        "disk": disk_info(),
        "gpu": gpu_info(),
        "cuda": cuda_runtime_available(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
