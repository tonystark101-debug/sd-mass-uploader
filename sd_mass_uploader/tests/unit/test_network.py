"""Several internet lines: finding them, balancing files across them, and lines coming and going."""

from __future__ import annotations

import threading

import pytest

from src.core.exceptions import (
    LineLostError,
    ReadOnlyViolation,
    SourceReadError,
    TransientUploadError,
)
from src.core.settings import NetworkSettings
from src.network.discovery import DiscoveryError, FoundLine, parse_linux, parse_windows
from src.network.lines import (
    ACTIVE,
    GONE,
    NO_INTERNET,
    RESTING,
    SPARE,
    LinePool,
    current_line,
)
from src.platform.process import assert_read_only
from tests.unit.test_upload_engine import Harness


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def found(name: str, ip: str, gateway: str = "192.168.1.1") -> FoundLine:
    return FoundLine(key=name, name=name, kind="Ethernet", ip=ip, gateway=gateway, device=name)


class Net:
    """What the computer reports: change `lines` to plug in or pull out a line."""

    def __init__(self, *lines: FoundLine) -> None:
        self.lines = list(lines)
        self.public: dict[str, str] = {}
        self.broken: set[str] = set()
        self.fail_discovery = False
        self.probed: list[str] = []

    def discover(self) -> list[FoundLine]:
        if self.fail_discovery:
            raise DiscoveryError("ip failed")
        return list(self.lines)

    def probe(self, line):
        self.probed.append(line.name)
        if line.name in self.broken:
            return False, None, "timed out"
        return True, self.public.get(line.name, f"public-{line.name}"), None


def make_pool(net: Net, clock: Clock | None = None, events=None, **options) -> LinePool:
    settings = NetworkSettings(**options)
    return LinePool(settings, fallback_workers=4, discover=net.discover, probe=net.probe,
                    clock=clock or Clock(),
                    on_event=(lambda kind, **data: events.append((kind, data["line"])))
                    if events is not None else None)


# ------------------------------------------------------------------ discovery parsing

def test_linux_lines_need_their_own_default_route():
    routes = [
        {"dst": "default", "gateway": "192.168.1.1", "dev": "enp1s0", "metric": 100},
        {"dst": "default", "gateway": "192.168.42.129", "dev": "usb0", "metric": 101},
        {"dst": "default", "gateway": "10.0.0.1", "dev": "enp2s0", "flags": ["linkdown"]},
    ]
    addresses = [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
        {"ifname": "enp1s0", "operstate": "UP",
         "addr_info": [{"family": "inet", "local": "192.168.1.20", "prefixlen": 24}]},
        {"ifname": "usb0", "operstate": "UNKNOWN",
         "addr_info": [{"family": "inet", "local": "192.168.42.77"}]},
        {"ifname": "enp2s0", "operstate": "DOWN",
         "addr_info": [{"family": "inet", "local": "10.0.0.5"}]},
        {"ifname": "wlp3s0", "operstate": "UP",  # No default route: not a line.
         "addr_info": [{"family": "inet", "local": "172.16.0.9"}]},
    ]
    assert parse_linux(routes, addresses) == [
        ("enp1s0", "192.168.1.20", "192.168.1.1"),
        ("usb0", "192.168.42.77", "192.168.42.129"),
    ]


def test_linux_skips_link_local_addresses():
    routes = [{"dst": "default", "gateway": "192.168.1.1", "dev": "eth0"}]
    addresses = [{"ifname": "eth0", "operstate": "UP",
                  "addr_info": [{"family": "inet", "local": "169.254.3.3"}]}]
    assert parse_linux(routes, addresses) == []


def test_windows_rows_become_lines_with_kind_and_warning():
    rows = [
        {"alias": "Ethernet 2", "guid": "{A}", "ip": "192.168.0.5", "gateway": "192.168.0.1",
         "desc": "Realtek USB GbE Family Controller", "media": "802.3", "weak": "Disabled"},
        {"alias": "Wi-Fi", "guid": "{B}", "ip": "192.168.29.12", "gateway": "192.168.29.1",
         "desc": "Intel Wi-Fi 6", "media": "Native 802.11", "weak": "Enabled"},
        {"alias": "Ethernet 3", "guid": "{C}", "ip": "169.254.1.1", "gateway": "x"},
        {"alias": "Ethernet 4", "guid": "{D}", "ip": "192.168.42.7", "gateway": "192.168.42.129",
         "desc": "Remote NDIS based Internet Sharing Device", "media": "802.3", "weak": "Disabled"},
    ]
    lines = parse_windows(rows)
    # A hub's LAN port is a wired line; a phone sharing its connection is "USB".
    assert [(line.name, line.kind) for line in lines] == [("Ethernet 2", "Ethernet (USB)"),
                                                         ("Wi-Fi", "Wi-Fi"),
                                                         ("Ethernet 4", "USB")]
    assert lines[0].key == "{A}" and lines[0].warning is None
    assert "WeakHostSend" in lines[1].warning


