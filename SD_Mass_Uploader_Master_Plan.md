# Multi-SD-Card Mass Upload System

## Master Project Plan, Architecture, Requirements, Roadmap, and Coding-Agent Instructions

**Document type:** Engineering master specification\
**Project status:** Planning / Phase 1 implementation\
**Primary platforms:** Windows 10/11 and Ubuntu Linux\
**Cloud destination:** Google Cloud Storage (GCS)\
**Primary language:** Python 3.12 (use a supported Python version
consistently across both platforms)\
**Initial interface:** Command-line interface (CLI)\
**Local persistence:** SQLite\
**Source-media policy:** Read-only; never modify source SD cards

------------------------------------------------------------------------

# 1. Executive Summary

Build a reliable, cross-platform application that detects multiple USB
card readers and inserted micro-SD cards connected through one or more
powered USB 3.x hubs, inventories the storage devices, scans their
files, collects an operator-provided metadata template, and uploads the
data directly from the laptop to a Google Cloud Storage bucket.

The laptop is the **local controller**. It detects USB hardware, reads
local media, manages upload queues, verifies transfers, records status,
and reports errors. Google Cloud Storage is the **destination**. A cloud
service is not required to detect locally attached USB devices.

The application must work on Windows and Ubuntu using the same CLI and
normalized internal data model. Operating-system-specific detection
adapters will handle platform differences.

The project must prioritize: 1. Source-data safety. 2. Accurate device
inventory. 3. Reliable uploads and recovery. 4. Verifiable data
integrity. 5. Useful error reporting. 6. Measured throughput
optimization.

Do not optimize for theoretical maximum speed at the expense of
reliability.

------------------------------------------------------------------------

# 2. Project Goals

## 2.1 Functional goals

The completed application should:

-   Detect USB hubs where the operating system exposes them.
-   Detect connected USB card-reader devices.
-   Detect storage devices and SD-card media exposed by the operating
    system.
-   Identify mounted volumes and their drive letters or mount points.
-   Report card capacity, free space, filesystem, device model and
    available identifiers.
-   Report USB negotiated connection speed when reliable
    operating-system information is available.
-   Distinguish hubs, readers, physical disks, partitions/volumes and
    files.
-   Distinguish an inserted card from an empty reader when the hardware
    and OS expose enough information.
-   Scan files and calculate file counts and total bytes.
-   Collect a reusable upload-session metadata template.
-   Upload files to a configured GCS bucket.
-   Organize objects under predictable project/date/session/card paths.
-   Support concurrent uploads with configurable limits.
-   Use resumable uploads for large objects where supported.
-   Retry transient failures safely.
-   Verify uploaded objects and record checksums.
-   Persist session, device, file and upload state in SQLite.
-   Recover pending or interrupted work after application restart.
-   Produce human-readable terminal output and JSON/CSV reports.
-   Work on both Windows and Ubuntu.

## 2.2 Non-functional goals

-   Read-only access to source media.
-   No silent data loss or silent overwrites.
-   Clear logs and actionable errors.
-   Bounded memory use; do not load entire large files into RAM.
-   Configurable concurrency and chunk sizes.
-   Graceful shutdown.
-   Unit tests that do not require physical hardware.
-   Separate physical hardware acceptance tests.
-   No credentials committed to source control.
-   No assumption that all USB 3.x-labelled devices negotiate at USB 3.x
    speed.

## 2.3 Out of scope for the first release

-   Formatting, partitioning, repairing or modifying SD cards.
-   Deleting or moving source files after upload.
-   Automatically mounting or unmounting devices.
-   A web dashboard or GUI.
-   Editing media or transcoding video.
-   Cloud-side USB detection.
-   Guaranteed detection of an empty card slot when the reader does not
    expose slot state.
-   Automatic deduplication based only on filenames.
-   Automatic deletion of old cloud objects.

------------------------------------------------------------------------

# 3. System Overview

``` text
  Multiple micro-SD cards
           |
           v
  Individual USB card readers
           |
           v
  One or more powered USB 3.x hubs
           |
           v
  Laptop USB ports / controllers
           |
           v
  Windows or Ubuntu operating system
           |
           v
  Python local controller
    |       |        |        |
    |       |        |        +--> Logs and reports
    |       |        +-----------> SQLite state database
    |       +--------------------> Device and file inventory
    +----------------------------> Upload queue and workers
                                      |
                                      v
                              Ethernet / Internet
                                      |
                                      v
                            Google Cloud Storage
```

**Core principle:** Device detection and file reading happen locally.
The application uploads file contents from the laptop to GCS. Do not
design the cloud bucket as a mechanism for detecting local card readers.

------------------------------------------------------------------------

# 4. Hardware and Operating Environment

