# SD Mass Uploader — Linux Desktop App Plan

**Status:** D1–D15 decided (D14 assumed) on 2026-11-19. Built on 2026-11-19: Phases 1 (daemon core),
2 (local API), 3 (error advisor), 3B (AI troubleshooter), 3C (PeakAI integration), 4 (Operator
Console; React + TypeScript, served by the station, tested in a browser against a simulated
station) 5 (desktop companion: panel indicator, notifications, console window, autostart;
logic tested, GTK parts not yet run on Linux) and 6 (boot and kiosk: systemd readiness and
watchdog, power-cut recovery tested by killing the real process, kiosk unit and script; kiosk
not yet run on Linux) and 7 (.deb package built in pure Python with bundled Linux wheels for
Python 3.12 and 3.13; checked file by file, not yet installed on a real system). Tested with
mocks, simulated cards and local stand-ins for Claude, PeakAI and GCS. The real PeakAI pack
imports, and its token service accepts the key. Still pending: the Phase 0 hardware run, a real
upload to `peak-lake-raw`, and a real Claude API run.
**Date:** 2026-11-19
**Goal:** Turn the current command-line uploader into an installable app for Debian-based Linux
(Ubuntu, Kali). It starts at boot, stays open in a persistent panel, and gives operators a live,
card-by-card view of what is happening, what went wrong and how to fix it.

---

## 1. What we keep, what changes

The existing engine is solid and stays. It already has: read-only scanning, the Linux adapter,
CRC32C verification, no-silent-overwrite, retry with backoff, crash recovery, SQLite state and
reports. It is also cleanly separated from terminal printing (`UploadManager` reports progress
through a callback). The app is built **around** it, not instead of it.

| Area | Today (CLI) | App |
| --- | --- | --- |
| How it runs | Operator types a command; it exits when done | Background service starts at boot and never exits |
| Card detection | `check` / `monitor` polling | Instant, event-driven (udev), plus a slow safety re-scan |
| Mounting | Operator must mount cards | App mounts new cards **read-only** by itself (decision D3) |
| Session | One command = one fixed set of cards | Operator opens a session once; cards can be inserted and removed while it runs |
| Unit of status | Files and sessions | **Cards** (new) plus files and sessions |
| Pause | Stop whole run only | Pause / resume per card and for everything |
| Errors | Text in terminal and error log | Plain-language problem cards with the cause, fix steps and a fix button |
| Interface | Terminal | Operator Console (window, panel indicator, full-screen kiosk) |
| Install | `setup.sh` + venv | One `.deb` package |

The CLI stays available (`sdu check`, `sdu upload` and so on) for diagnostics, scripting and Windows.

---

## 2. Architecture

```text
 ┌──────────────────────── Linux machine ───────────────────────────┐
 │                                                                  │
 │  udev events ──┐                                                 │
 │                ▼                                                 │
 │  ┌──────────── sdu-daemon (systemd service, starts at boot) ───┐ │
 │  │  Device Watcher → Card Manager (state machine per card)     │ │
 │  │  Read-only Mounter (udisks2)    Station Session              │ │
 │  │  Existing engine: scanner · UploadManager · verifier ·       │ │
 │  │                   reconcile · reports · SQLite               │ │
 │  │  Error Advisor (error → plain-language fix)                  │ │
 │  │  Local API: REST + WebSocket live events                     │ │
 │  └───────────────▲─────────────────────────▲──────────────────┘ │
 │                  │ localhost / unix socket │                     │
 │   ┌──────────────┴───────┐      ┌──────────┴──────────────┐      │
 │   │ Operator Console     │      │ Panel indicator (tray)   │      │
 │   │ (app window / kiosk) │      │ counts + alerts + menu   │      │
 │   └──────────────────────┘      └──────────────────────────┘      │
 └──────────────────────────────────────────────────────────────────┘
                              │ HTTPS
                              ▼
                     Google Cloud Storage
```

**Why a background service plus a separate UI:** uploads keep running when the window is closed,
the screen locks or the desktop crashes. The UI can be closed and reopened at any time and
reconnects to live state. This is what makes the app "persistent" and able to work from boot.

### 2.1 Components

