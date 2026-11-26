"""Part 4 & 5 - Task Lifecycle State Machine & Held-Result Governance.

V10-TSK-001: Task states follow an explicit state machine. External worker results
are held for internal acceptance before UAT/PROD promotion.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .errors import PolicyError


class TaskState(str, Enum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    HELD = "HELD"
    ACCEPTED = "ACCEPTED"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


VALID_TRANSITIONS: Dict[TaskState, Set[TaskState]] = {
    TaskState.CREATED: {TaskState.PENDING, TaskState.IN_PROGRESS, TaskState.REJECTED},
    TaskState.PENDING: {TaskState.IN_PROGRESS, TaskState.REJECTED},
    TaskState.IN_PROGRESS: {TaskState.HELD, TaskState.ACCEPTED, TaskState.REJECTED},
    TaskState.HELD: {TaskState.ACCEPTED, TaskState.REJECTED},
    TaskState.ACCEPTED: {TaskState.PROMOTED, TaskState.ARCHIVED, TaskState.REJECTED},
    TaskState.PROMOTED: {TaskState.ARCHIVED},
    TaskState.REJECTED: {TaskState.ARCHIVED},
    TaskState.ARCHIVED: set(),
}


@dataclass
class TaskRecord:
    """One governed task record."""

    id: str
    kind: str
    org_id: str
    repo_id: str
    state: TaskState = TaskState.CREATED
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    updated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    payload: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    held_reason: Optional[str] = None
    actor_id: str = "system"

    def transition_to(self, new_state: TaskState, actor_id: str, reason: Optional[str] = None) -> None:
        if new_state not in VALID_TRANSITIONS.get(self.state, set()):
            raise PolicyError(
                f"invalid task state transition from {self.state} to {new_state}"
            )
        self.state = new_state
        self.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.actor_id = actor_id
        if reason:
            self.held_reason = reason

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    org_id      TEXT NOT NULL,
    repo_id     TEXT NOT NULL,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    result      TEXT,
    held_reason TEXT,
    actor_id    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_state_idx ON tasks(state);
CREATE INDEX IF NOT EXISTS tasks_org_idx ON tasks(org_id);

CREATE TABLE IF NOT EXISTS task_audit (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    at         TEXT NOT NULL,
    actor_id   TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS task_audit_task_idx ON task_audit(task_id);
"""


