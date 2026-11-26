"""Part 10 - Expert Decision Ledger and Virtual Persona Engine.

V10-EXP-001: The Expert records learning events in an append-only ledger.
AI learning cannot widen roles, grant release authority, or rewrite audit history.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .errors import PolicyError


@dataclass
class ExpertDecision:
    """One immutable decision ledger entry."""

    id: str
    category: str
    context: Dict[str, Any]
    decision: Dict[str, Any]
    confidence: float
    actor_id: str
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ExpertLedger:
    """Append-only ledger stored in Data/hub/self_annealing/expert_ledger.jsonl."""

    def __init__(self, workspace: Optional[Dict[str, Any]] = None):
        if workspace is None:
            self.workspace = config.load("workspace")
        else:
            self.workspace = workspace
        self.library_root = config.library_root(self.workspace)
        anneal_dir = self.library_root / self.workspace.get("selfAnnealing", {}).get("stateDirectory", "Data/hub/self_annealing")
        anneal_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_file = anneal_dir / "expert_ledger.jsonl"

    def record_decision(
        self,
        category: str,
        context: Dict[str, Any],
        decision: Dict[str, Any],
        confidence: float,
        actor_id: str = "expert-core",
    ) -> ExpertDecision:
        entry = ExpertDecision(
            id=f"exp-{uuid.uuid4().hex[:12]}",
            category=category,
            context=context,
            decision=decision,
            confidence=confidence,
            actor_id=actor_id,
        )
        with open(self.ledger_file, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry.to_dict(), separators=(",", ":")) + "\n")
        return entry

    def read_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.ledger_file.exists():
            return []
        lines = self.ledger_file.read_text(encoding="utf-8").splitlines()
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
        return list(reversed(entries))
