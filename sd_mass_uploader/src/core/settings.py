"""Load and validate config.yaml. Unknown keys are errors so typos cannot be ignored."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from src.core.enums import DuplicatePolicy
from src.core.exceptions import ConfigurationError

GCS_CHUNK_MULTIPLE = 256 * 1024
_PLACEHOLDER = "REPLACE_WITH_"


@dataclass
class ApplicationSettings:
    name: str = "SD Mass Uploader"
    environment: str = "development"
    log_level: str = "INFO"


@dataclass
class DeviceDetectionSettings:
    scan_interval_seconds: int = 5
    detect_usb_topology: bool = True
    include_unmounted_storage: bool = True
    inventory_stale_seconds: int = 300


@dataclass
class FileScanningSettings:
    follow_symlinks: bool = False
    calculate_sha256: bool = False
    ignore_hidden_files: bool = False
    exclude_dirs: list[str] = field(
        default_factory=lambda: ["System Volume Information", "$RECYCLE.BIN"]
    )


@dataclass
class GoogleCloudSettings:
    project_id: str = ""
    bucket_name: str = ""
    root_prefix: str = "uploads"


@dataclass
class UploadSettings:
    max_workers: int = 4
    chunk_size_mib: float = 64
    max_retries: int = 5
    backoff_initial_seconds: float = 2
    backoff_max_seconds: float = 60
    request_timeout_seconds: float = 120
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.FAIL
    overwrite_existing: bool = False
    verify_after_upload: bool = True
    object_metadata_fields: list[str] = field(
        default_factory=lambda: ["project_name", "activity_name", "recording_date"]
    )

    @property
    def chunk_size_bytes(self) -> int:
        return int(self.chunk_size_mib * 1024 * 1024)



@dataclass
class NetworkSettings:
    """Upload over every internet line at once (src/network). Off: the default connection only."""

    enabled: bool = True
    max_lines: int = 0  # 0 = every working line; 2 = at most two (others wait as spares).
    workers_per_line: int = 4  # Files uploading at once on each line.
    max_total_workers: int = 16  # Cap across all lines. Memory: about this x chunk_size_mib.
    rescan_seconds: float = 5  # How often new or unplugged lines are noticed (Windows: >= 10).
    rest_seconds: float = 60  # A line with 3 network failures in a row rests this long.
    probe_url: str = "https://storage.googleapis.com/"  # Reached through each line to test it.
    # Learns each line's public address, to spot two cables that share one internet line.
    # Empty turns it off.
    public_ip_url: str = "https://1.1.1.1/cdn-cgi/trace"
    exclude_interfaces: list[str] = field(default_factory=lambda: [
        "lo", "docker*", "br-*", "virbr*", "veth*", "tun*", "tap*", "wg*", "tailscale*",
        "zt*", "vEthernet*", "*VPN*", "Loopback*",
    ])


@dataclass
class VendorPlatformSettings:
    """vendor.byndfrntr.com: an operator types a packet's code and the station reads the rest."""

    enabled: bool = False  # On: sessions open from a packet code (src/vendor).
    url: str = "https://vendor.byndfrntr.com"
    key_file: str = "data/station/secrets/vendor_station_key"  # This station's own key, 0600.
    timeout_seconds: float = 20
    # The vendor platform's city name -> the lake's folder name, where they differ.
    city_aliases: dict = field(default_factory=lambda: {"vijayapura": "bijapur"})
    # Device type on the vendor platform -> devices the cards may hold (src/peakai/devices.py).
    # A card holding a different device waits for a person. Types not listed are not checked.
    device_kinds: dict = field(default_factory=lambda: {
        "GI Lab": ["GI Labs EGO1GS"],
        "E2": ["E2 stereo"],
        "Build Stereo": ["BuildAI stereo (encrypted)"],
    })


@dataclass
class MetadataSettings:
    require_operator_name: bool = True
    require_place_of_recording: bool = True


