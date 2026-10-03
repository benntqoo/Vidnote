// 日志区。转发 Python 的 `logging`（经 stdin/stdout 协议）与 worker stderr 尾部。

import { useEffect, useRef } from "react";

export interface LogEntry {
  key: number;
  level: "debug" | "info" | "warning" | "error";
  msg: string;
  /** `stderr` 表示来自 Python 的直接 stderr 输出（带 traceback 的那种） */
  source?: string;
}

interface Props {
  entries: LogEntry[];
  onClear: () => void;
}

const LEVEL_ORDER: Record<string, number> = { debug: 0, info: 1, warning: 2, error: 3 };

export function LogPanel({ entries, onClear }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  // 只在用户本来就贴着底部时才自动滚动 —— 否则他往上翻查日志会被不断拽回去
  useEffect(() => {
    const el = boxRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [entries]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  return (
    <div className="logpanel">
      <div className="logpanel-head">
        <span>日志</span>
        <span className="muted">{entries.length} 条</span>
        <span className="spacer" />
        <button className="mini" onClick={onClear}>
          清空
        </button>
      </div>
      <div className="logpanel-body" ref={boxRef} onScroll={onScroll}>
        {entries.length === 0 ? (
          <div className="muted">暂无日志</div>
        ) : (
          entries.map((e) => (
            <div key={e.key} className={`logline log-${e.level}`}>
              <span className="log-level">{e.level}</span>
              {e.source === "stderr" && <span className="log-src">stderr</span>}
              <span className="log-msg">{e.msg}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export function levelRank(level: string): number {
  return LEVEL_ORDER[level] ?? 1;
}