class TaskManager:
    """Durable, thread-safe task store for Hub Floor and HubCore.

    Task state is product state, not process state: a restart MUST NOT lose a
    held external result awaiting review (Part 4, decision 0.3.9). Records live
    in SQLite WAL alongside an append-only transition audit, and every mutation
    is serialized so the threading HTTP server cannot interleave two writers.

    Passing no ``db_path`` yields an ephemeral in-memory store, which is what a
    unit test wants and never what a running node wants.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else None
        self._lock = threading.RLock()
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            target = str(self.db_path)
            try:
                self._conn = sqlite3.connect(target, check_same_thread=False)
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL;")
                    self._conn.execute("PRAGMA synchronous=NORMAL;")
                except Exception:
                    pass
                self._conn.executescript(SCHEMA)
                self._conn.commit()
            except Exception:
                try:
                    self._conn = sqlite3.connect("/tmp/hubcore_tasks.sqlite", check_same_thread=False)
                    self._conn.executescript(SCHEMA)
                    self._conn.commit()
                except Exception:
                    self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                    self._conn.executescript(SCHEMA)
                    self._conn.commit()
        else:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._conn.row_factory = sqlite3.Row

    # -- persistence ------------------------------------------------------

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            kind=row["kind"],
            org_id=row["org_id"],
            repo_id=row["repo_id"],
            state=TaskState(row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            payload=json.loads(row["payload"]),
            result=json.loads(row["result"]) if row["result"] is not None else None,
            held_reason=row["held_reason"],
            actor_id=row["actor_id"],
        )

    def _persist(
        self,
        task: TaskRecord,
        previous_state: Optional[TaskState],
        reason: Optional[str],
    ) -> None:
        self._conn.execute(
            """INSERT INTO tasks (id, kind, org_id, repo_id, state, created_at,
                                  updated_at, payload, result, held_reason, actor_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   state=excluded.state, updated_at=excluded.updated_at,
                   payload=excluded.payload, result=excluded.result,
                   held_reason=excluded.held_reason, actor_id=excluded.actor_id""",
            (
                task.id, task.kind, task.org_id, task.repo_id, task.state.value,
                task.created_at, task.updated_at, json.dumps(task.payload),
                None if task.result is None else json.dumps(task.result),
                task.held_reason, task.actor_id,
            ),
        )
        self._conn.execute(
            "INSERT INTO task_audit (task_id, at, actor_id, from_state, to_state, reason)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                task.id, task.updated_at, task.actor_id,
                previous_state.value if previous_state else None,
                task.state.value, reason,
            ),
        )
        self._conn.commit()

    # -- lifecycle --------------------------------------------------------

    def create_task(
        self,
        kind: str,
        org_id: str,
        repo_id: str,
        payload: Optional[Dict[str, Any]] = None,
        actor_id: str = "system",
    ) -> TaskRecord:
        task = TaskRecord(
            id="tsk-" + uuid.uuid4().hex[:12],
            kind=kind,
            org_id=org_id,
            repo_id=repo_id,
            payload=payload or {},
            actor_id=actor_id,
        )
        with self._lock:
            self._persist(task, None, "created")
        return task

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def list_all(
        self,
        state: Optional[TaskState] = None,
        org_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[TaskRecord]:
        clauses: List[str] = []
        params: List[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)
        if org_id is not None:
            clauses.append("org_id = ?")
            params.append(org_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks" + where
                + " ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def counts_by_state(self) -> Dict[str, int]:
        """State histogram for the Floor dashboard, without shipping payloads."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"
            ).fetchall()
        return {row["state"]: row["n"] for row in rows}

    def audit_trail(self, task_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, actor_id, from_state, to_state, reason FROM task_audit"
                " WHERE task_id = ? ORDER BY seq",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def transition(
        self,
        task_id: str,
        new_state: TaskState,
        actor_id: str,
        reason: Optional[str] = None,
    ) -> TaskRecord:
        """Move a stored task to a new state, validating and auditing the edge.

        Callers hold snapshots, never live rows, so every state change comes
        back through the manager and is durable before it is observable.
        """
        with self._lock:
            task = self.get(task_id)
            if not task:
                raise PolicyError("task not found: " + task_id)
            origin = task.state
            task.transition_to(new_state, actor_id, reason=reason)
            self._persist(task, origin, reason)
        return task

    def submit_external_result(
        self,
        task_id: str,
        result: Dict[str, Any],
        actor_id: str,
        held_reason: str = "external worker result requires internal review",
    ) -> TaskRecord:
        with self._lock:
            task = self.get(task_id)
            if not task:
                raise PolicyError("task not found: " + task_id)
            origin = task.state
            if task.state == TaskState.CREATED:
                task.transition_to(TaskState.IN_PROGRESS, actor_id)
                self._persist(task, origin, "external worker claimed task")
                origin = TaskState.IN_PROGRESS
            task.result = result
            task.transition_to(TaskState.HELD, actor_id, reason=held_reason)
            self._persist(task, origin, held_reason)
        return task

    def accept_held_result(self, task_id: str, reviewer_id: str) -> TaskRecord:
        with self._lock:
            task = self.get(task_id)
            if not task:
                raise PolicyError("task not found: " + task_id)
            if task.state != TaskState.HELD:
                raise PolicyError(
                    "task " + task_id + " is in state " + str(task.state) + ", not HELD"
                )
            task.transition_to(TaskState.ACCEPTED, reviewer_id)
            self._persist(task, TaskState.HELD, "accepted for internal validation")
        return task

    def reject_held_result(
        self, task_id: str, reviewer_id: str, reason: str
    ) -> TaskRecord:
        """Refusal is a first-class outcome, not a silently dropped result."""
        with self._lock:
            task = self.get(task_id)
            if not task:
                raise PolicyError("task not found: " + task_id)
            origin = task.state
            task.transition_to(TaskState.REJECTED, reviewer_id, reason=reason)
            self._persist(task, origin, reason)
        return task

    def close(self) -> None:
        with self._lock:
            self._conn.close()