@dataclass
class StationSettings:
    """The always-on service (`main.py daemon`)."""

    poll_seconds: float = 2.0  # How often presence is checked; udev events wake it sooner.
    auto_upload: bool = True  # Default for new sessions: start a card as soon as it is scanned.
    auto_mount: bool = True  # Mount new cards read-only through udisks2 (Linux only).
    mount_filesystems: list[str] = field(default_factory=lambda: ["vfat", "exfat"])
    mount_options: str = "ro,noatime,nosuid,nodev,noexec"
    simulator_dir: str = ""  # When set, sub-folders here act as cards instead of real devices.
    state_file: str = "data/station/state.json"
    lock_file: str = "data/station/station.lock"


@dataclass
class ApiSettings:
    """The station's local API, used by the console, the CLI and (later) the AI troubleshooter."""

    enabled: bool = True
    host: str = "127.0.0.1"  # Loopback only in this release (decision D5).
    port: int = 8765
    unix_socket: str = ""  # Linux: also listen here, e.g. /run/sd-mass-uploader/api.sock.
    token_file: str = "data/station/api-token"


@dataclass
class AiSettings:
    """The AI troubleshooter (Claude API). The key comes from ANTHROPIC_API_KEY, never here."""

    enabled: bool = True
    model: str = "claude-opus-5-5"
    effort: str = "high"  # low, medium, high, xhigh or max.
    max_iterations: int = 25  # Model turns per request before it must stop and report.
    max_output_tokens: int = 16000
    token_budget: int = 400_000  # Input + output tokens per AI session.
    command_timeout_seconds: float = 60
    approval_timeout_seconds: float = 600
    auto_station_actions: bool = True  # Retry/resume without asking; commands always ask.
    redact_personal: bool = True  # Hide operator name, place and description from the AI.


AI_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass
class PeakAiSettings:
    """PeakAI uploads. Active when a pack has been imported (`main.py pack import`)."""

    pack_dir: str = "data/station/pack"  # Imported pack: rules, documents, active.json.
    key_file: str = "data/station/secrets/laptop_key"  # 0600. /etc/... on an installed station.
    vendor_code: str = ""  # The `_info` "vendor" value for this laptop (decision D14).
    machine_name: str = ""  # `_info` written_by; the computer's name when empty.
    refresh_left_seconds: int = 2100  # Renew a pass token with under 35 minutes left.
    check_every_seconds: int = 300  # Ask which passes this laptop has every 5 minutes.
    retry_every_seconds: int = 60  # After a failed fetch, try again in 1 minute.
    http_timeout_seconds: float = 60


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass
class DatabaseSettings:
    path: str = "data/database/uploader.db"


@dataclass
class ReportingSettings:
    directory: str = "data/reports"
    json: bool = True
    csv: bool = True


@dataclass
class LoggingSettings:
    directory: str = "logs"
    max_bytes: int = 10_485_760
    backup_count: int = 5


