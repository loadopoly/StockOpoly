"""Fail-closed error taxonomy for HubCore.

Every guard in this package raises one of these rather than returning a
plausible-but-empty result (Part 7.2: "A rejected operation returns a policy
error, never a plausible empty result").
"""

from __future__ import annotations


class HubCoreError(Exception):
    """Base class for every HubCore policy or contract failure."""

    code = "hubcore.error"


class ConfigError(HubCoreError):
    """Configuration is missing, unreadable, or fails schema validation."""

    code = "hubcore.config.invalid"


class PolicyError(HubCoreError):
    """An operation was refused by policy. Fail closed, never fall through."""

    code = "hubcore.policy.refused"


class TenancyViolation(PolicyError):
    """Path, query, or token crossed an allocation-space boundary (V10-ALC-001)."""

    code = "hubcore.tenancy.violation"


class ContaminationError(PolicyError):
    """ContaminationGate rejected an egress or ingress artifact (V10-FED-003)."""

    code = "hubcore.contamination.rejected"


class TokenError(PolicyError):
    """Token is absent, malformed, expired, unsigned, or wrongly scoped."""

    code = "hubcore.token.invalid"


class StatePackError(HubCoreError):
    """State pack export/import failed integrity or signature verification."""

    code = "hubcore.statepack.invalid"
