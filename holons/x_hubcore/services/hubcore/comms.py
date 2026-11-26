"""Part 8 - Communications and Process Studio Consent Engine.

V10-COM-001: Media channels (voice, video, screen) require explicit consent
and org-level enablement before data acquisition.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set
from . import config
from .errors import PolicyError


class CommsManager:
    """Manages communications adapters and user consent records."""

    def __init__(self, org_doc: Optional[Dict[str, Any]] = None):
        if org_doc is None:
            self.org = config.load("org")
        else:
            self.org = org_doc
        self.capabilities = self.org.get("capabilities", {})
        self._user_consents: Dict[str, Set[str]] = {}

    def grant_consent(self, user_id: str, modality: str) -> None:
        if modality not in ("voice", "video", "screen", "telemetry"):
            raise PolicyError(f"unknown comms modality: {modality}")
        if not self.capabilities.get(modality, False) and modality != "telemetry":
            raise PolicyError(f"modality '{modality}' is disabled at organization level")
        self._user_consents.setdefault(user_id, set()).add(modality)

    def assert_consent(self, user_id: str, modality: str) -> None:
        if not self.capabilities.get(modality, False) and modality != "telemetry":
            raise PolicyError(f"modality '{modality}' is not enabled for this organization")
        consents = self._user_consents.get(user_id, set())
        if modality not in consents:
            raise PolicyError(f"user '{user_id}' has not granted explicit consent for '{modality}'")