1. **`sdu-daemon`** — Python 3.12, systemd service, runs as dedicated user `sdu` (not root).
   - **Device Watcher:** `pyudev` events for block devices, plus the existing `LinuxAdapter`
     snapshot every 30 s to catch anything missed. Event to screen in about 1–2 s.
   - **Read-only Mounter:** mounts new card partitions through udisks2 with
     `ro,noatime,nosuid,nodev,noexec`. udisks2 picks the folder (`/media/sdu/<label>`). It never mounts
     read-write, never runs `fsck`, never formats. A polkit rule allows only the `sdu` user to
     do this. A card the desktop already mounted is used where it is.
   - **Card Manager:** one state machine per card (section 3). It owns all the card counters.
   - **Station Session:** the operator enters the metadata once; every card inserted while the
     session is open joins it. `UploadManager` already pulls work from SQLite through
     `next_ready`, so new cards' files are picked up by the running worker pool.
   - **Error Advisor:** maps each error to a catalog entry (section 5).
   - **Local API:** FastAPI + Uvicorn. REST for commands, one WebSocket stream for live events
     (card changes, progress every 0.5 s, problems). Listens on a Unix socket and
     `127.0.0.1` only, with a per-install token. Remote access from another PC is off unless
     decided otherwise (D5).
2. **Operator Console** — the main UI (section 4). One web frontend, shown three ways:
   - **App window:** a native desktop window (pywebview on WebKitGTK) launched from the app menu.
   - **Kiosk mode:** full screen at boot with no desktop (section 6).
   - **Browser:** `http://127.0.0.1:8765` as a fallback.
3. **Panel indicator** — a small icon in the top or bottom panel (AppIndicator/StatusNotifier). It
   shows a badge (for example `12↑ 2✕`), a menu with the counts, Pause all / Resume all and Open
   console, and desktop notifications for problems. On Kali (Xfce) this works out of the box. On
   stock Ubuntu GNOME it needs the AppIndicator extension, which Ubuntu ships enabled.
4. **`sdu` CLI** — the existing commands, which also can talk to the daemon (`sdu status` shows
   live counts).

### 2.2 Technology choices

| Part | Choice | Reason |
| --- | --- | --- |
| Backend | Python 3.12, existing code, FastAPI, Uvicorn, pyudev | Reuses the tested engine; small, well-known libraries |
| Frontend | TypeScript + React + Vite, built once at package time | Mature, fast, many developers know it; the result is static files, so Node is **not** needed on the station |
| UI components | Hand-built design system on CSS variables, plus an icon set (Lucide) | Full control of a kiosk-friendly look with no heavy UI framework |
| Window | pywebview (WebKitGTK, from Debian packages) | Native window with no bundled Chromium |
| Kiosk | `cage` (Wayland kiosk compositor) + `cog` or Chromium `--kiosk` | Boots straight to one full-screen app |
| Tray | `AyatanaAppIndicator3` through PyGObject | The standard panel indicator on Ubuntu and Xfce |
| Packaging | `.deb` with a bundled virtualenv in `/opt/sd-mass-uploader` | One install command; libraries pinned and kept away from system Python |

Alternative considered: a native GTK4/libadwaita UI. It looks very native on GNOME, but it cannot
be reused for kiosk-in-browser or remote viewing, and it is slower to build rich live dashboards
in. Rejected unless you prefer it.

---

## 3. Card lifecycle and counters (new)

Each physical card (identified by volume serial plus fingerprint, as today, never by mount
path) gets one state:

```text
 INSERTED → MOUNTING → SCANNING → READY ──(auto or Start)──▶ QUEUED → UPLOADING
                                                               │  ▲        │
                                                      Pause ───┘  └─ Resume│
                                                               ▼           ▼
                                                            PAUSED     VERIFYING
                                                                           │
                                    ┌──────────────────┬───────────────────┤
                                    ▼                  ▼                   ▼
                                UPLOADED       UPLOADED_WITH_ISSUES      FAILED
                           (every file verified)  (some files need         (needs action)
                                                     attention)
 At any point:  card pulled out → REMOVED (work kept; continues if re-inserted)
                card unreadable → NEEDS_ATTENTION (with fix steps)
                already uploaded earlier → ALREADY_UPLOADED (skipped, nothing re-sent)
```