@pytest.mark.parametrize("argv", [
    ["ip", "-j", "-4", "route", "show", "default"],
    ["ip", "-j", "-4", "addr", "show"],
    ["/usr/sbin/ip", "-j", "-4", "addr", "show"],
])
def test_ip_may_only_list(argv):
    assert_read_only(argv)


@pytest.mark.parametrize("argv", [
    ["ip"], ["ip", "addr", "del", "1.2.3.4/24", "dev", "eth0"], ["ip", "link", "set", "eth0",
                                                                "down"],
    ["ip", "-j", "-4", "route", "show", "default", "extra"], ["ip", "route", "flush", "all"],
])
def test_ip_cannot_change_the_network(argv):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(argv)


# ------------------------------------------------------------------ the pool

def test_two_lines_are_both_used_with_their_own_slots():
    net = Net(found("eth0", "192.168.1.20"), found("usb0", "192.168.42.7", "192.168.42.1"))
    pool = make_pool(net)
    pool.refresh()
    assert [line.state for line in pool.views()] == [ACTIVE, ACTIVE]
    assert pool.capacity() == 8
    picked = [pool.acquire().name for _ in range(8)]
    assert sorted(picked) == ["eth0"] * 4 + ["usb0"] * 4  # Least-busy line first.
    view = pool.view()
    assert view["mode"] == "multi" and view["working"] == 2


def test_release_frees_the_slot_on_that_line():
    pool = make_pool(Net(found("eth0", "10.0.0.2"), found("eth1", "10.1.0.2", "10.1.0.1")))
    pool.refresh()
    first = pool.acquire()
    second = pool.acquire()
    assert first.name != second.name
    pool.release(first, None)
    assert pool.acquire().name == first.name


def test_plugging_in_a_line_adds_its_slots_and_says_so():
    events = []
    net = Net(found("eth0", "192.168.1.20"))
    pool = make_pool(net, events=events)
    pool.refresh()
    assert pool.capacity() == 4
    net.lines.append(found("usb0", "192.168.42.7", "192.168.42.1"))
    pool.refresh()
    assert pool.capacity() == 8
    assert ("line_ready", "usb0") in events


def test_unplugging_a_line_stops_its_uploads_and_moves_capacity():
    events = []
    net = Net(found("eth0", "192.168.1.20"), found("usb0", "192.168.42.7", "192.168.42.1"))
    pool = make_pool(net, events=events)
    pool.refresh()
    usb = next(line for line in pool.usable() if line.name == "usb0")
    generation = usb.generation
    usb.check_open(generation)  # Fine while plugged in.
    net.lines.pop()
    pool.refresh()
    assert usb.state == GONE
    with pytest.raises(LineLostError):
        usb.check_open(generation)
    assert pool.capacity() == 4
    assert all(pool.acquire().name == "eth0" for _ in range(4))
    assert ("line_lost", "usb0") in events


def test_a_line_that_comes_back_is_checked_and_used_again():
    net = Net(found("eth0", "192.168.1.20"), found("usb0", "192.168.42.7", "192.168.42.1"))
    pool = make_pool(net)
    pool.refresh()
    usb = net.lines.pop()
    pool.refresh()
    net.lines.append(usb)
    pool.refresh()
    assert pool.capacity() == 8
    assert net.probed.count("usb0") == 2


def test_new_address_stops_old_connections():
    net = Net(found("eth0", "192.168.1.20"))
    pool = make_pool(net)
    pool.refresh()
    line = pool.usable()[0]
    old = line.generation
    net.lines = [found("eth0", "192.168.1.99")]
    pool.refresh()
    assert line.ip == "192.168.1.99" and line.state == ACTIVE
    with pytest.raises(LineLostError):
        line.check_open(old)
    line.check_open(line.generation)


