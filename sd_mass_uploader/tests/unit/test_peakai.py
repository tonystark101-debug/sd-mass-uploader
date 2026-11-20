"""PeakAI: pack import, pass keeper, routing, device rules, session rules and the station flow."""

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from src.ai.policy import Verdict, review_command
from src.ai.tools import Toolbox
from src.api.app import ApiServices, create_app
from src.cli.commands import main
from src.cli.station_commands import render_station
from src.core.enums import CardState, TaskStatus
from src.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    GcsPermissionError,
    MetadataValidationError,
    PassDeniedError,
    PeakAiKeyError,
    PeakAiServiceError,
    TransientUploadError,
)
from src.core.settings import PeakAiSettings, UploadSettings
from src.database.peakai_repository import PeakAiRepository
from src.peakai.devices import (
    BUILDAI,
    E2,
    GILABS,
    HEAD_CAMERA,
    PHONE_RIG,
    choose_passes,
    classify,
    detect_details,
    probe_mp4,
)
from src.peakai.keeper import PassKeeper
from src.peakai.pack import apply_pack, load_active, read_document, read_pack, rollback_pack
from src.peakai.profile import PeakAiProfile
from src.peakai.session import (
    IST,
    card_folder,
    card_serial,
    contains_phone_number,
    info_document,
    object_name,
    upload_day,
    validate_session,
)
from src.peakai.store import PeakAiStore
from src.station.station import Station
from tests.unit.peakai_fakes import KEY, URL, FakeTokenService, make_pack, mp4_bytes
from tests.unit.test_station import StationHarness

SESSION = {"activity": "garage", "city": "bijapur", "vendor_name": "Narayan",
           "site": "Shop 4", "batch": "batch-9"}


@pytest.fixture
def pack(tmp_path):
    report = read_pack(make_pack(tmp_path / "pack.zip"))
    assert report.ok, report.errors
    return report.settings


class Clock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start

    def __call__(self):
        return self.now


def keeper_for(pack, service, clock=None):
    return PassKeeper(pack, PeakAiSettings(), lambda: KEY, http=service,
                      clock=clock or service.clock)


# ------------------------------------------------------------------ pack

def test_pack_is_read_and_the_key_is_never_shown(tmp_path):
    report = read_pack(make_pack(tmp_path / "p.zip"))
    assert report.ok and report.key == KEY
    s = report.settings
    assert (s.bucket, s.token_url, s.version, s.dated) == ("peak-lake-test", URL, "2",
                                                           "12 Nov 2026")
    assert s.passes["buildai"].folder == "buildai/stereo/"
    assert s.activities == ["agriculture", "construction", "garage", "salon"]
    text = "\n".join(report.lines())
    assert KEY not in text and "found, will be stored privately" in text
    assert KEY not in repr(report) and KEY not in s.to_json()


@pytest.mark.parametrize(("overrides", "message"), [
    ({"laptop_key.txt": None}, "laptop_key.txt is missing"),
    ({"laptop_key.txt": "short"}, "does not look like a laptop key"),
    ({"README.md": "# no version\n"}, "no version line"),
    ({"peakai-tokens.sh": "#!/bin/bash\n"}, "No token service address"),
    ({"SITE_CHAT_INSTRUCTIONS.md": "**Activity:** `garage`.\n\n"}, "folder layout"),
])
def test_bad_packs_change_nothing(tmp_path, overrides, message):
    report = read_pack(make_pack(tmp_path / "bad.zip", **overrides))
    assert not report.ok and any(message in error for error in report.errors), report.errors


def test_changed_device_rules_or_version_are_refused(tmp_path):
    from tests.unit.peakai_fakes import README, SITE

    report = read_pack(make_pack(tmp_path / "a.zip",
                                 **{"SITE_CHAT_INSTRUCTIONS.md": SITE.replace("GKENC1", "X")}))
    assert any("device rules changed" in error for error in report.errors)
    report = read_pack(make_pack(tmp_path / "b.zip",
                                 **{"README.md": README.replace("(v2,", "(v3,")}))
    assert any("v3 is not supported" in error for error in report.errors)


def test_unsafe_or_damaged_zip(tmp_path):
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("../../etc/x", "x")
    assert "Unsafe path" in read_pack(path).errors[0]
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    assert "Cannot open" in read_pack(tmp_path / "junk.zip").errors[0]


