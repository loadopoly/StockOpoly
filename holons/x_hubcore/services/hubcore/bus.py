"""Part 4.1 - Durable Message Bus (Inbox / Outbox).

V10-BUS-001: Transport uses explicit at-least-once message delivery with atomic
file commits, idempotency hashing, and deduplication.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .errors import PolicyError


class DurableMessageBus:
    """Durable file message bus over Data/hub/inbox and Data/hub/outbox."""

    def __init__(self, workspace: Optional[Dict[str, Any]] = None):
        if workspace is None:
            self.workspace = config.load("workspace")
        else:
            self.workspace = workspace
        self.library_root = config.library_root(self.workspace)
        hub_data = self.library_root / self.workspace.get("library", {}).get("hubData", "Data/hub")
        self.inbox_dir = hub_data / "inbox"
        self.outbox_dir = hub_data / "outbox"
        self.deadletter_dir = hub_data / "quarantine"
        self.cursor_path = hub_data / "outbox_relay_cursor.json"

        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self.deadletter_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._seen_hashes: set = set()
        self._relay_cursors: Dict[str, int] = self._load_cursors()

    def _load_cursors(self) -> Dict[str, int]:
        if not self.cursor_path.exists():
            return {}
        try:
            doc = json.loads(self.cursor_path.read_text(encoding="utf-8") or "{}")
            return {str(k): int(v) for k, v in (doc.get("cursors") or {}).items()}
        except Exception:
            return {}

    def _save_cursors(self) -> None:
        tmp_path = self.cursor_path.parent / f".{self.cursor_path.name}.tmp"
        doc = {
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cursors": self._relay_cursors,
        }
        tmp_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp_path.replace(self.cursor_path)

    def get_relay_cursor(self, stream_id: str = "holonhub-live") -> int:
        with self._lock:
            return int(self._relay_cursors.get(str(stream_id), 0))

    def set_relay_cursor(self, cursor: int, stream_id: str = "holonhub-live") -> None:
        with self._lock:
            self._relay_cursors[str(stream_id)] = max(0, int(cursor))
            self._save_cursors()

    def _hash(self, payload: Dict[str, Any]) -> str:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def send(self, topic: str, payload: Dict[str, Any], sender_id: str) -> Dict[str, Any]:
        """Publish a message to the durable outbox atomically."""
        msg_hash = self._hash(payload)
        msg_id = f"msg-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"
        record = {
            "messageId": msg_id,
            "topic": topic,
            "senderId": sender_id,
            "contentHash": msg_hash,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "payload": payload,
        }

        tmp_path = self.outbox_dir / f".{msg_id}.tmp"
        final_path = self.outbox_dir / f"{msg_id}.json"

        tmp_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmp_path.replace(final_path)

        return record

    def receive_inbox(self) -> List[Dict[str, Any]]:
        """Read and deduplicate incoming messages from the inbox."""
        messages: List[Dict[str, Any]] = []
        for file in sorted(self.inbox_dir.glob("*.json")):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                chash = data.get("contentHash")
                if chash and chash in self._seen_hashes:
                    # Move duplicate to quarantine
                    file.replace(self.deadletter_dir / file.name)
                    continue
                if chash:
                    self._seen_hashes.add(chash)
                messages.append(data)
            except Exception:
                # Corrupted message -> quarantine
                file.replace(self.deadletter_dir / file.name)
        return messages

    def relay_outbox_events(
        self,
        events: List[Dict[str, Any]],
        *,
        sender_id: str = "holonhub-live",
        stream_id: str = "holonhub-live",
    ) -> Dict[str, Any]:
        """Deduplicate and relay ordered outbox events, advancing a durable cursor."""
        with self._lock:
            prev_cursor = self.get_relay_cursor(stream_id)
            new_cursor = prev_cursor
            relayed = 0
            deduped = 0
            quarantined = 0

            ordered = sorted(
                events or [],
                key=lambda item: int(item.get("seq", 0)) if isinstance(item, dict) and str(item.get("seq", "")).isdigit() else 0,
            )
            for ev in ordered:
                if not isinstance(ev, dict) or not ev.get("eventId"):
                    q_id = f"bad-outbox-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}.json"
                    (self.deadletter_dir / q_id).write_text(json.dumps(ev, default=str, indent=2), encoding="utf-8")
                    quarantined += 1
                    continue

                seq_val = int(ev.get("seq") or 0)
                if seq_val and seq_val <= prev_cursor:
                    deduped += 1
                    continue

                chash = self._hash(ev)
                if chash in self._seen_hashes:
                    deduped += 1
                    if seq_val > new_cursor:
                        new_cursor = seq_val
                    continue

                self._seen_hashes.add(chash)
                self.send(
                    topic=str(ev.get("eventType") or "osq.event"),
                    payload=ev,
                    sender_id=sender_id,
                )
                relayed += 1
                if seq_val > new_cursor:
                    new_cursor = seq_val

            if new_cursor > prev_cursor:
                self.set_relay_cursor(new_cursor, stream_id=stream_id)

            return {
                "ok": True,
                "streamId": stream_id,
                "previousCursor": prev_cursor,
                "newCursor": new_cursor,
                "relayed": relayed,
                "deduped": deduped,
                "quarantined": quarantined,
            }

    def pull_from_owner(
        self,
        outbox_url: str,
        *,
        stream_id: str = "holonhub-live",
        timeout: float = 3.0,
    ) -> Dict[str, Any]:
        """Pull committed events since the durable cursor from an owner Holon outbox."""
        cursor = self.get_relay_cursor(stream_id)
        sep = "&" if "?" in outbox_url else "?"
        url = f"{outbox_url.rstrip('/')}{sep}sinceCursor={cursor}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "HubCore/10.1 outbox-relay"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8") or "{}")
        events = list(doc.get("events") or doc.get("outboxEventsSinceCursor") or [])
        res = self.relay_outbox_events(events, sender_id=stream_id, stream_id=stream_id)
        res["pulledFrom"] = url
        return res

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "inboxCount": len(list(self.inbox_dir.glob("*.json"))),
                "outboxCount": len(list(self.outbox_dir.glob("*.json"))),
                "quarantineCount": len(list(self.deadletter_dir.glob("*.json"))),
                "relayCursors": dict(self._relay_cursors),
            }
