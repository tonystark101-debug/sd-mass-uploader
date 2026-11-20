# SD Mass Uploader

Reads many SD cards at once through USB readers and uploads them to Google Cloud Storage. Every
file is checked against the bucket before it counts as done. It runs on Windows and Ubuntu.

Source cards are read-only: this program never writes to, formats, repairs, moves or deletes
anything on them.

## Install on Ubuntu or Kali (the station)

```bash
sudo apt install ./sd-mass-uploader_0.9.0-1_amd64.deb
sudo usermod -aG sdu <operator>        # Each operator who uses the console; then log in again
sdu console                            # Or "SD Uploader" in the app menu
```

The package works on Ubuntu 24.04 or later and Kali Rolling, 64-bit PC. It brings its own Python
packages, so no internet is needed to install. It:
- creates the `sdu` station user;
- installs the app in `/opt/sd-mass-uploader`, with settings in
  `/etc/sd-mass-uploader/config.yaml` and data in `/var/lib/sd-mass-uploader`;
- starts the station service, and starts it again at every boot;
- adds the menu entry and the panel icon at login.

Upgrades keep your settings and data. `sudo apt remove sd-mass-uploader` keeps the data too;
`sudo apt purge sd-mass-uploader` deletes it.

Commands that change the station's own files run as the station user, for example
`sudo -u sdu sdu pack import <zip>`. The console's Setup page does the same without a terminal.

To build the package (on Linux or Windows): `cd web && npm run build`, then
`python scripts/build_deb.py`. The result is in `dist/`.

## Setup (development, Windows or Ubuntu)

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
.\.venv\Scripts\Activate.ps1
```

Ubuntu:

```bash
bash scripts/ubuntu/setup.sh
source .venv/bin/activate
```

Then:

1. Set `google_cloud.project_id` and `google_cloud.bucket_name` in `config.yaml`.
2. Sign in with Application Default Credentials:

   ```bash
   gcloud auth application-default login
   ```

   Never put keys in `config.yaml`. In production, use a service account outside this folder.
3. Confirm the bucket works. This uploads one small generated file to a test prefix and checks
   it. The file stays in the bucket.

   ```bash
   python main.py gcs-test
   ```

The identity needs `storage.objects.create` and `storage.objects.get` on the bucket
(`roles/storage.objectUser` covers both, or a narrower custom role). Keep the bucket private.

## Everyday workflow

1. Connect the powered hubs, readers, cards and Ethernet.
2. Check the hardware. Look for readers below 5 Gbps and anything not `ACTIVE`.

   ```bash
   python main.py check
   ```

3. Review what is on the cards.

   ```bash
   python main.py scan --files
   ```

4. Upload. It asks for the session details, shows the destination and totals, and starts only
   after you type `yes`.

   ```bash
   python main.py upload
   ```

5. If it did not end in `SUCCESS`, follow the command it prints. To re-check an old session:

   ```bash
   python main.py verify --session SESSION_ID
   ```

To skip the prompts, keep the answers in a file (see `session.example.json`):

```bash
python main.py upload --metadata-file session.json
```

## Operator Console

The console is the screen operators use. It works while the station runs:

```bash
python main.py console              # Opens it in the browser, already signed in
python main.py console --print-link # Or print the sign-in link
```

The link carries the API token after `#`, which the browser never sends to the station or
writes to any log. The token is kept only for that browser tab. The console works only on this
computer.

- **Dashboard:** live counts you can click to filter (inserted, uploading, waiting, uploaded,
  failed, paused, removed, needs you), overall progress with speed and time left, and one tile
  per card that needs attention first. The side column has problems with fix buttons and recent
  activity. At the bottom: pause or resume everything, start ready cards, automatic upload, and
  end session.
- **Card panel:** the card's state, actions (pause, resume, start, confirm, retry), PeakAI
  placement ("Where it goes"), the device choice for held cards, and every file with its status
  and last error.
- **Problems:** every open problem with its steps, fix button and **Ask AI**.
- **AI panel:** the troubleshooter's conversation. Each command it wants to run shows with its
  reason and **Allow once / Decline**.
- **History, Hardware, Setup:** past sessions and reports, the USB tree with link speeds, and
  the PeakAI pack (passes, token time, dashboard entries, import a new pack) with the settings.

