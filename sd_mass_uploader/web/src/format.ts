// Presentation only: units, times, and the status language (color + icon + word, never color alone).
import type { CardState, CardView } from "./types";

export function bytes(value: number | null | undefined): string {
  if (value == null) return "unknown";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1000 && unit < units.length - 1) {
    size /= 1000;
    unit += 1;
  }
  return `${unit === 0 ? size : size.toFixed(size < 10 ? 1 : 0)} ${units[unit]}`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "unknown";
  if (seconds < 60) return "under a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes % 60} min`;
}

export function since(iso: string | null | undefined): string {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

export function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit",
                                                second: "2-digit" });
}

export function speed(mbps: number | null | undefined): string {
  if (!mbps) return "unknown";
  return mbps >= 1000 ? `${mbps / 1000} Gbps` : `${mbps} Mbps`;
}

export type Tone = "blue" | "gray" | "amber" | "green" | "orange" | "red" | "purple";

export interface StateLook {
  tone: Tone;
  word: string;
  icon: "upload" | "wait" | "pause" | "check" | "alert" | "x" | "eject" | "help" | "scan";
  rank: number; // Lower comes first: what needs a person, then what is moving.
}

const LOOKS: Record<CardState, StateLook> = {
  FAILED: { tone: "red", word: "Failed", icon: "x", rank: 0 },
  NEEDS_ATTENTION: { tone: "orange", word: "Needs attention", icon: "alert", rank: 1 },
  NEEDS_DECISION: { tone: "orange", word: "Which device?", icon: "help", rank: 1 },
  NEEDS_CONFIRMATION: { tone: "orange", word: "Confirm device", icon: "help", rank: 1 },
  UPLOADED_WITH_ISSUES: { tone: "orange", word: "Finished with issues", icon: "alert", rank: 2 },
  REMOVED: { tone: "purple", word: "Removed early", icon: "eject", rank: 3 },
  UPLOADING: { tone: "blue", word: "Uploading", icon: "upload", rank: 4 },
  PAUSING: { tone: "amber", word: "Pausing", icon: "pause", rank: 5 },
  PAUSED: { tone: "amber", word: "Paused", icon: "pause", rank: 5 },
  SCANNING: { tone: "gray", word: "Scanning", icon: "scan", rank: 6 },
  READY: { tone: "gray", word: "Ready to start", icon: "wait", rank: 6 },
  QUEUED: { tone: "gray", word: "Waiting", icon: "wait", rank: 7 },
  NO_FILES: { tone: "gray", word: "No files", icon: "check", rank: 8 },
  UPLOADED: { tone: "green", word: "Uploaded", icon: "check", rank: 9 },
};

export function look(state: CardState): StateLook {
  return LOOKS[state] ?? { tone: "gray", word: state, icon: "wait", rank: 7 };
}

export function cardName(card: Pick<CardView, "card_id" | "label">): string {
  return card.label || card.card_id;
}

/** Rounded down, so 100% only ever means finished. */
export function percent(done: number, total: number): number {
  if (total <= 0) return 0;
  return done >= total ? 100 : Math.min(99, Math.floor((100 * done) / total));
}

/** Plain words for station events in the activity feed. */
export function describeEvent(kind: string, data: Record<string, unknown>): string | null {
  const card = String(data.card_id ?? "");
  switch (kind) {
    case "card_inserted": return `${data.label || card} inserted`;
    case "card_removed": return `${card} removed`;
    case "card_returned": return `${card} put back`;
    case "card_scanned": return `${card} scanned: ${data.files} files, ${bytes(Number(data.bytes))}`;
    case "card_needs_decision": return `${card} waits for a decision`;
    case "card_state": return data.state === "UPLOADED" ? `${card} uploaded and verified` : null;
    case "session_opened": return "Session opened";
    case "session_closed": return `Session ended (${data.status})`;
    case "paused_all": return "Everything paused";
    case "resumed_all": return "Everything resumed";
    case "card_paused": return `${card} paused`;
    case "card_resumed": return `${card} resumed`;
    case "card_retry": return `${card}: ${data.files} file(s) queued again`;
    case "problem_opened": return `Problem: ${data.title}`;
    case "problem_resolved": return `Resolved: ${data.code}`;
    case "line_ready": return `Internet line ${data.line} ready (${data.kind_of_line})`;
    case "line_lost": return `Internet line ${data.line} unplugged: its files moved to the others`;
    case "line_changed": return `Internet line ${data.line} got a new address`;
    case "line_resting": return `Internet line ${data.line} resting after network failures`;
    case "line_no_internet": return `Internet line ${data.line} cannot reach Google`;
    case "packet_opened": return `Packet ${data.ref} from ${data.vendor}: ${data.cards} card(s)`;
    case "claude_fix_started": return `Claude is fixing: ${(data.titles as string[] ?? []).join("; ")} (uploads continue)`;
    case "claude_fix_step": return `Claude: ${data.text}`;
    case "claude_fix_finished": return data.error
      ? `Claude stopped: ${data.error}`
      : `Claude finished: fixed ${data.fixed ?? "unknown"} after ${data.commands} step(s)`;
    case "vendor_session_opened": return `Started from vendor code ${data.code} (${data.vendor})`;
    case "engine_started": return "Uploader started";
    case "engine_stopped": return data.reason ? `Uploader stopped: ${data.reason}` : null;
    default: return null;
  }
}
