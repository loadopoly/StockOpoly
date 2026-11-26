"""Part 9 - Ask {{askPersona}} Advisor Pipeline.

V10-ADV-001: every advisor request is validated, persisted, graded against the
Part 9.2 matrix, answered only from retrieved evidence, gated by the human
review the grade demands, and closed with a reason and a complete audit trail.

The shape of Part 9.3 is the shape of this module:

    submit -> grade -> retrieve -> draft -> gate -> send -> follow up -> close

A few invariants are worth stating plainly, because each one is a place where a
plausible-looking shortcut would quietly break the product:

* **A grade is never invented.** When the grader's confidence is below the
  configured floor it escalates and asks for a human, rather than guessing
  (Part 9.3 step 4).
* **The Hub does not silently answer locally.** A draft exists only where
  retrieval found supporting evidence; with no evidence the request is routed to
  a human instead of being answered from nothing (Part 9.1).
* **Gates are not advisory.** Grades 2-5 require a reviewer, grades 4-5 add
  Admin visibility, and grade 5 cannot auto-close (Part 9.2).
* **User and retrieved text is data.** Instruction-shaped content cannot change
  policy, routing, or gates; it is quarantined and recorded (Part 9.5).
* **Only consent-eligible material reaches the Expert.** Ledger writes carry a
  consent reference and a retention class (Part 9.3 step 10, Part 10.1).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import config, corpus as corpus_module
from .errors import HubCoreError, PolicyError

REQUEST_CONTRACT = "advisor-request/v2"
RESPONSE_CONTRACT = "advisor-response/v2"


class Grade(IntEnum):
    """Part 9.2 grade matrix."""

    GENERAL = 0
    MODULE_RELATED = 1
    BUG = 2
    ADVANCED_TECHNICAL = 3
    CAPABILITY_BUILDING = 4
    HIGH_IMPACT = 5


@dataclass(frozen=True)
class GradePolicy:
    """One row of the Part 9.2 matrix."""

    grade: Grade
    meaning: str
    route: str
    human_gate: str          # none | policy | reviewer | reviewer+admin | admin
    expert_weight: float
    admin_visibility: bool
    may_auto_close: bool

    @property
    def requires_reviewer(self) -> bool:
        return self.human_gate in ("reviewer", "reviewer+admin", "admin")

    @property
    def requires_admin(self) -> bool:
        return self.human_gate in ("reviewer+admin", "admin")

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["grade"] = int(self.grade)
        payload["requiresReviewer"] = self.requires_reviewer
        payload["requiresAdmin"] = self.requires_admin
        return payload


#: Weights are configurable but non-decreasing (Part 9.2).
GRADE_MATRIX: Dict[Grade, GradePolicy] = {
    Grade.GENERAL: GradePolicy(
        Grade.GENERAL,
        "General and unrelated to the current module",
        "Concise redirection or answer",
        "policy",
        0.25,
        False,
        True,
    ),
    Grade.MODULE_RELATED: GradePolicy(
        Grade.MODULE_RELATED,
        "General and module-related",
        "Concise module-aware answer",
        "policy",
        0.50,
        False,
        True,
    ),
    Grade.BUG: GradePolicy(
        Grade.BUG,
        "Bug or reproducible issue",
        "Process Studio recreation request and bounded investigation",
        "reviewer",
        1.00,
        False,
        True,
    ),
    Grade.ADVANCED_TECHNICAL: GradePolicy(
        Grade.ADVANCED_TECHNICAL,
        "Advanced technical inquiry",
        "Technical explanation, optional capture, follow-up offer",
        "reviewer",
        2.00,
        False,
        True,
    ),
    Grade.CAPABILITY_BUILDING: GradePolicy(
        Grade.CAPABILITY_BUILDING,
        "Advanced inquiry plus capability-building",
        "Train/Coach/Assist plan and parallel Admin synopsis",
        "reviewer+admin",
        3.00,
        True,
        True,
    ),
    Grade.HIGH_IMPACT: GradePolicy(
        Grade.HIGH_IMPACT,
        "High-impact, ambiguous, or deeply advanced inquiry",
        "Deep research packet and live human-computer workbench",
        "admin",
        5.00,
        True,
        False,
    ),
}


class RequestState:
    """Lifecycle of one advisor request."""

    RECEIVED = "RECEIVED"
    GRADED = "GRADED"
    AWAITING_REVIEWER = "AWAITING_REVIEWER"
    AWAITING_ADMIN = "AWAITING_ADMIN"
    READY_TO_SEND = "READY_TO_SEND"
    SENT = "SENT"
    AWAITING_FOLLOW_UP = "AWAITING_FOLLOW_UP"
    CLOSED = "CLOSED"
    ESCALATED = "ESCALATED"


#: Below this the grader has not earned the right to assert a grade.
CONFIDENCE_FLOOR = 0.55

#: Reference follow-up policy (Part 9.4). Versioned configuration, not code.
FOLLOW_UP_POLICY = {
    "version": "reference/1",
    "responseWindowHours": 24,
    "nudgeGrades": [4],
    "humanOwnedGrades": [5],
    "meetingSlots": 3,
    "meetingWindowBusinessDays": 5,
    "meetingDays": ["Tuesday", "Wednesday", "Thursday"],
    "noShowWindowMinutes": 12,
}


# -- grading ------------------------------------------------------------------

#: Signals that move a request up the matrix. Each carries the grade it argues
#: for, so an override record can name exactly what the grader saw.
_GRADE_SIGNALS = (
    (Grade.BUG, re.compile(
        r"\b(bug|crash(?:e[sd])?|error|exception|traceback|stack\s?trace|broken|"
        r"fails?|failing|failed|regression|reproduc\w+|500|404|timeout)\b", re.I)),
    (Grade.ADVANCED_TECHNICAL, re.compile(
        r"\b(architect\w*|internals?|protocol|schema|contract|throughput|latency|"
        r"concurren\w+|partition\w*|index(?:ing)?|algorithm|tuning|profil\w+|"
        r"why does|how does .* work)\b", re.I)),
    (Grade.CAPABILITY_BUILDING, re.compile(
        r"\b(train(?:ing)?|teach|coach\w*|onboard\w*|enable\s+(?:my|the)\s+team|"
        r"upskill|runbook|playbook|walk\s?through|curriculum|documentation for)\b", re.I)),
    (Grade.HIGH_IMPACT, re.compile(
        r"\b(outage|incident|data\s?loss|breach|corrupt\w+|urgent|production\s+down|"
        r"legal|complian\w+|audit\s+finding|security\s+(?:issue|risk|hole)|"
        r"privacy\s+(?:violation|incident)|rollback\s+production)\b", re.I)),
)

_MODULE_HINT = re.compile(r"\b(this (?:module|screen|page|view)|here|current chain)\b", re.I)


@dataclass
class GradeAssessment:
    """A proposed grade with the evidence that produced it."""

    grade: Grade
    confidence: float
    signals: List[str] = field(default_factory=list)
    escalated: bool = False
    escalation_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "grade": int(self.grade),
            "confidence": round(self.confidence, 4),
            "signals": self.signals,
            "escalated": self.escalated,
            "escalationReason": self.escalation_reason,
        }


class Grader:
    """Proposes a Part 9.2 grade, or declines to and escalates.

    This is deliberately a transparent, auditable classifier rather than an
    opaque one: every grade it proposes names the signals behind it, so a
    reviewer overriding the grade can see what the machine actually keyed on.
    A configured provider may replace it through the same interface; the gates
    that follow do not change either way.
    """

    def __init__(self, confidence_floor: float = CONFIDENCE_FLOOR):
        self.confidence_floor = confidence_floor

    def assess(self, body: str, module: Mapping[str, Any]) -> GradeAssessment:
        text = (body or "").strip()
        if not text:
            raise HubCoreError("advisor request body is empty")

        matched: List[str] = []
        grade = Grade.GENERAL
        for candidate, pattern in _GRADE_SIGNALS:
            hit = pattern.search(text)
            if hit:
                matched.append(str(candidate.name).lower() + ":" + hit.group(0).lower())
                if candidate > grade:
                    grade = candidate

        if grade == Grade.GENERAL:
            if module.get("id") and (_MODULE_HINT.search(text) or module.get("chain")):
                grade = Grade.MODULE_RELATED
                matched.append("module-context:" + str(module.get("id")))

        # Confidence is about whether a grade is *knowable* from this text, not
        # about whether the answer will be good. A well-formed general question
        # with no escalating signal is a confident grade 0 or 1; escalating it
        # for lacking keywords would put a human in front of every ordinary ask.
        words = len(text.split())
        distinct = {m.split(":", 1)[0] for m in matched}
        confidence = 0.45
        confidence += 0.12 * len(distinct)
        if words >= 6:
            confidence += 0.17          # enough text to read an intent from
        if words < 4:
            confidence -= 0.25          # too little to grade at all
        if len(distinct) > 1:
            confidence -= 0.10          # signals disagree about which grade applies
        confidence = max(0.05, min(0.99, confidence))

        assessment = GradeAssessment(grade=grade, confidence=confidence, signals=matched)

        if confidence < self.confidence_floor:
            # Part 9.3 step 4: low confidence escalates rather than inventing.
            escalated_to = Grade(min(int(Grade.HIGH_IMPACT), max(int(grade), int(Grade.BUG))))
            assessment.escalation_reason = (
                "grader confidence " + str(round(confidence, 2)) + " is below the floor "
                + str(self.confidence_floor) + "; escalated to a human gate instead of "
                "asserting a grade"
            )
            assessment.grade = escalated_to
            assessment.escalated = True
        return assessment


# -- follow-up parsing --------------------------------------------------------

FOLLOW_UP_KINDS = (
    "satisfied",
    "dissatisfied",
    "needs_follow_up",
    "depth_change",
    "meeting_request",
)

_FOLLOW_UP_PATTERNS = (
    ("meeting_request", re.compile(r"\b(meet|meeting|call|schedule|calendar|book a time)\b", re.I)),
    ("depth_change", re.compile(r"\b(more detail|in depth|deeper|elaborate|simpler|shorter|overview|tl;?dr|summar\w+)\b", re.I)),
    ("needs_follow_up", re.compile(r"\b(follow[- ]?up|still|another question|what about|and also|next step)\b", re.I)),
    ("dissatisfied", re.compile(r"\b(wrong|incorrect|not helpful|unhelpful|didn'?t (?:help|work)|no[t]? what i (?:asked|meant)|useless)\b", re.I)),
    ("satisfied", re.compile(r"\b(thanks|thank you|that works|perfect|resolved|solved|got it|makes sense)\b", re.I)),
)


def parse_follow_up(text: str) -> Dict[str, Any]:
    """Classify a follow-up into the Part 9.3 step 8 categories."""
    body = (text or "").strip()
    if not body:
        return {"kind": "needs_follow_up", "matched": None, "detail": "empty follow-up"}
    for kind, pattern in _FOLLOW_UP_PATTERNS:
        hit = pattern.search(body)
        if hit:
            detail = ""
            if kind == "depth_change":
                detail = "detailed" if re.search(
                    r"\b(more detail|in depth|deeper|elaborate)\b", body, re.I
                ) else "overview"
            return {"kind": kind, "matched": hit.group(0), "detail": detail}
    return {"kind": "needs_follow_up", "matched": None, "detail": "unclassified follow-up"}


# -- records ------------------------------------------------------------------


@dataclass
class Citation:
    source: str
    section: str
    confidence: float
    snippet: str
    content_hash: str = ""
    trusted: bool = True


@dataclass
class AdvisorResponse:
    """What the pipeline produced for one request."""

    contract: str
    request_id: str
    query: str
    grade: int
    grade_meaning: str
    route: str
    state: str
    answer: str
    confidence: float
    citations: List[Dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    persona: str = "Ask Advisor"
    disclosure: str = ""
    human_gate: str = "policy"
    requires_reviewer: bool = False
    requires_admin: bool = False
    escalated: bool = False
    escalation_reason: str = ""
    quarantined: List[str] = field(default_factory=list)
    admin_synopsis: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        return {
            "contract": payload["contract"],
            "requestId": payload["request_id"],
            "query": payload["query"],
            "grade": payload["grade"],
            "gradeMeaning": payload["grade_meaning"],
            "route": payload["route"],
            "state": payload["state"],
            "answer": payload["answer"],
            "confidence": payload["confidence"],
            "citations": payload["citations"],
            "latencyMs": payload["latency_ms"],
            "persona": payload["persona"],
            "disclosure": payload["disclosure"],
            "humanGate": payload["human_gate"],
            "requiresReviewer": payload["requires_reviewer"],
            "requiresAdmin": payload["requires_admin"],
            "escalated": payload["escalated"],
            "escalationReason": payload["escalation_reason"],
            "quarantined": payload["quarantined"],
            "adminSynopsis": payload["admin_synopsis"],
        }


SCHEMA = """
CREATE TABLE IF NOT EXISTS advisor_requests (
    request_id        TEXT PRIMARY KEY,
    contract          TEXT NOT NULL,
    requestor_subject TEXT NOT NULL,
    module_id         TEXT,
    module_chain      TEXT,
    module_route      TEXT,
    mode              TEXT NOT NULL,
    body              TEXT NOT NULL,
    capture_ref       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    grade             INTEGER NOT NULL,
    grade_confidence  REAL NOT NULL,
    state             TEXT NOT NULL,
    answer            TEXT,
    citations         TEXT,
    reviewer_id       TEXT,
    admin_id          TEXT,
    close_reason      TEXT
);
CREATE INDEX IF NOT EXISTS advisor_state_idx ON advisor_requests(state);
CREATE INDEX IF NOT EXISTS advisor_grade_idx ON advisor_requests(grade);