def test_line_without_internet_is_not_used_and_is_rechecked():
    clock = Clock()
    net = Net(found("eth0", "192.168.1.20"), found("usb0", "192.168.42.7", "192.168.42.1"))
    net.broken.add("usb0")
    pool = make_pool(net, clock)
    pool.refresh()
    states = {line.name: line.state for line in pool.views()}
    assert states == {"eth0": ACTIVE, "usb0": NO_INTERNET}
    assert pool.capacity() == 4
    assert [code for code, _, _ in pool.issues()] == ["W-LINE-NO-INTERNET"]
    net.broken.clear()
    pool.refresh()  # Too soon: not checked again yet.
    assert pool.capacity() == 4
    clock.now += 31
    pool.refresh()
    assert pool.capacity() == 8
    assert pool.issues() == []


def test_three_network_failures_rest_a_line_then_it_recovers():
    clock = Clock()
    events = []
    net = Net(found("eth0", "192.168.1.20"), found("usb0", "192.168.42.7", "192.168.42.1"))
    pool = make_pool(net, clock, events=events, rest_seconds=60)
    pool.refresh()
    usb = next(line for line in pool.usable() if line.name == "usb0")
    for _ in range(3):
        usb.active += 1
        pool.release(usb, TransientUploadError("connection reset"))
    assert usb.state == RESTING and pool.capacity() == 4
    assert ("line_resting", "usb0") in events
    clock.now += 61
    pool.refresh()
    assert usb.state == ACTIVE and pool.capacity() == 8


def test_card_errors_and_lost_lines_do_not_count_against_a_line():
    pool = make_pool(Net(found("eth0", "192.168.1.20")))
    pool.refresh()
    line = pool.usable()[0]
    for error in [SourceReadError("bad card"), LineLostError("gone")] * 3:
        line.active += 1
        pool.release(line, error)
    assert line.state == ACTIVE and line.failures == 0


def test_max_lines_keeps_extra_lines_as_spares():
    net = Net(found("eth0", "10.0.0.2"), found("eth1", "10.1.0.2", "10.1.0.1"),
              found("usb0", "10.2.0.2", "10.2.0.1"))
    pool = make_pool(net, max_lines=2)
    pool.refresh()
    assert [line.state for line in pool.views()] == [ACTIVE, ACTIVE, SPARE]
    assert pool.capacity() == 8
    net.lines.pop(0)  # A working line is pulled: the spare steps in.
    pool.refresh()
    assert [line.state for line in pool.views()] == [GONE, ACTIVE, ACTIVE]
    assert pool.capacity() == 8


def test_total_workers_are_capped():
    net = Net(*[found(f"eth{i}", f"10.{i}.0.2", f"10.{i}.0.1") for i in range(6)])
    pool = make_pool(net, max_total_workers=16)
    pool.refresh()
    assert pool.capacity() == 16


def test_two_cables_into_one_router_are_marked_shared():
    net = Net(found("eth0", "192.168.1.20"), found("eth1", "192.168.1.21"))
    net.public = {"eth0": "49.37.1.1", "eth1": "49.37.1.1"}
    pool = make_pool(net)
    pool.refresh()
    views = {line.name: line for line in pool.views()}
    assert views["eth1"].shares_with == "eth0" and views["eth0"].shares_with is None
    assert pool.capacity() == 8  # Still used: it adds no speed, but costs nothing.
    assert [code for code, _, _ in pool.issues()] == ["I-LINE-SHARED"]


def test_same_local_address_on_two_connections_cannot_be_pinned():
    net = Net(found("usb0", "172.20.10.2", "172.20.10.1"),
              found("usb1", "172.20.10.2", "172.20.10.1"), found("eth0", "192.168.1.20"))
    pool = make_pool(net)
    pool.refresh()
    states = {line.name: line.state for line in pool.views()}
    assert states == {"usb0": NO_INTERNET, "usb1": NO_INTERNET, "eth0": ACTIVE}
    net.lines.pop(1)  # One iPhone unplugged: the other works again.
    pool.refresh()
    assert {line.name: line.state for line in pool.views()}["usb0"] == ACTIVE


def test_failed_discovery_keeps_the_lines_it_had():
    net = Net(found("eth0", "192.168.1.20"), found("usb0", "192.168.42.7", "192.168.42.1"))
    pool = make_pool(net)
    pool.refresh()
    net.fail_discovery = True
    pool.refresh()
    assert pool.capacity() == 8
    assert pool.view()["error"] == "ip failed"