If the station stops answering, a red banner says the view may be out of date, and the console
reconnects by itself. It follows the computer's light or dark theme, and every button is
large enough to use on a touch screen.

To change the console, edit `web/src` and rebuild with `cd web && npm install && npm run build`.
The built files go to `src/web_dist/`, so stations never need Node.

### On the Linux desktop

```bash
python main.py desktop            # Panel icon with counts, menu and notifications
python main.py desktop --window   # The console in its own window (--fullscreen for a big screen)
python main.py desktop --check    # What this computer supports, and what to install
```

The **panel icon** shows uploading, uploaded, failed and needs-you counts, for example
`2↑ 5✓ 1✕ 1?`. Its colour shows whether the station is busy, fine, has a problem or is not
answering. The menu lists the counts and the top problem, and has Open console and Pause all or
Resume all.

**Notifications** come only when you are needed:
- a problem that stops uploads or needs a person;
- a card that is safe to remove (several at once become one notification);
- a session ending.

Repeats are held back for two minutes.

Once installed, "SD Uploader" is in the app menu, and the panel icon starts at login.
`packaging/linux/README.md` lists the files and the system packages they need. On stock
Ubuntu GNOME, the panel icon relies on the AppIndicator extension, which Ubuntu turns on by
default. Without it, notifications still work.

### Start-up, crashes and kiosk mode

On an installed station, the background service starts at boot, before anyone logs in:
- **Crash or power cut:** it starts again by itself. Files that were mid-upload are checked
  against the bucket when their card is seen, then finished. Nothing that was already verified
  is sent again.
- **A hung station:** if its loop stops for 5 minutes, systemd's watchdog restarts it. A pass
  that is only waiting on the network is not treated as hung.
- **Status:** `systemctl status sdu-daemon` shows a one-line summary, for example "3 inserted,
  1 uploading, 2 uploaded, 0 failed".

**Kiosk mode** makes the computer boot straight into the full-screen console, with no desktop
to click around in:

```bash
sudo bash scripts/ubuntu/kiosk.sh enable    # Asks first; takes effect at the next boot
sudo bash scripts/ubuntu/kiosk.sh disable   # Back to the normal desktop
bash scripts/ubuntu/kiosk.sh status
```

It runs the console under a dedicated `sdu-kiosk` user in `cage`, a minimal kiosk display
server, and shows the console again if it ever closes. For maintenance, Ctrl+Alt+F2 still gives
a text login.

## Station mode (always-on service)

The station is a background service that is always on. You open a session once, then insert
cards whenever you like. Each card is mounted read-only, scanned and uploaded automatically, and
it is safe to remove when it shows `UPLOADED`. A desktop console comes later (see
[docs/LINUX_APP_PLAN.md](docs/LINUX_APP_PLAN.md)). For now, it is controlled from the CLI.

```bash
python main.py daemon                                    # Run it (systemd runs it on Linux)
python main.py station open --metadata-file session.json # Cards inserted from now on join
python main.py station status                            # Live counts and every card
python main.py station pause CARD_ID                     # Or resume, retry, start, confirm
python main.py station pause-all                         # Or resume-all
python main.py station close                             # End the session, write reports
```

To try it without readers, use simulated cards. Each folder is one card, and removing it renames
the folder:

```bash
python main.py daemon --simulate sim
python main.py simulate create CARD01 --files 50 --size-kib 512 --dir sim
python main.py simulate remove CARD01 --dir sim
python main.py simulate insert CARD01 --dir sim
```

| Card state | Meaning |
| --- | --- |
| `NEEDS_CONFIRMATION` | Might be a USB drive, or shares a serial with another card. `station confirm` |
| `SCANNING` | Listing its files |
| `READY` | Scanned, waiting for `station start` (only when automatic upload is off) |
| `QUEUED` / `UPLOADING` | Waiting for a worker / being sent |
| `PAUSING` / `PAUSED` | Files in flight are finishing / held until resumed |
| `UPLOADED` | Every file verified in GCS. Safe to remove |
| `UPLOADED_WITH_ISSUES` / `FAILED` | Some / no files made it. `station retry` |
| `NEEDS_ATTENTION` | Could not be scanned |
| `REMOVED` | Taken out early. Put the same card back in any slot and it continues |

