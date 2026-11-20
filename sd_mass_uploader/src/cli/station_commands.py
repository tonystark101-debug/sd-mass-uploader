"""CLI for the station: run it, send it commands, read its state, and simulate cards."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from src.api.client import ApiUnavailable, StationClient, route
from src.cli import render
from src.cli.wiring import App
from src.core.exceptions import ConfigurationError
from src.core.timeutil import age_seconds
from src.database.station_repository import StationRepository
from src.metadata.templates import load_metadata_file
from src.station.daemon import Daemon, read_state
from src.station.simulator import create_card, eject_card, insert_card

_WAIT_SECONDS = 15.0


def cmd_daemon(app: App, args: argparse.Namespace) -> int:
    daemon = Daemon(app, simulate_dir=args.simulate, metadata_file=args.metadata_file)
    source = "simulated cards in " + app.settings.station.simulator_dir \
        if app.settings.station.simulator_dir else "card readers"
    print(f"Station running, watching {source}. Press Ctrl+C to stop "
          "(files in progress finish first).")
    return daemon.run(max_seconds=args.max_seconds)


# ---------------------------------------------------------------- station commands

def _send(app: App, command: str, args: dict | None = None, wait: float = _WAIT_SECONDS) -> int:
    """Through the API when it is reachable; otherwise through the database command queue."""
    client = StationClient.from_settings(app.settings, timeout=max(wait, _WAIT_SECONDS))
    if client is not None:
        method, path, body = route(command, args or {})
        try:
            status, payload = client.request(method, path, body)
        except ApiUnavailable:
            pass  # Not running, or the API is off: the queue still reaches a running station.
        else:
            return _print_api_result(status, payload)
    return _send_queued(app, command, args, wait)


def _print_api_result(status: int, payload) -> int:
    if 200 <= status < 300:
        values = [value for key, value in (payload or {}).items() if key != "ok"]
        print("ok" + (f": {values[0]}" if values else ""))
        return 0
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    print(f"refused: {detail}" if status in (404, 409) else f"error (HTTP {status}): {detail}")
    return 1


def _send_queued(app: App, command: str, args: dict | None, wait: float) -> int:
    repo = StationRepository(app.db)
    command_id = repo.enqueue(command, args)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        row = repo.command(command_id)
        if row and row["finished_at"]:
            print(row["result"])
            return 0 if row["result"].startswith("ok") else 1
        time.sleep(0.25)
    print(f"Queued (command {command_id}), but the station did not answer within {wait:.0f}s. "
          "Is `main.py daemon` running?")
    return 1


def cmd_station(app: App, args: argparse.Namespace) -> int:
    action = args.station_action
    if action == "status":
        return _status(app, args)
    if action == "open":
        metadata = load_metadata_file(args.metadata_file)
        payload: dict = {"metadata": metadata}
        if args.manual_start:
            payload["auto_upload"] = False
        return _send(app, "open", payload)
    if action == "close":
        return _send(app, "close", {"force": args.force}, wait=args.wait)
    if action in {"pause", "resume", "start", "confirm", "retry"}:
        return _send(app, action, {"card_id": args.card_id})
    if action == "place":
        return _send(app, "place", {"card_id": args.card_id, "device": args.device,
                                    "skip": args.skip})
    if action == "auto-upload":
        return _send(app, "auto_upload", {"enabled": args.state == "on"})
    return _send(app, action.replace("-", "_"))


def _status(app: App, args: argparse.Namespace) -> int:
    state = None
    client = StationClient.from_settings(app.settings, timeout=5)
    if client is not None:
        try:
            state = client.state()
        except ApiUnavailable:
            state = None
    if state is None:  # The file the station writes on every change.
        state = read_state(app.settings.resolve(app.settings.station.state_file))
    if state is None:
        print("No station state yet. Start it with `python main.py daemon`.")
        return 1
    if args.json:
        print(json.dumps(state, indent=2))
        return 0
    print(render_station(state, app.settings.station.poll_seconds))
    return 0


def render_station(state: dict, poll_seconds: float) -> str:
    lines: list[str] = []
    daemon = state.get("daemon") or {}
    age = age_seconds(state["updated_at"])
    if not daemon.get("running"):
        lines.append(f"Station is STOPPED (last state {age:.0f}s ago).")
    elif age > max(30.0, poll_seconds * 5):
        lines.append(f"Station is NOT RESPONDING: state is {age:.0f}s old. Data below is stale.")
    else:
        lines.append(f"Station running (pid {daemon.get('pid')}, {daemon.get('source')}), "
                     f"updated {age:.0f}s ago.")
    engine = state.get("engine") or {}
    if engine.get("problem") or engine.get("store_error"):
        lines.append("UPLOADS ARE NOT RUNNING. See PROBLEMS below.")
    session = state.get("session")
    if session is None:
        waiting = state.get("waiting_cards") or []
        lines.append("No session is open. Start one with: "
                     "python main.py station open --metadata-file session.json")
        if waiting:
            lines.append(f"{len(waiting)} card(s) inserted and waiting for a session.")
    else:
        meta = session.get("metadata") or {}
        lines.append(f"Session {session['session_id']}  |  {meta.get('activity_name', '')}  |  "
                     f"{meta.get('place_of_recording', '')}  |  {meta.get('recording_date', '')}"
                     + ("  |  ALL PAUSED" if session.get("paused_all") else ""))
        c = state["counters"]
        lines.append(
            f"Inserted {c['inserted']}  Total {c['total']}  Uploading {c['uploading']}  "
            f"Queued {c['queued']}  Uploaded {c['uploaded']}  Failed {c['failed']}  "
            f"Paused {c['paused']}  Removed {c['removed']}"
            + (f"  Need confirmation {c['needs_confirmation']}" if c["needs_confirmation"]
               else "")
        )
        p = state.get("progress") or {}
        if p.get("bytes_total"):
            share = 100 * p["bytes_done"] / p["bytes_total"]
            eta = p.get("eta_seconds")
            lines.append(
                f"{share:.0f}%  {render.human_bytes(p['bytes_done'])} of "
                f"{render.human_bytes(p['bytes_total'])}  |  {p['current_mb_per_s']} MB/s now  |  "
                + (f"about {eta // 60} min left" if eta else "time left unknown")
            )
        lines.append("")
        lines.append(f"{'Card':<28} {'State':<22} {'Files':>13} {'Size':>21}  Note")
        for card in state.get("cards") or []:
            files = f"{card['files_done']}/{card['files_total']}"
            size = (f"{render.human_bytes(card['bytes_done'])}/"
                    f"{render.human_bytes(card['bytes_total'])}")
            label = card["card_id"] + (f" ({card['label']})" if card.get("label") else "")
            lines.append(f"{label[:28]:<28} {card['state']:<22} {files:>13} {size:>21}  "
                         f"{card.get('reason') or ''}")
    lines += _destination_lines(state.get("destination") or {})
    problems = state.get("problems") or []
    if problems:
        lines += ["", f"PROBLEMS ({len(problems)})"]
    for problem in problems:
        lines.append(f"[{problem['severity'].upper()}] {problem['title']}  ({problem['code']})")
        lines.append(f"    {problem['message']}")
        for number, step in enumerate(problem["steps"], 1):
            lines.append(f"    {number}. {step}")
        fix = _cli_fix(problem.get("action"))
        if fix:
            lines.append(f"    Fix: {fix}")
        lines.append(f"    Ask AI: python main.py ai --problem {problem['id']}")
    return "\n".join(lines)


def _destination_lines(destination: dict) -> list[str]:
    if destination.get("profile") != "peakai":
        return []
    pack = destination["pack"]
    keeper = destination.get("keeper") or {}
    lines = ["", f"PEAKAI  pack {pack['name']} (v{pack['version']}, {pack['dated']})  "
                 f"->  gs://{pack['bucket']}"]
    if keeper.get("allowed") is None:
        lines.append("  Passes: not fetched yet")
    for item in keeper.get("passes") or []:
        token = (f"{item['minutes_left']} min left" if item["has_token"] else "NO valid token")
        error = f"  ({item['last_error']})" if item.get("last_error") else ""
        lines.append(f"  {item['name']:<11} {item['folder']:<16} {token}{error}")
    for report in destination.get("reports") or []:
        state = report["last_error"] or f"{report['status']} (sent {report['sent_at']})"
        lines.append(f"  Dashboard {report['pass_name']}: {state}")
    for card_id, card in (destination.get("cards") or {}).items():
        parts = ", ".join(f"{group['device']} -> {group.get('folder') or group['pass']}"
                          for group in card["groups"]) or "not placed"
        lines.append(f"  {card_id}: card-{card['serial'] or '?'}  {parts}")
    return lines


def _cli_fix(action: dict | None) -> str | None:
    if not action:
        return None
    commands = {
        "retry_card": "python main.py station retry {card}",
        "confirm_card": "python main.py station confirm {card}",
        "resume_card": "python main.py station resume {card}",
        "resume_engine": "python main.py station resume-engine",
        "place_card": 'python main.py station place {card} --device "<device>"  (or --skip)',
        "retry_reports": "python main.py station retry-reports",
    }
    template = commands.get(action["kind"])
    return template.format(card=action.get("card_id") or "") if template else None


# ---------------------------------------------------------------- simulator

def _simulator_root(app: App, args: argparse.Namespace) -> Path:
    folder = args.dir or app.settings.station.simulator_dir
    if not folder:
        raise ConfigurationError("Give --dir, or set station.simulator_dir in config.yaml")
    root = Path(folder)
    return root if root.is_absolute() else app.settings.base_dir / root


def cmd_simulate(app: App, args: argparse.Namespace) -> int:
    root = _simulator_root(app, args)
    if args.sim_action == "create":
        create_card(root, args.name, args.files, int(args.size_kib * 1024))
        print(f"Inserted simulated card {args.name}: {args.files} files of {args.size_kib} KiB")
    elif args.sim_action == "remove":
        eject_card(root, args.name)
        print(f"Removed simulated card {args.name}")
    else:
        insert_card(root, args.name)
        print(f"Inserted simulated card {args.name}")
    return 0


def cmd_problems(app: App, args: argparse.Namespace) -> int:
    """The catalog of problems, as a list or as the generated help page."""
    from src.advisor.catalog import CATALOG, catalog_markdown

    if args.markdown:
        print(catalog_markdown())
        return 0
    for spec in sorted(CATALOG.values(), key=lambda item: item.code):
        print(f"{spec.code:<26} {spec.severity.value:<9} {spec.title}")
    return 0


_ENTRY_PREFIX = {"operator": "You", "assistant": "AI", "note": "*"}


def _print_entry(entry: dict) -> None:
    kind, text = entry["kind"], entry["text"]
    if kind in ("operator", "assistant"):
        print(f"\n{_ENTRY_PREFIX[kind]}: {text}")
    elif kind == "tool_call":
        print(f"  > {text}")
    elif kind == "tool_result":
        lines = text.splitlines() or [""]
        flag = "error" if entry.get("is_error") else "ok"
        verdict = entry.get("verdict")
        shown = "\n      ".join(lines[:6]) + ("\n      ..." if len(lines) > 6 else "")
        print(f"    [{flag}{'' if verdict in (None, 'auto') else ', ' + verdict}] {shown}")
    elif kind == "approval":
        if entry.get("verdict") != "pending":
            print(f"  * {text}")
    else:
        print(f"  * {text}")


def cmd_ai(app: App, args: argparse.Namespace) -> int:
    """A terminal conversation with the station's AI troubleshooter, through the station API."""
    import getpass

    client = StationClient.from_settings(app.settings, timeout=30)
    if client is None:
        print("The station API is not available to this user (is the daemon running, and may "
              "you read its token?).")
        return 1
    operator = getpass.getuser()
    try:
        if args.session:
            session_id = args.session
        else:
            question = " ".join(args.question) or None
            status, body = client.request("POST", "/api/v1/ai/sessions", {
                "problem_id": args.problem, "question": question, "operator": operator})
            if status != 201:
                return _print_api_result(status, body)
            session_id = body["ai_session_id"]
            print(f"AI session {session_id}. Commands it wants to run need your approval. "
                  "Ctrl+C stops it.")
        return _follow(client, session_id, operator, args.no_follow_up)
    except ApiUnavailable as exc:
        print(f"Lost contact with the station: {exc}")
        return 1


