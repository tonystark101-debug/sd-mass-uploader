"""Shared fakes for PeakAI tests: a synthetic pack zip and a scripted token service.

The pack here is made up and follows the v2 layout. Tests never read the real pack or key.
"""

from __future__ import annotations

import json
import struct
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KEY = "test_laptop_key_0123456789abcdefABCDEF"
URL = "https://peakai-token-service-test.asia-south1.run.app"
FOLDERS = {"gilabs": "gilabs/", "buildai": "buildai/stereo/", "sieve": "sieve/mono/",
           "ots-mono": "ots-new/mono/", "ots-stereo": "ots-new/stereo/"}
DEVICES = {"gilabs": "GI Labs EGO1GS", "buildai": "Build AI stereo (encrypted)",
           "sieve": "Head camera", "ots-mono": "Head camera", "ots-stereo": "Stereo"}

README = """# PeakAI uploader passes (v2, 12 Nov 2026)

## The passes

| Pass | Folder in `gs://peak-lake-test/` | For |
|---|---|---|
| `gilabs` | `gilabs/` | GI Labs EGO1GS headsets |
| `buildai` | `buildai/stereo/` | Build AI stereo (encrypted) |
| `sieve` | `sieve/mono/` | Head-camera (mono) footage for Sieve |
| `ots-mono` | `ots-new/mono/` | Head-camera (mono) footage not for a client |
| `ots-stereo` | `ots-new/stereo/` | Phone stereo rig, E2 stereo, stereo hat |

## Rules for the uploader (Claude, read this)
Never print a token.
"""

SITE = """# PeakAI uploads: which folder (v2, from 12 Nov 2026)

```
gs://peak-lake-test/<pass folder>/<activity>/<city>/<upload day>/card-<SERIAL>/<the card, exactly as it is>
```

| What is on the card | Device | Pass |
|---|---|---|
| `data/session_*/ego_*.mcap` + `session.json` whose `hostname` starts with `GILABS-` | GI | gilabs |
| `recordings/*-sn<serial>-*-left.mp4.enc` (files start with `GKENC1`) | BuildAI | buildai |
| `Stereo/recordings/stereo_*/chunk_*/video.h265` | Phone | ots-stereo |
| `recordings/ego_*/metadata.json` + `video_live_*.mp4` | E2 | ots-stereo |
| `DVR/VIDEO/*.mp4` (head camera) | Head | sieve |

Footage that is not ours (`Android/` folders) is not uploaded.

**Activity:** `agriculture`, `construction`, `garage`, `salon`.

- A new kind of work: lowercase.
"""

TOKENS_SH = '#!/usr/bin/env bash\nURL="${PEAKAI_TOKEN_URL:-' + URL + '}"\n'


def make_pack(path: Path, **overrides: str | None) -> Path:
    files = {
        "PeakAI-Uploader-v2/README.md": README,
        "PeakAI-Uploader-v2/SITE_CHAT_INSTRUCTIONS.md": SITE,
        "PeakAI-Uploader-v2/peakai-tokens.sh": TOKENS_SH,
        "PeakAI-Uploader-v2/peakai-report.sh": "#!/usr/bin/env bash\n",
        "PeakAI-Uploader-v2/install.sh": "#!/usr/bin/env bash\n",
        "PeakAI-Uploader-v2/laptop_key.txt": KEY + "\n",
    }
    for name, value in overrides.items():
        target = f"PeakAI-Uploader-v2/{name}"
        if value is None:
            files.pop(target, None)
        else:
            files[target] = value
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


class FakeTokenService:
    """Answers like PeakAI's token service. `fail` maps a path prefix to HTTP codes to return."""

    def __init__(self, passes=("gilabs", "buildai", "sieve", "ots-mono", "ots-stereo"),
                 clock=time.time) -> None:
        self.passes = list(passes)
        self.clock = clock
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail: dict[str, list[int]] = {}
        self.reports: list[dict] = []
        self.issued = 0
        self.ttl = 3600
        self.wrong_folder: dict[str, str] = {}

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlparse(url)
        path = parsed.path
        self.calls.append((method, path, headers.get("X-PeakAI-Key")))
        for prefix, codes in self.fail.items():
            if path.startswith(prefix) and codes:
                return codes.pop(0), b'{"error": "scripted failure"}'
        if headers.get("X-PeakAI-Key") != KEY:
            return 401, b'{"error": "unknown laptop key"}'
        if path == "/v1/passes":
            return 200, json.dumps({"passes": [{"name": name} for name in self.passes]}).encode()
        if path == "/v1/token":
            name = parse_qs(parsed.query)["pass"][0]
            if name not in self.passes:
                return 403, f'{{"error": "this laptop may not use the {name} pass"}}'.encode()
            self.issued += 1
            expires = int(self.clock()) + self.ttl
            folder = self.wrong_folder.get(name, FOLDERS.get(name, name + "/"))
            text = (f"token: tok-{name}-{self.issued}\nexpires_unix: {expires}\n"
                    f"expires_ist: later\ndestination.{name}: {folder.rstrip('/')}\n"
                    f"device: {DEVICES.get(name, '?')}\n")
            return 200, text.encode()
        if path == "/v1/report":
            payload = json.loads(body)
            self.reports.append(payload)
            return 200, json.dumps({"entry": payload, "action": "updated"}).encode()
        return 404, b"{}"


def mp4_bytes(seconds: float, created: datetime | None = None, version: int = 0) -> bytes:
    """A tiny but well-formed MP4: ftyp, mdat, then moov/mvhd (as cameras often write it)."""
    origin = datetime(1904, 1, 1, tzinfo=UTC).timestamp()
    when = int(created.timestamp() - origin) if created else 0
    scale = 1000
    if version == 1:
        body = b"\x01\x00\x00\x00" + struct.pack(">QQIQ", when, when, scale, int(seconds * scale))
    else:
        body = b"\x00\x00\x00\x00" + struct.pack(">IIII", when, when, scale, int(seconds * scale))
    body += b"\x00" * 80
    mvhd = struct.pack(">I4s", 8 + len(body), b"mvhd") + body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x00\x00"
    mdat = struct.pack(">I4s", 8 + 64, b"mdat") + b"\x00" * 64
    return ftyp + mdat + moov