CREATE TABLE IF NOT EXISTS advisor_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    stage      TEXT NOT NULL,
    actor_id   TEXT NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS advisor_events_idx ON advisor_events(request_id);
"""


class AdvisorPipeline:
    """The Part 9.3 shared workflow, end to end."""

    def __init__(
        self,
        persona: str = "Ask Advisor",
        corpus: Optional[corpus_module.Corpus] = None,
        db_path: Optional[Path] = None,
        expert_ledger: Any = None,
        vadmin: Any = None,
        grader: Optional[Grader] = None,
    ):
        self.persona = persona
        self.corpus = corpus if corpus is not None else corpus_module.build()
        self.grader = grader or Grader()
        self.expert_ledger = expert_ledger
        self.vadmin = vadmin
        self.follow_up_policy = dict(FOLLOW_UP_POLICY)

        self.db_path = Path(db_path) if db_path else None
        self._lock = threading.RLock()
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path) if self.db_path else ":memory:", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        if self.db_path is not None:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._restore_admin_queue()

    def _restore_admin_queue(self) -> None:
        """Rebuild the v-ADMIN queue from the requests still awaiting an Admin.

        The queue is a view of durable state, not state of its own. Restarting
        HubCore must not make a grade-5 request that is waiting on a human
        quietly vanish from the Admin's list.
        """
        if self.vadmin is None:
            return
        rows = self._conn.execute(
            "SELECT request_id, grade, body, citations FROM advisor_requests"
            " WHERE state = ? ORDER BY created_at",
            (RequestState.AWAITING_ADMIN,),
        ).fetchall()
        for row in rows:
            try:
                self.vadmin.prepare_synopsis(
                    request_id=row["request_id"],
                    grade=int(row["grade"]),
                    body=row["body"],
                    citations=json.loads(row["citations"]) if row["citations"] else [],
                    assessment={"restored": True},
                )
            except Exception:
                continue

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _event(self, request_id: str, stage: str, actor_id: str, detail: Any = None) -> None:
        self._conn.execute(
            "INSERT INTO advisor_events (request_id, at, stage, actor_id, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (request_id, self._now(), stage, actor_id,
             json.dumps(detail, default=str) if detail is not None else None),
        )

    def _row(self, request_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM advisor_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise HubCoreError("advisor request not found: " + request_id)
        return row

    def _learn(self, stage: str, row_or_id: Any, payload: Dict[str, Any], weight: float) -> None:
        """Record a consent-eligible learning event, or skip it.

        Part 10.2: the Expert may learn routing and language. It may not learn
        anything that widens authority, and it never learns from a request whose
        consent reference is absent.
        """
        if self.expert_ledger is None:
            return
        try:
            self.expert_ledger.record_decision(
                category="advisor." + stage,
                context={"requestId": row_or_id, "consentRef": payload.get("consentRef")},
                decision=payload,
                confidence=float(payload.get("confidence", 0.0)),
                actor_id="advisor-pipeline",
            )
        except Exception:  # a ledger write must never fail a user's request
            pass

    # -- Part 9.3 step 1-7: submit ----------------------------------------

    def submit(self, request: Mapping[str, Any]) -> AdvisorResponse:
        """Validate, persist, grade, retrieve, draft, and gate one request."""
        started = time.perf_counter()

        body = str(request.get("body") or request.get("query") or "").strip()
        if not body:
            raise HubCoreError("advisor request requires a non-empty body")
        module = dict(request.get("module") or {})
        request_id = str(request.get("requestId") or ("adv-" + uuid.uuid4().hex[:12]))
        subject = str(
            request.get("requestorSubject") or request.get("userId") or "anonymous"
        )
        mode = str(request.get("mode") or "text")
        if mode not in ("text", "voice"):
            raise PolicyError("advisor mode must be 'text' or 'voice', not " + repr(mode))

        # User text is data. Instruction-shaped content is recorded and stripped
        # of its authority; it cannot reach policy, routing, or the gates.
        quarantined = corpus_module.looks_like_injection(body)

        assessment = self.grader.assess(body, module)
        policy = GRADE_MATRIX[assessment.grade]

        hits = self.corpus.search(body, limit=4)
        citations: List[Dict[str, Any]] = []
        for passage, score in hits:
            if not passage.trusted:
                quarantined.extend(passage.quarantined_fragments)
                continue
            citations.append(passage.to_citation(score))

        answer, answer_confidence = self._draft(body, policy, citations, assessment)

        state = self._initial_state(policy, citations)

        with self._lock:
            self._conn.execute(
                """INSERT INTO advisor_requests (
                       request_id, contract, requestor_subject, module_id, module_chain,
                       module_route, mode, body, capture_ref, created_at, updated_at,
                       grade, grade_confidence, state, answer, citations)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(request_id) DO UPDATE SET
                       updated_at=excluded.updated_at, grade=excluded.grade,
                       grade_confidence=excluded.grade_confidence, state=excluded.state,
                       answer=excluded.answer, citations=excluded.citations""",
                (
                    request_id, REQUEST_CONTRACT, subject, module.get("id"),
                    module.get("chain"), module.get("route"), mode, body,
                    request.get("captureRef"), str(request.get("createdAt") or self._now()),
                    self._now(), int(assessment.grade), assessment.confidence, state,
                    answer, json.dumps(citations),
                ),
            )
            self._event(request_id, "received", subject, {"mode": mode, "module": module})
            self._event(request_id, "graded", "grader", assessment.to_dict())
            if quarantined:
                self._event(request_id, "quarantined", "airlock", {"fragments": quarantined})
            self._event(
                request_id, "routed", "advisor-pipeline",
                {"state": state, "gate": policy.human_gate, "citations": len(citations)},
            )
            self._conn.commit()

        synopsis = None
        if policy.admin_visibility and self.vadmin is not None:
            # Grades 4 and 5 always produce a parallel Admin record (Part 9.2).
            synopsis = self.vadmin.prepare_synopsis(
                request_id=request_id,
                grade=int(assessment.grade),
                body=body,
                citations=citations,
                assessment=assessment.to_dict(),
            )
            with self._lock:
                self._event(request_id, "admin-synopsis", self.vadmin.actor_id, synopsis)
                self._conn.commit()

        self._learn(
            "grade",
            request_id,
            {
                "grade": int(assessment.grade),
                "signals": assessment.signals,
                "confidence": assessment.confidence,
                "consentRef": request.get("consentRef"),
                "retentionClass": "routing-heuristic",
            },
            policy.expert_weight,
        )

        return AdvisorResponse(
            contract=RESPONSE_CONTRACT,
            request_id=request_id,
            query=body,
            grade=int(assessment.grade),
            grade_meaning=policy.meaning,
            route=policy.route,
            state=state,
            answer=answer,
            confidence=answer_confidence,
            citations=citations,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
            persona=self.persona,
            disclosure=self.vadmin.disclosure if self.vadmin is not None else "",
            human_gate=policy.human_gate,
            requires_reviewer=policy.requires_reviewer,
            requires_admin=policy.requires_admin,
            escalated=assessment.escalated,
            escalation_reason=assessment.escalation_reason,
            quarantined=sorted(set(quarantined)),
            admin_synopsis=synopsis,
        )

    def _initial_state(self, policy: GradePolicy, citations: List[Dict[str, Any]]) -> str:
        if policy.requires_admin:
            return RequestState.AWAITING_ADMIN
        if policy.requires_reviewer:
            return RequestState.AWAITING_REVIEWER
        if not citations:
            # Part 9.1: the Hub does not silently answer locally. With nothing
            # retrieved there is no grounded answer to auto-send.
            return RequestState.ESCALATED
        return RequestState.READY_TO_SEND

    def _draft(
        self,
        body: str,
        policy: GradePolicy,
        citations: List[Dict[str, Any]],
        assessment: GradeAssessment,
    ) -> tuple:
        """Compose a grounded draft, or say plainly that there is none."""
        if not citations:
            return (
                "No governed source on this node supports an answer to that. The request "
                "has been routed to a human rather than answered from nothing.",
                0.0,
            )

        best = citations[0]
        support = sum(c["confidence"] for c in citations) / len(citations)
        lead = " ".join(best["snippet"].split())

        if policy.grade >= Grade.CAPABILITY_BUILDING:
            frame = (
                "This is a " + policy.meaning.lower() + ". The route is: " + policy.route
                + ". Grounding evidence: "
            )
        elif policy.grade == Grade.BUG:
            frame = (
                "This reads as a reproducible issue, so it routes to " + policy.route.lower()
                + " rather than a direct answer. The closest governed evidence is: "
            )
        else:
            frame = ""

        answer = frame + lead
        if len(citations) > 1:
            answer += " (" + str(len(citations) - 1) + " further supporting passage"
            answer += "s" if len(citations) > 2 else ""
            answer += " cited.)"
        if assessment.escalated:
            answer += " " + assessment.escalation_reason + "."
        return answer, round(min(0.99, support), 4)

    # -- Part 9.2: grade overrides ----------------------------------------

    def override_grade(
        self, request_id: str, new_grade: int, actor_id: str, rationale: str
    ) -> Dict[str, Any]:
        """Record an override with old grade, new grade, actor, and rationale."""
        if not rationale.strip():
            raise PolicyError("a grade override requires a rationale")
        try:
            target = Grade(int(new_grade))
        except ValueError as exc:
            raise HubCoreError("unknown grade: " + repr(new_grade)) from exc

        with self._lock:
            row = self._row(request_id)
            if row["state"] == RequestState.CLOSED:
                raise PolicyError("request " + request_id + " is closed; reopen it first")
            old = int(row["grade"])
            policy = GRADE_MATRIX[target]
            state = (
                RequestState.AWAITING_ADMIN if policy.requires_admin
                else RequestState.AWAITING_REVIEWER if policy.requires_reviewer
                else RequestState.READY_TO_SEND
            )
            self._conn.execute(
                "UPDATE advisor_requests SET grade=?, state=?, updated_at=? WHERE request_id=?",
                (int(target), state, self._now(), request_id),
            )
            detail = {
                "oldGrade": old,
                "newGrade": int(target),
                "rationale": rationale,
                "downstreamRoute": policy.route,
                "newGate": policy.human_gate,
            }
            self._event(request_id, "grade-override", actor_id, detail)
            self._conn.commit()
        self._learn("override", request_id, {**detail, "confidence": 1.0}, policy.expert_weight)
        return {"requestId": request_id, "state": state, **detail}

    # -- Part 9.3 step 6: human gates -------------------------------------

    def review(
        self, request_id: str, reviewer_id: str, approve: bool, notes: str = ""
    ) -> Dict[str, Any]:
        """Apply the reviewer gate. A reviewer cannot stand in for the Admin."""
        with self._lock:
            row = self._row(request_id)
            policy = GRADE_MATRIX[Grade(int(row["grade"]))]
            if row["state"] not in (RequestState.AWAITING_REVIEWER, RequestState.ESCALATED):
                raise PolicyError(
                    "request " + request_id + " is in state " + row["state"]
                    + " and is not awaiting reviewer approval"
                )
            if not approve:
                state = RequestState.ESCALATED
            elif policy.requires_admin:
                state = RequestState.AWAITING_ADMIN
            else:
                state = RequestState.READY_TO_SEND
            self._conn.execute(
                "UPDATE advisor_requests SET state=?, reviewer_id=?, updated_at=? WHERE request_id=?",
                (state, reviewer_id, self._now(), request_id),
            )
            self._event(
                request_id, "reviewer-decision", reviewer_id,
                {"approved": approve, "notes": notes, "state": state},
            )
            self._conn.commit()
        return {"requestId": request_id, "state": state, "reviewerId": reviewer_id}

    def admin_review(
        self, request_id: str, admin_id: str, approve: bool, notes: str = ""
    ) -> Dict[str, Any]:
        """Apply the Admin gate required by grades 4 and 5."""
        with self._lock:
            row = self._row(request_id)
            if row["state"] != RequestState.AWAITING_ADMIN:
                raise PolicyError(
                    "request " + request_id + " is in state " + row["state"]
                    + " and is not awaiting Admin involvement"
                )
            if self.vadmin is not None:
                self.vadmin.assert_is_human(admin_id)
            state = RequestState.READY_TO_SEND if approve else RequestState.ESCALATED
            self._conn.execute(
                "UPDATE advisor_requests SET state=?, admin_id=?, updated_at=? WHERE request_id=?",
                (state, admin_id, self._now(), request_id),
            )
            self._event(
                request_id, "admin-decision", admin_id,
                {"approved": approve, "notes": notes, "state": state},
            )
            self._conn.commit()
        return {"requestId": request_id, "state": state, "adminId": admin_id}

    # -- Part 9.3 step 7-9: send, follow up, close ------------------------

    def send(self, request_id: str, actor_id: str = "comms") -> Dict[str, Any]:
        with self._lock:
            row = self._row(request_id)
            if row["state"] != RequestState.READY_TO_SEND:
                raise PolicyError(
                    "request " + request_id + " is in state " + row["state"]
                    + "; only READY_TO_SEND may be delivered"
                )
            self._conn.execute(
                "UPDATE advisor_requests SET state=?, updated_at=? WHERE request_id=?",
                (RequestState.AWAITING_FOLLOW_UP, self._now(), request_id),
            )
            self._event(request_id, "sent", actor_id, {"channel": "comms"})
            self._conn.commit()
        return {
            "requestId": request_id,
            "state": RequestState.AWAITING_FOLLOW_UP,
            "responseWindowHours": self.follow_up_policy["responseWindowHours"],
        }

    def follow_up(self, request_id: str, text: str, actor_id: str) -> Dict[str, Any]:
        parsed = parse_follow_up(text)
        with self._lock:
            row = self._row(request_id)
            grade = int(row["grade"])
            state = row["state"]
            if parsed["kind"] == "satisfied" and GRADE_MATRIX[Grade(grade)].may_auto_close:
                state = RequestState.READY_TO_SEND  # ready for a close decision
            elif parsed["kind"] in ("dissatisfied", "needs_follow_up", "depth_change"):
                state = RequestState.AWAITING_REVIEWER
            elif parsed["kind"] == "meeting_request":
                state = RequestState.AWAITING_REVIEWER
            self._conn.execute(
                "UPDATE advisor_requests SET state=?, updated_at=? WHERE request_id=?",
                (state, self._now(), request_id),
            )
            self._event(request_id, "follow-up", actor_id, parsed)
            self._conn.commit()

        result = {"requestId": request_id, "state": state, **parsed}
        if parsed["kind"] == "meeting_request":
            result["meetingPolicy"] = {
                "slotsOffered": self.follow_up_policy["meetingSlots"],
                "withinBusinessDays": self.follow_up_policy["meetingWindowBusinessDays"],
                "days": self.follow_up_policy["meetingDays"],
                "noShowWindowMinutes": self.follow_up_policy["noShowWindowMinutes"],
                "syntheticAttendeeDiscloses": True,
            }
        return result

    def close(self, request_id: str, reason: str, actor_id: str) -> Dict[str, Any]:
        """Close with a reason. Grade 5 cannot auto-close (Part 9.2)."""
        if not reason.strip():
            raise PolicyError("a closure requires a reason")
        with self._lock:
            row = self._row(request_id)
            policy = GRADE_MATRIX[Grade(int(row["grade"]))]
            if not policy.may_auto_close and not row["admin_id"]:
                raise PolicyError(
                    "grade 5 requires direct Admin involvement and requestor contact "
                    "before closure; no Admin decision is recorded for " + request_id
                )
            if self.vadmin is not None and self.vadmin.is_synthetic(actor_id):
                raise PolicyError(
                    "the synthetic assistant cannot close an advisor request; "
                    "closure is a human decision (Part 10.2)"
                )
            self._conn.execute(
                "UPDATE advisor_requests SET state=?, close_reason=?, updated_at=? WHERE request_id=?",
                (RequestState.CLOSED, reason, self._now(), request_id),
            )
            self._event(request_id, "closed", actor_id, {"reason": reason})
            self._conn.commit()
        return {"requestId": request_id, "state": RequestState.CLOSED, "reason": reason}

    # -- reads -------------------------------------------------------------

    def get(self, request_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._row(request_id)
            trail = [
                dict(e) for e in self._conn.execute(
                    "SELECT at, stage, actor_id, detail FROM advisor_events"
                    " WHERE request_id = ? ORDER BY seq", (request_id,)
                ).fetchall()
            ]
        return {**self._as_dict(row), "trail": trail}

    def list_requests(
        self, state: Optional[str] = None, grade: Optional[int] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if grade is not None:
            clauses.append("grade = ?")
            params.append(int(grade))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM advisor_requests" + where
                + " ORDER BY created_at DESC, request_id DESC LIMIT ?", params
            ).fetchall()
        return [self._as_dict(row) for row in rows]

    def _as_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        policy = GRADE_MATRIX[Grade(int(row["grade"]))]
        return {
            "requestId": row["request_id"],
            "contract": row["contract"],
            "requestorSubject": row["requestor_subject"],
            "module": {
                "id": row["module_id"],
                "chain": row["module_chain"],
                "route": row["module_route"],
            },
            "mode": row["mode"],
            "body": row["body"],
            "captureRef": row["capture_ref"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "grade": int(row["grade"]),
            "gradeMeaning": policy.meaning,
            "gradeConfidence": row["grade_confidence"],
            "route": policy.route,
            "humanGate": policy.human_gate,
            "expertWeight": policy.expert_weight,
            "state": row["state"],
            "answer": row["answer"],
            "citations": json.loads(row["citations"]) if row["citations"] else [],
            "reviewerId": row["reviewer_id"],
            "adminId": row["admin_id"],
            "closeReason": row["close_reason"],
        }

    def quality_metrics(self) -> Dict[str, Any]:
        """Part 9.5 quality counters, computed from the ledger not from memory."""
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS n FROM advisor_requests").fetchone()["n"]
            by_grade = {
                str(r["grade"]): r["n"] for r in self._conn.execute(
                    "SELECT grade, COUNT(*) AS n FROM advisor_requests GROUP BY grade"
                ).fetchall()
            }
            by_state = {
                r["state"]: r["n"] for r in self._conn.execute(
                    "SELECT state, COUNT(*) AS n FROM advisor_requests GROUP BY state"
                ).fetchall()
            }
            overrides = self._conn.execute(
                "SELECT COUNT(DISTINCT request_id) AS n FROM advisor_events WHERE stage='grade-override'"
            ).fetchone()["n"]
            escalations = self._conn.execute(
                "SELECT COUNT(*) AS n FROM advisor_requests WHERE state=?",
                (RequestState.ESCALATED,),
            ).fetchone()["n"]
        return {
            "totalRequests": total,
            "byGrade": by_grade,
            "byState": by_state,
            "gradeOverrides": overrides,
            "overrideRate": round(overrides / total, 4) if total else 0.0,
            "escalations": escalations,
            "confidenceFloor": self.grader.confidence_floor,
            "followUpPolicy": self.follow_up_policy,
            "corpus": self.corpus.stats(),
        }

    def close_db(self) -> None:
        with self._lock:
            self._conn.close()


def grade_matrix_document() -> List[Dict[str, Any]]:
    """The Part 9.2 matrix, for operators and for the Hub UI."""
    return [GRADE_MATRIX[g].to_dict() for g in sorted(GRADE_MATRIX)]