### 3.1 Counters shown at the top of the console and in the panel

| Counter | Exact meaning |
| --- | --- |
| **Inserted** | Cards physically present right now |
| **Total cards** | Cards that have joined this session (present or removed) |
| **Uploading** | Cards with files being sent right now |
| **Queued** | Cards ready and waiting for a free worker |
| **Uploaded** | Cards where every file is `VERIFIED` in GCS. This is the "safe to remove" signal |
| **Failed** | Cards that stopped and need an operator action (each has a problem card) |
| **Paused** | Cards paused by the operator (or all, when Pause all is on) |
| **Removed early** | Cards pulled out before finishing (work resumes when re-inserted) |

Also shown: total files and bytes done and remaining, current and average speed (MB/s, labeled),
estimated time left, retries, network state, and GCS connection state.

### 3.2 Engine changes needed

- `session_cards` gets `card_state`, `paused`, `state_reason` and `updated_at` columns
  (versioned schema migration; existing databases upgrade in place).
- `next_ready()` skips tasks of paused or removed cards. Pausing lets files already in flight
  finish (honest "pausing…" state), then the card shows `PAUSED`.
- `UploadManager` runs as a long-lived loop inside the daemon instead of exiting when the
  queue is empty, and accepts new cards while it runs.
- Card states are derived from task statuses plus presence, so the database stays the single
  source of truth and the UI can never show a count the data does not support.

---

## 4. UI: the Operator Console

**Professional description:** a *real-time operations dashboard* (an "Operator Console") for an
ingest station. It is a single-window, glanceable monitoring interface that follows a
**status-at-a-glance → card grid → detail on demand** information hierarchy. It uses a consistent
status language (color + icon + word, never color alone), with actionable alerts that carry
guided remediation.

### 4.1 Screen layout (main screen)

```text
┌────────────────────────────────────────────────────────────────────────────────┐
│ SD Mass Uploader   Session: Field Survey · Bengaluru · 2026-11-19   ● GCS OK  ● Net 94 MB/s │
├────────────────────────────────────────────────────────────────────────────────┤
│ [Inserted 18] [Uploading 6] [Queued 7] [Uploaded 3] [Failed 1] [Paused 1]      │  ← counter tiles (click = filter)
│ ███████████████████░░░░░░░░░  61%  412 GB / 676 GB · 88 MB/s · ~50 min left    │  ← overall progress
├──────────────────────────────────────────────────────────┬─────────────────────┤
│  CARD GRID (one tile per card, sorted by attention)      │  PROBLEMS (2)       │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐     │ ✕ Card 07 removed   │
│  │✕ CARD 07 │ │⏸ CARD 03 │ │↑ CARD 01 │ │✓ CARD 05 │     │   during upload.    │
│  │ Removed  │ │ Paused   │ │ 72% ███▒ │ │ Uploaded │     │   Put the same card │
│  │ 120/796  │ │ 300/796  │ │ 31 MB/s  │ │ 796/796  │     │   back in any slot. │
│  │[Fix]     │ │[Resume]  │ │[Pause]   │ │ Safe to  │     │   [Show me]         │
│  └──────────┘ └──────────┘ └──────────┘ │ remove   │     │ ⚠ Network slow —    │
│   ...                                   └──────────┘     │   retrying in 12 s  │
├──────────────────────────────────────────────────────────┴─────────────────────┤
│ Activity: 14:02:11 Card 05 uploaded and verified (796 files) · 14:01:58 ...    │
└────────────────────────────────────────────────────────────────────────────────┘
 [Pause all]  [Resume all]  [End session]                         Settings · Help
```

### 4.2 Screens

1. **First-run setup wizard:** bucket and project, Google credential check (runs the existing
   `gcs-test`), default upload behavior, kiosk on/off.
2. **Start session:** the metadata form (activity, place, date, operator, project, description)
   with validation and a live preview of the destination path
   `gs://bucket/project/YYYY/MM/DD/SESSION/CARD/…`, plus a "reuse last session's values" button.