def test_apply_and_rollback(tmp_path):
    pack_dir, key_file = tmp_path / "pack", tmp_path / "secrets" / "laptop_key"
    first = apply_pack(read_pack(make_pack(tmp_path / "1.zip")), pack_dir, key_file)
    assert key_file.read_text().strip() == KEY
    if os.name == "posix":
        assert key_file.stat().st_mode & 0o077 == 0
    assert load_active(pack_dir).source_sha256 == first.source_sha256
    assert "Never print a token" in read_document(pack_dir, "README.md")
    assert (pack_dir / "active" / "documents" / "install.sh.txt").exists()  # Never executable.
    assert read_document(pack_dir, "../pack.json") is None

    other_key = "another_laptop_key_0123456789xyzXYZ"
    second = apply_pack(read_pack(make_pack(tmp_path / "2.zip",
                                            **{"laptop_key.txt": other_key})),
                        pack_dir, key_file)
    assert second.source_name == "2.zip" and key_file.read_text().strip() == other_key
    back = rollback_pack(pack_dir, key_file)
    assert back.source_name == "1.zip" and key_file.read_text().strip() == KEY
    with pytest.raises(ConfigurationError):
        apply_pack(read_pack(make_pack(tmp_path / "3.zip", **{"laptop_key.txt": None})),
                   pack_dir, key_file)


# ------------------------------------------------------------------ keeper

def test_keeper_fetches_reuses_and_renews_tokens(pack):
    clock = Clock()
    service = FakeTokenService(clock=clock)
    keeper = keeper_for(pack, service, clock)
    assert keeper.refresh_passes() == ["gilabs", "buildai", "sieve", "ots-mono", "ots-stereo"]
    first, _ = keeper.token("gilabs")
    assert first == "tok-gilabs-1" and keeper.token("gilabs")[0] == first
    clock.now += 3600 - 2100 + 1  # Under 35 minutes left: renew.
    assert keeper.token("gilabs")[0] == "tok-gilabs-2"
    assert all(key == KEY for _, _, key in service.calls)  # Only ever in the header.
    status = json.dumps(keeper.status())
    assert "tok-" not in status and KEY not in status


def test_keeper_keeps_current_token_when_renewal_fails(pack):
    clock = Clock()
    service = FakeTokenService(clock=clock)
    keeper = keeper_for(pack, service, clock)
    token, _ = keeper.token("sieve")
    clock.now += 2000
    service.fail["/v1/token"] = [502]
    assert keeper.token("sieve")[0] == token  # Still has ~26 minutes: keep it meanwhile.
    assert "HTTP 502" in keeper.status()["passes"][2]["last_error"]
    clock.now += 2000  # Expired: no token to fall back on.
    service.fail["/v1/token"] = [0]
    with pytest.raises(PeakAiServiceError, match="HTTP 000"):
        keeper.token("sieve")


def test_keeper_errors_follow_peakai_meanings(pack):
    service = FakeTokenService(passes=["gilabs"])
    keeper = keeper_for(pack, service)
    with pytest.raises(PassDeniedError):
        keeper.token("buildai")  # Not a pass this laptop has.
    service.fail["/v1/token"] = [403]
    with pytest.raises(PassDeniedError):
        keeper.token("gilabs")
    bad = PassKeeper(pack, PeakAiSettings(), lambda: "wrong_key_000000000000000000",
                     http=service)
    with pytest.raises(PeakAiKeyError, match="unknown laptop key"):
        bad.refresh_passes()
    assert "401" in bad.status()["key_error"]
    service.wrong_folder["gilabs"] = "somewhere-else/"
    fresh = keeper_for(pack, service)
    with pytest.raises(PassDeniedError, match="Import PeakAI's latest pack"):
        fresh.token("gilabs")


def test_removed_pass_is_dropped_and_rejected_token_replaced(pack):
    service = FakeTokenService()
    keeper = keeper_for(pack, service)
    token, _ = keeper.token("buildai")
    keeper.reject("buildai", token)
    assert keeper.token("buildai")[0] != token
    service.passes.remove("buildai")
    keeper.refresh_passes()
    with pytest.raises(PassDeniedError):
        keeper.token("buildai")
    assert "buildai" not in [item["name"] for item in keeper.status()["passes"]]