## 4.1 Known project configuration

-   Windows and Ubuntu are both required.
-   Two or more powered USB 3.0 hubs may be used.
-   The number of cards is variable.
-   Individual USB card readers are used.
-   A Google Cloud Storage bucket already exists.
-   The laptop is connected to the network through Ethernet.
-   The expected operating scale is approximately 16--20 readers/cards,
    but the software must not hard-code this limit.

## 4.2 Hardware facts to validate

The coding agent must not assume: - Every reader has a unique serial
number. - Every empty reader is visible to the OS. - A reader's USB
speed equals the SD card's read speed. - Every port on a USB 3.x hub
runs at USB 3.x speed. - Two hubs use separate host controllers. - A
mounted volume always corresponds one-to-one with a physical reader. - A
USB device name reliably identifies a card reader.

The actual topology, negotiated link speed, hub power, reader model,
card model and SD-card read throughput must be measured on the physical
setup.

## 4.3 USB speed terminology

Record these as separate concepts where possible:

-   **Advertised capability:** USB capability supported by a device or
    hub.
-   **Negotiated link speed:** Speed negotiated on a particular USB
    connection.
-   **Card read throughput:** Actual rate at which the SD card and
    reader deliver data.
-   **Network upload throughput:** Actual rate at which the laptop sends
    data to GCS.

Do not report a device as "USB 3.0 active" merely because its product
description says USB 3.0.

On Linux, `lsusb -t` commonly provides useful topology and speed
information. On Windows, use reliable USB
topology/connection-information APIs or a documented diagnostic utility
such as Microsoft USBView where appropriate. Generic PnP device
enumeration alone does not prove negotiated speed.

------------------------------------------------------------------------

# 5. Recommended Technology Stack

  ------------------------------------------------------------------------
  Area                    Recommended technology   Notes
  ----------------------- ------------------------ -----------------------
  Main application        Python 3.12              Shared cross-platform
                                                   business logic

  CLI                     argparse or Typer        Pick one and use
                                                   consistently

  Windows inventory       PowerShell CIM/WMI, PnP  Use read-only queries
                          APIs, Windows storage    
                          APIs                     

  Linux inventory         `lsblk`, `lsusb`, sysfs, Use read-only queries
                          udev/pyudev              

  USB topology            OS-specific adapter      Do not infer
                                                   unsupported information

  File scanning           `os.scandir`, `pathlib`, Stream directory
                          `stat`                   traversal

  Cloud upload            `google-cloud-storage`   GCS client library

  Concurrency             `concurrent.futures`     Bounded worker pool

  Persistent state        SQLite                   Transactional upload
                                                   queue

  Configuration           YAML                     Non-secret
                                                   configuration only

  Logging                 Python `logging`         Rotating logs

  Testing                 pytest                   Mock OS commands and
                                                   cloud client

  Checksums               GCS CRC32C plus optional Validate content
                          SHA-256                  integrity

  Packaging               `pyproject.toml`         Reproducible
                          preferred                dependencies
  ------------------------------------------------------------------------

Avoid installing unnecessary libraries. Prefer standard-library features
unless a dependency has a clear role.

------------------------------------------------------------------------

# 6. High-Level Architecture

Use modular layers with explicit interfaces.

``` text
CLI
 |
 +-- Application / Use Cases
      |
      +-- Device Service
      |     +-- Windows Adapter
      |     +-- Linux Adapter
      |     +-- USB Topology Adapter
      |
      +-- Inventory Service
      |     +-- Volume Scanner
      |     +-- File Scanner
      |
      +-- Session Metadata Service
      |
      +-- Upload Service
      |     +-- GCS Client
      |     +-- Upload Queue
      |     +-- Bounded Workers
      |     +-- Retry Policy
      |
      +-- Verification Service
      |
      +-- Reporting Service
      |
      +-- SQLite Repository
```

## 6.1 Architectural rules

1.  Keep OS-specific commands inside the platform adapters.
2.  Keep cloud SDK calls inside the GCS adapter.
3.  Keep business logic independent of Windows and Linux command output.
4.  Normalize all device results into common Python models.
5.  Keep database access behind repository functions.
6.  Use dependency injection where it makes tests easier.
7.  Do not mix terminal printing with detection or upload logic.
8.  Never store credentials in `config.yaml`, source files or logs.
9.  Use typed data models and clear error classes.
10. Keep the first release CLI-based; a GUI is not required.

------------------------------------------------------------------------

# 7. Device and Storage Data Model

The system must keep separate concepts for USB topology, physical
readers, storage devices, partitions/volumes and files.

## 7.1 USB topology record

Suggested fields:

-   `topology_id`
-   `parent_topology_id`
-   `device_path`
-   `vendor_id`
-   `product_id`
-   `manufacturer`
-   `product_name`
-   `serial_number` (nullable)
-   `port_path` or platform location path
-   `advertised_capability` (if known)
-   `negotiated_speed_mbps` (nullable)
-   `speed_source`
-   `last_seen_at`

## 7.2 Reader record

Suggested fields:

-   `reader_id`
-   `platform_device_id`
-   `vendor_id`
-   `product_id`
-   `serial_number` (nullable)
-   `model`
-   `topology_path`
-   `status`
-   `last_seen_at`

Do not identify readers solely by drive letter. Drive letters and mount
points can change.

## 7.3 Storage device record

Suggested fields:

-   `storage_id`
-   `platform_device_id`
-   `reader_id` (nullable if correlation is uncertain)
-   `model`
-   `serial_number` (nullable)
-   `capacity_bytes`
-   `removable`
-   `read_only`
-   `bus_type`
-   `status`

## 7.4 Volume record

Suggested fields:

-   `volume_id`
-   `storage_id`
-   `partition_id`
-   `mount_point`
-   `drive_letter`
-   `filesystem`
-   `volume_label`
-   `capacity_bytes`
-   `free_bytes`
-   `is_mounted`
-   `access_status`

## 7.5 Device status vocabulary

Use explicit statuses: - `ACTIVE`: storage is detected and accessible. -
`EMPTY_READER`: reader is detected and the OS/hardware confirms no card
is inserted. - `READER_PRESENT_SLOT_UNKNOWN`: reader is visible but
slot/card presence cannot be determined. - `UNMOUNTED_STORAGE`: storage
device is detected but has no accessible mounted volume. -
`INACCESSIBLE`: access denied or device cannot be read. - `ERROR`:
detection or I/O error. - `DISCONNECTED`: previously observed device is
no longer present. - `UNKNOWN`: insufficient evidence for a reliable
classification.

Do not label a reader empty merely because no mounted volume was found.
It may contain an unmounted, unsupported, damaged or inaccessible card.

------------------------------------------------------------------------

# 8. Phase 1 -- Hardware and SD Card Detection

## 8.1 Objective

Build and validate a read-only cross-platform inventory engine.

Required CLI commands:

``` bash
python main.py check
python main.py scan
python main.py status
```

### `check`

-   Detect operating system.
-   Enumerate USB devices and hubs where possible.
-   Enumerate reader candidates.
-   Enumerate storage devices.
-   Correlate devices conservatively.
-   Identify available USB topology and negotiated speed information.
-   Print a concise summary and detailed inventory.
-   Save a timestamped inventory snapshot.

### `scan`

-   Inventory mounted volumes.
-   Read capacity, free space, filesystem and labels.
-   Optionally enumerate files and total file sizes.
-   Never write to source media.
-   Clearly distinguish storage scan from full file scan.

### `status`

-   Display the latest known inventory and application state.
-   Indicate when the inventory was last refreshed.
-   Do not present stale data as live data.

## 8.2 Windows adapter

Use read-only PowerShell/CIM/PnP/storage queries. Candidate discovery
commands for manual validation:

``` powershell
Get-CimInstance Win32_DiskDrive |
  Select-Object DeviceID, Model, InterfaceType, MediaType, Size, PNPDeviceID

Get-PnpDevice -PresentOnly -Class USB |
  Select-Object Status, FriendlyName, InstanceId

Get-Disk |
  Select-Object Number, FriendlyName, BusType, OperationalStatus,
                Size, IsOffline, IsReadOnly

Get-Partition |
  Select-Object DiskNumber, PartitionNumber, DriveLetter, Type, Size

Get-Volume |
  Select-Object DriveLetter, FileSystem, FileSystemLabel,
                Size, SizeRemaining, HealthStatus
```

Implementation requirements: - Invoke PowerShell safely with argument
lists, timeouts and captured stderr. - Prefer structured output such as
JSON over parsing formatted tables. - Do not rely on localized display
text when a stable property exists. - Correlate disk, partition and
volume identifiers using stable platform fields. - Capture errors per
device; one failure must not terminate the entire inventory. - Do not
execute destructive storage commands. - Use a reliable Windows USB
topology method for negotiated connection speed. If unavailable, report
`UNKNOWN`.

## 8.3 Ubuntu adapter

Use read-only commands and system interfaces.

Manual validation commands:

``` bash
lsusb
lsusb -t

lsblk -J -b -o \
  NAME,PATH,TYPE,TRAN,SIZE,RM,RO,FSTYPE,LABEL,UUID,MOUNTPOINTS,MODEL,SERIAL

findmnt --json
```