### PeakAI uploads (context pack)

PeakAI's zip is the manual for where and how to upload. Import it once:

```bash
python main.py pack import PeakAI-Uploader-v2_<laptop>.zip   # Shows a report; apply with 'yes'
python main.py pack status
python main.py pack rollback                                 # Back to the previous pack
```

The import reads the zip and runs nothing from it. It checks the rules and asks PeakAI which
passes this laptop has. Only after you confirm does it store the laptop key privately in
`peakai.key_file` and keep PeakAI's documents. Set `peakai.vendor_code` in `config.yaml` (the
`vendor` value of `_info` files) and restart the station. From then on:

- **Tokens:** the station keeps a 1-hour token per pass from PeakAI's token service, renewed
  with 35 minutes left. `peakai-tokens.sh` and `install.sh` are not needed.
- **Passes:** each part of a card goes to the pass of the device that recorded it, read from
  the files on the card. If a card holds files that match no device, it waits as
  `NEEDS_DECISION`. Answer with `station place CARD --device "Stereo hat"` or `--skip`. The
  station never guesses.
- **Folders:** `gs://<bucket>/<pass folder>/<activity>/<city>/<upload day, India time>/card-<SERIAL>/`
  holds the card exactly as it is. `Android/` folders are not uploaded.
- **Session details:** `activity` (from PeakAI's list), `city` (lowercase), `vendor_name`
  (required, ask), and optionally `site`, `batch`, `recorded_from`, `recorded_to` and
  `video_hours`. Phone numbers are refused everywhere.
- **`_info` files:** when a part is fully verified, a new `_info/<machine>_<time>.json` is
  written in its card folder.
- **Dashboard:** one PeakAI dashboard entry per pass, sent as `uploading` when cards start and
  `uploaded` when all are done. `station status` shows each pass's token time and each
  dashboard entry. A failed entry is a problem; `station retry-reports` sends it again.

```json
{"activity": "garage", "city": "bijapur", "vendor_name": "Narayan", "batch": "batch-9"}
```

### Problems and how to fix them

When something goes wrong, `station status` lists a **PROBLEMS** section, most severe first.
Each problem has a code (such as `E-CARD-REMOVED`), a plain explanation, numbered steps and the
command that fixes it. A problem disappears by itself when its cause is gone: the card is back,
the retry succeeded, or the sign-in was renewed. Every engine error has an entry. The full list
is in [docs/PROBLEMS.md](docs/PROBLEMS.md), and `python main.py problems` prints the codes.

### AI troubleshooter

The station can ask Claude to diagnose and fix a problem:

```bash
python main.py ai --problem E-CARD-REMOVED:sim-CARD01   # The id shown under each problem
python main.py ai "Cards are not appearing"
```

It works like a terminal agent. You see what it checks, every command it wants to run, and its
explanation. Then you can ask follow-up questions. Its permissions are tiered (decision D9):

| Tier | What | Runs |
| --- | --- | --- |
| Diagnose | Station state, problems, card files, its log, fixed checks (`lsusb -t`, `lsblk`, `findmnt`, network, journal, GCS access) | At once |
| Station actions | Retry or pause a card, resume uploads | At once (`ai.auto_station_actions: false` makes them ask). Confirming a device always asks |
| Any other command | One program, no shell, as the station's user | Only after you approve the exact command |
| Never | Formatting, repair, partitioning, `dd`, mounting, any write that names a card path, shells and interpreters, reading credentials | Refused in code, even if approved |

Every request, command, approval and result is saved to the `ai_audit` table. Personal session
details (operator, place, description) are hidden from the AI unless `ai.redact_personal` is
off. Each session has a token budget (`ai.token_budget`) and a step limit. The AI uses the
model `claude-opus-5-5` with server-side refusal fallback.

Set the key in the environment as `ANTHROPIC_API_KEY`. On an installed station, put it in
`/etc/sd-mass-uploader/ai.env`. Never put it in `config.yaml`. Without a key, the AI says how to
set one up, and nothing else is affected. The built-in fix steps always work, even offline.

### Local API

While the station runs, it serves an API on `http://127.0.0.1:8765` (loopback only). The console
and the CLI use it. Every request except `/api/v1/health` needs
`Authorization: Bearer <token>`, and the token is in `data/station/api-token`. The interactive
docs are at `/api/docs`. The main endpoints:

| Endpoint | What it does |
| --- | --- |
| `GET /api/v1/state` | Everything the dashboard shows: session, counters, cards, progress, problems |
| `GET /api/v1/cards/{id}` | One card, a page of its files (problems first), and its scan errors |
| `POST /api/v1/session`, `/session/close` | Open or end a session |
| `POST /api/v1/cards/{id}/pause` (`resume`, `start`, `confirm`, `retry`) | Card controls |
| `POST /api/v1/session/pause-all` (`resume-all`, `start-all`) | Whole-station controls |
| `GET /api/v1/sessions`, `/sessions/{id}/report` | History and reports |
| `GET /api/v1/problems`, `/problems/catalog/{code}` | Open problems with fixes, and help pages |
| `POST /api/v1/ai/sessions`, `/ai/sessions/{id}/approvals/{approval_id}` | Ask the AI; approve or decline its commands |
| `WS /api/v1/ws` | Live stream: send `{"type": "auth", "token": ...}`, then receive state and events |

`station` commands go through the API. If it can't be reached, they fall back to a command queue
in the database, so a running station still gets them.

The counters at the top of `station status` are built from these states. Inserted means cards
present now. Uploaded means every file is verified. Failed covers failed, finished with issues
and needs attention. On Linux, `packaging/linux/` holds the systemd unit and the polkit rule
that allows read-only mounts only.

## Commands

| Command | What it does |
| --- | --- |
| `check` | Refresh the hardware inventory. `--all` shows internal disks, `--json` prints raw data |
| `scan` | List volumes. `--files` adds a full file scan, `--sha256` also hashes, `--path` scans a folder |
| `status` | Show the last saved state and how old it is. It does not query devices |
| `monitor` | Watch for readers and cards being added or removed |
| `upload` | Metadata, selection, preview, confirmation, upload. `--workers N` overrides concurrency |
| `retry` | Reconcile with GCS, then resume or retry a session (latest by default) |
| `verify` | Recheck objects in GCS. `--deep` also re-reads the source cards |
| `report` | Rewrite the JSON, CSV and error log for a session |
| `stop` | Ask a running upload to stop after the files in progress |
| `gcs-test` | Upload and check one small test file |
| `benchmark` | Measure simultaneous read speed from all cards |
| `daemon` | Run the always-on station (see Station mode) |
| `station` | Send the station a command, or `station status` |
| `simulate` | Create, remove or insert simulated cards for testing |
| `problems` | List every problem code; `--markdown` prints the help page |
| `ai` | Ask the AI troubleshooter about a problem or a question |

Reports are written to `data/reports/SESSION_ID/`. Logs are in `logs/`.

## How to read the results

### Device status

| Status | Meaning |
| --- | --- |
| `ACTIVE` | The card is detected and readable |
| `EMPTY_READER` | The OS confirms no medium. Never guessed from a missing volume |
| `READER_PRESENT_SLOT_UNKNOWN` | The reader is visible but says nothing about its slot |
| `UNMOUNTED_STORAGE` | A card is present with no usable mount. Mount it yourself |
| `INACCESSIBLE` | Access was denied |
| `ERROR` | A read or detection error, or a card that stopped responding |
| `DISCONNECTED` | Seen in the previous inventory, absent now |
| `UNKNOWN` | Not enough evidence to say |

### File status

| Status | Meaning | What to do |
| --- | --- | --- |
| `VERIFIED` | GCS has the same size and CRC32C | Nothing |
| `UPLOADED_UNVERIFIED` | Uploaded with verification turned off | Run `verify` |
| `FAILED` | Network retries ran out, or the card went away | Run `retry` |
| `FAILED_PERMANENT` | The file changed or vanished after the scan | Check the source, then start a new session |
| `CONFLICT` | A different object is at that path, or a checksum did not match | Investigate, then `retry --include-conflicts` |
| `UNCERTAIN` | Interrupted, and the card is not here to prove the object matches | Reattach the card, run `retry` |

A session is `SUCCESS` only if every file is `VERIFIED` and the scan found no unreadable files.

## Behavior worth knowing

- **Cards are tracked by volume serial, not drive letter.** After a replug, the card is found
  again wherever it mounted. A different card in the same slot is refused.
- **Internal disks can never be selected.** Only volumes proven to be on a USB, SD or MMC bus are
  offered.
- **Some cards need confirmation.** A device that does not call itself a card reader could be a
  USB drive, and a cloned card shares a serial with another. These are left out of `all` and
  `--yes`, and are uploaded only if you choose them by number or with `--cards`.
- **Nothing is overwritten silently.** Uploads use a GCS generation precondition. If an object
  exists with identical content it is accepted. If different, the file becomes `CONFLICT`, or
  sits beside it under `duplicate_policy: new_path`. Overwriting needs
  `upload.overwrite_existing: true` plus `--allow-overwrite --overwrite-reason "..."`.
- **Stopping.** `stop` or one Ctrl+C finishes the files in progress, so it can take a while on a
  large file. A second Ctrl+C abandons them after the current read, and they stay queued.
- **Crashes.** After a crash, `retry` is refused for up to a minute, in case the old process is
  still running. It then checks GCS first, so finished files are not sent again.
- **Network loss.** Each file retries with backoff up to `max_retries`, then becomes `FAILED`. A
  long outage therefore ends the run; `retry` picks it up. Authentication and permission errors
  stop the session at once.
- **Personal data.** `operator_name` and `place_of_recording` are not copied to object metadata
  unless added to `upload.object_metadata_fields`. They do appear in the manifest stored beside
  the files.

## Tuning

Start from `benchmark`, which shows what the cards, hubs and controllers can deliver together.
Then compare real uploads with `--workers 4`, `8` and `12`, using the MB/s in each report. Keep
the best *stable* result, not the fastest single run.

Peak memory is about `max_workers × chunk_size_mib`, so 12 workers at 64 MiB can hold 768 MiB.

## Maintenance

Back up `data/database/uploader.db` and `data/reports/` while nothing is uploading. Agree bucket
versioning, retention, lifecycle and data residency with the data owner before production use.
Test objects from `gcs-test` and benchmarks are not cleaned up automatically.

## Development

```bash
python -m pytest
python -m ruff check .
```

The integration tests run the real Google client against `tests/integration/fake_gcs_server.py`,
a local server that speaks the storage protocol. Passing them shows this code drives the client
correctly. It does not show how real GCS, IAM or your network behave.

Physical testing is separate: see [docs/HARDWARE_ACCEPTANCE.md](docs/HARDWARE_ACCEPTANCE.md).

## Known limitations

- **Windows speed cap:** SuperSpeedPlus shows as 10 Gbps, the lowest rate in that class, because
  the Windows query does not report the exact rate.
- **Hubs on Ubuntu:** a USB 3 hub appears twice, once per bus, so hub counts can be double.
- **`monitor` polls.** On Windows one pass takes about 10 seconds, so it is not instant.
- **Access times:** reading can update a file's last-access time where the filesystem records it.
  Contents and modified times are unchanged. On Ubuntu the program avoids this when it can.
- **CPU and RAM** are not recorded by `benchmark`. Read them from Task Manager or `top`.
- **PeakAI uploads have not run against the real bucket yet.** The real pack imports and the
  real token service accepts the key. Tokens, uploads, `_info` files and dashboard entries were
  tested against local stand-ins only.
- **The `.deb` has not been installed on a real system yet.** It is built and checked
  file by file (ownership, permissions, contents, scripts, settings), but `dpkg` is not
  available on the Windows build laptop.
- **Kiosk mode and the systemd watchdog have not run on Linux yet.** The power-cut recovery is
  tested by killing the real station process mid-upload (against the local storage stand-in).
- **The desktop pieces have not run on Linux yet.** The panel icon, notifications and console
  window are only exercised through tests of what they show; GTK is not available on the Windows
  build laptop.
- **Station mode on real hardware is untested.** Automatic read-only mounting (udisks2), udev
  events and the systemd unit have only been checked with mocks and simulated cards.
- **`stop` in station mode** pauses every card instead of ending the session.
- **Resumable sessions** are not kept across restarts. A large file interrupted by a crash starts
  again from zero, and an unfinished GCS session simply expires.