def test_legacy_and_empty_pass_lists(pack):
    keeper = keeper_for(pack, FakeTokenService(passes=["mono", "stereo"]))
    with pytest.raises(ConfigurationError, match="has not switched"):
        keeper.ensure_ready()
    assert keeper.legacy_only()
    keeper = keeper_for(pack, FakeTokenService(passes=[]))
    with pytest.raises(ConfigurationError, match="no PeakAI passes"):
        keeper.ensure_ready()
    ok = keeper_for(pack, FakeTokenService(passes=["sieve"]))
    ok.ensure_ready()
    assert ok.maintain() is True


# ------------------------------------------------------------------ store routing

class RaisingClient:
    """Stands in for a google client; each blob call raises the given error."""

    def __init__(self, error):
        self.error = error

    def bucket(self, name):
        error = self.error

        class Bucket:
            def get_blob(self, *a, **k):
                raise error

            def blob(self, *a, **k):
                raise error

        return Bucket()


def test_store_routes_by_pass_folder_and_maps_errors(pack):
    keeper = keeper_for(pack, FakeTokenService())
    store = PeakAiStore(pack, keeper, UploadSettings(), client_factory=lambda creds: None)
    assert store.pass_for("ots-new/mono/garage/x") == "ots-mono"
    assert store.pass_for("buildai/stereo/a") == "buildai"
    with pytest.raises(ConfigurationError):
        store.pass_for("uploads/elsewhere")

    from google.api_core import exceptions as api

    denied = PeakAiStore(pack, keeper, UploadSettings(),
                         client_factory=lambda creds: RaisingClient(api.Forbidden("no")))
    with pytest.raises(PassDeniedError, match="HTTP 403"):
        denied.get_object("gilabs/a")
    def unauthorized(creds):
        creds.refresh(None)  # As the auth library would before the request.
        return RaisingClient(api.Unauthorized("no"))

    refused = PeakAiStore(pack, keeper, UploadSettings(), client_factory=unauthorized)
    with pytest.raises(TransientUploadError, match="401"):
        refused.get_object("gilabs/a")
    held = keeper._tokens["gilabs"].token
    assert keeper._rejected["gilabs"] == held  # Never handed out again...
    assert keeper.token("gilabs")[0] != held  # ...the next use gets a new token.
    assert issubclass(PeakAiKeyError, AuthenticationError)
    assert not issubclass(PassDeniedError, GcsPermissionError)  # Fails one card, not all.


# ------------------------------------------------------------------ device rules

def _reader(contents):
    return lambda path, limit: contents.get(path, b"")[:limit]


def test_each_device_is_recognised_from_its_files():
    files = [
        ("data/session_00000/session.json", 40), ("data/session_00000/ego_0000.mcap", 1000),
        ("recordings/abc-sn123-1-left.mp4.enc", 500), ("recordings/abc-sn123-1.imu.enc", 50),
        ("recordings/ego_7/metadata.json", 5), ("recordings/ego_7/video_live_1.mp4", 300),
        ("Stereo/recordings/stereo_1/chunk_0/video.h265", 200),
        ("DVR/VIDEO/0001.mp4", 100), (".buildai-card-id.json", 1), ("Android/data/x", 9),
        ("._DVR", 1),
    ]
    result = classify(files, _reader({
        "data/session_00000/session.json": b'{"hostname": "GILABS-0042"}',
        "recordings/abc-sn123-1-left.mp4.enc": b"GKENC1....",
        "recordings/abc-sn123-1.imu.enc": b"GKENC1....",
    }))
    devices = {group.device: group for group in result.groups}
    assert set(devices) == {GILABS, BUILDAI, E2, PHONE_RIG, HEAD_CAMERA}
    assert devices[E2].files == ["recordings/ego_7/metadata.json",
                                 "recordings/ego_7/video_live_1.mp4"]
    assert ".buildai-card-id.json" in devices[GILABS].files  # Root files: main device.
    assert [path for path, _ in result.excluded] == ["Android/data/x", "._DVR"]
    assert result.unplaced == []
    problems = choose_passes(result, ["gilabs", "buildai", "ots-stereo", "ots-mono"])
    assert problems == []
    assert devices[HEAD_CAMERA].pass_name == "ots-mono"  # No sieve pass: ots-mono.
    assert devices[PHONE_RIG].pass_name == devices[E2].pass_name == "ots-stereo"


