"""SQLite 任务存储——断点续跑的事实来源（PLAN.md §6）。

表结构、状态机与重置规则严格照 PLAN.md §6。`url` 上的 UNIQUE 约束就是去重机制：
重复添加同一链接会被识别为「已处理过」而不是新建。

**线程安全（阶段 2 起必需）**：`dispatcher` 的各个线程池都要读写状态。这里选了
「单连接 + 进程内可重入锁」而不是「每 worker 一个连接」：

- 状态写入是**低频**操作——一条任务从 pending 到 done 只写 4~6 次，而它要占用
  约 150 秒（2 分 18 秒转写 + 抽音频）。锁竞争在总耗时里不可测量。
- 每 worker 一连接需要配 WAL + 各连接独立事务，反而引入「A 连接看不到 B 连接
  刚写的状态」这类只在竞态下出现的问题。为了一个不存在的性能问题付这些复杂度，
  是负收益。
- 连接用 `check_same_thread=False`，由本模块的锁保证串行访问——锁在模块内部，
  调用方不需要知道线程模型。

若将来状态写入变成高频（例如每段转写都落库），再改 WAL + 连接池。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from app.models.task import TERMINAL_STATUSES, Task, TaskStatus, now_iso

#: 与 Task 的 dataclass 字段一致，id 由数据库自增，不参与写入。
COLUMNS = (
    "url",
    "platform",
    "video_id",
    "title",
    "duration_sec",
    "status",
    "stage_error",
    "retry_count",
    "out_dir",
    "video_path",
    "audio_path",
    "transcript_json",
    "created_at",
    "updated_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  url             TEXT    NOT NULL UNIQUE,
  platform        TEXT,
  video_id        TEXT,
  title           TEXT,
  duration_sec    INTEGER,
  status          TEXT    NOT NULL,
  stage_error     TEXT,
  retry_count     INTEGER DEFAULT 0,
  out_dir         TEXT,
  video_path      TEXT,
  audio_path      TEXT,
  transcript_json TEXT,
  created_at      TEXT,
  updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


class Store:
    """任务表的最小读写封装。线程安全（内部可重入锁）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False + 内部锁：见模块 docstring 的取舍说明
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- 生命周期 ----

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- 写 ----

    def add(self, task: Task) -> tuple[Task, bool]:
        """插入任务。url 已存在时返回既有记录且 `created=False`（去重语义）。

        「先查后插」在锁内完成，否则两个线程可能同时通过 `get_by_url` 检查、
        其中一个 INSERT 撞上 UNIQUE 约束抛异常——那会把去重语义变成偶发崩溃。
        """
        with self._lock:
            existing = self.get_by_url(task.url)
            if existing is not None:
                return existing, False

            placeholders = ", ".join(f":{c}" for c in COLUMNS)
            cur = self._conn.execute(
                f"INSERT INTO tasks ({', '.join(COLUMNS)}) VALUES ({placeholders})",
                {c: getattr(task, c) for c in COLUMNS},
            )
            self._conn.commit()
            task.id = cur.lastrowid
            return task, True

    def update(self, task: Task) -> None:
        if task.id is None:
            raise ValueError("update() 需要 task.id——先用 add() 插入")
        task.updated_at = now_iso()
        assignments = ", ".join(f"{c} = :{c}" for c in COLUMNS)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id = :id",
                {**{c: getattr(task, c) for c in COLUMNS}, "id": task.id},
            )
            self._conn.commit()

    def set_status(self, task: Task, status: TaskStatus | str, error: str | None = None) -> None:
        """只改状态，避免调用方漏掉 update()。"""
        task.set_status(status, error)
        self.update(task)

    def delete(self, task_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def reset_interrupted(self) -> list[Task]:
        """把中途态重置为上一稳定态。**进程启动时必须先调用一次**。

        这些状态可能是进程被杀时留下的，直接信任会导致「假装已完成」。
        """
        changed: list[Task] = []
        with self._lock:
            for task in self.list():
                if task.reset_interrupted() is not None:
                    self.update(task)
                    changed.append(task)
        return changed

    # ---- 读 ----

    def get(self, task_id: int) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return Task.from_row(dict(row)) if row else None

    def get_by_url(self, url: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE url = ?", (url,)
            ).fetchone()
        return Task.from_row(dict(row)) if row else None

    def list(self, statuses: list[str] | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks"
        params: list = []
        if statuses:
            sql += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            params = list(statuses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Task.from_row(dict(r)) for r in rows]

    def pending(self) -> list[Task]:
        """待处理任务：**一切非终态**（`done` / `cancelled` 除外）。

        不能只取 `pending` + `failed`：`downloaded` / `transcribed` 这些稳定中间态
        同样需要继续推进——断点续跑的语义就是「从这里接着跑」。漏掉它们会让任务
        被永久搁置（进程中止时停在 `q_asr` 里的那几条就属于这一类）。

        中途态（`downloading` / `transcribing` / `framing`）理论上也在返回结果里，
        但进程启动时 `reset_interrupted()` 已把它们回退到上一稳定态，所以正常情况
        见不到——留着这个口子是为了「调用方忘了 reset 也不至于漏任务」。
        """
        terminal = {s.value for s in TERMINAL_STATUSES}
        return [task for task in self.list() if task.status not in terminal]

    def resumable(self) -> list[Task]:
        """可续跑的任务：非终态 **且非 failed**。

        与 `pending()` 的差别就是失败项——它需要用户显式重试。否则一条永久失效的
        链接会在每次点「开始」时被自动重试一遍，看起来像程序在空转。
        """
        return [task for task in self.pending() if task.status != TaskStatus.FAILED.value]

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}