3. **Dashboard:** the main screen above.
4. **Card detail drawer:** the card's identity (label, serial, capacity, filesystem, reader
   model, USB link speed with source), file breakdown by type, per-file table with status and
   error filter, attempts and retries, and Pause / Resume / Retry failed / Verify again buttons.
5. **Problems center:** all open problems, grouped by type, each with a fix guide.
6. **History:** past sessions, results, report downloads (JSON / CSV / error log).
7. **Settings:** workers, chunk size, auto-start rules, duplicate policy, metadata rules,
   theme, kiosk, sound alerts. Credentials are never shown.
8. **Hardware view:** USB tree with hubs, readers and link speeds, highlighting readers below
   5 Gbps (from the existing `check`).

### 4.3 Design rules

- **Status language** used everywhere (tile, badge, panel, notifications):

  | State | Color | Icon | Word |
  | --- | --- | --- | --- |
  | Uploading | Blue | ↑ | Uploading 72% |
  | Queued / Scanning | Gray | ⋯ | Waiting |
  | Paused | Amber | ⏸ | Paused |
  | Uploaded | Green | ✓ | Uploaded · safe to remove |
  | Issues | Orange | ⚠ | Needs attention |
  | Failed | Red | ✕ | Failed — see fix |
  | Removed | Purple | ⏏ | Removed early |
- **Never color alone:** every state has an icon and a word (color-blind safe, WCAG AA contrast).
- **Kiosk friendly:** touch targets of at least 44 px, readable from 1–2 m away, dark and light
  themes, no hover-only actions.
- **Honest data:** only measured numbers. "Unknown" is shown as unknown. Stale data is
  shown as stale (for example when the daemon is unreachable, the UI greys out and says so).
- **Calm by default:** sound and notifications only for things that need a person (failure, card
  removed early, card finished = "safe to remove").
- **Confirmation only for risky actions:** End session with unfinished cards, overwrite.
- **Physical mapping (optional):** an operator can label slots ("Hub A · Slot 3") once. Tiles
  then show the slot name so people know which card to pull. Uses the existing USB port path.

---

## 5. Errors: what happened and how to fix it

Every failure becomes a **problem card** with: a title, the affected card(s) and file count, what it
means in plain words, numbered fix steps, one main fix button, and "technical details" (exception,
path, timestamps) collapsed for support. The catalog maps the existing typed exceptions and
statuses, so nothing is guessed from text.

| Problem | Source in engine | Operator sees | Fix button |
| --- | --- | --- | --- |
| Card removed during upload | `SourceDisconnectedError` | "Card 07 was taken out at 120 of 796 files. Put the **same** card back in any slot." | Auto-resumes when re-inserted |
| Different card in its place | fingerprint mismatch | "This is not Card 07. It was added as a new card." | — |
| File changed after scan | `SourceChangedError` / `FAILED_PERMANENT` | "3 files changed on the card after scanning. Re-scan the card." | Re-scan |
| Network down / slow | `TransientUploadError` | "No connection to Google Cloud. Retrying in 12 s (attempt 3 of 5). Check the Ethernet cable." | Retry now |
| Sign-in expired / invalid | `AuthenticationError` | "Google sign-in is no longer valid. Uploads are paused." Steps to renew. | Open setup |
| No permission on bucket | `GcsPermissionError` | "This account cannot write to the bucket. Ask your admin for Storage Object User." | Re-check access |
| Object already exists, different | `CONFLICT` | "A different file already exists at this path. Nothing was overwritten." | Keep both / Investigate |
| Checksum mismatch | `VerificationMismatch` | "The cloud copy did not match. It will be uploaded again." | Retry |
| Unreadable files on card | scan errors | "12 files could not be read (card may be damaged)." List shown. | Retry read |
| Card won't mount | mount failure / unsupported FS | "Card filesystem not recognized (NTFS/ext4/raw?)." | Show details |
| Reader at USB 2 speed | topology | "Reader in Hub B port 4 runs at 480 Mbps. Move it to a blue USB 3 port." | — (warning) |
| Cloned card serial | duplicate serial | "Two cards report the same ID. Confirm which to upload." | Confirm |
| Local disk almost full | new check | "The station's disk is 95% full; logs/database may fail." | Open folder |
| Daemon not running | UI cannot connect | "Background service stopped." | Restart service |