Implementation requirements: - Parse JSON output from `lsblk` where
supported. - Use `lsusb -t` or sysfs for USB topology and speed
information. - Use pyudev for device discovery/events where
appropriate. - Handle permission restrictions. - Do not run `mount`,
`umount`, `mkfs`, `fsck` or partition-writing commands. - Do not assume
a device is empty merely because it is absent from `lsblk`.

## 8.4 Cross-platform normalization

Both adapters must return the same internal models. Preserve raw
platform properties for diagnostics.

Unknown values must be represented as null/unknown rather than guessed.

## 8.5 Phase 1 acceptance criteria

-   USB hubs are not counted as card readers.
-   Readers and storage devices are separate records.
-   Mounted volumes are associated with storage only when evidence
    supports the relationship.
-   Empty readers are reported only when confirmed.
-   Unknown slot state is represented honestly.
-   Capacity and free space are reported in bytes internally.
-   Human-readable units are only a presentation layer.
-   USB speed includes its source and confidence.
-   Device removal does not crash the application.
-   Inventory snapshots are persisted.
-   Windows and Ubuntu produce equivalent normalized schemas.
-   No source media is modified.

------------------------------------------------------------------------

# 9. Phase 2 -- File Inventory and Scanning

## 9.1 Objective

Build a read-only file inventory for every accessible mounted card.

For each file, record: - Stable scan/session reference. - Source
storage/volume reference. - Relative path. - File name. - Extension. -
Size in bytes. - Modification timestamp where available. - Readability
status. - Optional content hash, only when explicitly enabled.

For each card, calculate: - Total file count. - Total file bytes. - File
counts by extension. - Total directory count. - Scan errors. -
Unreadable file count.

## 9.2 Requirements

-   Use streaming directory traversal; do not load full directory trees
    into memory.
-   Handle long paths on Windows.
-   Handle Unicode file names.
-   Handle permission errors.
-   Detect a card disappearing during scanning.
-   Avoid following symlinks/reparse points outside the source volume
    unless explicitly configured.
-   Do not modify timestamps or source contents.
-   Do not calculate SHA-256 for every file by default if it would make
    scanning unacceptably slow. Make hashing an explicit policy.

## 9.3 Acceptance criteria

-   Correct file count and total bytes on test media.
-   Errors are recorded per path.
-   Disconnected media is reported.
-   No source file is changed.
-   Large directory trees do not exhaust memory.

------------------------------------------------------------------------

# 10. Phase 3 -- Google Cloud Storage Integration

## 10.1 Objective

Connect to the existing GCS bucket and upload one controlled test file.

Use the official `google-cloud-storage` Python client.

## 10.2 Authentication

Prefer Google Application Default Credentials (ADC) or an
organization-approved identity method.

Never: - Hard-code service-account keys. - Commit credentials to Git. -
Print secrets in logs. - Put private keys in `config.yaml`.

For local development, configure ADC using the approved Google Cloud
authentication workflow. For production, use an appropriately scoped
service account or workload identity arrangement based on the deployment
environment.

## 10.3 Bucket configuration

Store non-secret configuration such as: - GCP project ID. - Bucket
name. - Root prefix. - Upload chunk size. - Concurrency limit. - Retry
limits. - Duplicate policy. - Verification policy.

Validate bucket access before starting a session.

## 10.4 Single-file upload test

Before bulk upload: 1. Create a small test file outside all SD cards. 2.
Upload it to a test prefix. 3. Read back object metadata. 4. Verify
object size and checksum. 5. Record the result. 6. Do not delete source
data.

## 10.5 Object naming

Use deterministic, sanitized object paths. Do not allow untrusted
metadata to inject `../`, control characters or arbitrary bucket paths.

Recommended structure:

``` text
gs://BUCKET/
  PROJECT/
    YYYY/
      MM/
        DD/
          SESSION_ID/
            CARD_ID/
              relative/path/to/file.ext
            manifests/
              session-manifest.json
              card-inventory.json
```

Preserve the source directory hierarchy beneath each card directory.

## 10.6 Duplicate and overwrite policy

Default to **no silent overwrite**.

Use unique session IDs and GCS generation preconditions where
appropriate. Define one explicit policy: - Fail if the destination
object already exists. - Create a new unique object path. - Overwrite
only with explicit operator approval and a documented reason.

Never use a filename alone as a unique identity.

------------------------------------------------------------------------

# 11. Phase 4 -- Mass Upload Engine

## 11.1 Objective

Upload files from multiple cards concurrently while maintaining bounded
resource use and reliable progress tracking.

## 11.2 Concurrency

Use a bounded worker pool, initially testing 4 workers, then 8 and 12,
subject to hardware and network results.

Do not assume one worker per card or 20 simultaneous workers is optimal.

Tune based on: - Number of readers. - SD-card read speed. - Hub and
host-controller topology. - Laptop CPU and memory. - Ethernet link and
actual Internet upload speed. - GCS performance. - File size
distribution.

