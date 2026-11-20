// Small shared pieces: status badge, buttons, side panel, notices.
import {
  AlertTriangle, Check, CircleHelp, Clock, Eject, Loader2, Pause, ScanLine, Upload, X,
} from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiError } from "./api";
import { look, type StateLook, type Tone } from "./format";
import type { CardState } from "./types";

const ICONS: Record<StateLook["icon"], typeof Check> = {
  upload: Upload, wait: Clock, pause: Pause, check: Check, alert: AlertTriangle, x: X,
  eject: Eject, help: CircleHelp, scan: ScanLine,
};

export function StatusBadge({ state, extra }: { state: CardState; extra?: string }) {
  const item = look(state);
  const Icon = ICONS[item.icon];
  return (
    <span className={`badge tone-${item.tone}`}>
      <Icon size={14} aria-hidden="true" />
      {item.word}{extra ? ` ${extra}` : ""}
    </span>
  );
}

export function Pill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`badge tone-${tone}`}>{children}</span>;
}

export function Button({ children, onClick, kind = "secondary", disabled, busy, title,
                         icon }: {
  children: ReactNode; onClick?: () => void; kind?: "primary" | "secondary" | "danger" | "ghost";
  disabled?: boolean; busy?: boolean; title?: string; icon?: ReactNode;
}) {
  return (
    <button type="button" className={`btn btn-${kind}`} onClick={onClick}
            disabled={disabled || busy} title={title}>
      {busy ? <Loader2 size={16} className="spin" aria-hidden="true" /> : icon}
      <span>{children}</span>
    </button>
  );
}

export function Panel({ title, onClose, children, wide }: {
  title: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="overlay" role="presentation" onClick={onClose}>
      <aside className={`drawer${wide ? " drawer-wide" : ""}`} role="dialog" aria-modal="true"
             aria-label={typeof title === "string" ? title : undefined}
             onClick={(event) => event.stopPropagation()}>
        <header className="drawer-head">
          <h2>{title}</h2>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={20} />
          </button>
        </header>
        <div className="drawer-body">{children}</div>
      </aside>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children && <div className="muted">{children}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ notices

interface Notice { id: number; text: string; tone: "good" | "bad" }
const NoticeContext = createContext<(text: string, tone?: Notice["tone"]) => void>(() => {});

export function NoticeProvider({ children }: { children: ReactNode }) {
  const [notices, setNotices] = useState<Notice[]>([]);
  const show = useCallback((text: string, tone: Notice["tone"] = "good") => {
    const id = Date.now() + Math.random();
    setNotices((list) => [...list, { id, text, tone }]);
    window.setTimeout(() => setNotices((list) => list.filter((n) => n.id !== id)),
                      tone === "bad" ? 9000 : 4000);
  }, []);
  return (
    <NoticeContext.Provider value={show}>
      {children}
      <div className="notices" role="status" aria-live="polite">
        {notices.map((notice) => (
          <div key={notice.id} className={`notice notice-${notice.tone}`}>{notice.text}</div>
        ))}
      </div>
    </NoticeContext.Provider>
  );
}

/** Run a station command, show what happened, and track that it is in progress. */
export function useAction() {
  const notify = useContext(NoticeContext);
  const [busy, setBusy] = useState<string | null>(null);
  const run = useCallback(async (key: string, action: () => Promise<unknown>,
                                 success?: string) => {
    setBusy(key);
    try {
      const result = await action();
      if (success) notify(success);
      else if (typeof result === "string" && result) notify(result);
      return result;
    } catch (error) {
      notify(error instanceof ApiError ? error.message : "Something went wrong.", "bad");
      return undefined;
    } finally {
      setBusy(null);
    }
  }, [notify]);
  return { run, busy };
}