Each entry has an ID (for example `E-CARD-REMOVED`) and links to a help page shipped with the app.

---

## 5A. AI troubleshooter (added 2026-11-19; D9 decided: tiered with approval)

When a problem appears, the operator can press **Ask AI** on the problem card, or open an **AI
terminal** panel. The AI works like a CLI agent (Claude Code or Codex style). It reads the
station's state, logs and system information, explains the cause in plain words, and fixes what
it can. Each command it runs and the output appear live in the panel.

**How it is built:** Claude API (`anthropic` Python SDK, model `claude-opus-5-5`) with tool use,
running inside the daemon. The station gives the AI **tools**, so it never gets a raw root shell.

| Tier | Examples | Runs |
| --- | --- | --- |
| 1. Read and diagnose | station state, card and file errors, logs, `journalctl`, `lsblk`, `lsusb -t`, `ip addr`, `ping`/DNS, GCS access test, disk space | Automatically |
| 2. Station actions | retry failed files, re-scan a card, resume after pause, restart the upload engine, re-check credentials | Automatically or after one click (setting) |
| 3. Any shell command | `systemctl restart …`, network fixes, package installs, config edits | **Only after the operator approves that exact command** |
| Never | anything that writes to a card: `mkfs`, `fsck`, `dd`/`wipefs`/`parted` on card devices, mounting read-write, deleting or moving source files, printing credentials | Blocked in code, even if approved |

Every AI action is written to an audit log (who approved it, the command, its output). The AI has
a token and cost budget for each problem. The operator can stop it at any time.

**Why not full unrestricted root access:** the most important rule of this project is that
source cards are read-only. An AI with an unrestricted root shell could, by mistake, "repair" a
card that looks broken (for example the test card with the dirty bit set) and destroy recordings.
It could also be misled by text inside a file name or log line. Tiered tools keep its speed for
diagnosis and still let a person approve anything risky.

**Privacy:** logs contain file names, place and operator names. These are sent to the AI provider
only when someone asks the AI for help. An option redacts personal fields first.

**Offline:** if there is no internet connection, the AI isn't available. The built-in error guide
(section 5) always works.

Planned as **Phase 3B**, after the Error Advisor (Phase 3), because the AI uses the same problem
records and catalog as its starting point.

---

## 5B. PeakAI context pack (from `PeakAI-Uploader-v2_linux-laptop-1.zip`, read 2026-11-19)

The user's zip (v2, 12 Nov 2026) is the operating manual for uploads. It holds six files:
`README.md` and `SITE_CHAT_INSTRUCTIONS.md` (the rules), `install.sh`, `peakai-tokens.sh` (the
token keeper), `peakai-report.sh` (dashboard reporting) and `laptop_key.txt` (this laptop's
secret key). It was written for a person or chat assistant uploading with `gcloud`. The station
must do the same job automatically and follow the same rules.

### What the zip requires, and what changes in the station

