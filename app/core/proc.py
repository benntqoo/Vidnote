"""子进程回收工具。

抽出来是因为 `fetcher` / `audio` / `frames` 三个模块都要做同一件事：取消任务时
把正在跑的外部进程（yt-dlp / ffmpeg）干净地弄死并回收。写三遍必然写出三个略有
差异的版本——而这类差异往往只在「取消」这种低频路径上暴露，最不容易被测到。

Windows 注意：必须用 `kill()`（TerminateProcess）。`terminate()` 对 console 程序
只发 WM_CLOSE，ffmpeg 与 yt-dlp 通常直接忽略，进程会继续跑完——表现就是
「点了取消但 CPU 还在转」。
"""

from __future__ import annotations

import logging
import subprocess

#: 等待被 kill 的进程退出的上限。超时不抛异常，只记日志。
KILL_TIMEOUT_SEC = 10.0


def kill_process(proc: "subprocess.Popen[str] | None") -> None:
    """强杀进程并回收。**本函数不抛异常**——它常用在 `except` 块里，
    一次清理失败不该盖掉原始异常。
    """
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=KILL_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        logging.warning("回收子进程失败（pid=%s）：%s", getattr(proc, "pid", "?"), exc)
