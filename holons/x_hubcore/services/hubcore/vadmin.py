"""Part 10.4 - v-ADMIN, the disclosed virtual Admin.

v-ADMIN is {{virtualPersona}} bound to the Admin-visibility path of the advisor
pipeline. It prepares the parallel Admin record that grades 4 and 5 always
produce (Part 9.2), keeps a queue of what is waiting on a human, and speaks with
a standing synthetic-assistant disclosure.

What it is **not** is the Admin. Part 0.3.10 and Part 10.2 are categorical: AI
cannot acquire authority. So this module is written so that the interesting
methods are the refusals:

* :meth:`assert_is_human` rejects the synthetic actor wherever a human decision
  is required -- Admin approval, closure, promotion, acceptance of a held result.
* :meth:`assert_cannot_widen` rejects any proposal that would add a task kind or
  tool, widen a role, path, network, or autonomy setting, or approve its own
  evaluation.
* The kill switch is global and fails safe: with it engaged, v-ADMIN stops
  producing synopses and everything pending falls back to the human queue,
  rather than quietly continuing.

A synopsis is a *summary for a human to read*, never a decision. Nothing here
changes a grade, a gate, or a state.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .errors import PolicyError

#: Keys that describe authority. A synthetic proposal touching any of these is
#: refused outright rather than reviewed, because reviewing it would already
#: treat widening authority as a negotiable outcome (Part 10.2).
AUTHORITY_KEYS = frozenset(
    {
        "role", "roles", "grant", "grants", "permission", "permissions",
        "capability", "capabilities", "taskkind", "taskkinds", "tool", "tools",
        "autonomy", "scope", "scopes", "path", "paths", "network", "allowlist",
        "releasegate", "promote", "promotion", "policy", "gate", "gates",
    }
)

#: Stages a synthetic actor may never perform.
HUMAN_ONLY_STAGES = frozenset(
    {"admin-decision", "reviewer-decision", "closed", "promotion", "held-acceptance"}
)


#: Stems matched anywhere inside a normalized key. ``addTaskKind`` and
#: ``newPermissions`` describe authority just as plainly as ``taskKind`` does,
#: so an exact-match list alone would let a rename walk straight through.
AUTHORITY_STEMS = (
    "taskkind", "permission", "autonomy", "releasegate", "allowlist",
    "capabilit", "authoriz", "privileg", "grant", "rolelist",
)


def _normalize(key: str) -> str:
    return str(key).replace("-", "").replace("_", "").lower()


def _describes_authority(key: str) -> bool:
    normalized = _normalize(key)
    if normalized in AUTHORITY_KEYS:
        return True
    return any(stem in normalized for stem in AUTHORITY_STEMS)


@dataclass
class QueueEntry:
    """One item v-ADMIN has prepared and a human still owes a decision on."""

    id: str
    request_id: str
    grade: int
    summary: str
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    prepared_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    prepared_by: str = "vadmin"
    resolved: bool = False
    resolved_by: Optional[str] = None
    resolution: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "requestId": self.request_id,
            "grade": self.grade,
            "summary": self.summary,
            "evidence": self.evidence,
            "preparedAt": self.prepared_at,
            "preparedBy": self.prepared_by,
            "resolved": self.resolved,
            "resolvedBy": self.resolved_by,
            "resolution": self.resolution,
        }


class VirtualAdmin:
    """The disclosed synthetic assistant acting on the Admin-visibility path."""

    def __init__(
        self,
        persona: str = "Quipu",
        ask_persona: str = "Ask Advisor",
        expert_ledger: Any = None,
        enabled: bool = True,
    ):
        self.persona = persona
        self.ask_persona = ask_persona
        self.expert_ledger = expert_ledger
        self.actor_id = "vadmin:" + persona.lower().replace(" ", "-")
        self._lock = threading.RLock()
        self._queue: Dict[str, QueueEntry] = {}
        self._kill_switch_engaged = not enabled
        self._kill_switch_reason = "" if enabled else "disabled at startup"

    # -- disclosure --------------------------------------------------------

    @property
    def disclosure(self) -> str:
        """Part 10.4: every chat, card, call, and meeting opens with this."""
        return (
            self.persona + " is a synthetic assistant working alongside "
            + self.ask_persona + ". It is not the human administrator and does not "
            "make decisions on their behalf."
        )

    def is_synthetic(self, actor_id: str) -> bool:
        actor = str(actor_id or "").strip().lower()
        return actor == self.actor_id or actor.startswith("vadmin:") or actor == self.persona.lower()

    def assert_is_human(self, actor_id: str) -> None:
        """Refuse a synthetic actor where a human decision is required."""
        if self.is_synthetic(actor_id):
            raise PolicyError(
                self.persona + " is a disclosed synthetic assistant and cannot supply "
                "a human decision; this gate requires a human Admin (Part 0.3.10)"
            )
        if not str(actor_id or "").strip():
            raise PolicyError("a human decision requires an identified actor")

    # -- authority boundary ------------------------------------------------

    def assert_cannot_widen(self, proposal: Mapping[str, Any]) -> None:
        """Refuse any synthetic proposal that would widen authority."""
        for key in self._walk_keys(proposal):
            if _describes_authority(key):
                raise PolicyError(
                    self.persona + " may propose language, routing, and retrieval "
                    "improvements only; the field " + repr(key) + " describes authority "
                    "and cannot be changed by the Expert or the synthetic assistant "
                    "(Part 10.2)"
                )

    @staticmethod
    def _walk_keys(node: Any) -> List[str]:
        found: List[str] = []
        if isinstance(node, Mapping):
            for key, value in node.items():
                found.append(str(key))
                found.extend(VirtualAdmin._walk_keys(value))
        elif isinstance(node, (list, tuple)):
            for value in node:
                found.extend(VirtualAdmin._walk_keys(value))
        return found

    def assert_stage_permitted(self, stage: str, actor_id: str) -> None:
        if stage in HUMAN_ONLY_STAGES:
            self.assert_is_human(actor_id)

    # -- kill switch -------------------------------------------------------

    @property
    def engaged(self) -> bool:
        with self._lock:
            return self._kill_switch_engaged

    def set_kill_switch(self, engaged: bool, actor_id: str, reason: str = "") -> Dict[str, Any]:
        """Engage or release the global kill switch (Part 10.4).

        Engaging it does not cancel pending work: it hands that work to the
        human queue, which is where it would have gone anyway had the synthetic
        assistant never been enabled.
        """
        self.assert_is_human(actor_id)
        with self._lock:
            self._kill_switch_engaged = bool(engaged)
            self._kill_switch_reason = reason
            fallback = [e.request_id for e in self._queue.values() if not e.resolved]
        self._record("kill-switch", {"engaged": engaged, "reason": reason, "actor": actor_id})
        return {
            "engaged": bool(engaged),
            "reason": reason,
            "actor": actor_id,
            "fellBackToHumanQueue": fallback if engaged else [],
        }

    # -- the Admin-visibility path ----------------------------------------

    def prepare_synopsis(
        self,
        request_id: str,
        grade: int,
        body: str,
        citations: List[Dict[str, Any]],
        assessment: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Prepare the parallel Admin record grades 4 and 5 always produce.

        The synopsis summarizes; it decides nothing. With the kill switch
        engaged it is not produced at all, and the request is simply left for
        the human queue.
        """
        if self.engaged:
            return {
                "prepared": False,
                "reason": "kill switch engaged: " + (self._kill_switch_reason or "no reason given"),
                "fallback": "human queue",
                "disclosure": self.disclosure,
            }

        sources = sorted({c.get("source", "") for c in citations if c.get("source")})
        summary = (
            "Grade " + str(grade) + " request from the Hub. Subject line: "
            + " ".join(body.split())[:180]
            + (" …" if len(body) > 180 else "")
            + ". Grounding: "
            + (", ".join(sources) if sources else "no governed source matched")
            + ". This synopsis is informational; the decision remains with the Admin."
        )

        entry = QueueEntry(
            id="vadm-" + uuid.uuid4().hex[:12],
            request_id=request_id,
            grade=int(grade),
            summary=summary,
            evidence=[
                {
                    "source": c.get("source"),
                    "section": c.get("section"),
                    "confidence": c.get("confidence"),
                }
                for c in citations
            ],
            prepared_by=self.actor_id,
        )
        with self._lock:
            self._queue[entry.id] = entry

        payload = entry.to_dict()
        payload["prepared"] = True
        payload["disclosure"] = self.disclosure
        payload["assessment"] = dict(assessment or {})
        payload["decisionAuthority"] = "human-admin"
        self._record("synopsis", payload)
        return payload

    def queue(self, include_resolved: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            entries = list(self._queue.values())
        if not include_resolved:
            entries = [e for e in entries if not e.resolved]
        entries.sort(key=lambda e: (-e.grade, e.prepared_at))
        return [e.to_dict() for e in entries]

    def resolve(self, entry_id: str, actor_id: str, resolution: str) -> Dict[str, Any]:
        """Mark a queue entry decided. Only a human may do this."""
        self.assert_is_human(actor_id)
        if not resolution.strip():
            raise PolicyError("resolving an Admin queue entry requires a resolution")
        with self._lock:
            entry = self._queue.get(entry_id)
            if entry is None:
                raise PolicyError("no Admin queue entry " + repr(entry_id))
            entry.resolved = True
            entry.resolved_by = actor_id
            entry.resolution = resolution
            payload = entry.to_dict()
        self._record("queue-resolved", payload)
        return payload

    def propose_learning(self, proposal: Mapping[str, Any], actor_id: str) -> Dict[str, Any]:
        """Accept a bounded learning proposal, or refuse it.

        Accepted proposals are *staged*, never applied: Part 10.2 requires every
        candidate artifact to be versioned, evaluated against a holdout, reviewed,
        and explicitly promoted by someone other than its author.
        """
        self.assert_cannot_widen(proposal)
        staged = {
            "proposalId": "vadm-prop-" + uuid.uuid4().hex[:12],
            "proposedBy": self.actor_id if self.is_synthetic(actor_id) else actor_id,
            "status": "staged-for-evaluation",
            "appliedOnline": False,
            "requiresIndependentApproval": True,
            "proposal": dict(proposal),
            "stagedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._record("learning-proposal", staged)
        return staged

    def status(self) -> Dict[str, Any]:
        with self._lock:
            pending = [e for e in self._queue.values() if not e.resolved]
            return {
                "persona": self.persona,
                "actorId": self.actor_id,
                "disclosure": self.disclosure,
                "killSwitchEngaged": self._kill_switch_engaged,
                "killSwitchReason": self._kill_switch_reason,
                "pending": len(pending),
                "pendingByGrade": {
                    str(g): sum(1 for e in pending if e.grade == g) for g in sorted({e.grade for e in pending})
                },
                "resolved": sum(1 for e in self._queue.values() if e.resolved),
                "authorityBoundary": {
                    "mayDecide": False,
                    "mayClose": False,
                    "mayPromote": False,
                    "mayWidenAuthority": False,
                    "mayProposeLanguageAndRouting": True,
                },
            }

    # -- ledger ------------------------------------------------------------

    def _record(self, stage: str, payload: Dict[str, Any]) -> None:
        if self.expert_ledger is None:
            return
        try:
            self.expert_ledger.record_decision(
                category="vadmin." + stage,
                context={"persona": self.persona, "requestId": payload.get("requestId")},
                decision=json.loads(json.dumps(payload, default=str)),
                confidence=1.0,
                actor_id=self.actor_id,
            )
        except Exception:
            pass