Concurrency must be configurable.

## 11.3 Resumable uploads

Use GCS resumable upload support for large files. Configure chunk size
according to the client library requirements; GCS resumable chunk sizes
should be multiples of 256 KiB.

Do not implement a custom resumable protocol unless the official client
cannot meet a documented requirement.

Persist enough state to recover safely. Do not assume a local database
record proves that a remote resumable session is still valid.

## 11.4 Retry policy

Retry only transient errors, such as: - Temporary network failure. -
Rate limiting. - Retryable server errors. - Connection resets.

Use exponential backoff with jitter and a maximum retry count/elapsed
time.

Do not endlessly retry: - Invalid credentials. - Permission denied. -
Invalid bucket/path configuration. - Unsupported or permanently
unreadable source files.

## 11.5 Upload progress

Track: - Files pending. - Files active. - Files successful. - Files
failed. - Bytes pending. - Bytes uploaded. - Current throughput. -
Average throughput. - Retry count. - Elapsed time.

Throughput must be calculated from actual transferred bytes and elapsed
time. Label units clearly (MB/s vs MiB/s).

## 11.6 Source safety

-   Open source files read-only.
-   Never delete or move source files.
-   Never rename source files.
-   Never write temporary files to the SD card.
-   If a source card disappears, stop affected file tasks and record the
    failure.
-   Do not mark a partial transfer successful.

------------------------------------------------------------------------

# 12. Phase 5 -- Metadata and Session Management

## 12.1 Objective

Before upload, prompt the operator for metadata and apply the same
session metadata to all selected cards/files in that session.

Suggested fields:

-   Activity name (required).
-   Place of recording (required if relevant to the project).
-   Recording date (required).
-   Operator name (configurable requirement).
-   Project name (required).
-   Description (optional).
-   Additional custom fields (optional).

Example:

``` json
{
  "activity_name": "Field Survey",
  "place_of_recording": "Bengaluru",
  "recording_date": "2026-11-06",
  "operator_name": "Operator 01",
  "project_name": "Project Alpha",
  "description": "Outdoor recording session"
}
```

This is sample data only. Prompt the operator for actual values.

## 12.2 Session behavior

-   Generate a unique session ID.
-   Validate and sanitize metadata.
-   Preview destination paths before upload.
-   Show selected cards and file totals.
-   Ask for explicit confirmation.
-   Store metadata in SQLite.
-   Write a session manifest to GCS after successful or partially
    successful upload.
-   Store session metadata on objects where useful, respecting GCS
    metadata constraints.
-   Do not place sensitive personal data in object metadata unless
    explicitly approved.

## 12.3 Suggested commands

``` bash
python main.py upload
python main.py upload --metadata-file session.json
```

The interactive command should show a clear confirmation summary before
starting.

------------------------------------------------------------------------

# 13. Phase 6 -- Verification, Recovery and Reporting

## 13.1 Verification

For every uploaded object, record: - Source path. - Destination object
name. - Source size. - Remote object size. - GCS CRC32C checksum when
available. - Optional independently computed SHA-256. - Upload
timestamp. - Verification status.

Use CRC32C for GCS transfer-integrity verification where supported. Use
SHA-256 when the project needs an independent content identity check. Do
not compare unlike checksum algorithms or assume an ETag is a content
hash.

## 13.2 Recovery

On restart: - Load incomplete sessions. - Recheck source media
presence. - Reconcile local upload state with remote object metadata. -
Identify pending, failed and uncertain files. - Resume or retry only
when safe. - Do not duplicate or overwrite objects silently. - Require
operator intervention when source identity or destination state is
ambiguous.

## 13.3 Reports

Generate: - Terminal summary. - JSON session report. - CSV file-level
report. - Error log.

Example report:

``` text
=================================================
                UPLOAD REPORT
=================================================
Session ID                 : SESSION_YYYYMMDD_001
Cards discovered           : 18
Cards scanned              : 18
Cards with errors          : 1

Files discovered           : 12,500
Files uploaded             : 12,480
Files failed               : 20

Source bytes               : [measured]
Uploaded bytes             : [measured]
Average throughput         : [measured]
Elapsed time               : [measured]

Session status             : PARTIAL_SUCCESS
=================================================
```

All figures must come from actual measurements. Never hard-code
illustrative numbers into reports.

------------------------------------------------------------------------

# 14. Phase 7 -- Performance and Production Readiness

## 14.1 Benchmark plan

Test progressively: 1. One reader and one card. 2. Two readers. 3. Five
readers. 4. Ten readers. 5. All available readers. 6. Multiple hubs at
full intended load.

