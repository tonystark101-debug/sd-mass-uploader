// Shapes of what the station API returns (src/station/station.py, src/api/app.py).

export type CardState =
  | "NEEDS_CONFIRMATION" | "SCANNING" | "NEEDS_DECISION" | "READY" | "QUEUED" | "UPLOADING"
  | "PAUSING" | "PAUSED" | "UPLOADED" | "UPLOADED_WITH_ISSUES" | "FAILED" | "NEEDS_ATTENTION"
  | "REMOVED" | "NO_FILES";

export interface CardView {
  card_id: string;
  label: string | null;
  state: CardState;
  present: boolean;
  paused: boolean;
  reason: string | null;
  files_done: number;
  files_total: number;
  bytes_done: number;
  bytes_total: number;
  files_failed: number;
  mount_point: string | null;
  reader_model: string | null;
  filesystem: string | null;
  capacity_bytes: number | null;
  link_speed_mbps: number | null;
  inserted_at: string;
}

export interface Counters {
  inserted: number; total: number; scanning: number; ready: number; queued: number;
  uploading: number; paused: number; uploaded: number; failed: number; removed: number;
  needs_confirmation: number; needs_decision: number; no_files: number;
}

export interface Progress {
  files_total: number; files_done: number; files_failed: number; files_active: number;
  bytes_total: number; bytes_done: number; current_mb_per_s: number;
  average_mb_per_s: number; retries: number; eta_seconds: number | null;
}

export interface ProblemAction { kind: string; label: string; card_id: string | null }

export interface Problem {
  id: string;
  code: string;
  severity: "critical" | "error" | "warning" | "info";
  title: string;
  message: string;
  steps: string[];
  action: ProblemAction | null;
  card_id: string | null;
  files: number;
  detail: string | null;
  opened_at: string | null;
  tags: string[];
}

export interface SessionInfo {
  session_id: string;
  status: string | null;
  opened_at: string;
  auto_upload: boolean;
  paused_all: boolean;
  base_prefix: string | null;
  metadata: Record<string, string>;
}

export interface PassStatus {
  name: string; folder: string; description: string; minutes_left: number;
  has_token: boolean; device: string | null; last_error: string | null;
}

export interface PlacementGroup {
  device: string; pass: string | null; folder?: string; files: number; bytes: number;
  hours: number | null; recorded_from: string | null; recorded_to: string | null;
}

export interface Destination {
  profile: "peakai" | "generic";
  bucket?: string;
  pack?: {
    name: string; version: string; dated: string; bucket: string; imported_at: string;
    activities: string[]; documents: string[];
  };
  devices?: Record<string, string[]>;
  keeper?: {
    allowed: string[] | null; passes: PassStatus[]; legacy_only: boolean;
    checked_at: number | null; key_error: string | null; service_error: string | null;
  };
  cards?: Record<string, { serial: string | null; held_reason: string | null;
    groups: PlacementGroup[]; unplaced: number; excluded: number }>;
  reports?: { pass_name: string; status: string | null; sent_at: string | null;
    last_error: string | null; response: string }[];
  infos?: { card_id: string; pass_name: string; object_name: string | null;
    written_at: string | null; last_error: string | null }[];
}

export interface StationState {
  updated_at: string;
  session: SessionInfo | null;
  counters: Counters;
  cards: CardView[];
  progress: Partial<Progress>;
  engine: { running: boolean; problem: string | null; store_error: string | null;
    workers: number };
  issues: { code: string; message: string; subject: string | null }[];
  waiting_cards: { card_id: string; volume_label: string | null; mount_point: string }[];
  problems: Problem[];
  destination: Destination;
  daemon: { pid?: number; running?: boolean; source?: string; started_at?: string };
}

export interface StationEvent {
  kind: string;
  data: Record<string, unknown>;
  at: string;
  sequence: number;
}

export interface CardFile {
  task_id: number; relative_path: string; object_name: string; size_bytes: number;
  status: string; attempts: number; error_class: string | null; last_error: string | null;
  uploaded_at: string | null;
}

export interface CardDetail {
  card: CardView;
  files: CardFile[];
  file_status_counts: Record<string, number>;
  scan_errors: { path: string; kind: string; message: string }[];
}

export interface AiEntry {
  seq: number; at: string;
  kind: "operator" | "assistant" | "tool_call" | "tool_result" | "approval" | "note";
  text: string; tool: string | null; is_error: boolean; verdict: string | null;
}

export interface AiApproval {
  approval_id: string; kind: string; summary: string; reason: string; requested_at: string;
  status: string;
}

export interface AiSession {
  ai_session_id: string; status: string; started_at: string; problem_id: string | null;
  tokens_used: number; pending_approval: AiApproval | null; error: string | null;
  transcript: AiEntry[];
}

export interface PackCheck {
  ok: boolean; lines: string[]; errors: string[]; warnings: string[];
  live_passes: string[] | null;
}
