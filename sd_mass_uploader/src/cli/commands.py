"""Command-line parsing and dispatch. All real work happens in the services."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

from src.cli import render
from src.cli.station_commands import add_station_parsers
from src.cli.upload_flow import run_retry, run_upload
from src.cli.wiring import App
from src.core.exceptions import ConfigurationError, UploaderError
from src.core.timeutil import utc_now
from src.database.station_repository import StationRepository
from src.device_manager.detector import detect
from src.inventory.inventory_service import find_cards, folder_card, refresh_inventory
from src.reporting.logger import configure_logging
from src.uploader.benchmark import measure_read
from src.uploader.card_locator import locate_after_restart
from src.verification.checksums import crc32c_of_bytes
from src.verification.cloud_verifier import CloudVerifier

logger = logging.getLogger(__name__)


def cmd_check(app: App, args: argparse.Namespace) -> int:
    snapshot = refresh_inventory(app.adapter(), app.inventory)
    if args.json:
        print(json.dumps(snapshot.to_dict(), indent=2, default=str, ensure_ascii=False))
        return 0
    print(render.render_snapshot(snapshot, internal=args.all))
    print(render.render_usb_tree(snapshot))
    print(f"\nSnapshot saved: {snapshot.snapshot_id}")
    return 0


def cmd_scan(app: App, args: argparse.Namespace) -> int:
    if args.sha256 and not args.files:
        raise ConfigurationError("--sha256 needs --files")
    if args.path:
        cards = [folder_card(path) for path in args.path]
    else:
        cards = find_cards(refresh_inventory(app.adapter(), app.inventory))
    print("Storage scan: volumes found")
    print(render.render_cards(cards))
    if not args.files:
        print("\nStorage scan only; files were not listed. Add --files for a full file scan.")
        return 0
    print("\nFull file scan (read-only)")
    worst = 0
    for card in cards:
        summary = app.scan_service().scan_card(card.card_id, card.mount_point,
                                               hash_files=args.sha256)
        print(render.render_scan(summary))
        worst = 1 if summary.error_count else worst
    return worst


def cmd_status(app: App, args: argparse.Namespace) -> int:
    snapshot = app.inventory.latest()
    if snapshot is None:
        print("No inventory yet. Run `check`.")
    else:
        stale = app.settings.device_detection.inventory_stale_seconds
        print(render.freshness(snapshot.captured_at, stale))
        print(render.render_snapshot(snapshot))
    open_sessions = app.sessions.incomplete()
    if open_sessions:
        print("\nIncomplete sessions (resume with `retry --session ID`):")
        for session in open_sessions:
            counts = app.sessions.status_counts(session["session_id"])
            summary = ", ".join(f"{k}={v['files']}" for k, v in sorted(counts.items()))
            print(f"  {session['session_id']} [{session['status']}] {summary}")
    latest = app.sessions.latest_id()
    if latest:
        print(f"\nLatest session: {latest} [{app.sessions.get(latest)['status']}]")
    return 0


def _monitor_key(snapshot) -> dict[str, str]:
    state = {r.reader_id: f"reader {r.model or 'unnamed'} at {r.topology_path}: {r.status}"
             for r in snapshot.readers}
    state.update({s.storage_id: f"storage {s.model or 'unnamed'}: {s.status}"
                  for s in snapshot.storage_devices})
    return state


def diff_inventory(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changes = [f"REMOVED {text}" for key, text in before.items() if key not in after]
    changes += [f"ADDED   {text}" for key, text in after.items() if key not in before]
    changes += [f"CHANGED {after[key]}" for key in before.keys() & after.keys()
                if before[key] != after[key]]
    return changes


def cmd_monitor(app: App, args: argparse.Namespace) -> int:
    """Poll the OS. Each pass can take several seconds on Windows, so this is not instant."""
    interval = args.interval or app.settings.device_detection.scan_interval_seconds
    adapter = app.adapter()
    print(f"Watching every {interval}s. Press Ctrl+C to stop.")
    previous: dict[str, str] | None = None
    passes = 0
    try:
        while True:
            snapshot = detect(adapter)
            current = _monitor_key(snapshot)
            if previous is None:
                print(f"{utc_now()} watching {len(current)} device(s)")
            else:
                changes = diff_inventory(previous, current)
                for change in changes:
                    print(f"{utc_now()} {change}")
                if changes:
                    app.inventory.save(snapshot)
            previous = current
            passes += 1
            if args.passes and passes >= args.passes:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped watching.")
        return 0


def _session_or_latest(app: App, session_id: str | None) -> str:
    chosen = session_id or app.sessions.latest_id()
    if not chosen or app.sessions.get(chosen) is None:
        raise ConfigurationError(f"No such session: {session_id or '(none yet)'}")
    return chosen


def cmd_retry(app: App, args: argparse.Namespace) -> int:
    return run_retry(app, _session_or_latest(app, args.session), args.include_conflicts)


def cmd_verify(app: App, args: argparse.Namespace) -> int:
    session_id = _session_or_latest(app, args.session)
    store = app.store()
    locator = None
    if args.deep:
        cards = find_cards(detect(app.adapter()))
        locator = locate_after_restart(app.sessions.cards(session_id), cards)
    result = CloudVerifier(app.sessions, store).verify_session(session_id, locator)
    print(f"Session {session_id}")
    for key, value in asdict(result).items():
        print(f"  {key.replace('_', ' ')}: {value:,}")
    app.finalizer(store).refresh_after_verify(session_id)
    print(render.render_report(app.reports().build(session_id)))
    return 0 if not (result.mismatched or result.missing) else 1


def cmd_report(app: App, args: argparse.Namespace) -> int:
    session_id = _session_or_latest(app, args.session)
    generator = app.reports()
    paths = generator.write(session_id)
    print(render.render_report(generator.build(session_id)))
    for path in (paths.json_path, paths.csv_path, paths.errors_path):
        if path:
            print(f"Wrote {path}")
    return 0


def cmd_stop(app: App, args: argparse.Namespace) -> int:
    station = StationRepository(app.db).current()
    if station is not None:
        # A station session never "ends" on stop; pausing keeps every card resumable.
        StationRepository(app.db).enqueue("pause_all")
        print(f"Station session {station['session_id']}: pause requested for every card. "
              "Use `station resume-all` to continue, or `station close` to end it.")
    running = [s for s in app.sessions.incomplete() if s["status"] == "UPLOADING"
               and (station is None or s["session_id"] != station["session_id"])]
    if station is not None and not running:
        return 0
    if not running:
        print("No session is uploading.")
        return 0
    for session in running:
        app.sessions.request_stop(session["session_id"])
        print(f"Stop requested for {session['session_id']}.")
    print("Files in progress will finish first, so this can take a while on large files.")
    return 0


def cmd_gcs_test(app: App, args: argparse.Namespace) -> int:
    """Upload one small generated file to a test prefix and confirm it."""
    app.settings.require_gcs()
    store = app.store()
    store.validate_access()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    root = app.settings.google_cloud.root_prefix.strip("/")
    name = f"{root}/_connectivity_test/{stamp}/test.txt"
    body = f"SD Mass Uploader connectivity test {stamp}\n".encode()
    remote = store.upload_bytes(name, body, "text/plain", 0)
    check = store.get_object(name)
    expected = crc32c_of_bytes(body)
    passed = check is not None and check.size == len(body) and check.crc32c == expected
    result = {"object": name, "size": len(body), "local_crc32c": expected,
              "remote_crc32c": check.crc32c if check else None,
              "generation": remote.generation, "passed": passed, "at": utc_now()}
    folder = app.settings.resolve(app.settings.reporting.directory)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"gcs-test-{stamp}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print("\nPASSED" if passed else "\nFAILED: remote object does not match")
    return 0 if passed else 1


def cmd_benchmark(app: App, args: argparse.Namespace) -> int:
    cards = ([folder_card(path) for path in args.path] if args.path
             else find_cards(refresh_inventory(app.adapter(), app.inventory)))
    if not cards:
        print("Nothing to measure.")
        return 1
    limit = int(args.limit_mib * 1024 * 1024)
    results = measure_read(cards, app.settings.file_scanning, limit)
    print(f"Simultaneous read test, up to {args.limit_mib:g} MiB per card")
    for item in results:
        print(f"  {item.card_id}: {item.mb_per_s:.1f} MB/s, {item.bytes_read:,} bytes "
              f"in {item.seconds:.1f}s, {item.errors} errors")
    total = sum(item.mb_per_s for item in results)
    print(f"  Combined: {total:.1f} MB/s across {len(results)} card(s)")
    folder = app.settings.resolve(app.settings.reporting.directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"benchmark-{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps({"at": utc_now(), "limit_mib": args.limit_mib,
                                "results": [asdict(item) for item in results]}, indent=2),
                    encoding="utf-8")
    print(f"Wrote {path}")
    return 0


def cmd_lines(app: App, args: argparse.Namespace) -> int:
    """List the internet lines and check each one reaches Google on its own."""
    lines = app.lines()
    if lines is None:
        print("Several internet lines are not in use here (network.enabled is false, this "
              "system is not supported, or a storage emulator is set). Uploads use the "
              "computer's default connection.")
        return 0
    try:
        while True:
            lines.refresh()
            print(render.render_lines(lines.view()))
            if not args.watch:
                break
            print(f"(checking again in {app.settings.network.rescan_seconds:g} s; "
                  "Ctrl+C to stop)\n")
            time.sleep(app.settings.network.rescan_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        lines.stop()
    working = lines.view()["working"]
    return 0 if working else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="main.py", description="SD Mass Uploader")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="Refresh hardware inventory")
    check.add_argument("--json", action="store_true", help="Print the normalized inventory")
    check.add_argument("--all", action="store_true", help="Also list internal disks")
    check.set_defaults(handler=cmd_check)

    scan = commands.add_parser("scan", help="Scan storage, and files with --files")
    scan.add_argument("--files", action="store_true", help="Full file scan")
    scan.add_argument("--sha256", action="store_true", help="Hash every file (slow)")
    scan.add_argument("--path", action="append", help="Scan this folder instead of cards")
    scan.set_defaults(handler=cmd_scan)

    commands.add_parser("status", help="Show latest stored state").set_defaults(handler=cmd_status)

    monitor = commands.add_parser("monitor", help="Watch for device changes")
    monitor.add_argument("--interval", type=int)
    monitor.add_argument("--passes", type=int, help="Exit after this many checks")
    monitor.set_defaults(handler=cmd_monitor)

    upload = commands.add_parser("upload", help="Collect metadata and upload")
    upload.add_argument("--metadata-file", help="JSON file with session metadata")
    upload.add_argument("--save-template", help="Save the answers to this JSON file")
    upload.add_argument("--source", action="append",
                        help="Upload this folder as a card (not a detected card)")
    upload.add_argument("--cards", help="Comma-separated card IDs, instead of the prompt")
    upload.add_argument("--yes", action="store_true", help="Skip the final confirmation")
    upload.add_argument("--workers", type=int, help="Override upload.max_workers for this run")
    upload.add_argument("--allow-overwrite", action="store_true")
    upload.add_argument("--overwrite-reason", help="Required with --allow-overwrite")
    upload.set_defaults(handler=lambda app, args: run_upload(app, args))

    retry = commands.add_parser("retry", help="Reconcile, then retry eligible files")
    retry.add_argument("--session")
    retry.add_argument("--include-conflicts", action="store_true",
                       help="Check conflicts again; identical files are accepted")
    retry.set_defaults(handler=cmd_retry)

    verify = commands.add_parser("verify", help="Verify remote objects")
    verify.add_argument("--session")
    verify.add_argument("--deep", action="store_true", help="Also re-read the source cards")
    verify.set_defaults(handler=cmd_verify)

    report = commands.add_parser("report", help="Generate reports")
    report.add_argument("--session")
    report.set_defaults(handler=cmd_report)

    commands.add_parser("stop", help="Ask a running upload to stop gracefully"
                        ).set_defaults(handler=cmd_stop)
    commands.add_parser("gcs-test", help="Upload and verify one small test file"
                        ).set_defaults(handler=cmd_gcs_test)

    benchmark = commands.add_parser("benchmark", help="Measure simultaneous read speed")
    benchmark.add_argument("--path", action="append", help="Measure this folder instead of cards")
    benchmark.add_argument("--limit-mib", type=float, default=512)
    benchmark.set_defaults(handler=cmd_benchmark)

    lines = commands.add_parser("lines", help="List internet lines and check each reaches Google")
    lines.add_argument("--watch", action="store_true",
                       help="Keep checking, to see a line appear when it is plugged in")
    lines.set_defaults(handler=cmd_lines)
    add_station_parsers(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # File names may not fit the console encoding.
    args = build_parser().parse_args(argv)
    config = args.config or str(Path(__file__).resolve().parents[2] / "config.yaml")
    try:
        app = App.open(config)
        configure_logging(app.settings)
        logger.info("Command: %s", args.command)
        return args.handler(app, args)
    except UploaderError as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        print(f"Error ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