| Rule in the zip | Station today | Change needed |
| --- | --- | --- |
| Bucket `gs://peak-lake-raw` | Bucket from `config.yaml` | Pack sets it |
| No long-lived Google key. The laptop key (`X-PeakAI-Key`) gets a **1-hour token per "pass"** from the PeakAI token service. Renew it when under 35 minutes are left, check every 5 minutes, re-read it after any 401, never keep it in memory longer than 5 minutes | Application Default Credentials or a service-account key (D6) | New **pass keeper** in the daemon, using the same protocol as `peakai-tokens.sh`. The GCS client takes its token from it. **Replaces D6** |
| A token can write **only in its pass folder**. Five passes: `gilabs` → `gilabs/`, `buildai` → `buildai/stereo/`, `sieve` → `sieve/mono/`, `ots-mono` → `ots-new/mono/`, `ots-stereo` → `ots-new/stereo/`. PeakAI decides which passes this laptop has (`/v1/passes`) | One credential for the whole bucket | Each file is uploaded with its own pass's token. A pass that is taken away is dropped at once |
| **The pass comes from the files on the card**, not from what a person says. Each device has a file signature, for example `data/session_*/ego_*.mcap` with a `session.json` whose hostname starts `GILABS-` → `gilabs`, and `recordings/*-sn<serial>-*-left.mp4.enc` with files starting `GKENC1` → `buildai` | No device detection | New **device classifier**. A card is held as "Needs a decision" when it can't be placed. **Never guess** |
| One card can hold **two devices**. Each part goes to its own pass folder under the same `card-<SERIAL>`. The card's small root files go with the main device | One card, one folder | Each file gets its own pass and path |
| Path: `<pass folder>/<activity>/<city>/<upload day, India time>/card-<SERIAL>/<the card exactly as it is>`. No vendor, batch or session ID in the path | `uploads/<project>/YYYY/MM/DD/<session>/<card>/...` (UTC) | New path template. The upload day uses Asia/Kolkata time |
| Activity from a fixed list (`garage`, `construction`, `salon`, ...). A new activity is lowercase-with-hyphens, and the person is told. City is a lowercase town name. **Ask; never write `unknown`** unless nobody knows | Free-text session fields | New session form: activity list, city, vendor name (required, ask), site, batch. The device is detected |
| **No phone numbers anywhere** (storage, `_info`, dashboard) | Not checked | Reject phone-number-like text in every field |
| Footage that isn't ours (YouTube downloads, phone photos, `Android/` folders) is **not uploaded** | Everything readable is uploaded | Exclusion rules from the pack. Excluded files are reported, not uploaded |
| Never overwrite or delete (`--no-clobber` / `ifGenerationMatch=0`) | Same (generation precondition; identical files accepted) | Fits as is. Two uploads of the same card on one day share a folder, and identical files are accepted |
| After each card: write a **new** `_info/<machine>_<YYYYMMDDTHHMMSS>.json` in the card folder (pass, folder, vendor, vendor_name, device, camera stereo/mono, activity, city, site, batch, dates, serial, file count, bytes, video-hours estimate, written by and at, +05:30) | Session manifests under `<prefix>/manifests/` | Replace the manifests with `_info` files. A pass token can't write outside its folder, so the old manifest path would be refused (403) |
| **Report on the PeakAI dashboard**: one entry per upload day + activity + city + pass, `uploading` when cards start and `uploaded` when all are done (`POST /v1/report`, laptop key, upsert, `dry_run` available). Show the person the entry | Not done | New **dashboard reporter**. Its status shows in the console. A failed report becomes a problem with a retry |
| Error meanings: 401 unknown laptop key → ask PeakAI; 403 pass not allowed → check the device, ask PeakAI; 502 → wait, it retries; 000 → fix the internet; no valid token → keeper not running | Generic GCS errors | New problem codes with these exact fixes |
| Never print, show, copy or retype a token or the laptop key | Same rule for credentials | Add the laptop key and token files to the AI's blocked-secrets list. They are never shown in the console, logs or reports |
| If only the old `mono`/`stereo` passes exist, PeakAI hasn't switched the laptop yet: tell the person, and upload the old way | — | Detect this and show it as a problem. The old layout is out of scope unless needed |

### How importing works

1. `python main.py pack import <zip>` (and the Setup wizard in Phase 4) reads the zip without
   running its scripts. It checks the zip, then shows a report: bucket, token service, passes
   (fetched live with the key), path template, activity list, rules found, and anything it
   couldn't understand. Nothing changes until the operator confirms.
2. On confirmation: the laptop key goes to `/etc/sd-mass-uploader/laptop_key` (0600). The rules
   and the device-to-pass table go to the station's pack library. The two documents are kept as
   reference for the problem cards and the AI troubleshooter. The previous pack is kept for
   `pack rollback`.
3. The console shows the active pack (name, version, date), each pass with its token time left,
   and the dashboard-report status.
4. The station doesn't run `install.sh`. It replaces the keeper and report scripts with its own
   implementation of the same protocol. If the PeakAI keeper is already installed on the laptop,
   the station can read its token files instead (decision D11).

### Limits the pack can't change

Cards stay read-only. Cloud files are never overwritten or deleted. The AI keeps the D9
permissions: the pack's documents are reference for it, not extra permissions. Secrets never
reach the console, the logs, the reports or the AI.

