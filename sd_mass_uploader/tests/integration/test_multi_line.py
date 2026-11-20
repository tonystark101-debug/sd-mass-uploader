"""The real Google client over two "lines", using two loopback addresses as the two lines.

127.0.0.1 and 127.0.0.2 are separate source addresses on every system, so the local storage
server can see which line sent each request. This proves the pinning, the byte counting and the
hand-over when a line is pulled. It does not prove that a real second internet line is routed
separately by the operating system: `python main.py lines` on the station checks that.
"""

from __future__ import annotations

import threading
import time

import pytest

from src.core.settings import GoogleCloudSettings, NetworkSettings
from src.network.discovery import FoundLine
from src.network.lines import LinePool
from src.uploader.card_locator import locate_fresh
from src.uploader.gcs_client import GcsObjectStore
from src.uploader.upload_manager import UploadManager
from tests.integration.fake_gcs_server import FakeGcsServer
from tests.unit.test_upload_engine import Harness

LINE_A = FoundLine("A", "line-A", "Ethernet", "127.0.0.1", "127.0.0.254")
LINE_B = FoundLine("B", "line-B", "USB", "127.0.0.2", "127.0.0.254")


@pytest.fixture
def server(monkeypatch):
    with FakeGcsServer() as fake:
        monkeypatch.setenv("STORAGE_EMULATOR_HOST", fake.url)
        yield fake


class Net:
    def __init__(self, *lines: FoundLine) -> None:
        self.lines = list(lines)

    def discover(self):
        return list(self.lines)


def pool_for(net: Net) -> LinePool:
    pool = LinePool(NetworkSettings(workers_per_line=3), fallback_workers=2,
                    discover=net.discover, probe=lambda line: (True, f"pub-{line.name}", None))
    pool.refresh()
    return pool


def harness(tmp_path, **upload) -> Harness:
    h = Harness(tmp_path, chunk_size_mib=0.25, request_timeout_seconds=10, **upload)
    h.settings.google_cloud = GoogleCloudSettings("test-project", "test-bucket", "uploads")
    h.store = GcsObjectStore(h.settings.google_cloud, h.settings.upload)
    return h


def run(h: Harness, plan, pool: LinePool):
    locator = locate_fresh(h.sessions.cards(plan.session_id))
    return UploadManager(h.settings, h.sessions, h.store, lines=pool).run(plan.session_id,
                                                                          locator)


def test_real_client_sends_each_file_through_one_line_and_uses_both(server, tmp_path):
    h = harness(tmp_path)
    pool = pool_for(Net(LINE_A, LINE_B))
    size = 700 * 1024  # Above the 256 KiB chunk: resumable, several requests per file.
    plan = h.plan(h.card("CARD1", {f"v{i}.mp4": bytes([i]) * size for i in range(12)}))
    run(h, plan, pool)
    assert h.counts(plan) == {"VERIFIED": 12}
    senders = server.state.upload_ips
    assert len(senders) == 12
    assert all(len(ips) == 1 for ips in senders.values())  # One file, one line.
    used = [next(iter(ips)) for ips in senders.values()]
    assert used.count("127.0.0.1") >= 3 and used.count("127.0.0.2") >= 3
    lines = {line["name"]: line for line in pool.view()["lines"]}
    # Each line counted what it really sent (file bytes plus HTTP framing).
    assert lines["line-A"]["sent_bytes"] >= used.count("127.0.0.1") * size
    assert lines["line-B"]["sent_bytes"] >= used.count("127.0.0.2") * size
    assert lines["line-A"]["files_done"] + lines["line-B"]["files_done"] == 12


def test_pulling_a_line_mid_upload_moves_its_files_to_the_other(server, tmp_path):
    h = harness(tmp_path, max_retries=0)  # A lost line must not use up a retry.
    server.state.upload_delay = 0.05  # Slow enough to pull the line while files are moving.
    net = Net(LINE_A, LINE_B)
    pool = pool_for(net)
    plan = h.plan(h.card("CARD2", {f"v{i}.mp4": bytes([i]) * (600 * 1024)
                                   for i in range(10)}))

    def pull_line_b() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(line.name == "line-B" and line.active for line in pool.usable()):
                break
            time.sleep(0.005)
        net.lines = [LINE_A]
        pool.refresh()

    puller = threading.Thread(target=pull_line_b)
    puller.start()
    run(h, plan, pool)
    puller.join()
    assert h.counts(plan) == {"VERIFIED": 10}
    states = {line["name"]: line["state"] for line in pool.view()["lines"]}
    assert states == {"line-A": "active", "line-B": "gone"}
    rows = h.rows(plan)
    assert all(row["attempts"] == 1 for row in rows)  # No retry was spent on the lost line.
    with h.db.transaction() as tx:
        lost = tx.execute("SELECT COUNT(*) FROM upload_attempts WHERE error_class = "
                          "'LineLostError'").fetchone()[0]
    assert lost >= 1  # Files really were cut off on line B and handed over.
    finished_on_b = [name for name, ips in server.state.upload_ips.items()
                     if "127.0.0.2" in ips]
    assert len(finished_on_b) < 10  # Line A finished what B could not.


def test_no_lines_means_the_default_connection(server, tmp_path):
    h = harness(tmp_path)
    pool = pool_for(Net())
    plan = h.plan(h.card("CARD3", {"a.mp4": b"a" * 1000, "b.mp4": b"b" * 1000}))
    run(h, plan, pool)
    assert h.counts(plan) == {"VERIFIED": 2}
    assert pool.view()["mode"] == "default"