def test_no_working_line_falls_back_to_the_default_connection():
    net = Net(found("eth0", "192.168.1.20"))
    net.broken.add("eth0")
    pool = make_pool(net)
    pool.refresh()
    assert pool.acquire() is None
    assert pool.capacity() == 4  # upload.max_workers on the default connection.
    assert pool.view()["mode"] == "default"


def test_gone_lines_are_forgotten_after_two_minutes():
    clock = Clock()
    net = Net(found("eth0", "192.168.1.20"), found("usb0", "192.168.42.7", "192.168.42.1"))
    pool = make_pool(net, clock)
    pool.refresh()
    net.lines.pop()
    pool.refresh()
    assert len(pool.views()) == 2
    clock.now += 121
    pool.refresh()
    assert [line.name for line in pool.views()] == ["eth0"]


def test_speed_is_measured_per_line_from_sent_bytes():
    clock = Clock()
    pool = make_pool(Net(found("eth0", "10.0.0.2"), found("eth1", "10.1.0.2", "10.1.0.1")),
                     clock)
    pool.refresh()
    eth0, eth1 = pool.usable()
    eth0.add_sent(50_000_000)  # 50 MB in the last 10 s = 40 Mbit/s.
    eth1.add_sent(25_000_000)
    view = pool.view()
    speeds = {line["name"]: line["mbit_per_s"] for line in view["lines"]}
    assert speeds == {"eth0": 40.0, "eth1": 20.0}
    assert view["total_mbit_per_s"] == 60.0
    clock.now += 11  # Nothing sent for 11 s: speed now is zero.
    assert pool.view()["total_mbit_per_s"] == 0.0
    assert pool.view()["lines"][0]["sent_bytes"] == 50_000_000


# ------------------------------------------------------------------ the engine with lines

class LineRecordingStore:
    """Wraps the engine tests' fake store and records which line each upload ran on."""

    def __init__(self, inner, lose_on: str | None = None) -> None:
        self.inner = inner
        self.lines: dict[str, str | None] = {}
        self.lose_on = lose_on
        self.lost = 0
        self.lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def upload_stream(self, name, *args):
        line = current_line()
        with self.lock:
            if line is not None and line.name == self.lose_on and self.lost < 2:
                self.lost += 1
                raise LineLostError(f"{line.name} unplugged")
            self.lines[name] = line.name if line else None
        return self.inner.upload_stream(name, *args)


def run_with_lines(harness: Harness, plan, pool: LinePool, store):
    from src.uploader.card_locator import locate_fresh
    from src.uploader.upload_manager import UploadManager

    locator = locate_fresh(harness.sessions.cards(plan.session_id))
    return UploadManager(harness.settings, harness.sessions, store, lines=pool).run(
        plan.session_id, locator)


def test_engine_spreads_files_over_both_lines(tmp_path):
    h = Harness(tmp_path)
    pool = make_pool(Net(found("eth0", "10.0.0.2"), found("usb0", "10.1.0.2", "10.1.0.1")))
    pool.refresh()
    barrier = threading.Barrier(8, timeout=5)  # Proves 8 run at once: 4 per line.
    h.store.before_upload = lambda _: barrier.wait()
    store = LineRecordingStore(h.store)
    plan = h.plan(h.card("A", {f"{i}.bin": b"x" * 100 for i in range(16)}))
    run_with_lines(h, plan, pool, store)
    assert h.counts(plan) == {"VERIFIED": 16}
    assert h.store.max_concurrent == 8
    used = list(store.lines.values())
    assert used.count("eth0") == 8 and used.count("usb0") == 8


def test_files_from_a_lost_line_go_back_without_using_a_retry(tmp_path):
    h = Harness(tmp_path, max_retries=0)  # Any retry used would fail the file.
    pool = make_pool(Net(found("eth0", "10.0.0.2"), found("usb0", "10.1.0.2", "10.1.0.1")))
    pool.refresh()
    store = LineRecordingStore(h.store, lose_on="usb0")
    plan = h.plan(h.card("A", {f"{i}.bin": b"y" * 50 for i in range(6)}))
    run_with_lines(h, plan, pool, store)
    assert store.lost == 2
    assert h.counts(plan) == {"VERIFIED": 6}
    assert all(row["attempts"] == 1 for row in h.rows(plan))
