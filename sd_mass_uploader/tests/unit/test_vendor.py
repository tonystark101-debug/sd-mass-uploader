"""Opening a session from a vendor platform packet: cards in, how many, the code. Nothing else."""

from __future__ import annotations

import json

import pytest

from src.core.enums import CardState
from src.core.settings import VendorPlatformSettings
from src.database.vendor_repository import VendorPacketRepository
from src.peakai.devices import GILABS
from src.peakai.session import card_serial, upload_day, validate_session
from src.station.station import StationError
from src.vendor.client import (
    VendorPlatform,
    VendorPlatformError,
    VendorPlatformUnreachable,
    check_code,
    check_letter,
)
from src.vendor.packet import check_counts, expected_devices, session_metadata, slug
from tests.unit.test_peakai import PeakAiHarness

STATION_KEY = "s" * 40
# Made by the vendor platform's own code (src/shipments.js makeCode): Garage, Vijayapura, GI Lab.
CODE = "GVGPA3311U"
REF = f"{CODE}-201126-0930"


def packet(**changes) -> dict:
    item = {
        "ref": REF, "code": CODE, "vendor": "Ravi Kumar", "manager": "Suresh",
        "received_by": "Suresh", "environment": "Garage", "site": "Station road garage",
        "city": "Vijayapura", "device_kind": "GI Lab", "cards_sent": 2, "cards_received": 2,
        "sent_at": "2026-11-20 04:00:00", "received_at": "2026-11-20 06:00:00",
        "dispatched_at": None, "arrived_at": None, "transport": "",
        "days": [{"recorded_from": "2026-11-18T09:30", "recorded_to": "2026-11-18T18:00",
                  "devices": 5, "cards": 2, "hours_estimate": 21.5}],
        "hours_declared": 21.5, "recorded_from": "2026-11-18", "recorded_to": "2026-11-18",
        "stages": {}, "uploaded": None,
    }
    item.update(changes)
    return item


class FakeVendor:
    """The vendor platform's station access, in memory."""

    def __init__(self, item: dict | None = None) -> None:
        self.packet = item or packet()
        self.calls: list[tuple] = []
        self.down = False
        self.several: list[dict] | None = None  # A code with several packets to choose from.

    def lookup(self, raw: str) -> dict:
        check_code(raw)
        self.calls.append(("lookup", raw))
        if self.down:
            raise VendorPlatformUnreachable("no answer")
        clean = raw.upper().replace("-", "")
        if clean[:10] != CODE:
            raise VendorPlatformError("no one has this code", 404)
        if self.several and len(clean) == 10:
            return {"packets": [dict(item) for item in self.several]}
        return {"packet": dict(self.packet)}

    def start(self, ref, detected, counted, note=""):
        if self.down:
            raise VendorPlatformUnreachable("no answer")
        self.calls.append(("start", ref, detected, counted, note))
        return {"packet": self.packet}

    def uploaded(self, ref, hours, cards, files, size, note=""):
        if self.down:
            raise VendorPlatformUnreachable("no answer")
        self.calls.append(("uploaded", ref, hours, cards, files, size, note))
        return {"ok": True}


@pytest.fixture
def station(tmp_path):
    h = PeakAiHarness(tmp_path)
    h.vendor = FakeVendor()
    h.profile.vendor = h.vendor
    h.profile.packets = VendorPacketRepository(h.db)
    yield h
    h.close()


def insert_cards(station, *names):
    for name in names:
        station.gilabs_card(name)
    station.wait_for(lambda: len(station.station.view().waiting_cards) == len(names))


# ------------------------------------------------------------------ codes and mapping

def test_typos_are_caught_before_asking_the_platform():
    assert check_code(" gvgpa-3311u ") == CODE
    assert check_code(f"{CODE.lower()}-201126-0930") == REF.replace("-", "")
    assert check_letter(CODE[:9]) == CODE[9]
    with pytest.raises(VendorPlatformError, match="typo"):
        check_code(CODE[:9] + "A")
    with pytest.raises(VendorPlatformError, match="5 letters"):
        check_code("12345")
    with pytest.raises(VendorPlatformError, match="date and time"):
        check_code(CODE + "-2011")


def test_a_packet_becomes_the_session_details_with_no_questions():
    settings = VendorPlatformSettings()
    details = session_metadata(packet(environment="Phone repair"), settings)
    assert details == {"activity": "phone-repair", "city": "bijapur",
                       "vendor_name": "Ravi Kumar", "site": "Station road garage",
                       "packet": REF, "packet_device": "GI Lab", "recorded_from": "2026-11-18",
                       "recorded_to": "2026-11-18", "video_hours": 21.5}
    assert slug("Furniture  Workshop!") == "furniture-workshop"


