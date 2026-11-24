"""Settings, metadata, retries, checksums, logging, progress and source-safety guards."""

from __future__ import annotations

import io
import logging
import re
import threading
from pathlib import Path

import pytest
import yaml

from src.core.enums import DuplicatePolicy
from src.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    GcsPermissionError,
    MetadataValidationError,
    ObjectExistsError,
    PermanentUploadError,
    ReadOnlyViolation,
    SourceChangedError,
    SourceDisconnectedError,
    SourceReadError,
    StopRequested,
    TransientUploadError,
    VerificationMismatch,
)
from src.core.settings import MetadataSettings, Settings, load_settings
from src.metadata.templates import load_metadata_file, save_metadata_file
from src.metadata.validation import object_name_for, safe_path_segment, session_prefix
from src.metadata.validation import validate_metadata as check
from src.platform.paths import is_device_gone_error, open_source_readonly, to_extended_path
from src.platform.process import SubprocessRunner, assert_read_only, run_powershell
from src.reporting.logger import RedactingFilter, redact
from src.uploader.gcs_client import translate_error
from src.uploader.progress import ProgressTracker
from src.uploader.retry_policy import Disposition, backoff_delay, classify
from src.verification.checksums import HashingReader, crc32c_of_bytes, crc32c_of_file
from src.verification.cloud_verifier import compare

ROOT = Path(__file__).resolve().parents[2]

# --- Settings ---


def write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_shipped_config_loads_and_requires_real_bucket():
    settings = load_settings(ROOT / "config.yaml")
    assert settings.upload.max_workers == 4
    assert settings.upload.duplicate_policy is DuplicatePolicy.FAIL
    with pytest.raises(ConfigurationError, match="project_id"):
        settings.require_gcs()


def test_shipped_config_has_no_secret_fields():
    text = (ROOT / "config.yaml").read_text(encoding="utf-8").lower()
    for word in ("private_key:", "password:", "secret:", "token:"):
        assert word not in text


def test_paths_resolve_from_config_folder(tmp_path):
    settings = load_settings(write_config(tmp_path, {}))
    assert settings.resolve("data/x.db") == tmp_path / "data/x.db"


@pytest.mark.parametrize("bad,match", [
    ({"upload": {"chunk_size_mib": 0.1}}, "256 KiB"),
    ({"upload": {"max_workers": 0}}, "max_workers"),
    ({"upload": {"max_retries": -1}}, "max_retries"),
    ({"upload": {"typo_setting": 1}}, "Unknown setting"),
    ({"not_a_section": {}}, "Unknown setting"),
    ({"upload": {"duplicate_policy": "overwrite"}}, "duplicate_policy"),
    ({"upload": {"verify_after_upload": "yes"}}, "true or false"),
    ({"upload": {"max_workers": "four"}}, "number"),
    ({"application": {"log_level": "LOUD"}}, "log_level"),
    ({"upload": 5}, "mapping"),
])
def test_invalid_config_is_refused(tmp_path, bad, match):
    with pytest.raises(ConfigurationError, match=match):
        load_settings(write_config(tmp_path, bad))


