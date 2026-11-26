"""Part 12.5.3 - native scoped token minting and verification.

Mints short-lived RS256 JWTs bound to the composite tenancy tuple. No third
party is involved: the signing key is generated and held machine-local under the
OS secret directory, never in the library (decision 0.3.6).

Implemented directly on ``cryptography`` so the broker has no PyJWT dependency.

Every claim that grants reach -- ``org_id``, ``repo_id``, ``allowed_caps``, and
``role`` -- is set by HubCore at mint time from the provisioned allocation
record. A caller cannot request wider scope than its allocation carries.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from .allocations import TenancyTuple, validate_id
from .errors import PolicyError, TokenError

ISSUER = "hubcore-native-token-broker"
ALGORITHM = "RS256"
DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 3600

#: The complete capability vocabulary. A token may carry no capability outside
#: this set, and no capability grants release, promotion, or write authority --
#: decision 0.3.10, "AI cannot acquire authority".
KNOWN_CAPABILITIES = frozenset(
    {
        "data.read",
        "data.export",
        "vector.search",
        "ocr.capture",
        "metrology.touch",
        "observer.read",
        "observer.feedback",
        "ask",
        "comms",
        "blob.write",
    }
)

#: Capabilities that MUST NOT be mintable by any tenant token. Promotion and
#: release authority belong to the internal release identity alone (Part 5.1).
FORBIDDEN_CAPABILITIES = frozenset(
    {"release.promote", "uat.confirm", "archive.write", "prod.write", "policy.write", "shell"}
)

STANDARD_ROLES = ("Viewer", "Requester", "ModuleAuthor", "WorkerOperator", "Reviewer", "Admin")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def key_directory() -> Path:
    """Machine-local key location. Never inside the synced library."""
    override = os.environ.get("HUBCORE_KEY_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return base / "HubCore" / "keys"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "hubcore" / "keys"


@dataclass
class TokenClaims:
    """The verified claim set returned by :meth:`TokenBroker.verify`."""

    subject: str
    tenancy: TenancyTuple
    role: str
    allowed_caps: frozenset
    allocation_space_id: str
    expires_at: int
    raw: Dict[str, Any]

    def require(self, capability: str) -> None:
        """Raise unless the token carries ``capability``."""
        if capability not in self.allowed_caps:
            raise TokenError(
                "token for " + self.allocation_space_id + " does not carry capability "
                + repr(capability)
            )

    def assert_tenant(self, org_id: str, repo_id: str) -> None:
        """Raise unless the token addresses exactly this allocation space."""
        if self.tenancy.org_id != org_id or self.tenancy.repo_id != repo_id:
            raise TokenError(
                "token is scoped to " + self.allocation_space_id
                + " and cannot address " + org_id + "/" + repo_id + " (V10-ALC-001)"
            )


class TokenBroker:
    """RS256 minting and verification for tenant session tokens."""

    def __init__(self, key_dir: Optional[Path] = None, issuer: str = ISSUER):
        self.key_dir = Path(key_dir) if key_dir else key_directory()
        self.issuer = issuer
        self._private = None
        self._public = None

    # -- key management ----------------------------------------------------

    @property
    def private_key_path(self) -> Path:
        return self.key_dir / "hubcore_broker_rs256.pem"

    @property
    def public_key_path(self) -> Path:
        return self.key_dir / "hubcore_broker_rs256.pub.pem"

    def ensure_keypair(self) -> Path:
        """Create the signing keypair if absent, with owner-only permissions."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        if self.private_key_path.exists():
            return self.private_key_path
        self.key_dir.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        self.private_key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        try:
            os.chmod(self.private_key_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        self.public_key_path.write_bytes(
            key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        return self.private_key_path

    def _load_private(self):
        from cryptography.hazmat.primitives import serialization

        if self._private is None:
            self.ensure_keypair()
            self._private = serialization.load_pem_private_key(
                self.private_key_path.read_bytes(), password=None
            )
        return self._private

    def _load_public(self):
        from cryptography.hazmat.primitives import serialization

        if self._public is None:
            self.ensure_keypair()
            self._public = serialization.load_pem_public_key(self.public_key_path.read_bytes())
        return self._public

    # -- minting -----------------------------------------------------------

    @staticmethod
    def _screen_capabilities(capabilities: Sequence[str]) -> frozenset:
        requested = frozenset(capabilities or ())
        forbidden = requested & FORBIDDEN_CAPABILITIES
        if forbidden:
            raise PolicyError(
                "capabilities " + ", ".join(sorted(forbidden)) + " can never be minted "
                "into a tenant token; release authority is not delegable (Part 5.1)"
            )
        unknown = requested - KNOWN_CAPABILITIES
        if unknown:
            raise PolicyError(
                "unknown capabilities " + ", ".join(sorted(unknown))
                + "; a token receives no undeclared capability"
            )
        return requested

    def mint(
        self,
        tenancy: TenancyTuple,
        subject: str,
        role: str = "Requester",
        capabilities: Sequence[str] = (),
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        """Mint a short-lived scoped token for one allocation space."""
        validate_id(subject, "subject")
        if role not in STANDARD_ROLES:
            raise PolicyError(
                "role " + repr(role) + " is not one of " + ", ".join(STANDARD_ROLES)
            )
        allowed = self._screen_capabilities(capabilities)
        ttl = int(ttl_seconds)
        if ttl <= 0 or ttl > MAX_TTL_SECONDS:
            raise PolicyError(
                "ttl " + str(ttl) + "s is outside the permitted 1.." + str(MAX_TTL_SECONDS)
                + "s window; tokens are short-lived by contract"
            )

        issued = int(time.time())
        header = {"alg": ALGORITHM, "typ": "JWT"}
        payload = {
            "iss": self.issuer,
            "sub": subject,
            "org_id": tenancy.org_id,
            "site_id": tenancy.site_id,
            "device_id": tenancy.device_id,
            "repo_id": tenancy.repo_id,
            "user_id": tenancy.user_id,
            "allocation_space_id": tenancy.allocation_space_id,
            "role": role,
            "allowed_caps": sorted(allowed),
            "iat": issued,
            "nbf": issued,
            "exp": issued + ttl,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
            + "."
            + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        ).encode("ascii")

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        signature = self._load_private().sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return signing_input.decode("ascii") + "." + _b64url(signature)

    # -- verification ------------------------------------------------------

    def verify(self, token: str, leeway_seconds: int = 30) -> TokenClaims:
        """Verify signature, issuer, and validity window; return the claims.

        Fails closed on every malformation. There is no unsigned or ``alg=none``
        acceptance path.
        """
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        if not isinstance(token, str) or token.count(".") != 2:
            raise TokenError("token is malformed")
        header_b64, payload_b64, signature_b64 = token.split(".")
        try:
            header = json.loads(_b64url_decode(header_b64))
            payload = json.loads(_b64url_decode(payload_b64))
            signature = _b64url_decode(signature_b64)
        except Exception as exc:
            raise TokenError("token is not decodable: " + str(exc)) from exc

        if header.get("alg") != ALGORITHM:
            raise TokenError(
                "token algorithm " + repr(header.get("alg")) + " is refused; only "
                + ALGORITHM + " is accepted"
            )

        signing_input = (header_b64 + "." + payload_b64).encode("ascii")
        try:
            self._load_public().verify(
                signature, signing_input, padding.PKCS1v15(), hashes.SHA256()
            )
        except InvalidSignature as exc:
            raise TokenError("token signature is invalid") from exc

        if payload.get("iss") != self.issuer:
            raise TokenError("token issuer " + repr(payload.get("iss")) + " is not trusted")

        now = int(time.time())
        if now + leeway_seconds < int(payload.get("nbf", 0)):
            raise TokenError("token is not yet valid")
        if now - leeway_seconds >= int(payload.get("exp", 0)):
            raise TokenError("token has expired")

        try:
            tenancy = TenancyTuple(
                org_id=payload["org_id"],
                site_id=payload["site_id"],
                device_id=payload["device_id"],
                repo_id=payload["repo_id"],
                user_id=payload["user_id"],
            )
        except KeyError as exc:
            raise TokenError("token is missing tenancy claim " + str(exc)) from exc

        caps = frozenset(payload.get("allowed_caps") or ())
        if caps & FORBIDDEN_CAPABILITIES:
            raise TokenError("token carries a forbidden capability and is refused")

        return TokenClaims(
            subject=str(payload.get("sub", "")),
            tenancy=tenancy,
            role=str(payload.get("role", "Viewer")),
            allowed_caps=caps,
            allocation_space_id=str(payload.get("allocation_space_id", "")),
            expires_at=int(payload["exp"]),
            raw=payload,
        )

    def public_jwk(self) -> Dict[str, Any]:
        """Publish the verification key so a client can validate offline."""
        from cryptography.hazmat.primitives.asymmetric import rsa

        public = self._load_public()
        if not isinstance(public, rsa.RSAPublicKey):
            raise TokenError("broker public key is not an RSA key")
        numbers = public.public_numbers()
        size = (public.key_size + 7) // 8
        return {
            "kty": "RSA",
            "alg": ALGORITHM,
            "use": "sig",
            "n": _b64url(numbers.n.to_bytes(size, "big")),
            "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }


def require_capabilities(claims: TokenClaims, capabilities: Iterable[str]) -> None:
    """Convenience guard for endpoint adapters."""
    for capability in capabilities:
        claims.require(capability)


def endpoint_manifest(workspace: Mapping[str, Any], claims: TokenClaims) -> Dict[str, Any]:
    """The endpoint list a verified client may see.

    Only loopback-safe descriptors are returned, and never a filesystem path,
    database location, or internal credential (Part 4.6 bridge status rules).
    """
    manifest = {}
    for name, endpoint in (workspace.get("endpoints") or {}).items():
        required_cap = {
            "visionCapture": "ocr.capture",
            "structuredMetrology": "metrology.touch",
            "observer": "observer.read",
        }.get(name)
        if required_cap and required_cap not in claims.allowed_caps:
            continue
        manifest[name] = {
            "id": endpoint.get("id"),
            "protocol": endpoint.get("protocol"),
            "port": endpoint.get("port"),
            "healthPath": endpoint.get("healthPath"),
            "axis": endpoint.get("axis"),
            "role": endpoint.get("role"),
        }
    return {
        "allocationSpaceId": claims.allocation_space_id,
        "expiresAt": claims.expires_at,
        "endpoints": manifest,
    }
