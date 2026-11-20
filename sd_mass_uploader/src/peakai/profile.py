"""The PeakAI destination: pass routing, card folders, `_info` files and dashboard reports.

Follows the imported pack (README.md and SITE_CHAT_INSTRUCTIONS.md, v2):
- each part of a card goes to the pass of the device that recorded it, never a guess;
- `<pass folder>/<activity>/<city>/<upload day>/card-<SERIAL>/<the card as it is>`;
- after a part is fully verified, a new `_info/<machine>_<stamp>.json` is written beside it;
- the PeakAI dashboard gets one entry per upload day + activity + city + pass, "uploading" when
  its cards start and "uploaded" when every card is done and has its `_info` file (D15).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable

from src.core.enums import SessionStatus
from src.core.exceptions import MetadataValidationError, UploaderError
from src.core.models import Card, ScanSummary
from src.core.settings import Settings
from src.database.peakai_repository import PeakAiRepository
from src.database.repositories import ScanRepository, SessionRepository
from src.metadata.session import new_session_id
from src.peakai.devices import (
    DEVICE_NAMES,
    DEVICE_PASSES,
    Classification,
    DeviceGroup,
    assign_files,
    choose_passes,
    classify,
    detect_details,
)
from src.peakai.keeper import Http, PassKeeper, _service_error, urllib_http
from src.peakai.pack import PackSettings
from src.peakai.session import (
    card_folder,
    card_serial,
    info_document,
    info_object_name,
    machine_name,
    object_name,
    validate_session,
)
from src.peakai.store import PeakAiStore
from src.platform.paths import open_source_readonly, to_extended_path
from src.station.profile import QueueOutcome, SessionCreated

logger = logging.getLogger(__name__)

RETRY_SECONDS = 60
_TASK_BATCH = 500
_REPORT_TIMEOUT = 90


def _group_dict(group: DeviceGroup) -> dict:
    return {"device": group.device, "prefixes": group.prefixes, "pass": group.pass_name,
            "files": len(group.files), "bytes": group.bytes, "hours": group.hours,
            "recorded_from": group.recorded_from, "recorded_to": group.recorded_to}


class PeakAiProfile:
    name = "peakai"

    def __init__(self, settings: Settings, pack: PackSettings, keeper: PassKeeper,
                 store: PeakAiStore, sessions: SessionRepository, scans: ScanRepository,
                 repo: PeakAiRepository, key_loader: Callable[[], str],
                 http: Http = urllib_http, clock: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self.pack = pack
        self.keeper = keeper
        self._store = store
        self._sessions = sessions
        self._scans = scans
        self._repo = repo
        self._key_loader = key_loader
        self._http = http
        self._clock = clock
        self._machine = machine_name(settings.peakai.machine_name)
        self._lock = threading.Lock()
        # Object metadata: the session's activity, city and day (never vendor or people).
        settings.upload.object_metadata_fields = ["activity", "city", "upload_date"]

    def store(self) -> PeakAiStore:
        return self._store

    # ------------------------------------------------------------------ sessions

    def create_session(self, metadata: dict) -> SessionCreated:
        details, notes = validate_session(metadata, self.pack)
        if not self._settings.peakai.vendor_code:
            raise MetadataValidationError("Set peakai.vendor_code in config.yaml (the `vendor` "
                                          "value of this laptop's _info files) first")
        session_id = new_session_id()
        prefix = f"gs://{self.pack.bucket}"
        self._sessions.create(session_id, prefix, details)
        self._sessions.set_status(session_id, SessionStatus.READY)
        return SessionCreated(session_id, prefix, notes)

    # ------------------------------------------------------------------ cards

    def _files(self, scan_id: str) -> list[dict]:
        return list(self._scans.iter_readable_files(scan_id))

    @staticmethod
    def _reader(mount: str):
        def read(relative: str, limit: int) -> bytes | None:
            try:
                path = to_extended_path(os.path.join(mount, *relative.split("/")))
                with open_source_readonly(path) as handle:
                    return handle.read(limit)
            except OSError:
                return None

        def opener(relative: str):
            try:
                path = to_extended_path(os.path.join(mount, *relative.split("/")))
                return open_source_readonly(path)
            except OSError:
                return None

        return read, opener

    def queue_card(self, session_id: str, card: Card, summary: ScanSummary) -> QueueOutcome:
        rows = self._files(summary.scan_id)
        files = [(row["relative_path"], row["size_bytes"]) for row in rows]
        read, opener = self._reader(card.mount_point)
        result = classify(files, read)
        for group in result.groups:
            detect_details(group, opener)
        plan = {"scan_id": summary.scan_id, "notes": result.notes, "decision": None,
                "groups": [_group_dict(group) for group in result.groups]}
        return self._finish(session_id, card, card.card_id, plan, rows, result)

    def decide(self, session_id: str, card: Card | None, card_id: str,
               decision: dict) -> QueueOutcome:
        """The operator's answer for a held card: the device of the unplaced files, or skip
        them. An empty decision re-checks (after PeakAI added a pass, for example)."""
        from src.station.station import StationError

        record = self._repo.card(session_id, card_id)
        if record is None:
            raise StationError(f"{card_id} has no placement to decide")
        if not record["held_reason"]:
            raise StationError(f"{card_id} is already placed")
        plan = record["plan"]
        device, skip = decision.get("device"), bool(decision.get("skip"))
        if device and skip:
            raise StationError("Choose a device or skip, not both")
        if device and device not in DEVICE_NAMES:
            raise StationError(f"Unknown device {device!r}. Choose one of: "
                               + ", ".join(DEVICE_NAMES))
        if device or skip:
            plan["decision"] = {"device": device, "skip": skip}
        rows = self._files(plan["scan_id"])
        groups = [DeviceGroup(item["device"], item["prefixes"], hours=item["hours"],
                              recorded_from=item["recorded_from"],
                              recorded_to=item["recorded_to"])
                  for item in plan["groups"]]
        result = Classification(groups, [], [], plan.get("notes", []))
        if card is None:
            card = Card(card_id, "", None)
        return self._finish(session_id, card, card_id, plan, rows, result, rebuild=True)

    def _finish(self, session_id: str, card: Card, card_id: str, plan: dict, rows: list[dict],
                result: Classification, rebuild: bool = False) -> QueueOutcome:
        files = [(row["relative_path"], row["size_bytes"]) for row in rows]
        decision = plan.get("decision") or {}
        catch_all = DeviceGroup(decision["device"], []) if decision.get("device") else None
        assign_files(files, result, catch_all)
        if not rebuild:
            self._scans.add_errors(plan["scan_id"], [
                (path, "NOT_PROJECT_FOOTAGE", why) for path, why in result.excluded])
        reasons = []
        serial = card_serial(card.fingerprint) if card.fingerprint else plan.get("serial")
        plan["serial"] = serial
        if card.volume_label is not None or "label" not in plan:
            plan["label"] = card.volume_label or ""
        if not serial:
            reasons.append("the card has no usable volume serial, so its folder "
                           "card-<SERIAL> cannot be named")
        if not self._usable_known():
            reasons.append("this laptop's PeakAI passes could not be fetched yet; "
                           "retry when online")
        reasons += choose_passes(result, self.keeper.usable_passes())
        if result.unplaced and not decision.get("skip"):
            sample = ", ".join(result.unplaced[:3])
            reasons.append(f"{len(result.unplaced)} file(s) match no known device (for example "
                           f"{sample}). Choose the device that recorded them, or skip them")
        if not result.groups and not reasons:
            reasons.append("nothing on the card matches a known device. Choose the device "
                           "that recorded it")
        plan["groups"] = [_group_dict(group) for group in result.groups]
        plan["unplaced"] = len(result.unplaced)
        plan["excluded"] = len(result.excluded)
        held = "; ".join(reasons) or None
        self._repo.save_card(session_id, card_id, serial, plan, held)
        if held:
            return QueueOutcome(held_reason=held)
        if decision.get("skip") and result.unplaced:
            self._scans.add_errors(plan["scan_id"], [
                (path, "SKIPPED_BY_OPERATOR", "the operator chose not to upload it")
                for path in result.unplaced])
        return self._queue_tasks(session_id, card_id, plan, rows, result, serial)

    def _usable_known(self) -> bool:
        if self.keeper.status()["allowed"] is None:
            try:
                self.keeper.refresh_passes()
            except UploaderError:
                return False
        return True

    def _queue_tasks(self, session_id: str, card_id: str, plan: dict, rows: list[dict],
                     result: Classification, serial: str) -> QueueOutcome:
        details = self._sessions.metadata(session_id)
        by_path = {row["relative_path"]: row for row in rows}
        outcome = QueueOutcome()
        batch: list[tuple] = []
        for group, stored in zip(result.groups, plan["groups"], strict=True):
            folder = card_folder(self.pack, group.pass_name, details, serial)
            stored["folder"] = folder
            for path in group.files:
                row = by_path[path]
                try:
                    name = object_name(folder, path)
                except MetadataValidationError as exc:
                    self._scans.add_errors(plan["scan_id"],
                                           [(path, "UNSAFE_OBJECT_NAME", str(exc))])
                    outcome.rejected += 1
                    continue
                batch.append((session_id, row["file_id"], card_id, path, name,
                              row["size_bytes"], row["mtime_ns"]))
                outcome.planned_files += 1
                outcome.planned_bytes += row["size_bytes"]
                if len(batch) >= _TASK_BATCH:
                    self._sessions.add_tasks(batch)
                    batch = []
        if batch:
            self._sessions.add_tasks(batch)
        self._repo.save_card(session_id, card_id, serial, plan, None)
        return outcome

    # ------------------------------------------------------------------ after uploading

    def housekeeping(self, session_id: str) -> None:
        """Write due `_info` files, then bring the dashboard up to date. Never raises."""
        if not self._lock.acquire(blocking=False):
            return  # Already running.
        try:
            self._write_infos(session_id)
            self._report(session_id)
        except UploaderError as exc:
            logger.warning("PeakAI housekeeping: %s", exc)
        finally:
            self._lock.release()

    def _part_complete(self, session_id: str, card_id: str, group: dict) -> bool:
        counts = self._repo.folder_counts(session_id, card_id, group["folder"])
        total = sum(counts.values())
        return bool(total) and counts.get(self._repo.verified_status(), 0) == total

    def _write_infos(self, session_id: str) -> None:
        details = self._sessions.metadata(session_id)
        now = self._clock()
        for record in self._repo.cards(session_id):
            if record["held_reason"]:
                continue
            for group in record["plan"]["groups"]:
                if "folder" not in group or not self._part_complete(
                        session_id, record["card_id"], group):
                    continue
                info = self._repo.info(session_id, record["card_id"], group["pass"])
                if info and (info["written_at"] or info["next_try"] > now):
                    continue
                self._write_info(session_id, record, group, details)

    def _write_info(self, session_id: str, record: dict, group: dict, details: dict) -> None:
        card_id = record["card_id"]
        name = info_object_name(group["folder"], self._machine)
        try:
            document = info_document(
                pass_name=group["pass"], folder=group["folder"],
                vendor_code=self._settings.peakai.vendor_code, details=details,
                device=group["device"], serial=record["serial"],
                label=record["plan"].get("label"), file_count=group["files"],
                total_bytes=group["bytes"], hours=group["hours"],
                recorded_from=group["recorded_from"], recorded_to=group["recorded_to"],
                machine=self._machine,
            )
            body = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")
            self._store.upload_bytes(name, body, "application/json", 0)  # New file only.
        except (UploaderError, MetadataValidationError) as exc:
            self._repo.record_info(session_id, card_id, group["pass"], None,
                                   f"{type(exc).__name__}: {exc}", self._clock() + RETRY_SECONDS)
            return
        self._repo.record_info(session_id, card_id, group["pass"], name, None)
        logger.info("Wrote %s", name)

    def _payloads(self, session_id: str) -> dict[str, dict]:
        details = self._sessions.metadata(session_id)
        parts: dict[str, list[tuple[dict, dict]]] = {}
        for record in self._repo.cards(session_id):
            if record["held_reason"]:
                continue
            for group in record["plan"]["groups"]:
                if "folder" in group:
                    parts.setdefault(group["pass"], []).append((record, group))
        infos = {(row["card_id"], row["pass_name"]): row
                 for row in self._repo.infos(session_id)}
        payloads = {}
        for pass_name, items in parts.items():
            done = all(self._part_complete(session_id, record["card_id"], group)
                       and (infos.get((record["card_id"], pass_name)) or {}).get("written_at")
                       for record, group in items)
            payload = {
                "pass": pass_name, "camera": pass_name,
                "upload_date": details["upload_date"],
                "activity": details["activity"], "location": details["city"],
                "vendor": details["vendor_name"],
                "device": ", ".join(sorted({group["device"] for _, group in items})),
                "status": "uploaded" if done else "uploading", "dry_run": False,
                "cards": len({record["card_id"] for record, _ in items}),
            }
            hours = [group["hours"] for _, group in items]
            if all(value is not None for value in hours):
                payload["hours"] = round(sum(hours), 2)
            elif details.get("video_hours"):
                payload["hours"] = float(details["video_hours"])
            starts = [group["recorded_from"] for _, group in items]
            ends = [group["recorded_to"] for _, group in items]
            if all(starts) and all(ends):
                payload["collected_from"], payload["collected_to"] = min(starts), max(ends)
            else:
                if details.get("recorded_from"):
                    payload["collected_from"] = details["recorded_from"]
                if details.get("recorded_to"):
                    payload["collected_to"] = details["recorded_to"]
            payloads[pass_name] = payload
        return payloads

    def _report(self, session_id: str) -> None:
        now = self._clock()
        for pass_name, payload in self._payloads(session_id).items():
            last = self._repo.report(session_id, pass_name)
            body = json.dumps(payload, sort_keys=True)
            if last and not last["last_error"] and last["payload_json"] == body:
                continue  # The dashboard already shows this.
            if last and last["last_error"] and last["next_try"] > now:
                continue
            self._post(session_id, pass_name, payload)

    def _post(self, session_id: str, pass_name: str, payload: dict) -> None:
        try:
            key = self._key_loader()
        except UploaderError as exc:
            self._repo.record_report(session_id, pass_name, None, payload, None, str(exc),
                                     self._clock() + RETRY_SECONDS)
            return
        code, response = self._http(
            "POST", self.pack.token_url + "/v1/report",
            {"X-PeakAI-Key": key, "Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"), _REPORT_TIMEOUT)
        text = response.decode("utf-8", errors="replace")[:2000]
        if code == 200:
            self._repo.record_report(session_id, pass_name, payload["status"], payload, text,
                                     None)
            logger.info("PeakAI dashboard: %s %s %s", pass_name, payload["status"], text[:200])
        else:
            error = str(_service_error(code, "Dashboard report", response))
            self._repo.record_report(session_id, pass_name, None, payload, text or None,
                                     f"FAILED, nothing recorded: {error}",
                                     self._clock() + RETRY_SECONDS)

    def retry_reports(self, session_id: str) -> None:
        self._repo.clear_retry_delays(session_id)

    # ------------------------------------------------------------------ views

    def problems(self, session_id: str | None) -> list[dict]:
        found: list[dict] = []
        status = self.keeper.status()
        if status["key_error"]:
            found.append({"code": "E-PEAKAI-KEY", "detail": status["key_error"]})
        elif status["legacy_only"]:
            found.append({"code": "W-PEAKAI-OLD-PASSES"})
        elif status["allowed"] == [] or (status["allowed"] is not None
                                         and not status["passes"]):
            found.append({"code": "E-PEAKAI-NO-PASSES"})
        elif status["service_error"] or any(p["last_error"] and not p["has_token"]
                                            for p in status["passes"]):
            detail = status["service_error"] or next(
                p["last_error"] for p in status["passes"] if p["last_error"])
            found.append({"code": "W-PEAKAI-SERVICE", "detail": detail})
        if session_id is None:
            return found
        details = self._sessions.metadata(session_id)
        for record in self._repo.cards(session_id):
            if record["held_reason"]:
                found.append({"code": "W-CARD-DECISION", "card_id": record["card_id"],
                              "detail": record["held_reason"]})
        for info in self._repo.infos(session_id):
            if info["last_error"] and not info["written_at"]:
                found.append({"code": "E-PEAKAI-INFO", "card_id": info["card_id"],
                              "detail": info["last_error"]})
        for report in self._repo.reports(session_id):
            if report["last_error"]:
                found.append({"code": "E-PEAKAI-REPORT", "subject": report["pass_name"],
                              "detail": f"{report['pass_name']}: {report['last_error']}"})
        for pass_name, payload in self._payloads(session_id).items():
            if "hours" not in payload or "collected_from" not in payload:
                missing = [label for key, label in (("hours", "video hours"),
                                                    ("collected_from", "recording dates"))
                           if key not in payload]
                if not (details.get("video_hours") and details.get("recorded_from")):
                    found.append({"code": "I-PEAKAI-DETAILS", "subject": pass_name,
                                  "detail": f"{pass_name}: {' and '.join(missing)}"})
        return found

    def view(self, session_id: str | None) -> dict:
        pack = self.pack
        result = {
            "profile": self.name,
            "pack": {"name": pack.source_name, "version": pack.version, "dated": pack.dated,
                     "bucket": pack.bucket, "imported_at": pack.imported_at,
                     "activities": pack.activities, "documents": pack.documents},
            "devices": {device: list(passes) for device, passes in DEVICE_PASSES.items()},
            "keeper": self.keeper.status(),
        }
        if session_id is not None:
            result["cards"] = {
                record["card_id"]: {"serial": record["serial"],
                                    "held_reason": record["held_reason"],
                                    "groups": record["plan"]["groups"],
                                    "unplaced": record["plan"].get("unplaced", 0),
                                    "excluded": record["plan"].get("excluded", 0)}
                for record in self._repo.cards(session_id)}
            result["reports"] = [
                {key: row[key] for key in ("pass_name", "status", "sent_at", "last_error")}
                | {"response": (row["response_text"] or "")[:500]}
                for row in self._repo.reports(session_id)]
            result["infos"] = [
                {key: row[key] for key in ("card_id", "pass_name", "object_name", "written_at",
                                           "last_error")}
                for row in self._repo.infos(session_id)]
        return result
