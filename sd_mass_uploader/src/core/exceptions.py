"""Typed application errors. Only adapters translate third-party exceptions into these."""


class UploaderError(Exception):
    """Base class for every expected application failure."""


class ConfigurationError(UploaderError):
    """Configuration is missing or invalid."""


class MetadataValidationError(UploaderError):
    """Operator-provided session metadata is invalid."""


class DatabaseError(UploaderError):
    """The local SQLite state could not be read or written."""


class DeviceDetectionError(UploaderError):
    """An OS query for hardware failed."""


class ReadOnlyViolation(UploaderError):
    """Code tried to run a command that is not on the read-only allowlist."""


class StorageAccessError(UploaderError):
    """A storage device or volume cannot be accessed."""


class SourceReadError(UploaderError):
    """A source file could not be read. Deliberately not an OSError: see gcs_client."""


class SourceDisconnectedError(SourceReadError):
    """The source card vanished while it was being used."""


class SourceChangedError(SourceReadError):
    """A source file no longer matches what the scan recorded."""


class AuthenticationError(UploaderError):
    """Google credentials are missing or rejected. Never retried."""


class GcsPermissionError(UploaderError):
    """The identity lacks permission on the bucket. Never retried."""


class TransientUploadError(UploaderError):
    """A temporary network or service failure. Safe to retry."""


class LineLostError(TransientUploadError):
    """The internet line an upload was using was unplugged or changed address.

    Deliberately not an OSError, so neither the HTTP stack nor the Google client retries it on
    the dead line. The file goes back in the queue without using a retry.
    """


class PermanentUploadError(UploaderError):
    """GCS rejected the request in a way a retry cannot fix."""


class ObjectExistsError(UploaderError):
    """The destination object already exists (generation precondition failed)."""


class VerificationMismatch(UploaderError):
    """The remote object does not match the source."""


class PassDeniedError(UploaderError):
    """PeakAI: storage refused this pass's token for this folder (HTTP 403). Wrong pass, or a
    pass this laptop does not have."""


class PeakAiKeyError(AuthenticationError):
    """PeakAI: the token service rejected this laptop's key (HTTP 401). Only PeakAI can fix it."""


class PeakAiServiceError(TransientUploadError):
    """PeakAI: the token service did not give a token just now (no internet, or HTTP 5xx)."""


class StopRequested(UploaderError):
    """A hard stop interrupted a transfer; the file was not completed."""
