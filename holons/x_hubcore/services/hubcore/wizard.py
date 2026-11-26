"""Part 2.3 - Idempotent Setup Wizard.

V10-CFG-002: Setup is idempotent. Re-running it preserves immutable identifiers,
task history, receipts, consent history, and release evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import allocations, config, hal, endpoints
from .errors import ConfigError, PolicyError


class SetupWizard:
    """9-step idempotent configuration and provisioning wizard."""

    def __init__(self, hub_dir: Optional[Path] = None):
        self.hub_dir = hub_dir
        self.documents = config.load_all(hub_dir)
        self.org = self.documents["org"]
        self.workspace = self.documents["workspace"]
        self.modules = self.documents["modules"]

    def run_step(self, step_number: int, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute one of the 9 setup wizard steps."""
        if step_number == 1:  # Identity
            if "orgName" in inputs:
                self.org["orgName"] = inputs["orgName"]
            if "hubName" in inputs:
                self.org["hubName"] = inputs["hubName"]
            if "askPersona" in inputs:
                self.org["askPersona"] = inputs["askPersona"]
            return {"step": 1, "status": "ok", "org": self.org}

        elif step_number == 2:  # Library
            lib = self.workspace.setdefault("library", {})
            if "kind" in inputs:
                lib["kind"] = inputs["kind"]
            if "localMirror" in inputs:
                lib["localMirror"] = inputs["localMirror"]
            return {"step": 2, "status": "ok", "library": lib}

        elif step_number == 3:  # Chains
            if "chains" in inputs and isinstance(inputs["chains"], list):
                self.org["chains"] = inputs["chains"]
            return {"step": 3, "status": "ok", "chains": self.org.get("chains", [])}

        elif step_number == 4:  # Modules
            return {"step": 4, "status": "ok", "modulesCount": len(self.modules.get("modules", []))}

        elif step_number == 5:  # Floor & Roster
            return {"step": 5, "status": "ok", "workers": len(self.workspace.get("workers", []))}

        elif step_number == 6:  # Comms & Consent
            caps = self.org.setdefault("capabilities", {})
            for k in ("voice", "video", "screen", "comms"):
                if k in inputs:
                    caps[k] = bool(inputs[k])
            return {"step": 6, "status": "ok", "capabilities": caps}

        elif step_number == 7:  # Hardware & Fleet
            profile = hal.probe(self.workspace)
            fleet_probe = endpoints.status_report(self.workspace)
            return {"step": 7, "status": "ok", "hardware": profile.to_dict(), "fleet": fleet_probe}

        elif step_number == 8:  # Self-annealing & Allocations
            engine = allocations.AllocationEngine(config.library_root(self.workspace), self.workspace)
            return {"step": 8, "status": "ok", "allocationsRoot": str(engine.allocations_root)}

        elif step_number == 9:  # Review & Commit
            config.save("org", self.org, self.hub_dir)
            config.save("workspace", self.workspace, self.hub_dir)
            config.save("modules", self.modules, self.hub_dir)
            return {"step": 9, "status": "committed", "orgId": self.org["orgId"]}

        else:
            raise ConfigError(f"invalid wizard step {step_number}; expected 1-9")
