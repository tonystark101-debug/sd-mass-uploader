"""Check whether mounted volumes can really be read, without hanging on a dead card."""

from __future__ import annotations

import os
import threading

from src.core.enums import AccessStatus
from src.core.models import Volume

PROBE_TIMEOUT_SECONDS = 5.0


def probe_access(mount_point: str) -> AccessStatus:
    """List the root directory. This only reads."""
    try:
        with os.scandir(mount_point) as entries:
            next(entries, None)
        return AccessStatus.OK
    except PermissionError:
        return AccessStatus.DENIED
    except OSError:
        return AccessStatus.ERROR


def probe_volumes(volumes: list[Volume], timeout: float = PROBE_TIMEOUT_SECONDS) -> None:
    """Probe in parallel. A failing card can block forever, so threads are daemons."""
    results: dict[str, AccessStatus] = {}

    def run(volume: Volume) -> None:
        results[volume.volume_id] = probe_access(volume.mount_point or "")

    threads = []
    for volume in volumes:
        if volume.is_mounted and volume.mount_point:
            thread = threading.Thread(target=run, args=(volume,), daemon=True)
            thread.start()
            threads.append((volume, thread))
    for volume, thread in threads:
        thread.join(timeout)
        volume.access_status = results.get(volume.volume_id, AccessStatus.ERROR)
