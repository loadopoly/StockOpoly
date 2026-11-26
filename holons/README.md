# Holons

Holon-related work for the Loadopoly Holon Hub lives here.

| Path | Contents |
|---|---|
| `registry.json` | Registry of holons (this repo and external). Served by `GET /api/holons`, shown in the **Holon Desk** tab. |
| `x_osq/` | x_OSQ holon: manifest, skills, `holon/` runtime scripts, `docs/ARCHITECTURE.md`, and the `x_OSQ.html`, `x_CRM.html`, `OSQ.html` surfaces. |
| `x_hubcore/` | HubCore kernel services (`services/hubcore`, `services/osq`, `services/marketplace`) and `HUBCORE_INSTRUCTIONS.md`. |
| `hub/` | Hub manifests (`hub_modules.json`, `hub_org.json`, `schemas/`), live-network launchers and `Hub_v10.html`. |

`x_deepmap` is hosted in [Perceptopoly](https://github.com/loadopoly/Perceptopoly)
(`holons/x_deepmap`) and appears here as an external registry entry.

Runtime data (relational store, temp tier, sample quotes) and HubCore zip
packages are not tracked.

Code under `x_hubcore/services/` is a copy of the service tree from
`loadopoly/VS-Code`, not a move. VS-Code keeps its own copy, so the two will
drift until one is removed.
