"""The error advisor: complete mapping, plain-language rendering, rules and problem lifecycle."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.core.exceptions as exceptions_module
from src.advisor.advisor import AdvisorFacts, ProblemTracker, evaluate
from src.advisor.catalog import (
    CATALOG,
    ERROR_CLASS_CODES,
    SCAN_KIND_CODES,
    FixAction,
    Severity,
    catalog_markdown,
    code_for_error,
)
from src.api.app import ApiServices, create_app
from src.cli.station_commands import render_station
from src.core.enums import CardState, VerificationStatus
from src.core.exceptions import AuthenticationError, UploaderError
from src.database.db import Database
from src.database.station_repository import StationRepository
from src.station.events import EventBus
from src.station.sources import SourceIssue
from tests.unit.test_station import METADATA, StationHarness

ROOT = Path(__file__).resolve().parents[2]
EVERY_VALUE = {"card": "Card 1", "files": 3, "detail": "x", "speed": "480 Mbps",
               "free": "1 GB", "attempt": 2, "max": 6}


# ------------------------------------------------------------------ completeness

def test_every_engine_exception_has_advice():
    classes = [cls for _, cls in inspect.getmembers(exceptions_module, inspect.isclass)
               if issubclass(cls, UploaderError)]
    assert len(classes) >= 16
    missing = [cls.__name__ for cls in classes if cls.__name__ not in ERROR_CLASS_CODES]
    assert not missing


def test_every_verification_result_has_advice():
    for status in VerificationStatus:
        if status is not VerificationStatus.MATCH:
            assert status.value in ERROR_CLASS_CODES
    assert "MISSING_REMOTE_OBJECT" in ERROR_CLASS_CODES  # Written by `verify`.


def test_every_scan_error_kind_has_advice():
    kinds = set()
    for name in ("inventory/file_scanner.py", "inventory/inventory_service.py",
                 "metadata/session.py"):
        text = (ROOT / "src" / name).read_text(encoding="utf-8")
        kinds |= set(re.findall(r'ScanProblem\([^,]+,\s*"([A-Z_]+)"', text))
        kinds |= set(re.findall(r'\(\s*[\w.\[\]"]+,\s*"([A-Z_]{6,})",', text))
    kinds = {kind for kind in kinds if not kind.startswith("FILE_ATTRIBUTE_")}  # stat flags
    assert {"DIRECTORY_UNREADABLE", "CARD_DISCONNECTED", "UNSAFE_OBJECT_NAME"} <= kinds
    assert kinds <= set(SCAN_KIND_CODES), kinds - set(SCAN_KIND_CODES)


def test_every_device_issue_code_is_in_the_catalog():
    codes = set()
    for name in ("station/sources.py", "station/daemon.py"):
        text = (ROOT / "src" / name).read_text(encoding="utf-8")
        codes |= set(re.findall(r'SourceIssue\(\s*"([A-Z-]+)"', text))
    assert {"E-MOUNT-FAILED", "W-FS-UNSUPPORTED", "W-DETECTION"} <= codes
    assert codes <= set(CATALOG)


def test_every_mapping_points_at_a_real_entry():
    assert set(ERROR_CLASS_CODES.values()) <= set(CATALOG)
    assert set(SCAN_KIND_CODES.values()) <= set(CATALOG)
    assert code_for_error("KeyError") == "E-INTERNAL"  # Unexpected errors still get advice.


@pytest.mark.parametrize("spec", CATALOG.values(), ids=lambda spec: spec.code)
def test_every_entry_is_complete_and_renders(spec):
    assert re.fullmatch(r"[EWI]-[A-Z0-9-]+", spec.code)
    assert spec.code[0] == {"critical": "E", "error": "E", "warning": "W",
                            "info": "I"}[spec.severity.value]
    assert spec.title and spec.steps and all(step.endswith((".", ")")) for step in spec.steps)
    title, message = spec.render(**EVERY_VALUE)
    assert "{" not in title + message and "}" not in title + message
    if spec.severity is Severity.CRITICAL:
        assert not spec.card_scoped


def test_help_page_is_up_to_date():
    """Regenerate with: python main.py problems --markdown > docs/PROBLEMS.md"""
    current = (ROOT / "docs" / "PROBLEMS.md").read_text(encoding="utf-8")
    assert current.replace("\r\n", "\n").strip() == catalog_markdown().strip()


# ------------------------------------------------------------------ rules

@dataclass
class FakeCard:
    card_id: str
    state: CardState = CardState.QUEUED
    present: bool = True
    label: str | None = None
    reason: str | None = None
    files_total: int = 10
    files_done: int = 0
    link_speed_mbps: float | None = None


def _codes(problems):
    return [problem.code for problem in problems]


def test_removed_card_reports_unfinished_files_once():
    card = FakeCard("c1", CardState.REMOVED, present=False, label="CARD01", files_done=4)
    lost = {"card_id": "c1", "status": "FAILED", "error_class": "SourceDisconnectedError",
            "files": 3, "attempts": 1, "sample": "a.bin: gone"}
    problems = evaluate(AdvisorFacts("s", [card], error_groups=[lost]))
    assert _codes(problems) == ["E-CARD-REMOVED"]
    assert problems[0].files == 6 and "CARD01 (c1)" in problems[0].message
    assert problems[0].action is None


def test_card_that_stopped_answering_while_listed_is_a_read_problem():
    lost = {"card_id": "c1", "status": "FAILED", "error_class": "SourceDisconnectedError",
            "files": 2, "attempts": 1, "sample": "a.bin: gone"}
    problems = evaluate(AdvisorFacts("s", [FakeCard("c1")], error_groups=[lost]))
    assert _codes(problems) == ["E-FILE-READ"]
    assert problems[0].action == {"kind": "retry_card", "label": "Retry this card",
                                  "card_id": "c1"}


def test_network_retries_are_one_station_warning():
    groups = [
        {"card_id": card, "status": "PENDING", "error_class": "TransientUploadError",
         "files": 2, "attempts": attempts, "sample": "x: timeout"}
        for card, attempts in (("c1", 2), ("c2", 3))
    ]
    problems = evaluate(AdvisorFacts("s", [FakeCard("c1"), FakeCard("c2")],
                                     error_groups=groups, max_retries=5))
    assert _codes(problems) == ["W-NETWORK-RETRYING"]
    assert "4 file(s)" in problems[0].message and "attempt 3 of 6" in problems[0].message


def test_sign_in_failure_is_critical_with_resume_and_not_duplicated():
    problems = evaluate(AdvisorFacts(
        "s", [FakeCard("c1")],
        engine_error=("AuthenticationError", "token expired"),
        store_error=("AuthenticationError", "token expired"),
        error_groups=[{"card_id": "c1", "status": "CONFLICT", "error_class": "ObjectExistsError",
                       "files": 1, "attempts": 1, "sample": "a: exists"}],
    ))
    assert _codes(problems) == ["E-GCS-AUTH", "E-CONFLICT"]  # Critical first.
    assert problems[0].action["kind"] == FixAction.RESUME_ENGINE.value
    assert "token expired" in problems[0].message


@pytest.mark.parametrize(("error_class", "code"), [
    ("ConfigurationError", "E-CONFIG"), ("GcsPermissionError", "E-GCS-PERMISSION"),
    ("TransientUploadError", "E-GCS-UNREACHABLE"), (None, "E-GCS-UNREACHABLE"),
])
def test_store_errors(error_class, code):
    assert _codes(evaluate(AdvisorFacts("s", [], store_error=(error_class, "x")))) == [code]


def test_unexpected_engine_crash_offers_resume_not_a_card_retry():
    problems = evaluate(AdvisorFacts("s", [], engine_error=("KeyError", "boom")))
    assert problems[0].code == "E-INTERNAL"
    assert problems[0].action["kind"] == "resume_engine"


def test_card_states_scan_errors_usb_and_disk():
    cards = [
        FakeCard("c1", CardState.NEEDS_CONFIRMATION, reason="may be a USB drive"),
        FakeCard("c2", CardState.NEEDS_ATTENTION, files_total=0, reason="the card could not "
                                                                         "be read"),
        FakeCard("c3", link_speed_mbps=480),
        FakeCard("c4", link_speed_mbps=5000),
    ]
    scans = [
        {"card_id": "c3", "kind": "ENTRY_UNREADABLE", "count": 2, "sample": "DCIM/x"},
        {"card_id": "c3", "kind": "DIRECTORY_UNREADABLE", "count": 1, "sample": "DCIM"},
        {"card_id": "c3", "kind": "LINK_SKIPPED", "count": 1, "sample": "link"},
        {"card_id": "gone", "kind": "ENTRY_UNREADABLE", "count": 1, "sample": "x"},
    ]
    issues = [SourceIssue("E-MOUNT-FAILED", "/dev/sdb1: busy", "/dev/sdb1"),
              SourceIssue("NOT-A-CODE", "ignored")]
    problems = evaluate(AdvisorFacts("s", cards, scan_groups=scans, issues=issues,
                                     disk_free=(500 * 1024 ** 2, 100 * 1024 ** 3)))
    by_code = {problem.code: problem for problem in problems}
    assert set(by_code) == {"W-CARD-CONFIRM", "E-CARD-UNREADABLE", "W-USB-SLOW",
                            "E-CARD-PARTLY-UNREADABLE", "I-SKIPPED", "E-MOUNT-FAILED",
                            "W-DISK-LOW"}
    assert by_code["E-CARD-PARTLY-UNREADABLE"].files == 3  # Both unreadable kinds, one card.
    assert "480 Mbps" in by_code["W-USB-SLOW"].message
    assert by_code["W-CARD-CONFIRM"].action["kind"] == "confirm_card"
    assert "524.3 MB" in by_code["W-DISK-LOW"].message
    assert [p.severity for p in problems] == sorted(
        (p.severity for p in problems), key=["critical", "error", "warning", "info"].index)


def test_plenty_of_disk_is_not_a_problem():
    assert evaluate(AdvisorFacts(None, [], disk_free=(50 * 1024 ** 3, 100 * 1024 ** 3))) == []


# ------------------------------------------------------------------ lifecycle

def test_tracker_opens_resolves_and_survives_a_restart():
    db = Database(":memory:")
    repo, bus = StationRepository(db), EventBus()
    problem = evaluate(AdvisorFacts("s", [], store_error=(None, "offline")))
    tracker = ProblemTracker(repo, bus)
    opened = tracker.update("s", problem)
    assert opened[0].opened_at and [e.kind for e in bus.recent()] == ["problem_opened"]
    tracker.update("s", evaluate(AdvisorFacts("s", [], store_error=(None, "offline"))))
    assert len(bus.recent()) == 1  # Still open: no new event.

    restarted = ProblemTracker(repo, bus)  # A new process sees the open row.
    restarted.update("s", [])
    assert [e.kind for e in bus.recent()][-1] == "problem_resolved"
    history = repo.problem_history()
    assert len(history) == 1 and history[0]["resolved_at"] is not None


# ------------------------------------------------------------------ through the station

@pytest.fixture
def harness(tmp_path):
    h = StationHarness(tmp_path)
    yield h
    h.close()


def _station_codes(harness):
    return {problem["code"] for problem in harness.station.view().problems}


def test_station_reports_and_resolves_sign_in_problem(harness):
    harness.store.errors = [AuthenticationError("token expired")]
    harness.station.open_session(METADATA)
    harness.card("A", 2)
    harness.wait_for(lambda: "E-GCS-AUTH" in _station_codes(harness))
    harness.station.resume_engine()
    harness.wait_states(A=CardState.UPLOADED)
    assert "E-GCS-AUTH" not in _station_codes(harness)
    kinds = [event.kind for event in harness.station.bus.recent()]
    assert "problem_opened" in kinds and "problem_resolved" in kinds


def test_station_reports_removed_card_until_it_returns(harness):
    harness.station.open_session(METADATA, auto_upload=False)
    harness.card("A", 2)
    harness.wait_states(A=CardState.READY)
    harness.remove("A")
    harness.wait_for(lambda: "E-CARD-REMOVED" in _station_codes(harness))
    harness.insert("A")
    harness.wait_for(lambda: "E-CARD-REMOVED" not in _station_codes(harness))


def test_problems_api_and_cli_output(harness):
    harness.station.open_session(METADATA, auto_upload=False)
    harness.card("A", 2)
    harness.wait_states(A=CardState.READY)
    harness.remove("A")
    harness.wait_for(lambda: "E-CARD-REMOVED" in _station_codes(harness))

    services = ApiServices(harness.station, "t")
    headers = {"Authorization": "Bearer t"}
    with TestClient(create_app(services), base_url="http://127.0.0.1") as client:
        body = client.get("/api/v1/problems?history=10", headers=headers).json()
        assert [p["code"] for p in body["open"]] == ["E-CARD-REMOVED"]
        assert body["history"][0]["code"] == "E-CARD-REMOVED"
        catalog = client.get("/api/v1/problems/catalog", headers=headers).json()
        assert {entry["code"] for entry in catalog} == set(CATALOG)
        help_page = client.get("/api/v1/problems/catalog/E-GCS-AUTH", headers=headers).json()
        assert help_page["action"]["kind"] == "resume_engine"
        assert client.get("/api/v1/problems/catalog/NOPE", headers=headers).status_code == 404
        state = client.get("/api/v1/state", headers=headers).json()

    state["daemon"] = {"running": True, "pid": 1, "source": "simulator"}
    text = render_station(state, 2)
    assert "PROBLEMS (1)" in text and "Card removed before it finished" in text
    assert "1. Put the same card back in" in text
