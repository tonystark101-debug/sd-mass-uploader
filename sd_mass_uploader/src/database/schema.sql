-- All timestamps are UTC ISO-8601. Local state only: never the sole proof of a cloud upload.

CREATE TABLE IF NOT EXISTS device_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    platform    TEXT NOT NULL,
    hostname    TEXT,
    issues_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON device_snapshots(captured_at);

CREATE TABLE IF NOT EXISTS usb_devices (
    snapshot_id TEXT NOT NULL REFERENCES device_snapshots(snapshot_id) ON DELETE CASCADE,
    topology_id TEXT NOT NULL,
    parent_topology_id TEXT,
    vendor_id TEXT,
    product_id TEXT,
    product_name TEXT,
    is_hub INTEGER NOT NULL,
    negotiated_speed_mbps REAL,
    speed_source TEXT NOT NULL,
    speed_confidence TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, topology_id)
);

CREATE TABLE IF NOT EXISTS readers (
    snapshot_id TEXT NOT NULL REFERENCES device_snapshots(snapshot_id) ON DELETE CASCADE,
    reader_id TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, reader_id)
);
CREATE INDEX IF NOT EXISTS idx_readers_id ON readers(reader_id);

CREATE TABLE IF NOT EXISTS storage_devices (
    snapshot_id TEXT NOT NULL REFERENCES device_snapshots(snapshot_id) ON DELETE CASCADE,
    storage_id TEXT NOT NULL,
    reader_id TEXT,
    status TEXT NOT NULL,
    capacity_bytes INTEGER,
    record_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, storage_id)
);
CREATE INDEX IF NOT EXISTS idx_storage_id ON storage_devices(storage_id);

CREATE TABLE IF NOT EXISTS volumes (
    snapshot_id TEXT NOT NULL REFERENCES device_snapshots(snapshot_id) ON DELETE CASCADE,
    volume_id TEXT NOT NULL,
    storage_id TEXT,
    mount_point TEXT,
    access_status TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, volume_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    base_prefix TEXT NOT NULL,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    owner_pid INTEGER,
    heartbeat_at TEXT,
    upload_seconds REAL NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

CREATE TABLE IF NOT EXISTS session_metadata (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (session_id, key)
);

CREATE TABLE IF NOT EXISTS session_cards (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    card_id TEXT NOT NULL,
    mount_point TEXT NOT NULL,
    fingerprint TEXT,
    volume_label TEXT,
    reader_model TEXT,
    is_folder INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, card_id)
);

CREATE TABLE IF NOT EXISTS scans (
    scan_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
    card_id TEXT NOT NULL,
    mount_point TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    dir_count INTEGER NOT NULL DEFAULT 0,
    unreadable_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    by_extension_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_scans_session ON scans(session_id);

CREATE TABLE IF NOT EXISTS scan_errors (
    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scan_errors_scan ON scan_errors(scan_id);

CREATE TABLE IF NOT EXISTS files (
    file_id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER,
    readable INTEGER NOT NULL DEFAULT 1,
    sha256 TEXT,
    UNIQUE (scan_id, relative_path)
);

CREATE TABLE IF NOT EXISTS upload_tasks (
    task_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES files(file_id),
    card_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    object_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    local_crc32c TEXT,
    local_sha256 TEXT,
    remote_generation INTEGER,
    error_class TEXT,
    last_error TEXT,
    uploaded_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (session_id, object_name)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON upload_tasks(session_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_card ON upload_tasks(session_id, card_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_object ON upload_tasks(object_name);

CREATE TABLE IF NOT EXISTS upload_attempts (
    attempt_id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES upload_tasks(task_id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    bytes_read INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    error_class TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON upload_attempts(task_id);

CREATE TABLE IF NOT EXISTS verification_results (
    verification_id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES upload_tasks(task_id) ON DELETE CASCADE,
    verified_at TEXT NOT NULL,
    source_size INTEGER NOT NULL,
    remote_size INTEGER,
    local_crc32c TEXT,
    remote_crc32c TEXT,
    status TEXT NOT NULL,
    method TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verification_task ON verification_results(task_id);

-- Station mode: the background service. At most one station session is open at a time.
CREATE TABLE IF NOT EXISTS station_sessions (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    auto_upload INTEGER NOT NULL DEFAULT 1,
    paused_all INTEGER NOT NULL DEFAULT 0
);

-- Intake phase and operator controls per card. Upload progress comes from upload_tasks.
CREATE TABLE IF NOT EXISTS station_cards (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    card_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    present INTEGER NOT NULL DEFAULT 1,
    paused INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    card_json TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, card_id)
);

-- Requests from other processes (CLI now, API later), handled by the station in order.
CREATE TABLE IF NOT EXISTS station_commands (
    command_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    command TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    taken_at TEXT,
    finished_at TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_station_commands_open ON station_commands(taken_at);

-- History of problems shown to operators. Open problems are recomputed; this records when.
CREATE TABLE IF NOT EXISTS station_problems (
    row_id INTEGER PRIMARY KEY,
    problem_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    card_id TEXT,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_station_problems_open ON station_problems(resolved_at);

-- Everything the AI troubleshooter did: requests, tool calls, approvals and results.
CREATE TABLE IF NOT EXISTS ai_audit (
    row_id INTEGER PRIMARY KEY,
    ai_session_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    tool TEXT,
    detail TEXT,
    verdict TEXT,
    decided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_audit_session ON ai_audit(ai_session_id);

-- PeakAI: how each card was placed (device -> pass -> folder), and what still needs a person.
CREATE TABLE IF NOT EXISTS peakai_cards (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    card_id TEXT NOT NULL,
    serial TEXT,
    plan_json TEXT NOT NULL,
    held_reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, card_id)
);

-- PeakAI: one _info file per card and pass, written once the part is fully verified.
CREATE TABLE IF NOT EXISTS peakai_info (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    card_id TEXT NOT NULL,
    pass_name TEXT NOT NULL,
    object_name TEXT,
    written_at TEXT,
    last_error TEXT,
    next_try REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, card_id, pass_name)
);

-- PeakAI dashboard: one entry per upload day + activity + city + pass (= per session and pass).
CREATE TABLE IF NOT EXISTS peakai_reports (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    pass_name TEXT NOT NULL,
    status TEXT,
    payload_json TEXT,
    response_text TEXT,
    sent_at TEXT,
    last_error TEXT,
    next_try REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, pass_name)
);
