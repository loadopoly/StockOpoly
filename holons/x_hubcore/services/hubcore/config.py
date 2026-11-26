"""Part 2.1 - configuration ownership split, atomic writes, schema validation.

hub_org.json        public organization and experience configuration
hub_workspace.json  locations, datasets, endpoints, hardware, allocations
hub_modules.json    module registry

V10-CFG-001: writes are schema-validated, atomic, versioned, backed up, audited.
V10-CFG-002: setup is idempotent; immutable IDs are never rewritten.

No secret is ever stored in these files (decision 0.3.6). assert_no_secrets is
enforced on every write.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from .errors import ConfigError

SCHEMA_VERSION = 10

ORG_FILE = "hub_org.json"
WORKSPACE_FILE = "hub_workspace.json"
MODULES_FILE = "hub_modules.json"

#: Keys that MUST NOT appear anywhere in a configuration document. The library
#: is not a secret store (decision 0.3.6); secrets live in the OS secret store.
FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "apikey", "secret", "clientsecret", "password", "passwd", "token",
        "accesstoken", "refreshtoken", "privatekey", "bearer", "credential",
        "credentials", "connectionstring",
    }
)

#: Immutable identifiers. Setup may change labels, never these (V10-ID-002).
IMMUTABLE_KEYS = frozenset(
    {"orgId", "workspaceId", "subjectId", "deviceId", "siteId", "moduleId"}
)


def hub_root(start: Path | str | None = None) -> Path:
    """Resolve the repository hub/ directory.

    Honours HUBCORE_HUB_ROOT so tests and alternate deployments can point at a
    scratch directory without editing code.
    """
    override = os.environ.get("HUBCORE_HUB_ROOT")
    if override:
        return Path(override).resolve()
    here = Path(start) if start else Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "hub" / ORG_FILE
        if candidate.exists():
            return (parent / "hub").resolve()
    raise ConfigError("cannot locate hub/ containing " + ORG_FILE + " from " + str(here))


def _normalize_key(key: str) -> str:
    return key.replace("-", "").replace("_", "").lower()


_FORBIDDEN_NORMALIZED = frozenset(_normalize_key(k) for k in FORBIDDEN_SECRET_KEYS)


def _walk(node: Any, path: str = "") -> Iterable[tuple[str, str, Any]]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield path, str(key), value
            yield from _walk(value, (path + "." + str(key)) if path else str(key))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk(value, path + "[" + str(index) + "]")


def assert_no_secrets(document: Mapping[str, Any]) -> None:
    """Raise ConfigError if the document embeds a secret-looking value.

    A key whose value is None or empty is a declared *reference slot*, not a
    secret, and is permitted. So is a value naming an environment variable.
    """
    for path, key, value in _walk(document):
        if _normalize_key(key) not in _FORBIDDEN_NORMALIZED:
            continue
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str) and value.endswith("_ENV"):
            continue
        location = (path + "." + key) if path else key
        raise ConfigError(
            "secret-bearing key " + repr(location) + " is not permitted in hub "
            "configuration; store the value in the OS secret store and keep "
            "only a reference here"
        )


def validate(document: Mapping[str, Any], kind: str) -> None:
    """Minimal structural validation.

    Deliberately dependency-free: jsonschema is optional on this host, and a
    configuration load MUST NOT fail closed merely because a linter is absent.
    The JSON Schemas under hub/schemas/ remain the published contract.
    """
    if not isinstance(document, Mapping):
        raise ConfigError(kind + ": document root must be an object")
    version = document.get("schemaVersion")
    if version != SCHEMA_VERSION:
        raise ConfigError(
            kind + ": schemaVersion " + repr(version) + " is not the supported "
            "version " + str(SCHEMA_VERSION)
        )
    required: Dict[str, tuple] = {
        "org": ("orgId", "chains", "capabilities", "admin"),
        "workspace": ("workspaceId", "library", "endpoints", "allocationSpaces"),
        "modules": ("modules",),
    }
    for field in required.get(kind, ()):
        if field not in document:
            raise ConfigError(kind + ": required field " + repr(field) + " is missing")
    if kind == "org":
        chains = document["chains"]
        found = len(chains) if isinstance(chains, list) else -1
        if found != 7:
            raise ConfigError(
                "org: exactly seven chain slots are required, found " + str(found)
            )
    if kind == "workspace":
        for name, endpoint in (document.get("endpoints") or {}).items():
            for field in ("id", "port", "healthPath"):
                if field not in endpoint:
                    raise ConfigError(
                        "workspace: endpoint " + repr(name) + " is missing "
                        "required descriptor field " + repr(field) + " (V10-CFG-003)"
                    )
    assert_no_secrets(document)


def load(kind: str, root: Path | None = None) -> Dict[str, Any]:
    """Load and validate one configuration document."""
    filename = {"org": ORG_FILE, "workspace": WORKSPACE_FILE, "modules": MODULES_FILE}.get(kind)
    if filename is None:
        raise ConfigError("unknown configuration kind " + repr(kind))
    path = (root or hub_root()) / filename
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(kind + ": configuration file not found at " + str(path)) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(kind + ": " + str(path) + " is not valid JSON: " + str(exc)) from exc
    validate(document, kind)
    return document


def save(kind: str, document: Mapping[str, Any], root: Path | None = None) -> Path:
    """Validate, back up, and atomically write one configuration document.

    Immutable identifiers present in the existing file are preserved; an attempt
    to change one raises rather than silently rewriting history (V10-CFG-002).
    """
    validate(document, kind)
    filename = {"org": ORG_FILE, "workspace": WORKSPACE_FILE, "modules": MODULES_FILE}[kind]
    directory = root or hub_root()
    path = directory / filename

    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        _assert_immutable_preserved(previous, document, kind)
        backups = directory / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        shutil.copy2(path, backups / (path.stem + "." + stamp + path.suffix))

    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    handle, tmp_name = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def _assert_immutable_preserved(previous: Any, current: Any, kind: str, path: str = "") -> None:
    if not (isinstance(previous, Mapping) and isinstance(current, Mapping)):
        return
    for key, old in previous.items():
        where = (path + "." + key) if path else key
        if key in IMMUTABLE_KEYS and key in current and current[key] != old:
            raise ConfigError(
                kind + ": immutable identifier " + repr(where) + " cannot change ("
                + repr(old) + " -> " + repr(current[key]) + "); see V10-ID-002"
            )
        if key in current:
            _assert_immutable_preserved(old, current[key], kind, where)


def load_all(root: Path | None = None) -> Dict[str, Dict[str, Any]]:
    """Load org, workspace, and modules together."""
    directory = root or hub_root()
    return {kind: load(kind, directory) for kind in ("org", "workspace", "modules")}


def library_root(workspace: Mapping[str, Any], base: Path | None = None) -> Path:
    """Canonicalize the configured library root (Part 2.2)."""
    mirror = str(workspace.get("library", {}).get("localMirror") or ".")
    root = Path(mirror)
    if not root.is_absolute():
        root = (base or hub_root().parent) / root
    return root.resolve()


# -- Part 0.2 brand token resolution ------------------------------------------

#: Every brand token the spec defines (Part 0.2). A configuration document that
#: still carries one of these literally has never been through setup: the value
#: is a placeholder, not an identity.
BRAND_TOKENS = (
    "orgName", "hubName", "askPersona", "virtualPersona", "libraryRoot",
    "specialistId", "dataRightsHolder", "meshOperator",
)

_TOKEN_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def unresolved_tokens(document: Any, path: str = "") -> Dict[str, str]:
    """Map every ``json.path -> {{token}}`` still present in a document.

    An empty result means display identity is fully configured. A non-empty one
    means setup has not completed, and rendering the document into a user-facing
    surface would show a placeholder where a name belongs.
    """
    found: Dict[str, str] = {}
    if isinstance(document, Mapping):
        for key, value in document.items():
            found.update(unresolved_tokens(value, (path + "." + str(key)) if path else str(key)))
    elif isinstance(document, (list, tuple)):
        for index, value in enumerate(document):
            found.update(unresolved_tokens(value, path + "[" + str(index) + "]"))
    elif isinstance(document, str):
        match = _TOKEN_PATTERN.search(document)
        if match:
            found[path] = match.group(0)
    return found


def assert_identity_resolved(document: Mapping[str, Any], kind: str = "org") -> None:
    """Fail closed when a placeholder would reach a user-facing surface.

    V10-ID-001 keeps deployment identity out of source; it does not license
    shipping ``{{hubName}}`` to a screen. Setup MUST supply a real value.
    """
    outstanding = unresolved_tokens(document)
    if outstanding:
        detail = ", ".join(k + "=" + v for k, v in sorted(outstanding.items()))
        raise ConfigError(
            kind + ": display identity is unresolved; run the setup wizard to "
            "supply a value for " + detail
        )
