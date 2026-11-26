"""HubCore v10 - sovereign authority layer for Black Bulls Hideout.

Implements the foundation contracts of ``docs/APP_RECREATION.md``:

* :mod:`services.hubcore.config`      - Part 2.1 configuration ownership split
* :mod:`services.hubcore.hal`         - Part 11.3 Hardware Abstraction Layer
* :mod:`services.hubcore.endpoints`   - Part 11.2.1 native endpoint registry
* :mod:`services.hubcore.allocations` - Part 12.5 tenant allocation confinement
* :mod:`services.hubcore.token_broker`- Part 12.5.3 native scoped token minting
* :mod:`services.hubcore.airlock`     - Part 1.3 data-only ingress air-lock
* :mod:`services.hubcore.statepack`   - Part 11.2.2 transferable state packs

Nothing in this package carries organization identity; all display identity is
resolved from ``hub/hub_org.json`` at runtime (V10-ID-001).
"""

__version__ = "10.0.0"

SPEC_VERSION = 10
SPEC_DOCUMENT = "docs/APP_RECREATION.md"

__all__ = ["__version__", "SPEC_VERSION", "SPEC_DOCUMENT"]