def test_lookalikes_are_not_guessed():
    files = [("data/session_1/session.json", 5), ("data/session_1/ego_0.mcap", 10),
             ("recordings/x-sn1-a-left.mp4.enc", 10), ("recordings/x-sn1.imu30", 10)]
    result = classify(files, _reader({"data/session_1/session.json": b'{"hostname": "PI"}',
                                      "recordings/x-sn1-a-left.mp4.enc": b"PLAIN"}))
    assert result.groups == [] and len(result.unplaced) == 4
    assert any("GILABS" in note for note in result.notes)
    assert any("GKENC1" in note for note in result.notes)


def test_missing_pass_is_reported_not_guessed():
    result = classify([("DVR/VIDEO/a.mp4", 1)], _reader({}))
    problems = choose_passes(result, ["gilabs"])
    assert result.groups[0].pass_name is None
    assert "sieve or ots-mono" in problems[0] and "ask PeakAI" in problems[0]


def test_mp4_hours_and_dates_from_headers():
    when = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
    assert probe_mp4(io.BytesIO(mp4_bytes(90.0, when))) == (90.0, when)
    assert probe_mp4(io.BytesIO(mp4_bytes(30.0, when, version=1)))[0] == 30.0
    assert probe_mp4(io.BytesIO(b"not an mp4 at all")) is None

    result = classify([("DVR/VIDEO/a.mp4", 1), ("DVR/VIDEO/b.mp4", 1)], _reader({}))
    blobs = {"DVR/VIDEO/a.mp4": mp4_bytes(1800, when),
             "DVR/VIDEO/b.mp4": mp4_bytes(1800, datetime(2026, 11, 9, tzinfo=UTC))}
    detect_details(result.groups[0], lambda path: io.BytesIO(blobs[path]))
    group = result.groups[0]
    assert (group.hours, group.recorded_from, group.recorded_to) == (1.0, "2026-11-08",
                                                                     "2026-11-09")
    old = classify([("DVR/VIDEO/a.mp4", 1)], _reader({}))
    detect_details(old.groups[0], lambda path: io.BytesIO(mp4_bytes(60, datetime(
        1980, 1, 1, tzinfo=UTC))))
    assert old.groups[0].hours == 0.02 and old.groups[0].recorded_from is None  # 1980: no.


# ------------------------------------------------------------------ session rules

def test_session_rules(pack):
    details, notes = validate_session({**SESSION, "activity": "Garage"}, pack)
    assert details["activity"] == "garage" and notes == []
    assert details["upload_date"] == upload_day()
    _, notes = validate_session({**SESSION, "activity": "boat-repair"}, pack)
    assert "new activity" in notes[0]
    for bad, message in (
        ({**SESSION, "vendor_name": ""}, "vendor_name is required"),
        ({**SESSION, "city": "Bijapur North"}, "lowercase town name"),
        ({**SESSION, "city": "unknown"}, "only if nobody knows"),
        ({**SESSION, "site": "call 98450 12345"}, "phone number"),
        ({**SESSION, "recorded_from": "2026-11-09", "recorded_to": "2026-11-01"}, "after"),
        ({**SESSION, "video_hours": "lots"}, "video_hours"),
        ({**SESSION, "operator_name": "x"}, "Unknown field"),
    ):
        with pytest.raises(MetadataValidationError, match=message):
            validate_session(bad, pack)
    details, notes = validate_session({**SESSION, "city": "unknown",
                                       "city_unknown_confirmed": True}, pack)
    assert details["city"] == "unknown" and "Tell the person" in notes[0]


def test_upload_day_is_india_time():
    late_utc = datetime(2026, 11, 12, 20, 0, tzinfo=UTC)  # 01:30 on the 13th in India.
    assert upload_day(late_utc) == "2026-11-13"


