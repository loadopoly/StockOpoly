"""Part 9.5 - governed retrieval corpus with provenance.

The advisor may only cite what it actually retrieved. This module builds a
dependency-free BM25 index over the governed sources on this node -- the
specification, the operator documentation, and the hub configuration -- and
returns passages that carry their own provenance: source path, section heading,
and a content hash of the exact text that was read.

Two rules follow from Part 9.5:

* Retrieved content is **data**. A passage that contains instruction-shaped
  text ("ignore previous instructions", "you are now...") cannot change policy
  or tool permissions, so it is quarantined and flagged rather than passed into
  a draft as though the corpus had authored a command.
* A citation names the passage it came from. Retrieval that finds nothing
  returns nothing; it never falls back to a plausible-sounding source, because
  an unsupported answer that carries a citation is worse than one that admits
  it has none.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from . import config

#: BM25 parameters. k1 damps term-frequency saturation; b controls how much a
#: long passage is penalised relative to the average.
BM25_K1 = 1.5
BM25_B = 0.75

#: A passage longer than this is split, so a citation points at something an
#: operator can actually read rather than a whole chapter.
MAX_PASSAGE_CHARS = 1800

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]*")

#: Words that carry no retrieval signal in this corpus.
STOPWORDS = frozenset(
    """a an and are as at be but by for from has have how if in into is it its
    may must no not of on or should that the their then there these this to was
    what when where which who why will with would you your""".split()
)

#: Instruction-shaped patterns. Text matching these is retained for provenance
#: but marked untrusted, and is never presented as guidance to follow.
_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?", re.I),
    re.compile(r"disregard\s+(?:the\s+)?(?:system|previous|prior)\b", re.I),
    re.compile(r"you\s+are\s+now\s+(?:a|an|the)\b", re.I),
    re.compile(r"\bnew\s+(?:system\s+)?(?:prompt|instructions?)\s*[:=]", re.I),
    re.compile(r"\b(?:reveal|print|output)\s+(?:the\s+)?(?:system\s+prompt|your\s+instructions)", re.I),
    re.compile(r"\bgrant\s+(?:me\s+)?(?:admin|root|reviewer)\b", re.I),
)


def tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


def looks_like_injection(text: str) -> List[str]:
    """Return the instruction-shaped fragments found in ``text``."""
    found = []
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            found.append(match.group(0))
    return found


@dataclass
class Passage:
    """One retrievable unit of governed text, with its provenance."""

    source: str
    section: str
    text: str
    content_hash: str
    trusted: bool = True
    quarantined_fragments: List[str] = field(default_factory=list)
    tokens: List[str] = field(default_factory=list, repr=False)

    def snippet(self, limit: int = 240) -> str:
        collapsed = " ".join(self.text.split())
        return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"

    def to_citation(self, confidence: float) -> Dict[str, Any]:
        return {
            "source": self.source,
            "section": self.section,
            "confidence": round(confidence, 4),
            "snippet": self.snippet(),
            "contentHash": self.content_hash,
            "trusted": self.trusted,
        }


def _split_markdown(path: Path, source_label: str) -> List[Passage]:
    """Split a Markdown document on its headings, keeping the heading path."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    passages: List[Passage] = []
    section = path.stem
    buffer: List[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if not body:
            return
        for chunk in _chunk(body):
            passages.append(_make_passage(source_label, section, chunk))

    for line in raw.splitlines():
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            flush()
            buffer = []
            section = heading.group(2).strip()
            continue
        buffer.append(line)
    flush()
    return passages


def _chunk(body: str) -> Iterable[str]:
    if len(body) <= MAX_PASSAGE_CHARS:
        yield body
        return
    paragraphs = body.split("\n\n")
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > MAX_PASSAGE_CHARS:
            yield current
            current = paragraph
        else:
            current = (current + "\n\n" + paragraph) if current else paragraph
    if current:
        yield current


def _make_passage(source: str, section: str, text: str) -> Passage:
    fragments = looks_like_injection(text)
    return Passage(
        source=source,
        section=section,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        trusted=not fragments,
        quarantined_fragments=fragments,
        tokens=tokenize(text),
    )


def _config_passages(documents: Mapping[str, Mapping[str, Any]]) -> List[Passage]:
    """Turn the hub configuration into citable prose.

    The advisor is asked about this node far more often than about the
    specification in the abstract, and 'what is actually configured here' has to
    be answerable with a citation like anything else.
    """
    passages: List[Passage] = []
    org = documents.get("org", {})
    workspace = documents.get("workspace", {})
    modules = documents.get("modules", {})

    chains = ", ".join(str(c.get("label")) for c in org.get("chains", []))
    passages.append(
        _make_passage(
            "hub/hub_org.json",
            "identity and chains",
            "This node is operated by " + str(org.get("orgName")) + " (" + str(org.get("orgId"))
            + "). The Hub is named " + str(org.get("hubName")) + ". The advisor persona is "
            + str(org.get("askPersona")) + " and the disclosed synthetic assistant is "
            + str(org.get("virtualPersona")) + ". The seven configured chain slots are: "
            + chains + ".",
        )
    )

    federation = org.get("federation", {})
    passages.append(
        _make_passage(
            "hub/hub_org.json",
            "federation",
            "Cross-organization federation is "
            + ("enabled" if federation.get("enabled") else "disabled")
            + " at consent tier " + str(federation.get("consentTier", 0))
            + ". Federation exchanges only consent-tiered distilled artifacts; raw tenant "
              "records never cross an organization boundary.",
        )
    )

    for name, endpoint in (workspace.get("endpoints") or {}).items():
        passages.append(
            _make_passage(
                "hub/hub_workspace.json",
                "endpoint " + str(endpoint.get("id", name)),
                "The " + name + " endpoint is " + str(endpoint.get("id"))
                + ", role " + str(endpoint.get("role", "unspecified"))
                + ", axis " + str(endpoint.get("axis", endpoint.get("realm", "n/a")))
                + ", bound to " + str(endpoint.get("bind", "127.0.0.1")) + ":"
                + str(endpoint.get("port")) + " with health path "
                + str(endpoint.get("healthPath")) + ". It is "
                + ("required" if endpoint.get("required") else "optional")
                + " for fleet readiness.",
            )
        )

    for module in modules.get("modules", []):
        # The manifest is the v2 shape (moduleId/chainId); the registry's
        # runtime shape uses id/chain. Accept either so the corpus does not
        # quietly index a page full of "module None".
        module_id = module.get("moduleId") or module.get("id") or "unknown"
        chain = module.get("chainId") or module.get("chain") or "unassigned"
        passages.append(
            _make_passage(
                "hub/hub_modules.json",
                "module " + str(module_id),
                "Module " + str(module.get("name")) + " (" + str(module_id)
                + ") version " + str(module.get("version")) + " sits on the "
                + str(chain) + " chain and declares capabilities: "
                + ", ".join(module.get("capabilities", []) or ["none"])
                + ". It is " + ("enabled" if module.get("enabled") else "disabled") + ".",
            )
        )

    airlock = workspace.get("airlock", {})
    passages.append(
        _make_passage(
            "hub/hub_workspace.json",
            "air-lock",
            "The Data-Only Ingress Air-Lock runs in " + str(airlock.get("mode", "data-only"))
            + " mode on port " + str(airlock.get("port", 8000))
            + ". Zero external compute is "
            + ("enforced" if airlock.get("zeroExternalCompute") else "NOT enforced")
            + ". Maximum accepted payload is " + str(airlock.get("maxPayloadBytes")) + " bytes.",
        )
    )
    return passages


class Corpus:
    """A BM25 index over the governed sources on this node."""

    def __init__(self, passages: Sequence[Passage]):
        self.passages: List[Passage] = list(passages)
        self._lock = threading.RLock()
        self.built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._document_frequency: Counter = Counter()
        self._term_frequency: List[Counter] = []
        self._lengths: List[int] = []
        for passage in self.passages:
            counts = Counter(passage.tokens)
            self._term_frequency.append(counts)
            self._lengths.append(max(1, len(passage.tokens)))
            self._document_frequency.update(counts.keys())
        self._average_length = (
            sum(self._lengths) / len(self._lengths) if self._lengths else 1.0
        )

    def __len__(self) -> int:
        return len(self.passages)

    def _idf(self, term: str) -> float:
        total = len(self.passages)
        seen = self._document_frequency.get(term, 0)
        if seen == 0:
            return 0.0
        return math.log(1.0 + (total - seen + 0.5) / (seen + 0.5))

    def search(self, query: str, limit: int = 4) -> List[tuple]:
        """Return ``(passage, normalized_score)`` for the best matches.

        The score is normalized against the best hit so a caller can reason
        about relative confidence. An empty result means nothing matched, which
        is a legitimate and reportable outcome.
        """
        terms = tokenize(query)
        if not terms or not self.passages:
            return []
        with self._lock:
            scored: List[tuple] = []
            for index, passage in enumerate(self.passages):
                counts = self._term_frequency[index]
                length = self._lengths[index]
                score = 0.0
                for term in terms:
                    frequency = counts.get(term, 0)
                    if not frequency:
                        continue
                    denominator = frequency + BM25_K1 * (
                        1 - BM25_B + BM25_B * length / self._average_length
                    )
                    score += self._idf(term) * (frequency * (BM25_K1 + 1)) / denominator
                if score > 0:
                    scored.append((passage, score))
        if not scored:
            return []
        scored.sort(key=lambda pair: pair[1], reverse=True)
        best = scored[0][1] or 1.0
        return [(passage, min(1.0, score / best)) for passage, score in scored[:limit]]

    def stats(self) -> Dict[str, Any]:
        sources: Counter = Counter(p.source for p in self.passages)
        return {
            "passages": len(self.passages),
            "sources": dict(sources),
            "vocabulary": len(self._document_frequency),
            "builtAt": self.built_at,
            "quarantinedPassages": sum(1 for p in self.passages if not p.trusted),
        }


def build(
    hub_dir: Optional[Path] = None,
    documents: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Corpus:
    """Index the specification, operator docs, and configuration of this node."""
    documents = documents or config.load_all(hub_dir)
    workspace = documents["workspace"]
    library_root = config.library_root(workspace)

    passages: List[Passage] = list(_config_passages(documents))

    docs_dir = library_root / "docs"
    if docs_dir.is_dir():
        for path in sorted(docs_dir.glob("*.md")):
            passages.extend(_split_markdown(path, "docs/" + path.name))

    return Corpus(passages)