def _follow(client: StationClient, session_id: str, operator: str, no_follow_up: bool) -> int:
    path = f"/api/v1/ai/sessions/{session_id}"
    shown = 0
    while True:
        try:
            status, session = client.request("GET", path)
            if status != 200:
                return _print_api_result(status, session)
            for entry in session["transcript"][shown:]:
                _print_entry(entry)
            shown = len(session["transcript"])
            pending = session.get("pending_approval")
            if pending:
                print(f"\nThe AI wants to: {pending['summary']}\n  Why: {pending['reason']}")
                answer = input("Allow this? [y/N] ").strip().lower()
                client.request("POST", f"{path}/approvals/{pending['approval_id']}",
                               {"approve": answer in ("y", "yes"), "operator": operator})
                continue
            if session["status"] in ("idle", "failed", "stopped"):
                if no_follow_up:
                    return 0 if session["status"] == "idle" else 1
                text = input("\nYou (Enter to finish): ").strip()
                if not text:
                    return 0 if session["status"] == "idle" else 1
                client.request("POST", f"{path}/messages", {"text": text, "operator": operator})
                continue
            time.sleep(0.5)
        except (KeyboardInterrupt, EOFError):
            client.request("POST", f"{path}/stop", {})
            print("\nStopped.")
            return 130


