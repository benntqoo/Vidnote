"""SQLite 任务存储——断点续跑的事实来源（PLAN.md §6）。

表结构、状态机与重置规则严格照 PLAN.md §6。`url` 上的 UNIQUE 约束就是去重机制：
重复添加同一链接会被识别为「已处理过」而不是新建。

并发说明：阶段 1 为单线程使用，连接不做线程隔离。阶段 2 引入调度器时
需改为「每 worker 一个连接」或加锁——sqlite3 连接默认不能跨线程共享。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models.task import Task, TaskStatus, now_iso

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
    """任务表的最小读写封装。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---- 生命周期 ----

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- 写 ----

    def add(self, task: Task) -> tuple[Task, bool]:
        """插入任务。url 已存在时返回既有记录且 `created=False`（去重语义）。"""
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
        cur = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def reset_interrupted(self) -> list[Task]:
        """把中途态重置为上一稳定态。**进程启动时必须先调用一次**。

        这些状态可能是进程被杀时留下的，直接信任会导致「假装已完成」。
        """
        changed: list[Task] = []
        for task in self.list():
            if task.reset_interrupted() is not None:
                self.update(task)
                changed.append(task)
        return changed

    # ---- 读 ----

    def get(self, task_id: int) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.from_row(dict(row)) if row else None

    def get_by_url(self, url: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE url = ?", (url,)).fetchone()
        return Task.from_row(dict(row)) if row else None

    def list(self, statuses: list[str] | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks"
        params: list = []
        if statuses:
            sql += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            params = list(statuses)
        sql += " ORDER BY id"
        rows = self._conn.execute(sql, params).fetchall()
        return [Task.from_row(dict(r)) for r in rows]

    def pending(self) -> list[Task]:
        """待处理任务：排除 done（失败项是否重试由调用方决定）。"""
        return self.list([TaskStatus.PENDING.value, TaskStatus.FAILED.value])

    def count_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