For each test record: - Card model and rated speed. - Reader model. -
Hub model and power supply. - Host USB port/controller path. -
Negotiated USB link speed. - Number and size distribution of files. -
Local read throughput. - Network upload throughput. - GCS upload
throughput. - CPU and RAM use. - Error and retry count.

## 14.2 Tuning

Benchmark: - 4 concurrent uploads. - 8 concurrent uploads. - 12
concurrent uploads. - Other values only when measurements justify
them. - Multiple chunk sizes that meet GCS requirements.

Select the best stable configuration, not simply the fastest single run.

## 14.3 Production readiness

-   Configuration validation.
-   Credential validation.
-   Disk and network error handling.
-   Graceful shutdown.
-   Restart recovery.
-   Log rotation.
-   Unit and integration tests.
-   Physical hardware acceptance tests.
-   Backup and retention policy for SQLite database and reports.
-   Documented operator workflow.
-   Security review of bucket IAM and data retention.

------------------------------------------------------------------------

# 15. CLI Command Specification

The eventual command set should include:

``` bash
python main.py check
python main.py scan
python main.py status
python main.py monitor
python main.py upload
python main.py retry
python main.py verify
python main.py report
python main.py stop
```

Expected behavior:

  Command     Behavior
  ----------- -----------------------------------------------------
  `check`     Refresh hardware inventory
  `scan`      Scan storage and file inventory
  `status`    Show current/latest state
  `monitor`   Watch for device add/remove events
  `upload`    Collect metadata and upload selected eligible files
  `retry`     Retry eligible failed uploads
  `verify`    Verify remote object state/checksums
  `report`    Generate reports
  `stop`      Gracefully stop active work where supported

Do not claim that a `stop` command can safely terminate every in-flight
transfer instantly. Implement graceful cancellation and document
behavior.

------------------------------------------------------------------------

# 16. Proposed Project Structure

``` text
sd_mass_uploader/
|
|-- main.py
|-- pyproject.toml
|-- requirements.txt
|-- config.yaml
|-- .env.example
|-- .gitignore
|-- README.md
|
|-- src/
|   |-- __init__.py
|   |
|   |-- cli/
|   |   |-- __init__.py
|   |   |-- commands.py
|   |
|   |-- core/
|   |   |-- __init__.py
|   |   |-- models.py
|   |   |-- enums.py
|   |   |-- exceptions.py
|   |   |-- settings.py
|   |
|   |-- device_manager/
|   |   |-- __init__.py
|   |   |-- base.py
|   |   |-- detector.py
|   |   |-- windows.py
|   |   |-- linux.py
|   |   |-- usb_topology.py
|   |   |-- correlation.py
|   |
|   |-- inventory/
|   |   |-- __init__.py
|   |   |-- volume_scanner.py
|   |   |-- file_scanner.py
|   |   |-- inventory_service.py
|   |
|   |-- metadata/
|   |   |-- __init__.py
|   |   |-- session.py
|   |   |-- templates.py
|   |   |-- validation.py
|   |
|   |-- uploader/
|   |   |-- __init__.py
|   |   |-- gcs_client.py
|   |   |-- upload_manager.py
|   |   |-- worker.py
|   |   |-- retry_policy.py
|   |   |-- progress.py
|   |
|   |-- verification/
|   |   |-- __init__.py
|   |   |-- checksums.py
|   |   |-- cloud_verifier.py
|   |
|   |-- database/
|   |   |-- __init__.py
|   |   |-- db.py
|   |   |-- schema.sql
|   |   |-- repositories.py
|   |
|   |-- reporting/
|   |   |-- __init__.py
|   |   |-- logger.py
|   |   |-- report_generator.py
|   |
|   |-- platform/
|       |-- __init__.py
|       |-- process.py
|       |-- paths.py
|
|-- data/
|   |-- database/
|   |-- reports/
|
|-- logs/
|
|-- tests/
|   |-- unit/
|   |-- integration/
|   |-- fixtures/
|   |-- hardware/
|
|-- scripts/
    |-- windows/
    |-- ubuntu/
```

The coding agent may adjust this structure when justified, but it must
preserve the separation of responsibilities.

------------------------------------------------------------------------

# 17. Configuration Design

Create a non-secret `config.yaml` with validated settings.

Illustrative configuration:

``` yaml
application:
  name: "SD Mass Uploader"
  environment: "development"
  log_level: "INFO"

device_detection:
  scan_interval_seconds: 5
  detect_usb_topology: true
  include_unmounted_storage: true

file_scanning:
  follow_symlinks: false
  calculate_sha256: false
  ignore_hidden_files: false

google_cloud:
  project_id: "REPLACE_WITH_PROJECT_ID"
  bucket_name: "REPLACE_WITH_BUCKET_NAME"
  root_prefix: "uploads"

upload:
  max_workers: 4
  chunk_size_mib: 64
  max_retries: 5
  backoff_initial_seconds: 2
  backoff_max_seconds: 60
  overwrite_existing: false
  verify_after_upload: true

database:
  path: "data/database/uploader.db"

reporting:
  directory: "data/reports"
  json: true
  csv: true
```

