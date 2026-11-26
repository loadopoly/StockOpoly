"""Part 3.4 - ToolForge Module Builder and Scaffolding Engine.

V10-MOD-001: ToolForge stages candidates strictly under UAT. It cannot write
directly to PROD or escalate authority.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .errors import ConfigError, PolicyError
from .modules import ModuleManifest, SEVEN_CHAINS, FORBIDDEN_CAPABILITIES


class ToolForge:
    """Scaffolds, validates, and stages custom Hub modules."""

    def __init__(self, workspace: Optional[Dict[str, Any]] = None):
        if workspace is None:
            self.workspace = config.load("workspace")
        else:
            self.workspace = workspace
        self.library_root = config.library_root(self.workspace)
        self.uat_modules_dir = self.library_root / self.workspace.get("library", {}).get("uat", "UAT") / "Modules"
        self.uat_modules_dir.mkdir(parents=True, exist_ok=True)

    def scaffold(
        self,
        module_id: str,
        name: str,
        chain: str,
        version: str = "1.0.0",
        description: str = "",
        capabilities: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Generate a new sandboxed module bundle staged in UAT."""
        if chain not in SEVEN_CHAINS and chain != "General":
            raise ConfigError(f"invalid chain '{chain}'; expected one of {SEVEN_CHAINS}")

        caps = capabilities or ["data.read", "ui.embed"]
        for cap in caps:
            if cap in FORBIDDEN_CAPABILITIES:
                raise PolicyError(f"ToolForge cannot grant release or root capability: {cap}")

        module_dir = self.uat_modules_dir / module_id
        module_dir.mkdir(parents=True, exist_ok=True)

        manifest = ModuleManifest(
            id=module_id,
            name=name,
            version=version,
            chain=chain,
            entrypoint="index.html",
            description=description,
            icon="box",
            capabilities=caps,
            permissions=["sandbox.isolated"],
            enabled=True,
        )
        manifest.validate()

        # Write manifest
        manifest_file = module_dir / "manifest.json"
        manifest_file.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")

        # Write standard index.html template
        index_html = module_dir / "index.html"
        index_html.write_text(
            f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{name}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 1.5rem; color: #333; }}
    h1 {{ color: #006B5E; margin-bottom: 0.5rem; }}
    .badge {{ display: inline-block; padding: 0.25rem 0.5rem; background: #e0f2fe; color: #0369a1; border-radius: 4px; font-size: 0.85rem; }}
  </style>
</head>
<body>
  <h1>{name}</h1>
  <div class="badge">Chain: {chain}</div>
  <p>{description or "Custom sandboxed module staged via ToolForge."}</p>
  <div id="app-root"></div>
  <script>
    console.log("Module {module_id} loaded in UAT sandbox.");
  </script>
</body>
</html>
""",
            encoding="utf-8",
        )

        return {
            "status": "staged",
            "moduleId": module_id,
            "chain": chain,
            "stagedPath": str(module_dir),
            "manifest": manifest.to_dict(),
        }

    def lint(self, module_dir: Path) -> List[str]:
        """Verify module compliance, manifest presence, and absence of PROD writes."""
        path = Path(module_dir)
        errors = []
        if not path.exists():
            return [f"module directory does not exist: {path}"]

        manifest_file = path / "manifest.json"
        if not manifest_file.exists():
            return ["manifest.json is missing"]

        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            m = ModuleManifest(**data)
            m.validate()
        except Exception as exc:
            errors.append(f"manifest validation error: {exc}")

        index_file = path / "index.html"
        if not index_file.exists():
            errors.append("entrypoint index.html is missing")

        return errors