def cmd_console(app: App, args: argparse.Namespace) -> int:
    """Open the Operator Console, signed in. The token travels in the link's #fragment, which
    the browser keeps to itself (it is never sent to the server or written to its logs)."""
    import webbrowser

    from src.api.client import console_link

    link = console_link(app.settings)
    if link is None:
        print(_NO_TOKEN)
        return 1
    if args.print_link:
        print(link)
        return 0
    if not webbrowser.open(link):
        print("Could not open a browser. Use --print-link and open the link yourself.")
        return 1
    print(f"Opened the console at {link.split('#', 1)[0]} (signed in).")
    return 0


_NO_TOKEN = ("Cannot read the station's API token. Start the station once (main.py daemon), "
             "and run this as a user allowed to read it.")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def cmd_desktop(app: App, args: argparse.Namespace) -> int:
    """The desktop companion: panel indicator with notifications, or the console window."""
    from src.api.client import console_link

    if args.check:
        return _desktop_check(app)
    if args.window:
        from src.desktop.window import open_console

        link = console_link(app.settings)
        if link is None:
            print(_NO_TOKEN)
            return 1
        how = open_console(link, fullscreen=args.fullscreen)
        if how == "browser":
            print("Opened the console in the browser (install pywebview for its own window).")
        return 0
    return _run_indicator(app, args)