@dataclass
class Settings:
    application: ApplicationSettings = field(default_factory=ApplicationSettings)
    device_detection: DeviceDetectionSettings = field(default_factory=DeviceDetectionSettings)
    file_scanning: FileScanningSettings = field(default_factory=FileScanningSettings)
    google_cloud: GoogleCloudSettings = field(default_factory=GoogleCloudSettings)
    upload: UploadSettings = field(default_factory=UploadSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    vendor_platform: VendorPlatformSettings = field(default_factory=VendorPlatformSettings)
    metadata: MetadataSettings = field(default_factory=MetadataSettings)
    station: StationSettings = field(default_factory=StationSettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    ai: AiSettings = field(default_factory=AiSettings)
    peakai: PeakAiSettings = field(default_factory=PeakAiSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    reporting: ReportingSettings = field(default_factory=ReportingSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    base_dir: Path = field(default_factory=Path.cwd)

    def resolve(self, relative: str) -> Path:
        """Resolve a configured path against the config file's folder, not the shell's cwd."""
        path = Path(relative)
        return path if path.is_absolute() else self.base_dir / path

    def require_gcs(self) -> None:
        """Fail early, with a useful message, if the bucket settings are still placeholders."""
        gcs = self.google_cloud
        for name, value in (("project_id", gcs.project_id), ("bucket_name", gcs.bucket_name)):
            if not value or value.startswith(_PLACEHOLDER):
                raise ConfigurationError(f"Set google_cloud.{name} in config.yaml")


def _build(cls: type, values: dict[str, Any], where: str) -> Any:
    known = {f.name for f in fields(cls)} - {"base_dir"}
    unknown = set(values) - known
    if unknown:
        raise ConfigurationError(f"Unknown setting(s) in {where}: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    defaults = cls()
    for name, value in values.items():
        default = getattr(defaults, name)
        if is_dataclass(default):
            if not isinstance(value, dict):
                raise ConfigurationError(f"{where}.{name} must be a mapping")
            kwargs[name] = _build(type(default), value, f"{where}.{name}")
        else:
            kwargs[name] = _coerce(value, default, f"{where}.{name}")
    return cls(**kwargs)


def _coerce(value: Any, default: Any, where: str) -> Any:
    if isinstance(default, DuplicatePolicy):
        try:
            return DuplicatePolicy(value)
        except ValueError as exc:
            choices = [p.value for p in DuplicatePolicy]
            raise ConfigurationError(f"{where} must be one of {choices}") from exc
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigurationError(f"{where} must be true or false")
        return value
    if isinstance(default, int | float):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigurationError(f"{where} must be a number")
        return value
    if isinstance(default, list) and not isinstance(value, list):
        raise ConfigurationError(f"{where} must be a list")
    if isinstance(default, dict) and not isinstance(value, dict):
        raise ConfigurationError(f"{where} must be a mapping")
    if isinstance(default, str) and not isinstance(value, str):
        raise ConfigurationError(f"{where} must be text")
    return value


def validate(settings: Settings) -> None:
    upload = settings.upload
    if upload.max_workers < 1:
        raise ConfigurationError("upload.max_workers must be at least 1")
    if upload.max_retries < 0:
        raise ConfigurationError("upload.max_retries cannot be negative")
    if upload.chunk_size_bytes <= 0 or upload.chunk_size_bytes % GCS_CHUNK_MULTIPLE:
        raise ConfigurationError("upload.chunk_size_mib must be a multiple of 256 KiB")
    if upload.backoff_initial_seconds <= 0 or upload.backoff_max_seconds <= 0:
        raise ConfigurationError("upload backoff values must be positive")
    network = settings.network
    if network.max_lines < 0:
        raise ConfigurationError("network.max_lines cannot be negative (0 means every line)")
    if network.workers_per_line < 1 or network.max_total_workers < 1:
        raise ConfigurationError("network.workers_per_line and max_total_workers must be >= 1")
    if network.rescan_seconds < 1 or network.rest_seconds <= 0:
        raise ConfigurationError("network.rescan_seconds must be >= 1 and rest_seconds > 0")
    vendor = settings.vendor_platform
    if vendor.enabled and not vendor.url.startswith(("https://", "http://127.0.0.1",
                                                      "http://localhost")):
        raise ConfigurationError("vendor_platform.url must be https (or a local test address)")
    if settings.device_detection.scan_interval_seconds < 1:
        raise ConfigurationError("device_detection.scan_interval_seconds must be at least 1")
    station = settings.station
    if station.poll_seconds <= 0:
        raise ConfigurationError("station.poll_seconds must be positive")
    if "ro" not in station.mount_options.split(",") or "rw" in station.mount_options.split(","):
        raise ConfigurationError("station.mount_options must include ro and must not include rw")
    if settings.ai.effort not in AI_EFFORTS:
        raise ConfigurationError(f"ai.effort must be one of {sorted(AI_EFFORTS)}")
    if settings.ai.max_iterations < 1 or settings.ai.token_budget < 1000:
        raise ConfigurationError("ai.max_iterations and ai.token_budget are too small")
    if settings.api.host not in LOOPBACK_HOSTS:
        raise ConfigurationError("api.host must be a loopback address (remote access is off)")
    if not 0 < settings.api.port < 65536:
        raise ConfigurationError("api.port must be between 1 and 65535")
    if settings.application.log_level.upper() not in {
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL",
    }:
        raise ConfigurationError("application.log_level is not a valid level")


def load_settings(path: str | Path = "config.yaml") -> Settings:
    config_path = Path(path).resolve()
    try:
        with open(config_path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Config file not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Config file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("Config file must contain a mapping")
    settings: Settings = _build(Settings, data, "config")
    settings.base_dir = config_path.parent
    validate(settings)
    return settings