def test_the_packet_reference_passes_the_phone_number_rule(station):
    details, _ = validate_session(session_metadata(packet(), VendorPlatformSettings()),
                                  station.pack)
    assert details["packet"] == REF and "batch" not in details
    with pytest.raises(Exception, match="packet reference"):
        validate_session({**session_metadata(packet(), VendorPlatformSettings()),
                          "packet": "+91 90000 00000"}, station.pack)


def test_counts_must_agree_or_be_explained():
    assert check_counts(2, 2, packet()).ok
    short = check_counts(9, 10, packet(cards_received=10))
    assert short.problems == ["You put in 10 card(s) but the station sees 9. 1 card(s) are not "
                              "showing: check each reader's light and push the card in again."]
    extra = check_counts(3, 2, packet())
    assert "1 more than you counted" in extra.problems[0]
    vendor_said = check_counts(2, 2, packet(cards_sent=3, cards_received=None))
    assert vendor_said.problems == ["The packet says the vendor sent 3 card(s); you counted 2."]
    again = check_counts(2, 2, packet(uploaded={"at": "2026-11-19", "hours": 20,
                                                "station": "rajajinagar-1"}))
    assert "already uploaded" in again.problems[0]


def test_device_cross_check_list():
    settings = VendorPlatformSettings()
    assert expected_devices("gi lab", settings) == [GILABS]
    assert expected_devices("iPhone (rented)", settings) is None  # Not checked.


# ------------------------------------------------------------------ the client

def test_client_maps_the_platforms_answers():
    seen = []

    def http(method, url, headers, body, timeout):
        seen.append((method, url, headers, body))
        if url.endswith(CODE):
            return 200, json.dumps({"packet": packet()}).encode()
        if url.endswith("/start"):
            return 401, b'{"error": "this station\'s key is missing or wrong"}'
        if url.endswith("/uploaded"):
            return 0, b""
        return 404, b'{"error": "no one has this code"}'

    client = VendorPlatform(VendorPlatformSettings(url="https://vendor.example"),
                            lambda: STATION_KEY, http)
    assert client.lookup(CODE.lower())["packet"]["ref"] == REF
    method, url, headers, _ = seen[0]
    assert (method, url) == ("GET", f"https://vendor.example/api/station/packets/{CODE}")
    assert headers["Authorization"] == f"Bearer {STATION_KEY}"
    with pytest.raises(VendorPlatformError, match="ask the office|Ask the office"):
        client.start(REF, 2, 2)
    with pytest.raises(VendorPlatformUnreachable):
        client.uploaded(REF, 1.0, 2, 3, 4)
    with pytest.raises(VendorPlatformError, match="no one has this code"):
        client.lookup("FVGPA3451G")
    with pytest.raises(VendorPlatformError, match="typo"):
        client.lookup(CODE[:9] + "A")
    assert len(seen) == 4  # The typo never left the station.


# ------------------------------------------------------------------ the station

def test_cards_in_count_code_then_it_uploads_and_reports_back(station):
    insert_cards(station, "A", "B")
    preview = station.station.packet_preview(CODE.lower(), 2)
    assert preview["problems"] == [] and preview["cards"] == {"detected": 2, "counted": 2,
                                                              "expected": 2}
    assert station.station.view().session is None  # A preview opens nothing.
    station.station.open_packet_session(CODE, 2)
    station.wait_states(A=CardState.UPLOADED, B=CardState.UPLOADED)
    station.wait_for(lambda: any(call[0] == "uploaded" for call in station.vendor.calls))

    # Everything came from the packet: activity, city, vendor, site; the packet in each _info.
    folder = f"gilabs/garage/bijapur/{upload_day()}/card-{card_serial('sim:A')}/"
    info = json.loads(next(station.store.objects[name]["data"]
                           for name in station.objects(folder) if "/_info/" in name))
    assert (info["vendor_name"], info["site"], info["packet"]) == (
        "Ravi Kumar", "Station road garage", REF)

    start = next(call for call in station.vendor.calls if call[0] == "start")
    assert start == ("start", REF, 2, 2, "")
    done = next(call for call in station.vendor.calls if call[0] == "uploaded")
    # .mcap footage has no readable length: the vendor's declared 21.5 h is reported instead,
    # so the platform's later pre-QC step (never more than uploaded) is not blocked.
    assert done[:4] == ("uploaded", REF, 21.5, 2)
    assert done[4] == 6 and "declared 21.5 h is reported" in done[6]  # 3 files per card.
    view = station.station.view().destination["packet"]
    assert view["ref"] == REF and view["uploaded_at"] and view["last_error"] is None