def test_serial_folder_object_and_info(pack):
    assert card_serial("6af7-f78b") == "6AF7F78B"
    assert card_serial("0123456789ABCDEF") == "0123456789ABCDEF"
    assert card_serial("folder:/x") is None and card_serial("WD-123") is None
    details = {"activity": "garage", "city": "bijapur", "upload_date": "2026-11-12",
               "vendor_name": "Narayan", "batch": "batch-9"}
    folder = card_folder(pack, "gilabs", details, "6AF7F78B")
    assert folder == "gilabs/garage/bijapur/2026-11-12/card-6AF7F78B/"
    assert object_name(folder, "data/a b.mcap") == folder + "data/a b.mcap"
    with pytest.raises(MetadataValidationError):
        object_name(folder, "../escape")
    info = info_document(pass_name="sieve", folder="sieve/mono/x/", vendor_code="mahadev",
                         details=details, device=HEAD_CAMERA, serial="6AF7F78B", label="",
                         file_count=3, total_bytes=9, hours=None, recorded_from=None,
                         recorded_to=None, machine="laptop-1",
                         when=datetime(2026, 11, 12, 6, 42, tzinfo=UTC))
    assert info["camera"] == "mono" and info["written_at"] == "2026-11-12T12:12:00+05:30"
    assert info["vendor"] == "mahadev" and info["vendor_name"] == "Narayan"
    assert contains_phone_number("+91 98450-12345") and not contains_phone_number("batch-9")
    assert IST.utcoffset(None).total_seconds() == 19800


# ------------------------------------------------------------------ the station with PeakAI

class PeakAiHarness(StationHarness):
    def __init__(self, tmp_path, passes=("gilabs", "buildai", "sieve", "ots-mono",
                                         "ots-stereo"), **upload_options):
        super().__init__(tmp_path, **upload_options)
        self.station.shutdown()
        self.settings.peakai.vendor_code = "mahadev"
        self.settings.peakai.machine_name = "laptop-1"
        self.pack = read_pack(make_pack(tmp_path / "pack.zip")).settings
        self.service = FakeTokenService(passes=passes)
        self.keeper = keeper_for(self.pack, self.service)
        self.peakai_repo = PeakAiRepository(self.db)
        self.profile = PeakAiProfile(self.settings, self.pack, self.keeper, self.store,
                                     self.sessions, self.scans, self.peakai_repo, lambda: KEY,
                                     http=self.service)
        from src.reporting.report_generator import ReportGenerator
        from src.uploader.finalize import SessionFinalizer

        reports = ReportGenerator(self.sessions, self.scans, tmp_path / "reports")
        self.station = Station(
            self.settings, self.sessions, self.scans, self.repo, lambda: self.store,
            lambda store: SessionFinalizer(self.sessions, self.scans, store, reports),
            profile=self.profile)

    def write(self, name, files: dict[str, bytes]):
        for relative, data in files.items():
            path = self.root / name / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def gilabs_card(self, name):
        self.write(name, {"data/session_0001/session.json": b'{"hostname": "GILABS-0042"}',
                          "data/session_0001/ego_0000.mcap": os.urandom(3000),
                          ".dserial": b"x", "Android/junk.bin": b"phone app"})

    def objects(self, prefix=""):
        return sorted(name for name in self.store.objects if name.startswith(prefix))


@pytest.fixture
def station(tmp_path):
    h = PeakAiHarness(tmp_path)
    yield h
    h.close()


def test_card_goes_to_its_pass_folder_with_info_and_dashboard(station):
    station.station.open_session(SESSION)
    station.gilabs_card("A")
    station.wait_states(A=CardState.UPLOADED)
    serial = card_serial("sim:A")
    folder = f"gilabs/garage/bijapur/{upload_day()}/card-{serial}/"
    station.wait_for(lambda: any("/_info/" in name for name in station.objects(folder)))
    station.wait_for(lambda: any(r["status"] == "uploaded" for r in station.service.reports))
    names = station.objects(folder)
    assert folder + "data/session_0001/ego_0000.mcap" in names
    assert folder + ".dserial" in names  # Root file with the main device.
    assert not any("Android" in name for name in station.store.objects)  # Not ours.
    info_name = next(name for name in names if "/_info/" in name)
    assert info_name.startswith(folder + "_info/laptop-1_")
    info = json.loads(station.store.objects[info_name]["data"])
    assert info["pass"] == "gilabs" and info["camera"] == "stereo" and info["file_count"] == 3
    assert info["vendor"] == "mahadev" and info["vendor_name"] == "Narayan"
    assert info["batch"] == "batch-9" and info["site"] == "Shop 4"
    first, last = station.service.reports[0], station.service.reports[-1]
    assert first["status"] == "uploading" and last["status"] == "uploaded"
    assert {key: last[key] for key in ("pass", "upload_date", "activity", "location", "vendor",
                                       "device", "cards")} == {
        "pass": "gilabs", "upload_date": upload_day(), "activity": "garage",
        "location": "bijapur", "vendor": "Narayan", "device": GILABS, "cards": 1}
    view = station.station.view()
    passes = view.destination["keeper"]["passes"]
    assert [item["name"] for item in passes][:2] == ["gilabs", "buildai"]
    assert "tok-" not in json.dumps(view.destination) and KEY not in json.dumps(view.to_dict())
    assert any(p["code"] == "I-PEAKAI-DETAILS" for p in view.problems)  # .mcap: no hours.
    assert view.counters.uploaded == 1


