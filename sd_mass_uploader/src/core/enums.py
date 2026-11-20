"""Explicit status vocabularies. Values are stored in SQLite and reports."""

from enum import StrEnum


class DeviceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EMPTY_READER = "EMPTY_READER"
    READER_PRESENT_SLOT_UNKNOWN = "READER_PRESENT_SLOT_UNKNOWN"
    UNMOUNTED_STORAGE = "UNMOUNTED_STORAGE"
    INACCESSIBLE = "INACCESSIBLE"
    ERROR = "ERROR"
    DISCONNECTED = "DISCONNECTED"
    UNKNOWN = "UNKNOWN"


class SpeedConfidence(StrEnum):
    """How much a negotiated-speed value can be trusted."""

    HIGH = "HIGH"  # Reported by the OS for this exact connection.
    MEDIUM = "MEDIUM"  # Parsed from a diagnostic tool's text output.
    NONE = "NONE"  # Not available; the speed value is null.


class ReaderKind(StrEnum):
    CARD_READER = "CARD_READER"  # The device's own name says it is a card reader.
    USB_STORAGE_UNCONFIRMED = "USB_STORAGE_UNCONFIRMED"  # Mass storage, could be a flash drive.


class AccessStatus(StrEnum):
    OK = "OK"
    DENIED = "DENIED"
    ERROR = "ERROR"
    NOT_MOUNTED = "NOT_MOUNTED"
    UNKNOWN = "UNKNOWN"


class ScanStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    COMPLETE_WITH_ERRORS = "COMPLETE_WITH_ERRORS"
    DISCONNECTED = "DISCONNECTED"
    FAILED = "FAILED"


class SessionStatus(StrEnum):
    CREATED = "CREATED"
    SCANNING = "SCANNING"
    READY = "READY"
    UPLOADING = "UPLOADING"
    STOPPED = "STOPPED"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    UPLOADED_UNVERIFIED = "UPLOADED_UNVERIFIED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"  # Can be retried by the operator.
    FAILED_PERMANENT = "FAILED_PERMANENT"  # Retrying cannot help.
    CONFLICT = "CONFLICT"  # Destination differs; an operator must decide.
    UNCERTAIN = "UNCERTAIN"  # Interrupted; must be reconciled against GCS.


class VerificationStatus(StrEnum):
    MATCH = "MATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    MISSING = "MISSING"
    CHECKSUM_UNAVAILABLE = "CHECKSUM_UNAVAILABLE"


class DuplicatePolicy(StrEnum):
    FAIL = "fail"
    NEW_PATH = "new_path"


class CardPhase(StrEnum):
    """Where a card is in station intake. Stored; upload progress is derived from tasks."""

    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"  # Could be a USB drive or a cloned card.
    SCANNING = "SCANNING"
    SCAN_FAILED = "SCAN_FAILED"
    NEEDS_DECISION = "NEEDS_DECISION"  # Scanned, but some files match no known device.
    READY = "READY"  # Scanned, waiting for the operator to start it.
    QUEUED = "QUEUED"  # Released to the upload engine.


class CardState(StrEnum):
    """What the operator sees for one card. Always derived, never stored."""

    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    SCANNING = "SCANNING"
    NEEDS_DECISION = "NEEDS_DECISION"  # A person must say which device recorded some files.
    READY = "READY"
    QUEUED = "QUEUED"
    UPLOADING = "UPLOADING"
    PAUSING = "PAUSING"  # Paused, but files already in flight are finishing.
    PAUSED = "PAUSED"
    UPLOADED = "UPLOADED"  # Every file verified in GCS: safe to remove.
    UPLOADED_WITH_ISSUES = "UPLOADED_WITH_ISSUES"
    FAILED = "FAILED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    REMOVED = "REMOVED"  # Taken out before it finished; resumes when put back.
    NO_FILES = "NO_FILES"