def test_a_missing_card_stops_the_start_until_found_or_explained(station):
    insert_cards(station, "A")
    with pytest.raises(StationError, match="station sees 1"):
        station.station.open_packet_session(CODE, 2)
    assert station.station.view().session is None
    station.station.open_packet_session(CODE, 2, "card 2 is cracked, kept aside")
    station.wait_states(A=CardState.UPLOADED)
    station.wait_for(lambda: any(call[0] == "uploaded" for call in station.vendor.calls))
    start = next(call for call in station.vendor.calls if call[0] == "start")
    assert start[2:] == (1, 2, "card 2 is cracked, kept aside")


def test_wrong_code_and_a_platform_that_is_down_open_nothing(station):
    insert_cards(station, "A", "B")
    with pytest.raises(StationError, match="no one has this code"):
        station.station.open_packet_session("FVGPA3451G", 2)
    with pytest.raises(StationError, match="typo"):
        station.station.open_packet_session(CODE[:9] + "A", 2)
    station.vendor.down = True
    with pytest.raises(Exception, match="no answer"):
        station.station.packet_preview(CODE, 2)
    assert station.station.view().session is None


def test_a_card_from_another_device_waits_for_a_person(station):
    station.vendor.packet = packet(device_kind="E2", cards_sent=1, cards_received=1)
    insert_cards(station, "A")
    station.station.open_packet_session(CODE, 1, "")  # Counts fine: no note needed.
    station.wait_states(A=CardState.NEEDS_DECISION)
    reason = station.station.view().destination["cards"]["sim-A"]["held_reason"]
    assert "the packet is E2 footage" in reason and GILABS in reason
    assert not any(call[0] == "uploaded" for call in station.vendor.calls)


def test_reporting_retries_until_the_platform_answers(station):
    insert_cards(station, "A", "B")
    station.station.open_packet_session(CODE, 2)
    station.vendor.down = True
    station.wait_states(A=CardState.UPLOADED, B=CardState.UPLOADED)
    station.wait_for(lambda: any(p["code"] == "E-VENDOR-REPORT"
                                 for p in station.station.view().problems))
    station.vendor.down = False
    station.station.execute("retry_reports", {})
    station.wait_for(lambda: any(call[0] == "uploaded" for call in station.vendor.calls))
    station.wait_for(lambda: not any(p["code"] == "E-VENDOR-REPORT"
                                     for p in station.station.view().problems))


# ------------------------------------------------------------------ one question: the code

def test_only_the_code_is_asked(station):
    insert_cards(station, "A", "B")
    preview = station.station.packet_preview(CODE)
    assert preview["problems"] == [] and preview["cards"] == {"detected": 2, "counted": 2,
                                                              "expected": 2}
    assert preview["vendor"]["vendor"] == "Ravi Kumar" and preview["choices"] == []
    station.station.execute("open_packet", {"code": CODE})
    station.wait_states(A=CardState.UPLOADED, B=CardState.UPLOADED)
    start = next(call for call in station.vendor.calls if call[0] == "start")
    assert start == ("start", REF, 2, 2, "")


def test_the_cards_seen_are_checked_against_the_packet(station):
    insert_cards(station, "A")
    preview = station.station.packet_preview(CODE)
    assert preview["problems"] == ["The packet holds 2 card(s) (the manager counted 2) but the "
                                   "station sees 1. 1 card(s) are not showing: check each "
                                   "reader's light and push the card in again."]
    with pytest.raises(StationError, match="station sees 1"):
        station.station.open_packet_session(CODE)
    station.station.open_packet_session(CODE, note="card 2 stayed with the vendor")
    station.wait_states(A=CardState.UPLOADED)


def test_a_code_with_several_packets_offers_them_or_just_the_vendor(station):
    second = packet(ref=f"{CODE}-221126-1015", cards_sent=1, cards_received=None)
    station.vendor.several = [second, packet()]
    insert_cards(station, "A")
    preview = station.station.packet_preview(CODE)
    assert preview["packet"] is None
    assert [item["ref"] for item in preview["choices"]] == [second["ref"], REF]
    assert preview["details"] == {"activity": "garage", "city": "bijapur",
                                  "vendor_name": "Ravi Kumar", "site": "Station road garage",
                                  "packet_device": "GI Lab"}
    with pytest.raises(StationError, match="more than one packet"):
        station.station.open_packet_session(CODE)
    # The vendor's details alone: no packet is marked arrived or reported uploaded.
    station.station.execute("open_packet", {"code": CODE, "vendor_only": True})
    station.wait_states(A=CardState.UPLOADED)
    meta = station.station.view().session["metadata"]
    assert (meta["vendor_name"], meta["activity"], meta["city"]) == ("Ravi Kumar", "garage",
                                                                    "bijapur")
    assert "packet" not in meta
    assert {call[0] for call in station.vendor.calls} == {"lookup"}