def test_missing_and_malformed_config(tmp_path):
    with pytest.raises(ConfigurationError, match="not found"):
        load_settings(tmp_path / "none.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="YAML"):
        load_settings(bad)


# --- Metadata ---

GOOD = {"activity_name": "Survey", "place_of_recording": "Pune", "recording_date": "2026-11-06",
        "operator_name": "Op", "project_name": "Project Alpha"}


def rules():
    return MetadataSettings()


def test_valid_metadata_and_flat_form():
    metadata = check({**GOOD, "custom": {"camera": "A7"}}, rules())
    assert metadata.as_flat_dict()["custom.camera"] == "A7"


@pytest.mark.parametrize("field", ["activity_name", "recording_date", "project_name",
                                   "place_of_recording", "operator_name"])
def test_required_fields(field):
    with pytest.raises(MetadataValidationError, match=field):
        check({**GOOD, field: "  "}, rules())


def test_optional_requirements_are_configurable():
    relaxed = MetadataSettings(require_operator_name=False, require_place_of_recording=False)
    assert check({**GOOD, "operator_name": "", "place_of_recording": ""}, relaxed)


@pytest.mark.parametrize("value", ["2026-13-01", "06-11-2026", "2026-02-30", "soon"])
def test_bad_dates(value):
    with pytest.raises(MetadataValidationError, match="recording_date"):
        check({**GOOD, "recording_date": value}, rules())


@pytest.mark.parametrize("value", ["line\nbreak", "tab\there", "nul\x00byte"])
def test_control_characters_refused(value):
    with pytest.raises(MetadataValidationError, match="control"):
        check({**GOOD, "activity_name": value}, rules())


def test_unknown_field_and_bad_custom_name_refused():
    with pytest.raises(MetadataValidationError, match="Unknown"):
        check({**GOOD, "extra": "x"}, rules())
    with pytest.raises(MetadataValidationError, match="custom"):
        check({**GOOD, "custom": {"../x": "y"}}, rules())


def test_project_with_no_usable_characters_refused():
    with pytest.raises(MetadataValidationError):
        check({**GOOD, "project_name": "../.."}, rules())


@pytest.mark.parametrize("value,expected", [
    ("../../etc", "etc"), ("a/b", "a_b"), ("Project Alpha", "Project_Alpha"),
    ("..\\..\\x", "x"), ("परियोजना", "परियोजना"),
])
def test_path_segment_cannot_traverse(value, expected):
    result = safe_path_segment(value)
    assert result == expected
    assert "/" not in result and ".." not in result


def test_prefix_structure():
    prefix = session_prefix("uploads", "Project Alpha", "2026-11-06", "SESSION_1")
    assert prefix == "uploads/Project_Alpha/2026/11/06/SESSION_1"
    assert session_prefix("/a/b/", "P", "2026-01-02", "S").startswith("a/b/P/2026/01/02/")
    with pytest.raises(MetadataValidationError):
        session_prefix("a/../b", "P", "2026-01-02", "S")


def test_object_name_preserves_hierarchy_and_exact_names():
    name = object_name_for("p", "card-1", "DCIM/100 CANON/IMG (1).JPG")
    assert name == "p/card-1/DCIM/100 CANON/IMG (1).JPG"


@pytest.mark.parametrize("path", ["../evil", "a/../../b", "a//b", "a/./b", "/abs", "x\ny", "a\rb"])
def test_unsafe_source_paths_are_refused(path):
    with pytest.raises(MetadataValidationError):
        object_name_for("p", "c", path)


def test_object_name_length_limit_counts_bytes():
    object_name_for("p", "c", "a" * 1000)
    with pytest.raises(MetadataValidationError, match="1024"):
        object_name_for("p", "c", "ह" * 400)


def test_template_round_trip(tmp_path):
    path = tmp_path / "t" / "session.json"
    save_metadata_file(path, {**GOOD, "project_name": "परियोजना"})
    assert load_metadata_file(path)["project_name"] == "परियोजना"


def test_bad_metadata_files(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("[", encoding="utf-8")
    with pytest.raises(MetadataValidationError, match="not valid JSON"):
        load_metadata_file(bad)
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(MetadataValidationError, match="object"):
        load_metadata_file(bad)
    with pytest.raises(MetadataValidationError, match="Cannot read"):
        load_metadata_file(tmp_path / "missing.json")


# --- Retry policy ---


@pytest.mark.parametrize("error,expected", [
    (TransientUploadError("x"), Disposition.RETRY),
    (AuthenticationError("x"), Disposition.ABORT_SESSION),
    (GcsPermissionError("x"), Disposition.ABORT_SESSION),
    (ConfigurationError("x"), Disposition.ABORT_SESSION),
    (ObjectExistsError("x"), Disposition.CONFLICT),
    (VerificationMismatch("x"), Disposition.CONFLICT),
    (SourceChangedError("x"), Disposition.FAIL_PERMANENT),
    (SourceDisconnectedError("x"), Disposition.FAIL),
    (SourceReadError("x"), Disposition.FAIL),
    (PermanentUploadError("x"), Disposition.FAIL_PERMANENT),
    (StopRequested("x"), Disposition.STOPPED),
    (ValueError("bug"), Disposition.FAIL_PERMANENT),
])
def test_classification(error, expected):
    assert classify(error) is expected


def test_backoff_grows_stays_capped_and_jitters():
    assert backoff_delay(1, 2, 60, rng=lambda: 0) == 1
    assert backoff_delay(1, 2, 60, rng=lambda: 1) == 2
    assert backoff_delay(3, 2, 60, rng=lambda: 1) == 8
    assert backoff_delay(30, 2, 60, rng=lambda: 1) == 60


def _google_errors():
    from google.api_core import exceptions as api
    from google.auth import exceptions as auth
    from google.cloud.storage.exceptions import DataCorruption

    class Response:
        def __init__(self, code):
            self.status_code = code
            self.content = b""
            self.headers = {}

    return [
        (api.PreconditionFailed("x"), ObjectExistsError),
        (api.Unauthorized("x"), AuthenticationError),
        (auth.DefaultCredentialsError("x"), AuthenticationError),
        (auth.TransportError("x"), TransientUploadError),
        (api.Forbidden("x"), GcsPermissionError),
        (api.NotFound("x"), ConfigurationError),
        (api.TooManyRequests("x"), TransientUploadError),
        (api.ServiceUnavailable("x"), TransientUploadError),
        (api.InternalServerError("x"), TransientUploadError),
        (api.BadRequest("x"), PermanentUploadError),
        (ConnectionResetError("reset"), TransientUploadError),
        (TimeoutError("slow"), TransientUploadError),
        (DataCorruption(Response(200), "bad"), VerificationMismatch),
    ]


@pytest.mark.parametrize("index", range(13))
def test_google_errors_translate(index):
    error, expected = _google_errors()[index]
    assert isinstance(translate_error(error, "ctx"), expected)


@pytest.mark.parametrize("code,expected", [
    (412, ObjectExistsError), (401, AuthenticationError), (403, GcsPermissionError),
    (503, TransientUploadError), (429, TransientUploadError), (400, PermanentUploadError),
])
def test_invalid_response_status_translates(code, expected):
    from google.cloud.storage.exceptions import InvalidResponse

    class Response:
        status_code = code
        content = b""
        headers = {}

    assert isinstance(translate_error(InvalidResponse(Response(), "x"), "ctx"), expected)


# --- Checksums ---


def test_known_crc32c_value():
    assert crc32c_of_bytes(b"") == "AAAAAA=="
    assert crc32c_of_bytes(b"123456789") == "4waSgw=="  # Standard check value 0xE3069283.


def test_file_crc_matches_bytes(tmp_path):
    path = tmp_path / "a"
    path.write_bytes(b"hello" * 100000)
    assert crc32c_of_file(str(path)) == crc32c_of_bytes(path.read_bytes())


def test_hashing_reader_ignores_resent_bytes():
    counted = []
    reader = HashingReader(io.BytesIO(b"abcdefghij"), counted.append)
    assert reader.read(6) == b"abcdef"
    reader.seek(2)
    assert reader.tell() == 2
    assert reader.read(4) == b"cdef"
    assert reader.read() == b"ghij"
    assert sum(counted) == 10
    assert reader.crc32c == crc32c_of_bytes(b"abcdefghij")
    assert reader.bytes_read == 14


def test_hashing_reader_partial_overlap_after_rewind():
    reader = HashingReader(io.BytesIO(b"abcdefghij"), sha256=True)
    reader.read(4)
    reader.seek(2)
    reader.read(5)
    reader.read()
    assert reader.crc32c == crc32c_of_bytes(b"abcdefghij")
    import hashlib

    assert reader.sha256 == hashlib.sha256(b"abcdefghij").hexdigest()


def test_reader_error_is_not_an_oserror():
    """If it were, the Google client would retry a dying card as a network fault."""
    assert not issubclass(SourceReadError, OSError)
    assert not issubclass(SourceDisconnectedError, OSError)


class _Broken(io.BytesIO):
    def read(self, *_):
        raise OSError(1167, "gone")


def test_read_failure_with_mount_gone_is_disconnect():
    with pytest.raises(SourceDisconnectedError):
        HashingReader(_Broken(), mount_check=lambda: False).read(1)


def test_read_failure_with_mount_present_is_read_error():
    with pytest.raises(SourceReadError) as caught:
        HashingReader(_Broken(), mount_check=lambda: True).read(1)
    assert not isinstance(caught.value, SourceDisconnectedError)


def test_cancel_stops_at_next_read():
    cancel = threading.Event()
    reader = HashingReader(io.BytesIO(b"abc"), cancel=cancel)
    reader.read(1)
    cancel.set()
    with pytest.raises(StopRequested):
        reader.read(1)


def test_compare_never_trusts_missing_checksums():
    from src.core.enums import VerificationStatus as V
    from src.core.models import RemoteObject

    remote = RemoteObject("n", 3, "abc=", 1)
    assert compare(3, "abc=", remote) is V.MATCH
    assert compare(4, "abc=", remote) is V.SIZE_MISMATCH
    assert compare(3, "xyz=", remote) is V.CHECKSUM_MISMATCH
    assert compare(3, None, remote) is V.CHECKSUM_UNAVAILABLE
    assert compare(3, "abc=", RemoteObject("n", 3, None, 1)) is V.CHECKSUM_UNAVAILABLE
    assert compare(3, "abc=", None) is V.MISSING


# --- Progress ---


def test_progress_uses_measured_bytes_and_time():
    now = [100.0]
    tracker = ProgressTracker(4, 4_000_000, clock=lambda: now[0])
    tracker.started()
    tracker.add_bytes(2_000_000)
    now[0] = 102.0
    view = tracker.view()
    assert view.average_mb_per_s == pytest.approx(1.0)
    assert view.current_mb_per_s == pytest.approx(1.0)
    assert (view.files_pending, view.files_active, view.bytes_pending) == (3, 1, 2_000_000)
    tracker.finished(True)
    now[0] = 120.0
    view = tracker.view()
    assert view.files_successful == 1 and view.current_mb_per_s == 0


def test_failed_attempt_bytes_are_taken_back():
    tracker = ProgressTracker(1, 100)
    tracker.add_bytes(60)
    tracker.rewind_bytes(60)
    assert tracker.view().bytes_uploaded == 0


# --- Logging ---


@pytest.mark.parametrize("text", [
    "token ya29.a0AfB_byC-secret-value", 'data {"private_key": "abc123"}',
    "access_token=abc123", "Authorization: Bearer abc123",
    "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----",
])
def test_secrets_are_redacted(text):
    result = redact(text)
    assert "abc123" not in result and "secret-value" not in result
    assert "REDACTED" in result


def test_filter_redacts_arguments():
    record = logging.LogRecord("x", logging.INFO, "f", 1, "value %s", ("ya29.secret",), None)
    RedactingFilter().filter(record)
    assert record.getMessage() == "value [REDACTED]"


def test_ordinary_text_is_untouched():
    assert redact("Uploaded DCIM/a.mp4 in 3s") == "Uploaded DCIM/a.mp4 in 3s"


# --- Source safety ---

SOURCE = sorted((ROOT / "src").rglob("*.py"))


@pytest.mark.parametrize("command", [
    ["diskpart"], ["mkfs.vfat", "/dev/sdb"], ["mount", "/dev/sdb1"], ["umount", "/x"],
    ["fsck", "/dev/sdb"], ["format.com", "F:"], ["sh", "-c", "ls"], ["cmd.exe"], [],
])
def test_only_read_only_programs_can_run(command):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(command)
    with pytest.raises(ReadOnlyViolation):
        SubprocessRunner().run(command)


@pytest.mark.parametrize("program", ["lsblk", "lsusb", "findmnt", "powershell.exe",
                                     "C:\\Windows\\System32\\powershell.exe", "/usr/bin/lsblk"])
def test_inventory_programs_are_allowed(program):
    assert_read_only([program])


@pytest.mark.parametrize("script", [
    "Format-Volume -DriveLetter F", "Clear-Disk 1", "Set-Disk 1 -IsReadOnly $false",
    "Remove-Partition 1", "Repair-Volume F", "Remove-Item F:\\x", "Set-Content F:\\x y",
    "diskpart", "chkdsk F:", "Dismount-DiskImage x", "format-volume f",
])
def test_destructive_powershell_is_refused(script):
    class Never:
        def run(self, *_a, **_k):
            raise AssertionError("must not run")

    with pytest.raises(ReadOnlyViolation):
        run_powershell(Never(), script)


def test_no_shell_execution_anywhere():
    for path in SOURCE:
        text = path.read_text(encoding="utf-8")
        assert "shell=True" not in text, path
        assert "os.system" not in text, path
        assert "os.popen" not in text, path


def test_subprocess_only_in_the_guarded_module():
    allowed = {"process.py"}
    for path in SOURCE:
        if path.name not in allowed:
            assert not re.search(r"^\s*(import|from) subprocess", path.read_text(encoding="utf-8"),
                                 re.M), path


def test_no_code_deletes_or_moves_source_files():
    forbidden = re.compile(r"\b(os\.(remove|unlink|rename|replace|rmdir|truncate|utime)|"
                           r"shutil\.(move|rmtree)|\.unlink\(|\.rmdir\(|\.rename\()")
    # App-owned files only: the station's state file (atomic replace) and simulated cards.
    allowed = {"daemon.py", "simulator.py",
               "pack.py"}  # The imported PeakAI pack and laptop key under data/station.
    for path in SOURCE:
        if path.name not in allowed:
            assert not forbidden.search(path.read_text(encoding="utf-8")), path


def test_scanner_and_uploader_only_open_read_only():
    """Writes exist only for app-owned outputs, in reports and templates."""
    writers = re.compile(r"""open\([^)]*["'][wax+]|write_text|write_bytes""")
    allowed = {"report_generator.py", "templates.py", "commands.py",
               "daemon.py",  # Station state and lock files under data/station.
               "simulator.py",  # Simulated cards, only inside the simulator folder.
               "auth.py",  # The API token under data/station.
               "claude_fixer.py",  # Claude Code's session logs and guard settings, under logs.
               "pack.py"}  # The imported PeakAI pack and laptop key under data/station.
    for path in SOURCE:
        if path.name not in allowed:
            assert not writers.search(path.read_text(encoding="utf-8")), path


def test_open_source_rejects_writing(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"data")
    with open_source_readonly(str(path)) as handle:
        assert handle.read() == b"data"
        with pytest.raises(io.UnsupportedOperation):
            handle.write(b"x")
    assert path.read_bytes() == b"data"


def test_session_defaults_are_safe():
    upload = Settings().upload
    assert upload.overwrite_existing is False
    assert upload.verify_after_upload is True
    assert upload.duplicate_policy is DuplicatePolicy.FAIL
    assert "operator_name" not in upload.object_metadata_fields


def test_device_error_detection():
    assert is_device_gone_error(OSError(5, "EIO"))
    assert not is_device_gone_error(OSError(13, "denied"))
    error = OSError()
    error.winerror = 1167
    assert is_device_gone_error(error)


def test_extended_path_is_idempotent():
    once = to_extended_path("somewhere/x.txt")
    assert to_extended_path(once) == once
