"""Every problem the station can report: what it means and how to fix it, in plain words.

Each engine error class, verification result and scan-error kind maps to exactly one entry
(tests enforce this), so an operator never sees a raw exception without guidance.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    CRITICAL = "critical"  # Uploads have stopped for everyone.
    ERROR = "error"  # Something failed and needs a person.
    WARNING = "warning"  # Working, but not as it should.
    INFO = "info"  # Worth knowing; nothing to do.


SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.ERROR: 1, Severity.WARNING: 2, Severity.INFO: 3}


class FixAction(StrEnum):
    """The one main button on a problem card. Each maps to a station command."""

    NONE = "none"
    RETRY_CARD = "retry_card"  # Queue failed files again, or scan again after a failed scan.
    CONFIRM_CARD = "confirm_card"
    RESUME_CARD = "resume_card"
    RESUME_ENGINE = "resume_engine"
    PLACE_CARD = "place_card"  # Say which device recorded a card's unplaced files.
    RETRY_REPORTS = "retry_reports"  # Send PeakAI dashboard entries and _info files again now.


FIX_LABELS = {
    FixAction.RETRY_CARD: "Retry this card",
    FixAction.CONFIRM_CARD: "Confirm it is a card",
    FixAction.RESUME_CARD: "Resume card",
    FixAction.RESUME_ENGINE: "Resume uploads",
    FixAction.PLACE_CARD: "Choose the device",
    FixAction.RETRY_REPORTS: "Try again now",
}


@dataclass(frozen=True)
class ProblemSpec:
    code: str
    severity: Severity
    title: str
    message: str  # May use {card}, {files}, {detail}, {speed}, {free}, {attempt}, {max}.
    steps: tuple[str, ...]
    action: FixAction = FixAction.NONE
    card_scoped: bool = True
    tags: tuple[str, ...] = field(default=())

    def render(self, **values: object) -> tuple[str, str]:
        """Title and message with the placeholders filled; missing values read as blank."""
        filled = defaultdict(str, {key: value for key, value in values.items()
                                   if value is not None})
        return self.title.format_map(filled), self.message.format_map(filled)


_SPECS = [
    # ---------------------------------------------------------------- station-wide
    ProblemSpec(
        "E-GCS-AUTH", Severity.CRITICAL, "Google sign-in is not valid",
        "Uploads are paused for every card because Google Cloud rejected the station's "
        "credentials. Nothing was lost; files wait in the queue. ({detail})",
        ("On the station, renew the credentials: replace the key file at "
         "/etc/sd-mass-uploader/credentials.json, or run `gcloud auth application-default login`.",
         "Press Resume uploads.",
         "If it happens again at once, ask your Google Cloud admin whether the key was disabled."),
        FixAction.RESUME_ENGINE, card_scoped=False, tags=("cloud",)),
    ProblemSpec(
        "E-GCS-PERMISSION", Severity.CRITICAL, "No permission to write to the bucket",
        "Uploads are paused because the station's Google account may not write to the bucket. "
        "({detail})",
        ("Ask your Google Cloud admin to grant the station's account "
         "`roles/storage.objectUser` (or create and get) on the bucket.",
         "Press Resume uploads once they confirm."),
        FixAction.RESUME_ENGINE, card_scoped=False, tags=("cloud",)),
    ProblemSpec(
        "E-CONFIG", Severity.CRITICAL, "Station settings are incomplete",
        "The station cannot upload until its settings are fixed: {detail}",
        ("Open the station's config.yaml (on an installed station: "
         "/etc/sd-mass-uploader/config.yaml) and correct the setting named above.",
         "Restart the station service, or press Resume uploads."),
        FixAction.RESUME_ENGINE, card_scoped=False, tags=("setup",)),
    ProblemSpec(
        "E-GCS-UNREACHABLE", Severity.ERROR, "Cannot reach Google Cloud",
        "The station cannot connect to Google Cloud Storage, so uploads have not started. "
        "It tries again by itself. ({detail})",
        ("Check the Ethernet cable and that other websites open on this computer.",
         "If the network is fine, check the Google Cloud status page.",
         "Nothing is lost: cards stay queued and continue when the connection returns."),
        card_scoped=False, tags=("network",)),
    ProblemSpec(
        "W-NETWORK-RETRYING", Severity.WARNING, "Connection problems: retrying",
        "{files} file(s) failed to send and are being retried (attempt {attempt} of {max}). "
        "({detail})",
        ("Check the Ethernet cable and the router.",
         "Nothing is needed if it recovers; files that run out of retries can be retried later."),
        card_scoped=False, tags=("network",)),
    ProblemSpec(
        "E-SESSION-BUSY", Severity.CRITICAL, "Another program is uploading this session",
        "{detail}. The station will not upload at the same time, to avoid duplicates.",
        ("Close the other upload (for example a `main.py upload` or `retry` in a terminal).",
         "If it was a crash, wait one minute, then press Resume uploads."),
        FixAction.RESUME_ENGINE, card_scoped=False),
    ProblemSpec(
        "E-DATABASE", Severity.CRITICAL, "The station's local database failed",
        "The station could not read or write its own records. ({detail})",
        ("Check that the station computer's disk is not full.",
         "Restart the station service.",
         "If it continues, contact support with the technical details."),
        FixAction.RESUME_ENGINE, card_scoped=False),
    ProblemSpec(
        "E-SAFETY-BLOCK", Severity.CRITICAL, "A safety check blocked an action",
        "The station refused to run something that could change a card. No card was touched. "
        "({detail})",
        ("Do not retry. Contact support with the technical details.",),
        card_scoped=False),
    ProblemSpec(
        "E-INTERNAL", Severity.ERROR, "Unexpected error",
        "Something went wrong that the station does not have specific advice for. ({detail})",
        ("Retry the card.",
         "If it repeats, restart the station service and contact support with the details."),
        FixAction.RETRY_CARD),
    ProblemSpec(
        "E-METADATA", Severity.ERROR, "Session details are not valid",
        "The session could not be opened: {detail}",
        ("Correct the session details and open the session again.",),
        card_scoped=False),
    ProblemSpec(
        "W-DISK-LOW", Severity.WARNING, "The station's disk is almost full",
        "Only {free} is free where the station keeps its database and logs. When it is full, "
        "uploads stop.",
        ("Delete old reports you have backed up from the reports folder.",
         "Free space elsewhere on this computer."),
        card_scoped=False),
    # ---------------------------------------------------------------- PeakAI (pack v2)
    ProblemSpec(
        "E-PEAKAI-KEY", Severity.CRITICAL, "PeakAI does not accept this laptop's key",
        "Uploads are paused: the PeakAI token service answered \"HTTP 401 unknown laptop "
        "key\". The key was switched off or is wrong. ({detail})",
        ("Ask PeakAI to check this laptop's key.",
         "If they send a new pack, import it: python main.py pack import <zip>.",
         "Then press Resume uploads."),
        FixAction.RESUME_ENGINE, card_scoped=False, tags=("peakai",)),
    ProblemSpec(
        "E-PEAKAI-NO-PASSES", Severity.CRITICAL, "This laptop has no PeakAI passes",
        "PeakAI has not given this laptop any upload pass, so nothing can be uploaded.",
        ("Ask PeakAI to add the passes this laptop needs.",
         "The station checks again every 5 minutes by itself."),
        card_scoped=False, tags=("peakai",)),
    ProblemSpec(
        "W-PEAKAI-OLD-PASSES", Severity.WARNING, "PeakAI has not switched this laptop yet",
        "This laptop only has the old mono/stereo passes. The new client folders cannot be "
        "used until PeakAI switches it.",
        ("Tell the person, and ask PeakAI to switch this laptop to the new passes.",
         "Cards wait; nothing is uploaded to the old folders by this station."),
        card_scoped=False, tags=("peakai",)),
    ProblemSpec(
        "W-PEAKAI-SERVICE", Severity.WARNING, "Cannot get upload passes right now",
        "The PeakAI token service did not answer. The station keeps the current tokens and "
        "tries again every minute. ({detail})",
        ("If it says HTTP 000: fix the internet connection. It picks up by itself.",
         "If it says HTTP 502: nothing to do; Google did not answer just then.",
         "If it lasts more than 15 minutes, tell PeakAI."),
        card_scoped=False, tags=("peakai", "network")),
    ProblemSpec(
        "E-PEAKAI-PASS-DENIED", Severity.ERROR, "Storage refused this card's pass",
        "{files} file(s) from {card} were refused (HTTP 403): the wrong pass for this card, "
        "or a pass this laptop does not have. ({detail})",
        ("Check which device recorded the card.",
         "If the device is right, ask PeakAI to add that pass to this laptop.",
         "Then press Retry."),
        FixAction.RETRY_CARD, tags=("peakai", "card")),
    ProblemSpec(
        "W-CARD-DECISION", Severity.WARNING, "Which device recorded this card?",
        "{card} is waiting: {detail}.",
        ("Look at the card's folders and ask the person which device recorded it.",
         "Choose that device, or skip the files that are not project footage.",
         "Never guess a pass; if unsure, ask PeakAI."),
        FixAction.PLACE_CARD, tags=("peakai", "card")),
    ProblemSpec(
        "E-PEAKAI-INFO", Severity.ERROR, "The card's _info file was not written",
        "{card} is uploaded, but its _info file could not be written yet. The station tries "
        "again every minute. ({detail})",
        ("Check the internet connection.",
         "Press Try again now."),
        FixAction.RETRY_REPORTS, tags=("peakai", "card")),
    ProblemSpec(
        "E-PEAKAI-REPORT", Severity.ERROR, "The PeakAI dashboard was not updated",
        "Nothing was recorded on the dashboard for this upload. The station tries again "
        "every minute. ({detail})",
        ("Check the internet connection.",
         "If it says HTTP 401, ask PeakAI about this laptop's key.",
         "Press Try again now."),
        FixAction.RETRY_REPORTS, card_scoped=False, tags=("peakai",)),
    ProblemSpec(
        "I-PEAKAI-DETAILS", Severity.INFO, "Recording dates or hours are not known",
        "The station could not read them from the cards ({detail}). The dashboard entry is "
        "sent without them.",
        ("Ask the person for the first and last recording day and the total video hours.",
         "Add them to the session details (recorded_from, recorded_to, video_hours)."),
        card_scoped=False, tags=("peakai",)),
    # ---------------------------------------------------------------- devices and mounting
    ProblemSpec(
        "E-MOUNT-FAILED", Severity.ERROR, "A card could not be opened",
        "The station tried to open a card read-only and failed: {detail}",
        ("Take the card out and put it back in.",
         "Try another slot or reader.",
         "Do not let the computer \"repair\" or format the card. Set it aside if it fails again."),
        card_scoped=False, tags=("device",)),
    ProblemSpec(
        "W-FS-UNSUPPORTED", Severity.WARNING, "A device was not opened",
        "{detail}. Only SD-card file systems are opened automatically.",
        ("If this is a camera card, check it in the camera; it may need formatting there "
         "(after its recordings are safe).",
         "If it is a USB stick or another drive, remove it."),
        card_scoped=False, tags=("device",)),
    ProblemSpec(
        "W-DETECTION", Severity.WARNING, "Device detection had a problem",
        "{detail}",
        ("Unplug and reconnect the hub or reader named above.",
         "If it persists, restart the station service."),
        card_scoped=False, tags=("device",)),
    ProblemSpec(
        "W-SIMULATOR-MISSING", Severity.WARNING, "Simulator folder not found",
        "{detail}",
        ("Create the folder, or correct station.simulator_dir.",),
        card_scoped=False),
    ProblemSpec(
        "W-USB-SLOW", Severity.WARNING, "Card reader on a slow USB connection",
        "{card} is connected at {speed}. It works, but reads much slower than USB 3 (5 Gbps).",
        ("Move the reader to a blue USB 3 port on the hub or laptop.",
         "Use a USB 3 cable and hub; some hub ports are USB 2 only."),
        tags=("device",)),
    # ---------------------------------------------------------------- card state
    ProblemSpec(
        "E-CARD-REMOVED", Severity.ERROR, "Card removed before it finished",
        "{card} was taken out with {files} file(s) not uploaded yet.",
        ("Put the same card back in, in any slot.",
         "It continues by itself. Nothing is uploaded twice."),
        tags=("card",)),
    ProblemSpec(
        "W-CARD-CONFIRM", Severity.WARNING, "Confirm this device",
        "{card} is held back: {detail}.",
        ("Check that it is a recording card from this session, not a USB stick or a copy of "
         "another card.",
         "Press Confirm to upload it, or remove it."),
        FixAction.CONFIRM_CARD, tags=("card",)),
    ProblemSpec(
        "E-CARD-UNREADABLE", Severity.ERROR, "Card could not be read",
        "{card} could not be scanned: {detail}.",
        ("Take the card out, check the contacts, and put it back in.",
         "Press Retry to scan it again.",
         "If it fails again, set it aside. Do not repair or format it."),
        FixAction.RETRY_CARD, tags=("card",)),
    ProblemSpec(
        "E-CARD-PARTLY-UNREADABLE", Severity.ERROR, "Some folders or files could not be read",
        "{files} item(s) on {card} could not be read and were not uploaded. Example: {detail}",
        ("Take the card out and put it back in, then press Retry to scan it again.",
         "If the same items fail again, the card may be damaged. Keep it safe and report it."),
        FixAction.RETRY_CARD, tags=("card",)),
    ProblemSpec(
        "W-INVALID-NAME", Severity.WARNING, "Some file names could not be read",
        "{files} file(s) on {card} have names this computer cannot read, so they were skipped. "
        "Example: {detail}",
        ("Report these files; a copy with corrected names may be needed.",),
        tags=("card",)),
    ProblemSpec(
        "W-UNSAFE-NAME", Severity.WARNING, "Some file names cannot be used in the bucket",
        "{files} file(s) on {card} were skipped because their names cannot be stored safely. "
        "Example: {detail}",
        ("Report these files; they need to be renamed on a copy before upload.",),
        tags=("card",)),
    ProblemSpec(
        "I-SKIPPED", Severity.INFO, "Some items were skipped",
        "{files} link(s) or special file(s) on {card} were not uploaded. This is normal for "
        "camera cards. Example: {detail}",
        ("Nothing to do unless these items are expected recordings.",),
        tags=("card",)),
    # ---------------------------------------------------------------- per-file upload results
    ProblemSpec(
        "E-FILE-READ", Severity.ERROR, "Files could not be read from the card",
        "{files} file(s) on {card} could not be read. ({detail})",
        ("Take the card out and put it back in.",
         "Press Retry.",
         "If the same files fail again, the card may be damaged. Keep it safe and report it."),
        FixAction.RETRY_CARD, tags=("card",)),
    ProblemSpec(
        "E-FILE-CHANGED", Severity.ERROR, "Files changed after the scan",
        "{files} file(s) on {card} changed or disappeared after scanning, so they were not "
        "uploaded. ({detail})",
        ("Make sure no camera or computer is writing to the card.",
         "End this session, then upload the card again in a new session so it is rescanned."),
        tags=("card",)),
    ProblemSpec(
        "E-CONFLICT", Severity.ERROR, "A different file already exists in the bucket",
        "{files} file(s) from {card} were not uploaded because a different file is already at "
        "the same place in the bucket. Nothing was overwritten. ({detail})",
        ("Ask your admin to compare the existing cloud file with this card.",
         "To check again later: python main.py retry --session <ID> --include-conflicts."),
        tags=("cloud", "card")),
    ProblemSpec(
        "E-VERIFY-MISMATCH", Severity.ERROR, "Cloud copy did not match the card",
        "{files} file(s) from {card} arrived in the bucket different from the card. ({detail})",
        ("This usually means a network or hardware fault. Check the cable and hub.",
         "To upload them again: python main.py retry --session <ID> --include-conflicts."),
        tags=("cloud", "card")),
    ProblemSpec(
        "E-MISSING-REMOTE", Severity.ERROR, "Files are missing from the bucket",
        "{files} file(s) from {card} were uploaded earlier but are no longer in the bucket. "
        "({detail})",
        ("Ask your admin whether a lifecycle or retention rule deletes objects.",
         "Press Retry to upload them again."),
        FixAction.RETRY_CARD, tags=("cloud", "card")),
    ProblemSpec(
        "W-CHECKSUM-UNAVAILABLE", Severity.WARNING, "Cloud checksum not available",
        "Google Cloud did not return a checksum for {files} file(s) from {card}, so they could "
        "not be fully verified.",
        ("Run python main.py verify --session <ID> later.",),
        tags=("cloud", "card")),
    ProblemSpec(
        "E-UPLOAD-REJECTED", Severity.ERROR, "Google Cloud rejected files",
        "Google Cloud refused {files} file(s) from {card} in a way that retrying will not fix. "
        "({detail})",
        ("Ask your admin to check bucket rules (retention, object size, location).",
         "Press Retry once fixed."),
        FixAction.RETRY_CARD, tags=("cloud", "card")),
    ProblemSpec(
        "E-UPLOAD-FAILED", Severity.ERROR, "Upload kept failing",
        "{files} file(s) from {card} ran out of retries because of network errors. ({detail})",
        ("Check the Ethernet cable and the internet connection.",
         "Press Retry."),
        FixAction.RETRY_CARD, tags=("network", "card")),
    ProblemSpec(
        "W-UNCERTAIN", Severity.WARNING, "Interrupted files need their card",
        "{files} file(s) from {card} were interrupted. The station needs the card to check "
        "whether the cloud copies are complete.",
        ("Insert the card. The check runs by itself.",),
        tags=("card",)),
    ProblemSpec(
        "I-STOPPED", Severity.INFO, "Files were stopped",
        "{files} file(s) from {card} were stopped part-way and went back in the queue.",
        ("Nothing to do.",),
        tags=("card",)),
]

CATALOG: dict[str, ProblemSpec] = {spec.code: spec for spec in _SPECS}

# Exception class name (as stored in upload_tasks.error_class or reported by the engine) -> code.
ERROR_CLASS_CODES: dict[str, str] = {
    "UploaderError": "E-INTERNAL",
    "ConfigurationError": "E-CONFIG",
    "MetadataValidationError": "E-METADATA",
    "DatabaseError": "E-DATABASE",
    "DeviceDetectionError": "W-DETECTION",
    "ReadOnlyViolation": "E-SAFETY-BLOCK",
    "StorageAccessError": "E-CARD-UNREADABLE",
    "SourceReadError": "E-FILE-READ",
    "SourceDisconnectedError": "E-CARD-REMOVED",
    "SourceChangedError": "E-FILE-CHANGED",
    "AuthenticationError": "E-GCS-AUTH",
    "GcsPermissionError": "E-GCS-PERMISSION",
    "TransientUploadError": "E-UPLOAD-FAILED",
    "PermanentUploadError": "E-UPLOAD-REJECTED",
    "ObjectExistsError": "E-CONFLICT",
    "VerificationMismatch": "E-VERIFY-MISMATCH",
    "StopRequested": "I-STOPPED",
    "PassDeniedError": "E-PEAKAI-PASS-DENIED",
    "PeakAiKeyError": "E-PEAKAI-KEY",
    "PeakAiServiceError": "W-PEAKAI-SERVICE",
    # Verification results stored as error classes (recovery and `verify`).
    "SIZE_MISMATCH": "E-VERIFY-MISMATCH",
    "CHECKSUM_MISMATCH": "E-VERIFY-MISMATCH",
    "MISSING": "E-MISSING-REMOTE",
    "MISSING_REMOTE_OBJECT": "E-MISSING-REMOTE",
    "CHECKSUM_UNAVAILABLE": "W-CHECKSUM-UNAVAILABLE",
    # Station conditions that are not exceptions.
    "SessionBusy": "E-SESSION-BUSY",
}

# Scan error kinds (scan_errors.kind) -> code.
SCAN_KIND_CODES: dict[str, str] = {
    "CARD_DISCONNECTED": "E-CARD-UNREADABLE",
    "DIRECTORY_UNREADABLE": "E-CARD-PARTLY-UNREADABLE",
    "ENTRY_UNREADABLE": "E-CARD-PARTLY-UNREADABLE",
    "INVALID_FILENAME": "W-INVALID-NAME",
    "UNSAFE_OBJECT_NAME": "W-UNSAFE-NAME",
    "LINK_OUTSIDE_VOLUME": "I-SKIPPED",
    "LINK_SKIPPED": "I-SKIPPED",
    "NOT_A_REGULAR_FILE": "I-SKIPPED",
    "NOT_PROJECT_FOOTAGE": "I-SKIPPED",
    "SKIPPED_BY_OPERATOR": "I-SKIPPED",
}


def code_for_error(error_class: str | None) -> str:
    return ERROR_CLASS_CODES.get(error_class or "", "E-INTERNAL")


def code_for_scan_kind(kind: str) -> str:
    return SCAN_KIND_CODES.get(kind, "E-CARD-PARTLY-UNREADABLE")


def catalog_markdown() -> str:
    """The operator help page, generated so it can never drift from the catalog."""
    lines = [
        "# Problems and how to fix them",
        "",
        "Generated from `src/advisor/catalog.py` by `python main.py problems --markdown`. "
        "Do not edit by hand.",
        "",
        "Severity: **critical** stops all uploads; **error** needs a person; **warning** means "
        "it works but not as it should; **info** needs nothing.",
        "",
    ]
    for spec in sorted(_SPECS, key=lambda item: (SEVERITY_ORDER[item.severity], item.code)):
        title, message = spec.render(card="the card", files="Some", detail="details",
                                     speed="480 Mbps", free="2 GB", attempt="2", max="5")
        lines += [f"## {spec.code}: {title}", "", f"*{spec.severity.value}*", "", message, ""]
        lines += [f"{number}. {step}" for number, step in enumerate(spec.steps, 1)]
        if spec.action is not FixAction.NONE:
            lines += ["", f"Button: **{FIX_LABELS[spec.action]}**"]
        lines.append("")
    return "\n".join(lines)