def test_unknown_files_hold_the_card_until_a_person_decides(station):
    station.station.open_session(SESSION)
    station.write("B", {"misc/clip.bin": b"x" * 10, "misc/clip2.bin": b"y" * 10})
    station.wait_states(B=CardState.NEEDS_DECISION)
    view = station.station.view()
    assert view.counters.needs_decision == 1
    problem = next(p for p in view.problems if p["code"] == "W-CARD-DECISION")
    assert problem["action"]["kind"] == "place_card" and "match no known device" in \
        problem["message"]
    assert station.store.uploads == 0
    with pytest.raises(Exception, match="Unknown device"):
        station.station.place_card("sim-B", device="Toaster")
    assert station.station.place_card("sim-B", device="Stereo hat") == "placed"
    station.wait_states(B=CardState.UPLOADED)
    assert station.objects("ots-new/stereo/garage/bijapur/")


def test_skipping_unplaced_files_uploads_the_rest(station):
    station.station.open_session(SESSION)
    station.write("C", {"DVR/VIDEO/a.mp4": mp4_bytes(60), "notes/readme.txt": b"hello"})
    station.wait_states(C=CardState.NEEDS_DECISION)
    station.station.place_card("sim-C", skip=True)
    station.wait_states(C=CardState.UPLOADED)
    assert [name.rsplit("/card-", 1)[1].split("/", 1)[1]
            for name in station.objects("sieve/mono/") if "_info" not in name] \
        == ["DVR/VIDEO/a.mp4"]


def test_card_waits_when_this_laptop_lacks_the_pass(tmp_path):
    h = PeakAiHarness(tmp_path, passes=("ots-stereo",))
    try:
        h.station.open_session(SESSION)
        h.gilabs_card("A")
        h.wait_states(A=CardState.NEEDS_DECISION)
        reason = h.station.view().cards[0].reason
        assert "gilabs pass" in reason and "ask PeakAI" in reason
        h.service.passes.append("gilabs")
        h.keeper.refresh_passes()
        h.station.retry_card("sim-A")
        h.wait_states(A=CardState.UPLOADED)
    finally:
        h.close()


def test_refused_pass_fails_only_that_card(station):
    station.store.errors = [PassDeniedError("Storage refused the gilabs pass (HTTP 403)")]
    station.settings.upload.max_workers = 1
    station.station.open_session(SESSION)
    station.gilabs_card("A")
    station.wait_for(lambda: any(p["code"] == "E-PEAKAI-PASS-DENIED"
                                 for p in station.station.view().problems))
    assert station.station.view().engine["problem"] is None  # The session keeps going.
    station.station.retry_card("sim-A")
    station.wait_states(A=CardState.UPLOADED)


def test_failed_dashboard_report_is_a_problem_and_retried(station):
    station.service.fail["/v1/report"] = [503, 503, 503, 503, 503, 503]
    station.station.open_session(SESSION)
    station.gilabs_card("A")
    station.wait_for(lambda: any(p["code"] == "E-PEAKAI-REPORT"
                                 for p in station.station.view().problems))
    station.service.fail["/v1/report"] = []
    station.station.retry_reports()
    station.wait_for(lambda: any(r["status"] == "uploaded" for r in station.service.reports))
    station.wait_for(lambda: not any(p["code"] == "E-PEAKAI-REPORT"
                                     for p in station.station.view().problems))