These are initial values, not performance guarantees. Validate GCS
chunk-size constraints and tune workers through benchmarks.

Never put secrets or private keys in this file.

------------------------------------------------------------------------

# 18. SQLite Persistence

At minimum, plan tables for:

-   `device_snapshots`
-   `usb_devices`
-   `readers`
-   `storage_devices`
-   `volumes`
-   `sessions`
-   `session_metadata`
-   `files`
-   `upload_tasks`
-   `upload_attempts`
-   `verification_results`

Use: - Primary keys. - Foreign keys. - Transactions. - Timestamps in a
consistent timezone (prefer UTC in storage). - Explicit status values. -
Indexes on session IDs, device IDs, task status and object names.

The database must be treated as a local state record, not the sole proof
of cloud upload success. Reconcile against GCS when recovering uncertain
uploads.

------------------------------------------------------------------------

# 19. Logging and Error Handling

Use structured, useful logs.

Recommended levels: - DEBUG: detailed diagnostics. - INFO: normal
operations and progress milestones. - WARNING: recoverable problems. -
ERROR: operation failed. - CRITICAL: application cannot safely continue.

Never log: - Credentials. - Access tokens. - Private keys. - Unnecessary
personal information.

Use per-device and per-file error isolation. One bad card must not crash
detection or block unrelated cards.

Define typed exceptions for: - Device detection failure. - Storage
access failure. - Source read failure. - Configuration error. -
Authentication failure. - GCS permission failure. - Network/transient
upload failure. - Verification mismatch. - Database failure.

------------------------------------------------------------------------

# 20. Testing Strategy

## 20.1 Unit tests

Use mocks and fixtures for: - PowerShell/CIM output. - Windows PnP
data. - `lsblk` JSON. - `lsusb -t` output. - sysfs device data. - GCS
client responses. - Network and retry errors. - SQLite repositories.

Test: - Multiple hubs. - Multiple readers. - Empty readers where
detectable. - Inserted cards. - Unknown devices. - Missing mount
points. - USB 2.0 and USB 3.x links. - Unknown speed. - Duplicate
identifiers. - Device removal. - Permission failures. - Unicode paths. -
Long paths. - Large files. - Duplicate object names. - Retryable and
permanent errors. - Restart recovery.

## 20.2 Integration tests

Use a test bucket/prefix and test files that are not on production SD
cards.

Test: - ADC authentication. - Bucket access. - Small upload. -
Large/resumable upload. - Remote size and checksum verification. -
Duplicate policy. - Failed upload recovery.

## 20.3 Physical hardware tests

Mocks are not proof of physical device compatibility.

Test with actual: - Hubs. - Readers. - SD cards. - Windows laptop
ports. - Ubuntu environment. - Ethernet connection.

Record hardware models and test results.

## 20.4 Source-media safety tests

Before production use: - Confirm the application opens source files
read-only. - Confirm no formatting or repair tools are invoked. -
Confirm no source files are moved or deleted. - Confirm no temporary
files are written to SD cards. - Confirm unplugging a card results in a
controlled error. - Compare sample source file hashes before and after a
test session.

------------------------------------------------------------------------

# 21. Security and Google Cloud Requirements

-   Use least-privilege IAM permissions.
-   Grant only the required bucket/object permissions.
-   Prefer ADC or an approved identity mechanism.
-   Keep secrets outside source control.
-   Add `.env`, local credentials and database files to `.gitignore`
    where appropriate.
-   Do not make the bucket public.
-   Use TLS through the official Google Cloud client.
-   Validate all metadata and object path components.
-   Consider bucket versioning, retention and lifecycle policies with
    the data owner.
-   Confirm data residency, privacy and retention requirements before
    production.
-   Do not upload sensitive recordings to a test or production bucket
    without authorization.

------------------------------------------------------------------------

# 22. Development Roadmap and Deliverables

  -----------------------------------------------------------------------
  Phase                   Main deliverable        Exit condition
  ----------------------- ----------------------- -----------------------
  1                       Hardware detection      Physical inventory
                                                  works on Windows and
                                                  Ubuntu

  2                       File scanning           Accurate read-only
                                                  inventory

  3                       GCS integration         Verified single-file
                                                  upload

  4                       Mass upload             Bounded concurrent and
                                                  resumable transfers

  5                       Metadata                Session template and
                                                  organized object paths

  6                       Verification/recovery   Reliable reconciliation
                                                  and reports

  7                       Optimization            Measured, stable
                                                  production
                                                  configuration
  -----------------------------------------------------------------------

