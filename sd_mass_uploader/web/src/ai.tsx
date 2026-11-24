// The AI troubleshooter: a conversation, with an approval step for every command it wants to run.
import {
  Bot, Check, Send, ShieldCheck, Square, X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { clock } from "./format";
import type { AiEntry, AiSession } from "./types";
import { Button, Panel, useAction } from "./ui";

export function AiPanel({ start, onClose }: {
  start: { problemId?: string; question?: string } | null; onClose: () => void;
}) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [session, setSession] = useState<AiSession | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { run, busy } = useAction();
  const started = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);

  const begin = useCallback(async (question?: string) => {
    try {
      const result = await api<{ ai_session_id: string }>("POST", "/api/v1/ai/sessions", {
        problem_id: start?.problemId ?? null, question: question ?? start?.question ?? null,
        operator: "console",
      });
      setSessionId(result.ai_session_id);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "Could not start the AI.");
    }
  }, [start]);

  useEffect(() => {
    if (start?.problemId && !started.current) {
      started.current = true;
      void begin();
    }
  }, [start, begin]);

  useEffect(() => {
    if (!sessionId) return undefined;
    const load = () => api<AiSession>("GET", `/api/v1/ai/sessions/${sessionId}`)
      .then(setSession).catch(() => undefined);
    load();
    const timer = window.setInterval(load, 1000);
    return () => window.clearInterval(timer);
  }, [sessionId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [session?.transcript.length, session?.pending_approval]);

  const working = session && ["running", "waiting_approval"].includes(session.status);
  const send = () => {
    const message = text.trim();
    if (!message) return;
    setText("");
    if (!sessionId) {
      void begin(message);
    } else {
      void run("send", () => api("POST", `/api/v1/ai/sessions/${sessionId}/messages`,
                                 { text: message, operator: "console" }));
    }
  };
  const decide = (approve: boolean) => {
    const pending = session?.pending_approval;
    if (!pending || !sessionId) return;
    void run("decide", () => api("POST",
      `/api/v1/ai/sessions/${sessionId}/approvals/${pending.approval_id}`,
      { approve, operator: "console" }), approve ? "Allowed." : "Declined.");
  };

  return (
    <Panel title={<span className="with-icon"><Bot size={20} /> AI troubleshooter</span>}
           onClose={onClose} wide>
      <p className="muted small with-icon">
        <ShieldCheck size={16} aria-hidden="true" />
        It checks the station by itself. Every command it wants to run waits for you. It can
        never change a card or read keys.
      </p>
      {error && <p className="error-text">{error}</p>}
      <div className="transcript" aria-live="polite">
        {!sessionId && !error && (
          <p className="muted">Ask about a problem, for example: "Why are uploads not
            running?"</p>
        )}
        {session?.transcript.map((entry) => <Entry key={entry.seq} entry={entry} />)}
        {session?.pending_approval && (
          <div className="approval">
            <h3>The AI wants to run this</h3>
            <code className="command">{session.pending_approval.summary}</code>
            <p><strong>Why:</strong> {session.pending_approval.reason}</p>
            <div className="actions-row">
              <Button icon={<Check size={16} />} kind="primary" busy={busy === "decide"} onClick={() => decide(true)}>
                Allow once
              </Button>
              <Button icon={<X size={16} />} kind="secondary" onClick={() => decide(false)}>Decline</Button>
            </div>
          </div>
        )}
        {working && !session?.pending_approval && <p className="muted">Working…</p>}
        <div ref={bottom} />
      </div>
      <div className="composer">
        <textarea value={text} rows={2} placeholder="Ask the AI or answer it…"
                  onChange={(event) => setText(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      send();
                    }
                  }}
                  disabled={Boolean(working)} aria-label="Message to the AI" />
        <Button kind="primary" icon={<Send size={16} />} onClick={send}
                disabled={Boolean(working) || !text.trim()}>Send</Button>
        {working && sessionId && (
          <Button kind="danger" icon={<Square size={14} />}
                  onClick={() => run("stop", () => api("POST",
                    `/api/v1/ai/sessions/${sessionId}/stop`), "Stopping.")}>Stop</Button>
        )}
      </div>
      {session && (
        <p className="muted small">{session.tokens_used.toLocaleString()} tokens used ·
          status {session.status.replace("_", " ")}</p>
      )}
    </Panel>
  );
}

function Entry({ entry }: { entry: AiEntry }) {
  if (entry.kind === "approval" && entry.verdict === "pending") return null;
  const time = <span className="muted small">{clock(entry.at)}</span>;
  switch (entry.kind) {
    case "operator":
      return <div className="msg msg-you"><strong>You</strong> {time}<p>{entry.text}</p></div>;
    case "assistant":
      return <div className="msg msg-ai"><strong>AI</strong> {time}<p>{entry.text}</p></div>;
    case "tool_call":
      return <div className="msg msg-tool">› {entry.text}</div>;
    case "tool_result":
      return (
        <details className={`msg msg-result${entry.is_error ? " is-error" : ""}`}>
          <summary>{entry.is_error ? "Refused or failed" : "Result"}
            {entry.verdict && entry.verdict !== "auto" ? ` (${entry.verdict})` : ""}</summary>
          <pre>{entry.text}</pre>
        </details>
      );
    default:
      return <div className="msg msg-note">{entry.text}</div>;
  }
}
