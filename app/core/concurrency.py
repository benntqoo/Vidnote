"""并发度计算（PLAN.md §3.2）。

**为什么必须运行时算**：本机可用内存在同日 1 小时内测得 12.6 ~ 26.2 GB，
差整整一倍（PLAN.md §2）。任何写死的并发数都会在内存紧张时炸掉。

**为什么转写恒为 1（GPU 模式下）**：单卡串行是吞吐最优解——显存够塞 4 个实例，
但 GPU 算力分时复用，多实例只会互相争抢带宽（PLAN.md §3.1）。

本模块是纯函数，无副作用，便于单测。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.env_probe import EnvReport
from app.models.config import Config


@dataclass(frozen=True)
class Concurrency:
    """各阶段并发数。字段名与 IPC 协议的 `concurrency` 事件一致。"""

    n_asr: int
    """转写并发。GPU 模式下恒为 1。"""

    n_dl: int
    """下载并发。网络密集，可高并发。"""

    n_frame: int
    """抽帧并发。CPU 密集但单任务短。"""

    n_sum: int
    """汇总并发。纯网络等待。"""

    def to_dict(self) -> dict[str, int]:
        return {
            "n_asr": self.n_asr,
            "n_dl": self.n_dl,
            "n_frame": self.n_frame,
            "n_sum": self.n_sum,
        }


#: 内存读取失败时的保守回退值（PLAN.md §4.1 接口契约）。
CONSERVATIVE = Concurrency(n_asr=1, n_dl=2, n_frame=1, n_sum=1)

#: 常驻开销（解释器 / GUI / 系统空闲），单位 GB。
RESIDENT_GB = 2.0
#: 各阶段的单实例内存预估，单位 GB。
MEM_PER_ASR_GPU = 1.5
MEM_PER_ASR_CPU = 3.0
MEM_PER_DL = 0.5
MEM_PER_FRAME = 0.6
#: 允许占用的可用内存上限比例。
BUDGET_RATIO = 0.8


def plan(
    cfg: Config,
    env: EnvReport,
    gpu_usable: bool | None = None,
) -> Concurrency:
    """按实测内存与 GPU 可用性推算并发度。

    `gpu_usable` 未显式给出时取 `env.cuda.usable`——注意 `usable` 是**三态**：
    `None` 表示尚未真实验证（只有快速探测结果），此时按**不可用**保守处理。

    调用方若想先用快速判据（`device_count > 0`）规划、等真实推理验证回来再纠正，
    显式传入即可——`rpc.py` 就是这么做的，见 `docs/IPC协议规格.md` §4.2。
    """
    mem = env.memory.avail_gb
    if mem is None or mem <= 0:
        return CONSERVATIVE

    if gpu_usable is None:
        gpu_usable = bool(env.cuda.usable)

    if not cfg.runtime.auto_concurrency:
        # 用户显式指定并发上限，只对转写做安全钳制
        return Concurrency(
            n_asr=1 if gpu_usable else max(1, min(2, int(mem // MEM_PER_ASR_CPU))),
            n_dl=max(1, cfg.runtime.max_download),
            n_frame=max(1, cfg.runtime.max_frame),
            n_sum=max(1, cfg.runtime.max_summarize),
        )

    # ① 转写：GPU 可用则强制 1（单卡串行是吞吐最优解）
    if gpu_usable:
        n_asr, mem_per_asr = 1, MEM_PER_ASR_GPU
    else:
        n_asr = max(1, min(2, int(mem // MEM_PER_ASR_CPU)))
        mem_per_asr = MEM_PER_ASR_CPU

    # ② 下载：内存占用小，但受总量与带宽约束，硬上限 8
    n_dl = max(1, min(cfg.runtime.max_download, int(mem // MEM_PER_DL), 8))

    # ③ 抽帧：CPU 密集且单任务短；32 逻辑核 → 4
    n_frame = max(1, min(cfg.runtime.max_frame, (env.cpu.logical or 4) // 8))

    # ④ 汇总：纯网络等待，可放宽
    n_sum = max(1, min(cfg.runtime.max_summarize, 4))

    # ⑤ 总内存预算校验，超了就按比例回退下载与抽帧
    budget = n_asr * mem_per_asr + n_dl * MEM_PER_DL + n_frame * MEM_PER_FRAME + RESIDENT_GB
    limit = mem * BUDGET_RATIO
    if budget > limit:
        scale = limit / budget
        n_dl = max(1, int(n_dl * scale))
        n_frame = max(1, int(n_frame * scale))

    return Concurrency(n_asr=n_asr, n_dl=n_dl, n_frame=n_frame, n_sum=n_sum)