### Decisions

D11–D13 and D15 were decided on 2026-11-19 (see section 9). D14 is assumed until confirmed.

Seen on the test card: its `recordings/` holds `.enc`, `.json` and `.imu30` files. That may be
BuildAI, but `.imu30` doesn't match the zip's `.imu.enc` pattern. The classifier would hold it
for a decision rather than guess.

---

## 6. Running at boot ("bootable") and persistence

Two levels are possible. **Decision D1** picks which ones.

**Level A: starts with the machine (recommended, included).**
- `sdu-daemon.service` is enabled in systemd and starts at boot, before anyone logs in. It
  restarts automatically if it crashes. A session that was interrupted by power loss is
  reconciled with GCS on start (existing `reconcile`), then continues.
- The console window and panel indicator start automatically on login (XDG autostart).
- **Kiosk mode (optional setting):** the machine boots straight into the full-screen console
  using a dedicated `sdu-kiosk` user with auto-login. There is no desktop, and nothing else to
  click. This is what an ingest station usually wants.

**Level B: bootable USB image (optional, later phase).**
- A custom live/installable Debian or Ubuntu image (built with `live-build`) with the app
  preinstalled and kiosk mode on, so any laptop can boot from a USB stick into the uploader.
- It needs a persistent partition for the database, settings and credentials.
- This is significant extra work and testing. It is planned as a separate phase only if you want it.

---

## 7. Install, security and operations

- **Package:** `sd-mass-uploader_<version>_amd64.deb`. Install with `sudo apt install ./sd-mass-uploader_*.deb`.
  It creates the `sdu` user, installs the systemd unit, the polkit rule (read-only mount only),
  the desktop entry, icon and autostart entry. Uninstall keeps data unless `purge` is used.
- **Paths (Linux standard):** config `/etc/sd-mass-uploader/config.yaml`; data and database
  `/var/lib/sd-mass-uploader/`; reports `/var/lib/sd-mass-uploader/reports/` (the UI can open
  and export them); logs to journald (`journalctl -u sdu-daemon`) plus rotating files.
- **Credentials:** a service-account key file at `/etc/sd-mass-uploader/credentials.json`,
  owned by `sdu` with mode `0600`, imported through the setup wizard. Or Workload Identity /
  ADC if available (D6). The key is never in config, logs or the UI.
- **API security:** Unix socket plus `127.0.0.1` only, with a random token stored for local users
  in the `sdu-operators` group. No remote access unless D5 says otherwise.
- **Source safety (unchanged rules):** read-only mounts, files opened read-only, no writes,
  `fsck`, format or delete on cards. The existing safety tests are extended with a "mount
  options must contain `ro`" test.
- **Targets:** Ubuntu 24.04 LTS (and later) and Kali Rolling, x86-64. Both ship Python ≥ 3.12.
  Debian 12 only has 3.11, so it is out of scope unless requested.

---

## 8. Delivery phases

Every phase ends with tests run and a short report. Hardware claims are only made after testing on
real hardware, as the master plan requires.

| # | Phase | Output | Exit condition |
| --- | --- | --- | --- |
| 0 | **Linux baseline on real hardware** | Run the existing CLI on an Ubuntu machine with the Norelsys reader and test card | HARDWARE_ACCEPTANCE Stages 1–2 pass on Ubuntu |
| 1 | **Daemon core** | `sdu-daemon`, udev watcher, read-only mounter, card state machine, station session, pause/resume, schema migration | Unit tests; cards appear and disappear correctly on real hardware |
| 2 | **Local API** | REST + WebSocket, token auth, OpenAPI schema | API tests; CLI `status` uses it |
| 3 | **Error Advisor** | Error catalog, problem lifecycle (open → resolved), help pages | Every engine exception maps to an entry (test enforces it) |
| 3B | **AI troubleshooter** | Claude API agent with tiered tools, approval flow, audit log, AI terminal panel | Fixes simulated faults; can't write to a card in any test |
| 3C | **PeakAI integration and context pack** | Pack import (report, confirm, apply, rollback); pass keeper; device classifier and pass routing; new path template and session form; `_info` files; dashboard reporter; PeakAI problem codes | Imports the real zip; uploads a simulated card of each device type to the right pass folder; a bad zip changes nothing; no secret in any output |
| 4 | **Operator Console** | Setup wizard, start session, dashboard, card drawer, problems, history, settings, hardware view | Works against a simulated card fleet; then real cards |
| 5 | **Desktop integration** | App window, panel indicator, notifications, autostart | Tested on Ubuntu GNOME and Kali Xfce |
| 6 | **Boot / kiosk** | systemd hardening, auto-restart, kiosk mode | Power-cut test: reboots and resumes with no duplicates |
| 7 | **Packaging** | `.deb`, install/upgrade/uninstall scripts, operator guide | Clean install on fresh Ubuntu 24.04 and Kali VMs |
| 8 | **Acceptance** | Full-load run (all hubs, 16–20 cards), real bucket | HARDWARE_ACCEPTANCE Stages 3–5 + new UI checks pass |
| 9 | *(optional)* **Bootable USB image** | live-build image with persistence | Boots on 2 different laptops and uploads |