def _run_indicator(app: App, args: argparse.Namespace) -> int:
    import sys
    import webbrowser

    from src.api.client import console_link, route
    from src.desktop.feed import StationFeed
    from src.desktop.indicator import run_indicator
    from src.desktop.notify import create_notifier
    from src.platform.process import launch_console_window

    def client() -> StationClient | None:
        return StationClient.from_settings(app.settings, timeout=5)

    config = args.config or str(_PROJECT_ROOT / "config.yaml")

    def open_window() -> None:
        argv = [sys.executable, str(_PROJECT_ROOT / "main.py"), "--config", str(config),
                "desktop", "--window"]
        try:
            launch_console_window(argv)
        except Exception:  # noqa: BLE001 - fall back to the browser
            link = console_link(app.settings)
            if link:
                webbrowser.open(link)

    def command(action: str) -> None:
        station = client()
        if station is None:
            return
        method, path, body = route(action, {})
        try:
            station.request(method, path, body)
        except ApiUnavailable:
            pass

    return run_indicator(StationFeed(client), create_notifier(), open_window, command)


def _desktop_check(app: App) -> int:
    """What this computer supports, so setup problems are easy to see."""
    from src.api.app import CONSOLE_DIR
    from src.api.client import console_link
    from src.desktop.window import webview_available

    rows: list[tuple[str, bool, str]] = []
    link = console_link(app.settings)
    rows.append(("Station API token readable", link is not None,
                 "" if link else "add this user to the station's group, or run as it"))
    reachable = False
    if link:
        try:
            StationClient.from_settings(app.settings, timeout=5).state()
            reachable = True
        except ApiUnavailable as exc:
            rows.append(("Station answering", False, str(exc)))
    if link and reachable:
        rows.append(("Station answering", True, ""))
    rows.append(("Console built", (CONSOLE_DIR / "index.html").exists(),
                 "run npm run build in web/"))
    rows.append(("Console window (pywebview)", webview_available(),
                 "pip install pywebview; uses the browser until then"))
    gtk = _gi_has("Gtk", "3.0")
    rows.append(("GTK 3 (python3-gi)", gtk, "sudo apt install python3-gi gir1.2-gtk-3.0"))
    rows.append(("Panel indicator (AyatanaAppIndicator)",
                 _gi_has("AyatanaAppIndicator3", "0.1") or _gi_has("AppIndicator3", "0.1"),
                 "sudo apt install gir1.2-ayatanaappindicator3-0.1"))
    rows.append(("Notifications (libnotify)", _gi_has("Notify", "0.7"),
                 "sudo apt install gir1.2-notify-0.7"))
    for name, ok, fix in rows:
        print(f"{'OK     ' if ok else 'MISSING'}  {name}" + ("" if ok or not fix else f"  ({fix})"))
    import shutil

    cage = shutil.which("cage") is not None
    print(f"{'OK     ' if cage else 'OPTIONAL'}  Kiosk mode (cage)"
          + ("" if cage else "  (only for kiosk mode: sudo bash scripts/ubuntu/kiosk.sh enable)"))
    return 0 if all(ok for _, ok, _ in rows) else 1


