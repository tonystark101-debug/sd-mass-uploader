"""The operator's upload conversation: metadata, selection, preview, confirmation, run."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Callable
from datetime import date

from src.cli import render
from src.cli.wiring import App
from src.core.enums import SessionStatus, TaskStatus
from src.core.exceptions import ConfigurationError, MetadataValidationError
from src.core.models import Card
from src.device_manager.detector import detect
from src.inventory.inventory_service import find_cards, folder_card, refresh_inventory
from src.metadata.session import SessionPlan, SessionPlanner
from src.metadata.templates import load_metadata_file, save_metadata_file
from src.metadata.validation import SessionMetadata, validate_metadata
from src.uploader.card_locator import locate_after_restart, locate_fresh
from src.uploader.recovery import reconcile
from src.uploader.upload_manager import RunControl, RunResult, UploadManager

Ask = Callable[[str], str]


def collect_metadata(app: App, args: argparse.Namespace, ask: Ask = input) -> SessionMetadata:
    if args.metadata_file:
        return validate_metadata(load_metadata_file(args.metadata_file), app.settings.metadata)
    rules = app.settings.metadata
    prompts = [
        ("activity_name", "Activity name", True, ""),
        ("place_of_recording", "Place of recording", rules.require_place_of_recording, ""),
        ("recording_date", "Recording date (YYYY-MM-DD)", True, date.today().isoformat()),
        ("operator_name", "Operator name", rules.require_operator_name, ""),
        ("project_name", "Project name", True, ""),
        ("description", "Description (optional)", False, ""),
    ]
    values: dict[str, str] = {}
    for key, label, required, default in prompts:
        suffix = f" [{default}]" if default else ""
        while True:
            answer = ask(f"{label}{suffix}: ").strip() or default
            if answer or not required:
                values[key] = answer
                break
            print("  This field is required.")
    metadata = validate_metadata(values, rules)
    if args.save_template:
        save_metadata_file(args.save_template, values)
    return metadata


def select_cards(cards: list[Card], args: argparse.Namespace, ask: Ask = input) -> list[Card]:
    """Ambiguous cards are only ever taken when the operator names them."""
    by_id = {card.card_id: card for card in cards}
    if args.cards:
        wanted = [item.strip() for item in args.cards.split(",") if item.strip()]
        missing = [item for item in wanted if item not in by_id]
        if missing:
            raise ConfigurationError(f"Cards not found: {', '.join(missing)}")
        return [by_id[item] for item in wanted]
    if args.source:
        return cards
    print(render.render_cards(cards))
    clear = [card for card in cards if not card.is_ambiguous]
    if args.yes:
        return clear
    while True:
        answer = ask("Select cards: 'all' for confirmed cards, or numbers such as 1,3: ").strip()
        if answer.lower() == "all":
            return clear
        try:
            return [cards[int(item) - 1] for item in answer.split(",")]
        except (ValueError, IndexError):
            print("  Enter 'all' or numbers from the list.")


def _print_preview(plan: SessionPlan, metadata: SessionMetadata) -> None:
    print("\n" + render.LINE)
    print("Ready to upload")
    print(render.LINE)
    print(f"Session     : {plan.session_id}")
    for key, value in metadata.as_flat_dict().items():
        print(f"{key:<12}: {value}")
    print(f"Destination : {plan.base_prefix}/")
    print("\nCards")
    for summary in plan.scans:
        print(render.render_scan(summary))
    print(f"\nFiles to upload: {plan.planned_files:,} ({render.human_bytes(plan.planned_bytes)})")
    if plan.rejected_paths:
        print(f"Files refused (unsafe names, see report): {plan.rejected_paths:,}")
    for name in plan.sample_objects:
        print(f"  e.g. {name}")


def _overwrite_reason(app: App, args: argparse.Namespace) -> str | None:
    if not args.allow_overwrite:
        return None
    if not app.settings.upload.overwrite_existing:
        raise ConfigurationError("--allow-overwrite also needs upload.overwrite_existing: true")
    if not args.overwrite_reason:
        raise ConfigurationError("--allow-overwrite needs --overwrite-reason")
    return args.overwrite_reason


class _Signals:
    """First Ctrl+C finishes files in progress; the second abandons them."""

    def __init__(self, control: RunControl) -> None:
        self._control = control
        self._saved: dict[int, object] = {}

    def __enter__(self) -> _Signals:
        for number in (signal.SIGINT, signal.SIGTERM):
            self._saved[number] = signal.signal(number, self._handle)
        return self

    def __exit__(self, *_: object) -> None:
        for number, handler in self._saved.items():
            signal.signal(number, handler)

    def _handle(self, *_: object) -> None:
        if self._control.stop.is_set():
            self._control.cancel.set()
            print("\nStopping now; interrupted files will be retried later.", file=sys.stderr)
        else:
            self._control.stop.set()
            print("\nFinishing files in progress. Ctrl+C again to stop now.", file=sys.stderr)


def _progress_printer() -> Callable:
    last = [0.0]
    live = sys.stderr.isatty()

    def tick(view) -> None:
        if time.monotonic() - last[0] < (1 if live else 15):
            return
        last[0] = time.monotonic()
        text = render.render_progress(view)
        print(f"\r{text:<118}" if live else text, end="" if live else "\n", file=sys.stderr)

    return tick


def _run_and_finish(app: App, session_id: str, locator, store,
                    overwrite_reason: str | None) -> int:
    control = RunControl()
    manager = UploadManager(app.settings, app.sessions, store, _progress_printer())
    with _Signals(control):
        result: RunResult = manager.run(session_id, locator, control, overwrite_reason)
    print(file=sys.stderr)
    status, paths = app.finalizer(store).finalize(result)
    print(render.render_report(app.reports().build(session_id)))
    if result.abort_reason:
        print(f"Session stopped early: {result.abort_reason}")
    if result.lost_cards:
        print(f"Cards lost during upload: {', '.join(sorted(result.lost_cards))}")
    for path in (paths.json_path, paths.csv_path, paths.errors_path):
        if path:
            print(f"Wrote {path}")
    if status is not SessionStatus.SUCCESS:
        print(f"To continue: python main.py retry --session {session_id}")
    return 0 if status is SessionStatus.SUCCESS else 1


def run_upload(app: App, args: argparse.Namespace) -> int:
    app.settings.require_gcs()
    overwrite_reason = _overwrite_reason(app, args)
    if args.workers is not None:
        if args.workers < 1:
            raise ConfigurationError("--workers must be at least 1")
        app.settings.upload.max_workers = args.workers
    for session in app.sessions.incomplete():
        print(f"Note: session {session['session_id']} is incomplete; "
              f"`retry --session {session['session_id']}` resumes it.")
    store = app.store()
    store.validate_access()  # Before asking the operator anything.

    if args.source:
        cards = [folder_card(path) for path in args.source]
    else:
        cards = find_cards(refresh_inventory(app.adapter(), app.inventory))
    if not cards:
        print("No uploadable cards found. Run `check` to see why.")
        return 1
    chosen = select_cards(cards, args)
    if not chosen:
        print("No cards selected.")
        return 1
    try:
        metadata = collect_metadata(app, args)
    except MetadataValidationError as exc:
        print(f"Metadata problem: {exc}", file=sys.stderr)
        return 2

    print("\nScanning selected cards...")
    planner = SessionPlanner(app.settings, app.sessions, app.scans)
    plan = planner.plan(metadata, chosen)
    _print_preview(plan, metadata)
    if plan.planned_files == 0:
        print("Nothing to upload.")
        app.sessions.set_status(plan.session_id, SessionStatus.CANCELLED)
        return 1
    if not args.yes and input("\nType 'yes' to start the upload: ").strip().lower() != "yes":
        print("Cancelled. Nothing was uploaded.")
        app.sessions.set_status(plan.session_id, SessionStatus.CANCELLED)
        return 1
    locator = locate_fresh(app.sessions.cards(plan.session_id))
    return _run_and_finish(app, plan.session_id, locator, store, overwrite_reason)


def run_retry(app: App, session_id: str, include_conflicts: bool) -> int:
    store = app.store()
    store.validate_access()
    cards = app.sessions.cards(session_id)
    present = [] if all(row["is_folder"] for row in cards) else find_cards(detect(app.adapter()))
    locator = locate_after_restart(cards, present)
    for row in cards:
        if not locator.is_present(row["card_id"]):
            print(f"Not available: {row['card_id']} ({row['volume_label'] or 'no label'})")

    result = reconcile(app.sessions, store, locator, session_id)
    print(f"Reconciled with GCS: {result.interrupted} interrupted, {result.confirmed} confirmed, "
          f"{result.requeued} requeued, {result.conflicts} conflicts, "
          f"{result.still_uncertain} still uncertain")
    statuses = [TaskStatus.FAILED]
    if include_conflicts:
        statuses.append(TaskStatus.CONFLICT)
    requeued = app.sessions.requeue(session_id, statuses)
    print(f"Queued for retry: {requeued}")
    return _run_and_finish(app, session_id, locator, store, None)
