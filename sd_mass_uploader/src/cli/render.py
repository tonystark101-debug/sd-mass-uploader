"""Terminal presentation only. Human-readable units exist here and nowhere else."""

from __future__ import annotations

from src.core.enums import DeviceStatus
from src.core.models import Card, InventorySnapshot, ScanSummary
from src.core.timeutil import age_seconds
from src.uploader.progress import ProgressView

LINE = "=" * 60


def human_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{value} B"


def human_speed(mbps: float | None) -> str:
    if mbps is None:
        return "unknown"
    return f"{mbps / 1000:g} Gbps" if mbps >= 1000 else f"{mbps:g} Mbps"


def render_snapshot(snapshot: InventorySnapshot, internal: bool = False) -> str:
    lines = [
        LINE,
        f"Inventory {snapshot.snapshot_id}",
        f"Captured {snapshot.captured_at} on {snapshot.platform} ({snapshot.hostname})",
        LINE,
    ]
    hubs = [device for device in snapshot.usb_devices if device.is_hub]
    statuses: dict[str, int] = {}
    for reader in snapshot.readers:
        statuses[reader.status.value] = statuses.get(reader.status.value, 0) + 1
    lines.append(f"USB hubs: {len(hubs)}   USB devices: {len(snapshot.usb_devices) - len(hubs)}"
                 f"   Readers: {len(snapshot.readers)}   Storage: "
                 f"{len(snapshot.storage_devices)}   Volumes: {len(snapshot.volumes)}")
    if statuses:
        lines.append("Reader status: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())))

    if snapshot.readers:
        lines += ["", "Readers"]
        usb = {device.topology_id: device for device in snapshot.usb_devices}
        for reader in snapshot.readers:
            link = usb.get(reader.topology_id or "")
            speed = "unknown"
            if link and link.negotiated_speed_mbps:
                speed = (f"{human_speed(link.negotiated_speed_mbps)} negotiated "
                         f"(source {link.speed_source}, confidence {link.speed_confidence})")
            lines.append(f"  [{reader.status}] {reader.model or 'unnamed'} "
                         f"{reader.vendor_id}:{reader.product_id} ({reader.kind})")
            capability = link.advertised_capability if link else None
            advertised = f"; advertises {capability}" if capability else ""
            lines.append(f"      port {reader.topology_path}; link {speed}{advertised}")

    shown = [s for s in snapshot.storage_devices if internal or s.reader_id or
             (s.bus_type or "").upper() in ("USB", "SD", "MMC") or
             s.status == DeviceStatus.DISCONNECTED]
    hidden = len(snapshot.storage_devices) - len(shown)
    if shown:
        volumes: dict[str | None, list] = {}
        for volume in snapshot.volumes:
            volumes.setdefault(volume.storage_id, []).append(volume)
        lines += ["", "Storage"]
        for device in shown:
            lines.append(f"  [{device.status}] {device.model or 'unnamed'} {device.bus_type or ''}"
                         f" {human_bytes(device.capacity_bytes)}")
            for volume in volumes.get(device.storage_id, []):
                lines.append(
                    f"      {volume.mount_point or '(not mounted)'} "
                    f"{volume.filesystem or '?'} label={volume.volume_label or '-'} "
                    f"free={human_bytes(volume.free_bytes)} of "
                    f"{human_bytes(volume.capacity_bytes)} access={volume.access_status}")
    if hidden:
        lines.append(f"\n({hidden} internal disk(s) hidden; they can never be uploaded from.)")
    if not snapshot.readers:
        lines.append("\nNo card readers detected.")
    if snapshot.issues:
        lines += ["", "Issues"]
        lines += [f"  {i.severity} {i.scope}: {i.message}" for i in snapshot.issues]
    return "\n".join(lines)


def render_usb_tree(snapshot: InventorySnapshot) -> str:
    children: dict[str | None, list] = {}
    for device in snapshot.usb_devices:
        children.setdefault(device.parent_topology_id, []).append(device)
    lines = ["", "USB topology"]

    def walk(parent: str | None, depth: int) -> None:
        for device in children.get(parent, []):
            kind = "hub" if device.is_hub else "device"
            speed = human_speed(device.negotiated_speed_mbps)
            lines.append(f"  {'  ' * depth}{device.product_name or 'unnamed'} [{kind}] "
                         f"{device.vendor_id or '----'}:{device.product_id or '----'} link {speed}")
            walk(device.topology_id, depth + 1)

    walk(None, 0)
    return "\n".join(lines)


def render_cards(cards: list[Card]) -> str:
    if not cards:
        return "No uploadable cards found."
    lines = []
    for index, card in enumerate(cards, 1):
        lines.append(f"  {index}. {card.card_id}  at {card.mount_point}  "
                     f"{card.filesystem or '?'}  label={card.volume_label or '-'}  "
                     f"{human_bytes(card.capacity_bytes)}  reader={card.reader_model or '?'}  "
                     f"link={human_speed(card.negotiated_speed_mbps)}")
        for reason in card.ambiguity:
            lines.append(f"       needs confirmation: {reason}")
    return "\n".join(lines)


def render_scan(summary: ScanSummary) -> str:
    extensions = sorted(summary.by_extension.items(), key=lambda item: -item[1])[:6]
    top = ", ".join(f"{name}={count}" for name, count in extensions) or "none"
    return (f"  {summary.card_id} [{summary.status}]: {summary.file_count:,} files, "
            f"{human_bytes(summary.total_bytes)}, {summary.dir_count:,} folders, "
            f"{summary.error_count} errors, {summary.unreadable_count} unreadable, "
            f"{summary.excluded_count} excluded\n      types: {top}")


def render_progress(view: ProgressView) -> str:
    return (f"pending {view.files_pending:,} active {view.files_active} "
            f"done {view.files_successful:,} failed {view.files_failed:,} "
            f"retries {view.retries} | {human_bytes(view.bytes_uploaded)} sent, "
            f"{human_bytes(view.bytes_pending)} left | "
            f"{view.current_mb_per_s:.1f} MB/s now, {view.average_mb_per_s:.1f} MB/s avg | "
            f"{view.elapsed_seconds:.0f}s")


def render_report(report: dict) -> str:
    def measured(value: object, suffix: str = "") -> str:
        return "not measured" if value is None else f"{value}{suffix}"

    rows = [
        ("Session ID", report["session_id"]),
        ("Cards discovered", report["cards_discovered"]),
        ("Cards scanned", report["cards_scanned"]),
        ("Cards with errors", report["cards_with_errors"]),
        ("", ""),
        ("Files discovered", f"{report['files_discovered']:,}"),
        ("Files verified", f"{report['files_verified']:,}"),
        ("Files unverified", f"{report['files_uploaded_unverified']:,}"),
        ("Files failed", f"{report['files_failed']:,}"),
        ("Files in conflict", f"{report['files_conflict']:,}"),
        ("Files uncertain", f"{report['files_uncertain']:,}"),
        ("Files pending", f"{report['files_pending']:,}"),
        ("Scan errors", f"{report['scan_errors']:,}"),
        ("", ""),
        ("Source bytes", human_bytes(report["source_bytes"])),
        ("Verified bytes", human_bytes(report["verified_bytes"])),
        ("Average throughput", f"{measured(report['average_mb_per_s'], ' MB/s')} "
                               f"({measured(report['average_mib_per_s'], ' MiB/s')})"),
        ("Elapsed time", measured(report["elapsed_seconds"], " s")),
        ("", ""),
        ("Session status", report["status"]),
    ]
    body = [f"{label:<27}: {value}" if label else "" for label, value in rows]
    if report.get("last_error"):
        body.append(f"Last error                 : {report['last_error']}")
    return "\n".join([LINE, "UPLOAD REPORT".center(60), LINE, *body, LINE])


def freshness(captured_at: str, stale_after: int) -> str:
    age = age_seconds(captured_at)
    label = "STALE, run `check` to refresh" if age > stale_after else "recent, not live"
    return f"Last refreshed {captured_at} ({age:.0f}s ago; {label})"