A **card simulator** (fake udev events plus folders acting as cards) is built in Phase 1. It lets
the UI and daemon be developed and demoed on any machine, including this Windows laptop through WSL,
before hardware is available.

---

## 9. Decisions (all accepted as recommended, 2026-11-19)

| ID | Question | Decision |
| --- | --- | --- |
| D1 | "Bootable" means: (A) starts automatically at boot, with optional full-screen kiosk, or (B) also a bootable USB image? | A now, B later only if needed |
| D2 | When a card is inserted during an open session, should upload start **automatically**, or wait for a **Start** click per card? | Automatic, with a session-level switch |
| D3 | Allow the app to mount cards **read-only** by itself? (The master plan listed auto-mounting as out of scope.) | Yes, read-only only. Required for boot/kiosk use |
| D4 | Frontend stack: web UI (React) in a native window + kiosk, or native GTK4? | Web UI |
| D5 | Should the console be viewable from another computer on the network? | No for v1 (local only) |
| D6 | Credential method at the station: service-account key file, or user `gcloud` login? | Key file managed by the setup wizard, if your security policy allows |
| D7 | Target OS versions: Ubuntu 24.04+ and Kali Rolling, x86-64 only? | Yes |
| D8 | Keep Windows support for the CLI? (The app itself is Linux-only.) | Keep CLI working on Windows, no Windows app |
| D10 | Context pack (section 5B): import the user's PeakAI zip, apply only after a report and confirmation, laptop key to a protected file, can't override card safety or D9 | Zip received and read 2026-11-19; design in 5B |
| D11 | Token source | **Decided 2026-11-19:** the station's own pass keeper, same protocol as `peakai-tokens.sh`; laptop key in a protected file |
| D12 | `card-<SERIAL>` | **Decided:** volume serial, 8 hex digits, uppercase, no dash |
| D13 | Recording dates and video hours | **Decided:** read them from device metadata where possible (session.json, metadata.json, MP4 length); otherwise ask in the session form; never guess |
| D14 | `_info` field `vendor` | **Decided 2026-11-19:** `mahadev` for this laptop (`peakai.vendor_code`); `vendor_name` is asked per session |
| D15 | PeakAI dashboard posting | **Decided:** automatic at start and finish, shown in the console; a failed post becomes a problem with a retry |
| D9 | AI troubleshooter permissions (section 5A): tiered tools with operator approval for shell commands, or unrestricted root shell? | **Decided 2026-11-19:** tiered tools; shell commands only after operator approval of the exact command; card writes always blocked |

---

## 10. Risks

- **No Linux hardware testing yet.** Phase 0 must happen first. The adapter has only been tested
  against fixtures.
- **Dirty exFAT cards** (the test card has the dirty bit set): read-only mount works with the kernel
  exFAT driver but logs a warning. The app must not "repair" them.
- **GNOME tray support** depends on the AppIndicator extension. The console window and
  notifications still work without it.
- **Reader quirks:** the Norelsys reader changes USB product ID (bootloader → active). The watcher
  must treat that as the same reader, not a new one.
- **Long sessions:** a daemon running for days needs memory and log-growth checks (Phase 8 soak test).