def _gi_has(namespace: str, version: str) -> bool:
    try:
        import gi

        gi.require_version(namespace, version)
        __import__("gi.repository", fromlist=[namespace])
        return True
    except (ImportError, ValueError):
        return False


def cmd_pack(app: App, args: argparse.Namespace) -> int:
    """Import, show or roll back PeakAI's context pack. The laptop key is never printed."""
    from src.peakai.importing import check_pack
    from src.peakai.pack import apply_pack, load_active, rollback_pack

    settings = app.settings
    pack_dir = settings.resolve(settings.peakai.pack_dir)
    key_file = settings.resolve(settings.peakai.key_file)
    if args.pack_action == "status":
        pack = load_active(pack_dir)
        if pack is None:
            print("No PeakAI pack is imported. Import one with: python main.py pack import <zip>")
            return 1
        print(f"Active pack: {pack.source_name} (v{pack.version}, {pack.dated}), imported "
              f"{pack.imported_at}")
        print(f"Bucket gs://{pack.bucket}, token service {pack.token_url}")
        print(f"vendor_code for _info files: {settings.peakai.vendor_code or '(not set)'}")
        return 0
    if args.pack_action == "rollback":
        pack = rollback_pack(pack_dir, key_file)
        print(f"Back to pack {pack.source_name} (v{pack.version}). Restart the station.")
        return 0

    report = check_pack(args.zip, settings.peakai, offline=args.offline)
    print("\n".join(report.lines()))
    if not report.ok:
        print("\nNothing was changed.")
        return 2
    if not args.yes and input("\nApply this pack? Type 'yes': ").strip().lower() != "yes":
        print("Nothing was changed.")
        return 1
    pack = apply_pack(report, pack_dir, key_file)
    print(f"Pack applied. Laptop key stored privately at {key_file}.")
    print("Restart the station to use it. The zip still holds the key: keep it somewhere "
          "private, or delete it.")
    return 0 if pack else 1


# ---------------------------------------------------------------- parser

