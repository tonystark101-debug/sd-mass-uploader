// The Operator Console: status at a glance, then the card grid, then detail on demand.
import {
  ArrowLeft, Bot, CircleCheck, CircleHelp, CirclePause, CircleX, Clock, Cloud, CloudOff,
  CloudUpload, Cpu, Eject, Eye, Gauge, HardDrive, HardDriveUpload, History, LayoutDashboard,
  Package, Pause, Play, Settings, Square, TriangleAlert, Wifi, WifiOff, X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { AiPanel } from "./ai";
import { api, useStation, type Connection } from "./api";
import { CardDrawer, CardTile } from "./cards";
import { bytes, clock, describeEvent, duration, look, percent } from "./format";
import { LinesPanel, speedText } from "./lines";
import { HardwarePage, HistoryPage, SetupPage } from "./pages";
import { ProblemList, type ProblemHandlers } from "./problems";
import { StartSession } from "./session";
import type { CardState, StationEvent, StationState } from "./types";
import { Button, Empty, Panel, useAction } from "./ui";

type Page = "dashboard" | "problems" | "history" | "hardware" | "setup";

const FILTERS: { key: string; label: string; states: CardState[]; tone: string;
                 icon: typeof Cpu }[] = [
  { key: "all", label: "Inserted", states: [], tone: "gray", icon: HardDrive },
  { key: "uploading", label: "Uploading", states: ["UPLOADING", "PAUSING"], tone: "blue",
    icon: CloudUpload },
  { key: "waiting", label: "Waiting", states: ["QUEUED", "READY", "SCANNING"], tone: "gray",
    icon: Clock },
  { key: "uploaded", label: "Uploaded", states: ["UPLOADED"], tone: "green", icon: CircleCheck },
  { key: "failed", label: "Failed", states: ["FAILED", "UPLOADED_WITH_ISSUES", "NEEDS_ATTENTION"],
    tone: "red", icon: CircleX },
  { key: "paused", label: "Paused", states: ["PAUSED"], tone: "amber", icon: CirclePause },
  { key: "removed", label: "Removed", states: ["REMOVED"], tone: "purple", icon: Eject },
  { key: "you", label: "Needs you", states: ["NEEDS_CONFIRMATION", "NEEDS_DECISION"],
    tone: "orange", icon: CircleHelp },
];

const PAGES = [
  ["dashboard", "Dashboard", LayoutDashboard],
  ["problems", "Problems", TriangleAlert],
  ["history", "History", History],
  ["hardware", "Hardware", Cpu],
  ["setup", "Setup", Settings],
] as const;

function count(state: StationState, key: string): number {
  const c = state.counters;
  switch (key) {
    case "all": return c.inserted;
    case "uploading": return c.uploading;
    case "waiting": return c.queued + c.ready + c.scanning;
    case "uploaded": return c.uploaded;
    case "failed": return c.failed;
    case "paused": return c.paused;
    case "removed": return c.removed;
    case "you": return c.needs_confirmation + c.needs_decision;
    default: return 0;
  }
}

export function App() {
  const { state, events, connection, lastUpdate } = useStation();
  const [page, setPage] = useState<Page>("dashboard");
  const [openCard, setOpenCard] = useState<string | null>(null);
  const [ai, setAi] = useState<{ problemId?: string } | null>(null);
  const handlers: ProblemHandlers = useMemo(() => ({
    openCard: (cardId) => setOpenCard(cardId),
    askAi: (problemId) => setAi({ problemId }),
  }), []);

  if (connection === "signed-out") return <SignedOut />;
  if (!state) return <Splash connection={connection} />;
  const stale = connection !== "live";
  const problems = state.problems;
  const urgent = problems.filter((p) => p.severity === "critical" || p.severity === "error");

  const title = page === "dashboard" ? (state.session ? "Dashboard" : "New upload")
    : PAGES.find(([key]) => key === page)![1];

  return (
    <div className={`shell${stale ? " is-stale" : ""}`}>
      <aside className="sidebar">
        <div className="logo">
          <span className="logo-mark" aria-hidden="true"><HardDriveUpload size={22} /></span>
          <div><strong>SD Uploader</strong><small>Upload station</small></div>
        </div>
        <nav className="nav" aria-label="Pages">
          {PAGES.map(([key, label, Icon]) => (
            <button key={key} type="button" className={`nav-item${page === key ? " active" : ""}`}
                    aria-current={page === key ? "page" : undefined} onClick={() => setPage(key)}>
              <Icon size={19} aria-hidden="true" />
              <span>{key === "dashboard" && !state.session ? "New upload" : label}</span>
              {key === "problems" && problems.length > 0 && (
                <span className={`nav-count${urgent.length ? " bad" : ""}`}>{problems.length}</span>
              )}
            </button>
          ))}
        </nav>
        <StationHealth state={state} connection={connection} lastUpdate={lastUpdate} />
      </aside>
      <div className="workspace">
        <Header state={state} title={title} onAi={() => setAi({})} />
        {stale && (
          <div className="banner banner-bad" role="alert">
            <WifiOff size={18} aria-hidden="true" />
            Not connected to the station. What you see may be out of date. Reconnecting…
          </div>
        )}
        <main className="main">
          {page === "dashboard" && (state.session
            ? <Dashboard state={state} events={events} handlers={handlers}
                         urgentCount={urgent.length} onOpenCard={setOpenCard}
                         onProblems={() => setPage("problems")} />
            : <StartSession state={state} />)}
          {page === "problems" && <ProblemList problems={problems} handlers={handlers} />}
          {page === "history" && <HistoryPage />}
          {page === "hardware" && <HardwarePage />}
          {page === "setup" && <SetupPage state={state} />}
        </main>
      </div>
      {openCard && <CardDrawer cardId={openCard} destination={state.destination}
                               onClose={() => setOpenCard(null)} />}
      {ai && <AiPanel start={ai} onClose={() => setAi(null)} />}
    </div>
  );
}

function StationHealth({ state, connection, lastUpdate }: {
  state: StationState; connection: Connection; lastUpdate: number;
}) {
  const live = connection === "live";
  const cloudBad = live && Boolean(state.engine.problem || state.engine.store_error);
  const net = state.network;
  return (
    <div className="health" aria-label="Station status">
      <div className={`health-row ${live ? "good" : "bad"}`}
           title={lastUpdate ? `Updated ${clock(new Date(lastUpdate).toISOString())}` : ""}>
        {live ? <Wifi size={16} /> : <WifiOff size={16} />}
        <span>Station</span><strong>{live ? "Live" : "Offline"}</strong>
      </div>
      <div className={`health-row ${cloudBad || !live ? "bad" : state.engine.running ? "good" : ""}`}>
        {cloudBad || !live ? <CloudOff size={16} /> : <Cloud size={16} />}
        <span>Cloud</span>
        <strong>{!live ? "Unknown" : cloudBad ? "Stopped" : state.engine.running ? "Uploading" : "Idle"}</strong>
      </div>
      {net?.enabled && (
        <div className={`health-row ${net.working > 0 ? "good" : "bad"}`}>
          <Gauge size={16} />
          <span>{net.working} {net.working === 1 ? "line" : "lines"}</span>
          <strong>{speedText(net.total_mbit_per_s)}</strong>
        </div>
      )}
    </div>
  );
}

function Header({ state, title, onAi }: { state: StationState; title: string; onAi: () => void }) {
  const meta = state.session?.metadata ?? {};
  const chips = state.session
    ? [meta.vendor_name ?? meta.operator_name, meta.activity ?? meta.activity_name,
       meta.city ?? meta.place_of_recording, meta.upload_date ?? meta.recording_date]
        .filter(Boolean)
    : [];
  const packet = state.destination.packet;
  return (
    <header className="topbar">
      <div className="topbar-title">
        <h1>{title}</h1>
        <div className="chips">
          {chips.length
            ? chips.map((chip) => <span key={String(chip)} className="chip">{String(chip)}</span>)
            : <span className="muted small">No upload running</span>}
          {packet && <span className="chip chip-blue"><Package size={13} aria-hidden="true" />
            {packet.ref}</span>}
        </div>
      </div>
      <Button kind="secondary" icon={<Bot size={17} />} onClick={onAi}>Ask AI</Button>
    </header>
  );
}

function Dashboard({ state, events, handlers, urgentCount, onOpenCard, onProblems }: {
  state: StationState; events: StationEvent[]; handlers: ProblemHandlers; urgentCount: number;
  onOpenCard: (id: string) => void; onProblems: () => void;
}) {
  const [filter, setFilter] = useState("all");
  const [confirmEnd, setConfirmEnd] = useState(false);
  const { run, busy } = useAction();
  const session = state.session!;
  const progress = state.progress;
  const active = FILTERS.find((item) => item.key === filter)!;
  const cards = [...state.cards]
    .filter((card) => (active.states.length ? active.states.includes(card.state)
                                            : card.present || filter !== "all"))
    .sort((a, b) => look(a.state).rank - look(b.state).rank
                    || a.inserted_at.localeCompare(b.inserted_at));
  const removedHidden = filter === "all" ? state.cards.filter((card) => !card.present).length : 0;
  const share = percent(progress.bytes_done ?? 0, progress.bytes_total ?? 0);

  return (
    <div className="dashboard">
      <section className="counters" aria-label="Card counts">
        {FILTERS.map((item) => (
          <button key={item.key} type="button"
                  className={`counter tone-${item.tone}${filter === item.key ? " selected" : ""}`}
                  onClick={() => setFilter(filter === item.key ? "all" : item.key)}
                  aria-pressed={filter === item.key}>
            <span className="counter-icon" aria-hidden="true"><item.icon size={18} /></span>
            <span className="counter-value">{count(state, item.key)}</span>
            <span className="counter-label">{item.label}</span>
          </button>
        ))}
      </section>

      {state.counters.needs_decision > 0 && (
        <div className="alert alert-warn" role="status">
          <CircleHelp size={22} aria-hidden="true" />
          <div className="grow">
            <strong>{state.counters.needs_decision} card(s) need your answer</strong>
            <p className="small">The station could not tell which device recorded them, or it
              differs from the packet. Open the card and choose the device.</p>
          </div>
          <Button icon={<Eye size={16} />} kind="primary" onClick={() => setFilter("you")}>Show them</Button>
        </div>
      )}

      <section className="overall" aria-label="Overall progress">
        <div className="bar big" role="progressbar" aria-valuenow={share} aria-valuemin={0}
             aria-valuemax={100}>
          <div className="bar-fill fill-blue" style={{ width: `${share}%` }} />
        </div>
        <div className="overall-text">
          <strong>{share}%</strong>
          <span>{bytes(progress.bytes_done)} of {bytes(progress.bytes_total)}</span>
          <span>{progress.files_done ?? 0} of {progress.files_total ?? 0} files</span>
          <span>{progress.current_mb_per_s ?? 0} MB/s now</span>
          {state.network?.enabled && state.network.working > 0 && (
            <span title="Internet upload speed over every line">
              {speedText(state.network.total_mbit_per_s)} on {state.network.working}
              {state.network.working === 1 ? " line" : " lines"}</span>
          )}
          <span>{progress.eta_seconds ? `about ${duration(progress.eta_seconds)} left`
                                      : "time left unknown"}</span>
          {state.destination.packet && (
            <span className="badge tone-blue" title="The vendor platform packet these cards came in">
              Packet {state.destination.packet.ref} · {state.destination.packet.packet.vendor}
              {state.destination.packet.uploaded_at ? " · reported uploaded" : ""}
            </span>
          )}
          {session.paused_all && <span className="tone-amber badge">Everything paused</span>}
        </div>
      </section>

      <div className="columns">
        <section className="grid-area" aria-label="Cards">
          {cards.length ? (
            <div className="grid">
              {cards.map((card) => (
                <CardTile key={card.card_id} card={card} destination={state.destination}
                          onOpen={() => onOpenCard(card.card_id)} />
              ))}
            </div>
          ) : (
            <Empty title={filter === "all" ? "Insert cards" : `No cards: ${active.label}`}>
              {filter === "all" && "Each card is opened read-only, scanned and uploaded."}
            </Empty>
          )}
          {removedHidden > 0 && (
            <button type="button" className="link small" onClick={() => setFilter("removed")}>
              {removedHidden} card(s) not inserted (removed or finished) — show
            </button>
          )}
        </section>
        <aside className="side">
          <LinesPanel network={state.network} />
          <div className="section-head">
            <h2>Problems {urgentCount > 0 && <span className="badge tone-red">{urgentCount}</span>}</h2>
            {state.problems.length > 3 && (
              <button type="button" className="link small" onClick={onProblems}>See all</button>
            )}
          </div>
          <ProblemList problems={state.problems} handlers={handlers} compact limit={3} />
          <h2>Activity</h2>
          <ul className="activity">
            {events.map((event) => {
              const text = describeEvent(event.kind, event.data);
              return text ? <li key={event.sequence}><span className="muted small">
                {clock(event.at)}</span> {text}</li> : null;
            })}
            {!events.length && <li className="muted small">Nothing yet.</li>}
          </ul>
        </aside>
      </div>

      <footer className="actionbar">
        {session.paused_all ? (
          <Button kind="primary" icon={<Play size={16} />} busy={busy === "resume"}
                  onClick={() => run("resume", () => api("POST", "/api/v1/session/resume-all"),
                                     "Resumed.")}>Resume all</Button>
        ) : (
          <Button icon={<Pause size={16} />} busy={busy === "pause"}
                  onClick={() => run("pause", () => api("POST", "/api/v1/session/pause-all"),
                                     "Pausing. Files in progress finish first.")}>Pause all</Button>
        )}
        {state.counters.ready > 0 && (
          <Button icon={<Play size={16} />} kind="primary" busy={busy === "start"}
                  onClick={() => run("start", () => api("POST", "/api/v1/session/start-all"),
                                     "Started.")}>Start {state.counters.ready} ready</Button>
        )}
        <label className="check small">
          <input type="checkbox" checked={session.auto_upload}
                 onChange={(event) => run("auto", () => api("POST",
                   "/api/v1/session/auto-upload", { enabled: event.target.checked }))} />
          Upload new cards automatically
        </label>
        <span className="spacer" />
        <Button kind="danger" icon={<Square size={14} />} onClick={() => setConfirmEnd(true)}>
          End session
        </Button>
      </footer>
      {confirmEnd && <EndSession state={state} onClose={() => setConfirmEnd(false)} />}
    </div>
  );
}

function EndSession({ state, onClose }: { state: StationState; onClose: () => void }) {
  const { run, busy } = useAction();
  const unfinished = state.cards.filter((card) => !["UPLOADED", "NO_FILES", "FAILED",
    "UPLOADED_WITH_ISSUES", "NEEDS_ATTENTION"].includes(card.state));
  const end = (force: boolean) => run("end", () => api("POST", "/api/v1/session/close",
                                                       { force }), "Session ended.").then(onClose);
  return (
    <Panel title="End this session?" onClose={onClose}>
      {unfinished.length ? (
        <>
          <p><strong>{unfinished.length} card(s) are not finished:</strong></p>
          <ul className="plain">
            {unfinished.slice(0, 8).map((card) => (
              <li key={card.card_id}>{card.label || card.card_id}: {look(card.state).word}</li>
            ))}
          </ul>
          <p className="muted">If you end it now, their remaining files are not uploaded. They
            can be finished later from the command line with <code>retry</code>.</p>
          <div className="actions-row">
            <Button icon={<ArrowLeft size={16} />} kind="secondary" onClick={onClose}>Keep going</Button>
            <Button icon={<Square size={16} />} kind="danger" busy={busy === "end"} onClick={() => end(true)}>
              End anyway
            </Button>
          </div>
        </>
      ) : (
        <>
          <p>Every card is finished. Ending writes the reports and closes the session.</p>
          <div className="actions-row">
            <Button icon={<X size={16} />} kind="secondary" onClick={onClose}>Cancel</Button>
            <Button icon={<CircleCheck size={16} />} kind="primary" busy={busy === "end"} onClick={() => end(false)}>
              End session
            </Button>
          </div>
        </>
      )}
    </Panel>
  );
}

function Splash({ connection }: { connection: Connection }) {
  return (
    <div className="splash">
      <span className="logo-mark big" aria-hidden="true"><HardDriveUpload size={34} /></span>
      <h1>SD Uploader</h1>
      <p className="muted">{connection === "offline"
        ? "The station is not answering. Is it running? Trying again…"
        : "Connecting to the station…"}</p>
    </div>
  );
}

function SignedOut() {
  return (
    <div className="splash">
      <h1>Sign in to the station</h1>
      <p>On this computer, open the console with:</p>
      <code className="command">python main.py console</code>
      <p className="muted small">That link signs you in. It works only on this computer.</p>
    </div>
  );
}
