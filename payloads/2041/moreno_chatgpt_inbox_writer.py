#!/usr/bin/env python3
"""Durable Moreno ChatGPT inbox writer.

Reads a JSON object with an ``entries`` array (or a bare array) from stdin,
normalizes and atomically appends unique response events to the governed
ChatGPT inbox, and emits a machine-readable JSON result.

Canonical event identity is ``request_id:STATUS``.  The writer fails closed:
malformed input, malformed existing state, or missing required fields never
results in an inbox overwrite.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(os.environ.get(
    "MORENO_CHATGPT_INBOX_BASE",
    "/home/jose/moreno-ai/memory/operator",
)).resolve()
INBOX_PATH = Path(os.environ.get(
    "MORENO_CHATGPT_INBOX_PATH",
    str(BASE_DIR / "chatgpt_inbox.json"),
)).resolve()
LOG_PATH = Path(os.environ.get(
    "MORENO_CHATGPT_INBOX_LOG_PATH",
    str(BASE_DIR / "chatgpt_inbox.log"),
)).resolve()
LEDGER_PATH = Path(os.environ.get(
    "MORENO_CHATGPT_RELAY_LEDGER_PATH",
    str(BASE_DIR / "relay_request_ledger.json"),
)).resolve()
HEARTBEAT_PATH = Path(os.environ.get(
    "MORENO_CHATGPT_RELAY_HEARTBEAT_PATH",
    str(BASE_DIR / "relay_heartbeat.json"),
)).resolve()
FAILURES_PATH = Path(os.environ.get(
    "MORENO_CHATGPT_RELAY_FAILURES_PATH",
    str(BASE_DIR / "relay_failures.json"),
)).resolve()
LOCK_PATH = Path(os.environ.get(
    "MORENO_CHATGPT_INBOX_LOCK_PATH",
    str(BASE_DIR / ".chatgpt_inbox.lock"),
)).resolve()
MAX_ENTRIES = int(os.environ.get("MORENO_CHATGPT_INBOX_MAX_ENTRIES", "200"))


class WriterError(RuntimeError):
    """Expected durable-writer failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    data = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, data)