def add_station_parsers(commands) -> None:
    daemon = commands.add_parser("daemon", help="Run the always-on station")
    daemon.add_argument("--simulate", help="Use sub-folders of this folder as cards")
    daemon.add_argument("--metadata-file", help="Open a session with these details if none is")
    daemon.add_argument("--max-seconds", type=float, help="Stop after this long (testing)")
    daemon.set_defaults(handler=cmd_daemon)

    station = commands.add_parser("station", help="Control the running station")
    actions = station.add_subparsers(dest="station_action", required=True)
    status = actions.add_parser("status", help="Show live counts and every card")
    status.add_argument("--json", action="store_true")
    opener = actions.add_parser("open", help="Open a session; inserted cards join it")
    opener.add_argument("--metadata-file", required=True)
    opener.add_argument("--manual-start", action="store_true",
                        help="Wait for `station start CARD` instead of uploading at once")
    closer = actions.add_parser("close", help="End the session and write reports")
    closer.add_argument("--force", action="store_true", help="Even with unfinished cards")
    closer.add_argument("--wait", type=float, default=600.0,
                        help="Seconds to wait for files in progress")
    for name, text in (("pause", "Pause one card"), ("resume", "Resume one card"),
                       ("start", "Start a card waiting for manual start"),
                       ("confirm", "Confirm a device that may not be a card"),
                       ("retry", "Retry a card's failed files")):
        actions.add_parser(name, help=text).add_argument("card_id")
    place = actions.add_parser("place", help="PeakAI: say which device recorded a held card")
    place.add_argument("card_id")
    which = place.add_mutually_exclusive_group(required=True)
    which.add_argument("--device", help='e.g. "Stereo hat" or "Head camera"')
    which.add_argument("--skip", action="store_true", help="Do not upload the unplaced files")
    actions.add_parser("retry-reports",
                       help="PeakAI: send failed dashboard entries and _info files now")
    for name, text in (("pause-all", "Pause every card"), ("resume-all", "Resume every card"),
                       ("start-all", "Start every waiting card"),
                       ("resume-engine", "Try uploading again after a stopping problem")):
        actions.add_parser(name, help=text)
    auto = actions.add_parser("auto-upload", help="Start new cards automatically, or not")
    auto.add_argument("state", choices=["on", "off"])
    station.set_defaults(handler=cmd_station)

    ai = commands.add_parser("ai", help="Ask the AI troubleshooter (needs the running station)")
    ai.add_argument("question", nargs="*", help="What to ask, in your own words")
    ai.add_argument("--problem", help="An open problem's id, as shown by `station status`")
    ai.add_argument("--session", help="Continue an existing AI session")
    ai.add_argument("--no-follow-up", action="store_true",
                    help="Exit when the AI finishes instead of asking for a follow-up")
    ai.set_defaults(handler=cmd_ai)

    desktop = commands.add_parser("desktop", help="Desktop companion: panel icon, "
                                                  "notifications, console window (Linux)")
    desktop.add_argument("--window", action="store_true", help="Open the console window")
    desktop.add_argument("--fullscreen", action="store_true", help="With --window: full screen")
    desktop.add_argument("--check", action="store_true",
                         help="Show what this computer supports")
    desktop.set_defaults(handler=cmd_desktop)

    console = commands.add_parser("console", help="Open the Operator Console in a browser")
    console.add_argument("--print-link", action="store_true",
                         help="Print the sign-in link instead of opening a browser")
    console.set_defaults(handler=cmd_console)

    pack = commands.add_parser("pack", help="PeakAI context pack: import, status, rollback")
    pack_actions = pack.add_subparsers(dest="pack_action", required=True)
    importer = pack_actions.add_parser("import", help="Check a pack zip, then apply it")
    importer.add_argument("zip")
    importer.add_argument("--yes", action="store_true", help="Apply without asking")
    importer.add_argument("--offline", action="store_true",
                          help="Do not ask the token service which passes this laptop has")
    pack_actions.add_parser("status", help="Show the active pack")
    pack_actions.add_parser("rollback", help="Go back to the previous pack")
    pack.set_defaults(handler=cmd_pack)

    problems = commands.add_parser("problems", help="List every problem code and its fix")
    problems.add_argument("--markdown", action="store_true",
                          help="Print the full help page (docs/PROBLEMS.md)")
    problems.set_defaults(handler=cmd_problems)

    simulate = commands.add_parser("simulate", help="Fake cards for testing without hardware")
    sim_actions = simulate.add_subparsers(dest="sim_action", required=True)
    create = sim_actions.add_parser("create", help="Create and insert a simulated card")
    create.add_argument("name")
    create.add_argument("--files", type=int, default=20)
    create.add_argument("--size-kib", type=float, default=256)
    for name in ("remove", "insert"):
        sim_actions.add_parser(name, help=f"{name.capitalize()} a simulated card").add_argument(
            "name")
    for parser in sim_actions.choices.values():
        parser.add_argument("--dir", help="Simulator folder (default: station.simulator_dir)")
    simulate.set_defaults(handler=cmd_simulate)
