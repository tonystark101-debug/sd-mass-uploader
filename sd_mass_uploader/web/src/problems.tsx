// Problem cards: what happened, why, numbered steps, one fix button, and "Ask AI".
import {
  Bot, HardDrive, Info, OctagonAlert, TriangleAlert, Wrench, XCircle,
} from "lucide-react";
import { useState } from "react";
import { api } from "./api";
import { since } from "./format";
import type { Problem } from "./types";
import { Button, Empty, useAction } from "./ui";

const SEVERITY = {
  critical: { icon: OctagonAlert, label: "Critical: uploads stopped" },
  error: { icon: XCircle, label: "Needs a person" },
  warning: { icon: TriangleAlert, label: "Warning" },
  info: { icon: Info, label: "For information" },
};

export interface ProblemHandlers {
  openCard: (cardId: string) => void;
  askAi: (problemId: string) => void;
}

/** The station command behind each fix button. */
async function runFix(problem: Problem, handlers: ProblemHandlers): Promise<string | null> {
  const action = problem.action;
  if (!action) return null;
  const card = encodeURIComponent(action.card_id ?? "");
  switch (action.kind) {
    case "retry_card":
      await api("POST", `/api/v1/cards/${card}/retry`);
      return "Queued again.";
    case "confirm_card":
      await api("POST", `/api/v1/cards/${card}/confirm`);
      return "Confirmed. Scanning the card.";
    case "resume_card":
      await api("POST", `/api/v1/cards/${card}/resume`);
      return "Resumed.";
    case "resume_engine":
      await api("POST", "/api/v1/engine/resume");
      return "Uploads will restart in a few seconds.";
    case "retry_reports":
      await api("POST", "/api/v1/destination/retry-reports");
      return "Trying again now.";
    case "place_card":
      if (action.card_id) handlers.openCard(action.card_id);
      return null;
    default:
      return null;
  }
}

export function ProblemCard({ problem, handlers, compact }: {
  problem: Problem; handlers: ProblemHandlers; compact?: boolean;
}) {
  const { run, busy } = useAction();
  const [open, setOpen] = useState(!compact);
  const severity = SEVERITY[problem.severity];
  const Icon = severity.icon;
  return (
    <article className={`problem sev-${problem.severity}`}>
      <header className="problem-head">
        <Icon size={20} aria-hidden="true" />
        <div className="problem-title">
          <h3>{problem.title}</h3>
          <span className="muted small">
            {severity.label} · {problem.code}{problem.opened_at ? ` · ${since(problem.opened_at)}` : ""}
          </span>
        </div>
      </header>
      <p>{problem.message}</p>
      {open ? (
        <ol className="steps">
          {problem.steps.map((step) => <li key={step}>{step}</li>)}
        </ol>
      ) : (
        <button type="button" className="link" onClick={() => setOpen(true)}>
          Show what to do
        </button>
      )}
      <div className="problem-actions">
        {problem.action && (
          <Button icon={<Wrench size={16} />} kind="primary" busy={busy === "fix"}
                  onClick={() => run("fix", () => runFix(problem, handlers))}>
            {problem.action.label}
          </Button>
        )}
        {problem.card_id && problem.action?.kind !== "place_card" && (
          <Button icon={<HardDrive size={16} />} kind="ghost" onClick={() => handlers.openCard(problem.card_id!)}>
            Open card
          </Button>
        )}
        <Button kind="ghost" icon={<Bot size={16} aria-hidden="true" />}
                onClick={() => handlers.askAi(problem.id)}>
          Ask AI
        </Button>
      </div>
      {problem.detail && open && (
        <details className="technical">
          <summary>Technical details</summary>
          <code>{problem.detail}</code>
        </details>
      )}
    </article>
  );
}

export function ProblemList({ problems, handlers, compact, limit }: {
  problems: Problem[]; handlers: ProblemHandlers; compact?: boolean; limit?: number;
}) {
  if (!problems.length) {
    return <Empty title="No problems">Everything the station can check looks fine.</Empty>;
  }
  const shown = limit ? problems.slice(0, limit) : problems;
  return (
    <div className="problem-list">
      {shown.map((problem) => (
        <ProblemCard key={problem.id} problem={problem} handlers={handlers} compact={compact} />
      ))}
      {limit && problems.length > limit && (
        <p className="muted small">{problems.length - limit} more on the Problems page.</p>
      )}
    </div>
  );
}