Do not skip a phase's acceptance criteria. Do not begin full-scale
uploading until Phase 1 and Phase 2 are physically tested.

------------------------------------------------------------------------

# 23. Coding-Agent Operating Instructions

You are the senior engineer responsible for implementing this
specification.

## Mandatory workflow

For every phase:

1.  Inspect the existing repository before editing.
2.  Summarize the current state.
3.  Identify assumptions and unknowns.
4.  Propose a small implementation plan.
5.  Implement only the approved phase/scope.
6.  Keep modules small and maintainable.
7.  Add type hints and docstrings for public interfaces.
8.  Add unit tests for new behavior.
9.  Run the tests and report exact results.
10. Run formatting/linting if configured.
11. Update README and configuration examples.
12. Report files changed, commands run and known limitations.
13. Stop at the phase boundary and request review before continuing.

## Do not

-   Generate the entire application in one huge code dump.
-   Pretend mocked tests prove hardware functionality.
-   Invent hardware identifiers or USB speeds.
-   Treat all USB storage devices as SD cards without evidence.
-   Treat an unmounted device as an empty reader.
-   Hard-code a maximum of 20 devices.
-   Use drive letters as permanent card identities.
-   Write to source SD cards.
-   Store credentials in code or Git.
-   Silently overwrite cloud objects.
-   Mark uploads successful without verification.
-   Hide errors or retry permanent failures indefinitely.
-   Add unnecessary dependencies or complexity.

## Required completion report

At the end of each phase, provide:

-   What was implemented.
-   Files created or modified.
-   Dependencies and versions.
-   Commands to run.
-   Test results.
-   Hardware tests completed.
-   Known limitations.
-   Security/safety considerations.
-   Exact next step.

If an OS API or hardware capability is unavailable, state that
limitation and provide a safe fallback. Never fabricate a successful
result.

------------------------------------------------------------------------

# 24. First Coding-Agent Task: Phase 1 Only

Start with **Phase 1 -- Hardware and SD Card Detection**.

Do the following in order:

1.  Inspect the current development machine and Python version.
2.  Confirm whether it is Windows or Ubuntu.
3.  Inspect the repository and do not overwrite existing user work.
4.  Create the normalized device data models.
5.  Implement the Windows adapter first if running on Windows, or Linux
    adapter first if running on Ubuntu.
6.  Implement USB topology discovery as a separate capability.
7.  Implement storage and volume inventory.
8.  Implement safe device correlation.
9.  Implement SQLite inventory snapshots.
10. Implement `check`, `scan` and `status`.
11. Write mocked unit tests.
12. Run tests and fix failures.
13. Document manual physical-hardware tests.
14. Stop. Do not implement GCS uploads yet.

The first physical acceptance test should use a small number of readers
and cards. Then scale to multiple hubs and the full intended setup.

**The Phase 1 definition of done is accurate, honest, read-only hardware
inventory on both operating systems---not merely a successful Python
test run.**

------------------------------------------------------------------------

# 25. Operator Workflow -- Intended Final Experience

The eventual operator workflow should be:

``` text
1. Connect powered USB hubs.
2. Connect card readers and insert SD cards.
3. Connect laptop to Ethernet.
4. Open terminal.
5. Run device check.
6. Review connected, active, empty and inaccessible devices.
7. Run scan.
8. Review card/file inventory.
9. Run upload.
10. Enter activity, location, date, operator and project metadata.
11. Review destination and file count.
12. Confirm upload.
13. Monitor progress and errors.
14. Verify uploaded objects.
15. Generate final session report.
```

The application should never require the operator to manually identify
drive letters if reliable device correlation is available. If
correlation is ambiguous, ask the operator to confirm rather than
guessing.

------------------------------------------------------------------------

# 26. Final Engineering Principles

1.  **Safety first:** Source SD cards are read-only.
2.  **Truthful detection:** Unknown means unknown; do not guess.
3.  **Separation of concerns:** OS detection, scanning, cloud upload and
    reporting are separate modules.
4.  **Cross-platform consistency:** One application and one normalized
    data model.
5.  **Reliability before speed:** Bounded concurrency, retries and
    recovery.
6.  **Integrity:** Verify uploaded objects.
7.  **No silent overwrites:** Destination conflicts require an explicit
    policy.
8.  **Observable behavior:** Every important action has a log and
    status.
9.  **Measured optimization:** Tune from actual hardware and network
    benchmarks.
10. **Incremental development:** Complete and test each phase before
    proceeding.

**Start with Phase 1 only. Do not implement later phases until Phase 1
has passed its acceptance tests.**