def test_session_needs_vendor_code_and_valid_details(station):
    station.settings.peakai.vendor_code = ""
    with pytest.raises(Exception, match="vendor_code"):
        station.station.open_session(SESSION)
    station.settings.peakai.vendor_code = "mahadev"
    with pytest.raises(Exception, match="vendor_name is required"):
        station.station.open_session({**SESSION, "vendor_name": ""})


def test_api_and_status_show_passes_and_placement(station):
    station.station.open_session(SESSION)
    station.write("B", {"misc/clip.bin": b"x"})
    station.wait_states(B=CardState.NEEDS_DECISION)
    headers = {"Authorization": "Bearer t"}
    with TestClient(create_app(ApiServices(station.station, "t")),
                    base_url="http://127.0.0.1") as client:
        destination = client.get("/api/v1/destination", headers=headers).json()
        assert destination["profile"] == "peakai" and destination["pack"]["bucket"] == \
            "peak-lake-test"
        assert "tok-" not in json.dumps(destination) and KEY not in json.dumps(destination)
        placed = client.post("/api/v1/cards/sim-B/place", headers=headers,
                             json={"device": "Head camera"})
        assert placed.json() == {"result": "placed"}
        state = client.get("/api/v1/state", headers=headers).json()
    station.wait_states(B=CardState.UPLOADED)
    state["daemon"] = {"running": True, "pid": 1, "source": "simulator"}
    text = render_station(state, 2)
    assert "PEAKAI  pack pack.zip (v2" in text and "gs://peak-lake-test" in text


def test_tasks_never_leave_the_pass_folders(station):
    station.station.open_session(SESSION)
    station.gilabs_card("A")
    station.write("D", {"DVR/VIDEO/a.mp4": mp4_bytes(5)})
    station.wait_states(A=CardState.UPLOADED, D=CardState.UPLOADED)
    session_id = station.repo.current()["session_id"]
    for row in station.sessions.iter_task_rows(session_id):
        assert row["object_name"].startswith(("gilabs/", "sieve/mono/"))
        assert row["status"] == TaskStatus.VERIFIED.value


# ------------------------------------------------------------------ CLI and AI

def test_pack_cli_import_status_rollback(tmp_path, capsys, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("peakai:\n  vendor_code: mahadev\n", encoding="utf-8")
    zip_path = make_pack(tmp_path / "pack.zip")
    base = ["--config", str(config), "pack"]
    assert main([*base, "status"]) == 1
    assert main([*base, "import", str(zip_path), "--offline", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Pack applied" in out and KEY not in out
    key_file = tmp_path / "data" / "station" / "secrets" / "laptop_key"
    assert key_file.read_text().strip() == KEY
    assert main([*base, "status"]) == 0
    assert "peak-lake-test" in capsys.readouterr().out
    bad = make_pack(tmp_path / "bad.zip", **{"laptop_key.txt": None})
    assert main([*base, "import", str(bad), "--offline", "--yes"]) == 2
    assert "Nothing was changed" in capsys.readouterr().out
    assert main([*base, "rollback"]) == 2  # Nothing to go back to: a clear error.
    monkeypatch.setattr("builtins.input", lambda _prompt="": "no")
    assert main([*base, "import", str(zip_path), "--offline"]) == 1


def test_ai_never_sees_peakai_secrets_but_reads_the_rules(station, tmp_path):
    for command in ("cat data/station/secrets/laptop_key",
                    "cat /home/op/PeakAI-Uploader/tokens/gilabs_token.txt",
                    "cat /home/op/PeakAI-Uploader/tokens/PEAKAI_PASS_gilabs.txt",
                    "grep -r X-PeakAI-Key /home/op"):
        assert review_command(command).verdict is Verdict.BLOCKED, command
    apply_pack(read_pack(make_pack(tmp_path / "p.zip")),
               station.settings.resolve(station.settings.peakai.pack_dir),
               station.settings.resolve(station.settings.peakai.key_file))
    toolbox = Toolbox(station.station, station.settings)

    class Gate:
        def request_approval(self, *a):
            raise AssertionError("not needed")

    result = toolbox.execute("read_site_rules", {"document": "SITE_CHAT_INSTRUCTIONS.md"},
                             Gate())
    assert "card-<SERIAL>" in result.content and KEY not in result.content
    assert toolbox.execute("read_site_rules", {"document": "laptop_key.txt"}, Gate()).is_error
