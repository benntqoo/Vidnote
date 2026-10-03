// 任务表格。列顺序与 `PLAN §7.2` 的布局图一致：
// 序号 │ 标题 │ 时长 │ 状态 │ 进度 │ 耗时 │ 备注

import {
  formatDuration,
  formatProgress,
  SETTLED_STATUSES,
  STATUS_META,
  type TaskSnapshot,
  type TaskStatus,
} from "../worker/types";

interface Props {
  tasks: TaskSnapshot[];
  onCancel: (id: number) => void;
  onRetry: (id: number) => void;
  onRemove: (id: number) => void;
  onOpenDir: (path: string) => void;
}

/** 进度单元。**`progress < 0` 是「不可知」，渲染不确定态而不是 0%**（规格 §5）。 */
function ProgressCell({ progress, status }: { progress: number; status: TaskStatus }) {
  const text = formatProgress(progress);
  const isActive = !SETTLED_STATUSES.has(status);
  if (text === null) {
    return isActive ? (
      <div className="progress indeterminate" title="进度不可知">
        <div className="bar" />
      </div>
    ) : (
      <span className="muted">--</span>
    );
  }
  return (
    <div className="progress" title={text}>
      <div
        className="bar"
        style={{
          width: text,
          background: status === "done" ? "var(--ok)" : "var(--accent)",
        }}
      />
      <span className="progress-text">{text}</span>
    </div>
  );
}

function rowActions(t: TaskSnapshot, p: Props) {
  const settled = SETTLED_STATUSES.has(t.status);
  return (
    <div className="row-actions">
      {t.status === "failed" && (
        <button className="mini" onClick={() => p.onRetry(t.task_id)} title="重新入队">
          重试
        </button>
      )}
      {!settled && (
        <button
          className="mini"
          onClick={() => p.onCancel(t.task_id)}
          title="协作式取消：转写会在当前 segment 结束后中断"
        >
          取消
        </button>
      )}
      {t.status === "pending" && (
        <button className="mini danger" onClick={() => p.onRemove(t.task_id)} title="从队列移除">
          移除
        </button>
      )}
      {t.out_dir && (
        <button className="mini" onClick={() => p.onOpenDir(t.out_dir)} title={t.out_dir}>
          目录
        </button>
      )}
    </div>
  );
}

export function TaskTable(props: Props) {
  const { tasks } = props;

  if (tasks.length === 0) {
    return (
      <div className="empty">
        <p>还没有任务。</p>
        <p className="muted">
          点左上角「添加链接」，可直接粘贴抖音分享文案（含干扰码也能抽出 URL），一次多条。
        </p>
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table className="tasks">
        <thead>
          <tr>
            <th className="col-id">#</th>
            <th className="col-title">标题</th>
            <th className="col-dur">时长</th>
            <th className="col-status">状态</th>
            <th className="col-progress">进度</th>
            <th className="col-elapsed">耗时</th>
            <th className="col-note">备注</th>
            <th className="col-actions" />
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => {
            const meta = STATUS_META[t.status] ?? { label: t.status, tone: "muted" as const };
            return (
              <tr key={t.task_id} className={`row-${meta.tone}`}>
                <td className="col-id">{t.task_id}</td>
                <td className="col-title" title={t.title || t.url}>
                  {t.title || <span className="muted">{t.url}</span>}
                </td>
                <td className="col-dur">{formatDuration(t.duration_sec)}</td>
                <td className="col-status">
                  <span className={`chip chip-${meta.tone}`} title={t.stage}>
                    {meta.label}
                  </span>
                </td>
                <td className="col-progress">
                  <ProgressCell progress={t.progress} status={t.status} />
                </td>
                <td className="col-elapsed">{formatDuration(t.elapsed_sec)}</td>
                <td className="col-note" title={t.error ?? ""}>
                  {t.error ? (
                    <span className="err-text">{t.error}</span>
                  ) : t.retry_count > 0 ? (
                    <span className="muted">重试 {t.retry_count} 次</span>
                  ) : null}
                </td>
                <td className="col-actions">{rowActions(t, props)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