def load_json_strict(path: Path, *, expected_type: type, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WriterError(f"Malformed existing JSON at {path}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, expected_type):
        raise WriterError(
            f"Invalid existing JSON type at {path}: expected {expected_type.__name__}, "
            f"got {type(value).__name__}"
        )
    return value


def append_failure(record: dict[str, Any]) -> None:
    record = {"recorded_at": utc_now(), **record}
    try:
        existing = load_json_strict(FAILURES_PATH, expected_type=list, default=[])
        existing.append(record)
        atomic_write_json(FAILURES_PATH, existing[-500:])
    except Exception as exc:
        fallback = FAILURES_PATH.with_name(
            f"{FAILURES_PATH.stem}.recovery-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}{FAILURES_PATH.suffix}"
        )
        atomic_write_json(
            fallback,
            {"primary_failure": record, "ledger_error": f"{type(exc).__name__}: {exc}"},
        )


def normalize_status(value: Any) -> str:
    status = str(value or "").strip().upper()
    if not status:
        raise WriterError("Entry is missing status")
    if len(status) > 64 or not all(ch.isalnum() or ch in "._-" for ch in status):
        raise WriterError(f"Entry status is invalid: {status!r}")
    return status


def first_nonempty(entry: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = entry.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_entry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WriterError(f"Entry must be an object, got {type(raw).__name__}")

    message_id = first_nonempty(raw, ("message_id", "messageId", "gmail_message_id", "id"))
    request_id = first_nonempty(raw, ("request_id", "requestId"))
    status = normalize_status(raw.get("status"))

    if not message_id:
        raise WriterError("Entry is missing Gmail message ID")
    if not request_id:
        raise WriterError("Entry is missing request_id")
    if len(request_id) > 200:
        raise WriterError("Entry request_id is too long")

    event_key = f"{request_id}:{status}"
    normalized = {
        "id": event_key,
        "event_key": event_key,
        "message_id": message_id,
        "thread_id": first_nonempty(raw, ("thread_id", "threadId")),
        "subject": first_nonempty(raw, ("subject", "Subject")),
        "request_id": request_id,
        "status": status,
        "task_id": first_nonempty(raw, ("task_id", "taskId")),
        "participant": first_nonempty(raw, ("participant",)),
        "body": str(raw.get("body") or raw.get("cleanBody") or ""),
        "polled_at": first_nonempty(raw, ("polled_at", "polledAt")) or utc_now(),
    }
    return normalized


def existing_event_key(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        raise WriterError("Existing inbox contains a non-object entry")
    explicit = first_nonempty(entry, ("event_key",))
    if explicit:
        return explicit
    request_id = first_nonempty(entry, ("request_id", "requestId"))
    status = str(entry.get("status") or "").strip().upper()
    if request_id and status:
        return f"{request_id}:{status}"
    legacy_id = first_nonempty(entry, ("id",))
    return legacy_id or None


def read_incoming() -> list[Any]:
    try:
        incoming = json.load(sys.stdin)
    except Exception as exc:
        raise WriterError(f"Unable to parse stdin JSON: {type(exc).__name__}: {exc}") from exc

    if isinstance(incoming, dict):
        entries = incoming.get("entries")
        if entries is None:
            if any(key in incoming for key in ("request_id", "requestId", "status")):
                entries = [incoming]
            else:
                raise WriterError("Input object must contain an entries array")
    else:
        entries = incoming

    if not isinstance(entries, list):
        raise WriterError("Input entries must be an array")
    if not entries:
        raise WriterError("Input entries array is empty")
    if len(entries) > 100:
        raise WriterError("Input contains too many entries")
    return entries


def append_log(lines: list[str]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOG_PATH, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line.rstrip("\n") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def write_entries(entries_raw: list[Any]) -> dict[str, Any]:
    normalized = [normalize_entry(entry) for entry in entries_raw]

    seen_batch: set[str] = set()
    unique_batch: list[dict[str, Any]] = []
    duplicate_batch = 0
    for entry in normalized:
        key = entry["event_key"]
        if key in seen_batch:
            duplicate_batch += 1
            continue
        seen_batch.add(key)
        unique_batch.append(entry)

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        os.chmod(LOCK_PATH, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        existing = load_json_strict(INBOX_PATH, expected_type=list, default=[])
        ledger = load_json_strict(LEDGER_PATH, expected_type=dict, default={})

        existing_keys: set[str] = set()
        for item in existing:
            key = existing_event_key(item)
            if key:
                existing_keys.add(key)
        existing_keys.update(str(key) for key in ledger.keys())

        added: list[dict[str, Any]] = []
        duplicate_existing = 0
        now = utc_now()
        for entry in unique_batch:
            key = entry["event_key"]
            if key in existing_keys:
                duplicate_existing += 1
                continue
            existing_keys.add(key)
            added.append(entry)
            ledger[key] = {
                "first_seen": now,
                "message_id": entry["message_id"],
                "request_id": entry["request_id"],
                "status": entry["status"],
            }

        final_entries = (existing + added)[-MAX_ENTRIES:]
        atomic_write_json(INBOX_PATH, final_entries)
        atomic_write_json(LEDGER_PATH, ledger)
        heartbeat = {
            "active": True,
            "last_poll": now,
            "last_pickup": now if added else None,
            "received_count": len(normalized),
            "added_count": len(added),
            "duplicate_batch_count": duplicate_batch,
            "duplicate_existing_count": duplicate_existing,
            "inbox_count": len(final_entries),
            "inbox_sha256": sha256_file(INBOX_PATH),
        }
        atomic_write_json(HEARTBEAT_PATH, heartbeat)

        log_lines = [
            (
                f"{now} | received={len(normalized)} | added={len(added)} | "
                f"duplicate_batch={duplicate_batch} | duplicate_existing={duplicate_existing} | "
                f"total={len(final_entries)} | sha256={heartbeat['inbox_sha256']}"
            )
        ]
        for entry in added:
            log_lines.append(
                f"  {entry['event_key']} | {entry['message_id']} | {entry['subject']}"
            )
        append_log(log_lines)

        return {
            "ok": True,
            "received_count": len(normalized),
            "added_count": len(added),
            "duplicate_batch_count": duplicate_batch,
            "duplicate_existing_count": duplicate_existing,
            "inbox_count": len(final_entries),
            "inbox_path": str(INBOX_PATH),
            "inbox_sha256": heartbeat["inbox_sha256"],
            "event_keys_added": [entry["event_key"] for entry in added],
        }


def record_error_from_stdin() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except Exception as exc:
        raise WriterError(f"Unable to parse error-record stdin JSON: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict):
        raise WriterError("Error record must be a JSON object")
    category = first_nonempty(value, ("category", "error_category")) or "UNKNOWN"
    record = {
        "category": category,
        "request_id": first_nonempty(value, ("request_id", "requestId")),
        "status": first_nonempty(value, ("status",)),
        "message_id": first_nonempty(value, ("message_id", "messageId", "gmail_message_id", "id")),
        "workflow": first_nonempty(value, ("workflow",)) or "moreno-chatgpt-email-poller",
        "detail": str(value.get("detail") or value.get("error") or "")[:4000],
    }
    append_failure(record)
    return {"ok": True, "recorded": True, "category": category, "failures_path": str(FAILURES_PATH)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record-error", action="store_true")
    args = parser.parse_args()

    try:
        result = record_error_from_stdin() if args.record_error else write_entries(read_incoming())
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        error = {
            "category": "WRITER_ERROR",
            "error_type": type(exc).__name__,
            "detail": str(exc)[:4000],
        }
        append_failure(error)
        print(
            json.dumps(
                {"ok": False, "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
