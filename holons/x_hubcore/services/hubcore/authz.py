"""V10-SEC-001 - which routes require what, in one table.

Part 1.3 says every command in the HubCore zone is "authenticated, authorized,
schema-validated, and audited". Schema validation and audit held; authentication
did not. Of 67 served routes, 8 required a token. Release promotion, held-result
acceptance, the advisor Admin gate, and the v-ADMIN kill switch were all
reachable without one, and each took its actor from the request body -- so the
recorded actor was whatever the caller typed.

The policy lives here, as data, and is enforced once in the dispatcher. It was
previously expressed as a `self._claims()` call inside each handler, which is
precisely the shape of thing that gets forgotten: a new route is authenticated
only if its author remembered, and nothing fails when they did not. A missing
entry in this table is a startup error, not an open door.

Two rules the table encodes:

* **Least privilege by consequence.** A read of governed state needs a token; a
  reviewer decision needs Reviewer; anything that crosses a release gate, moves
  value, or overrides the synthetic-assistant boundary needs Admin.
* **The actor is the token's subject.** A route that records who did something
  reads it from the verified claims, never from the body. Otherwise the audit
  trail records an assertion rather than an identity.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from .errors import PolicyError, TokenError

#: Ordered privilege ladder. Index is the comparison, so a Reviewer satisfies a
#: Requester requirement and an Admin satisfies everything.
ROLE_LADDER = ("Viewer", "Requester", "ModuleAuthor", "WorkerOperator", "Reviewer", "Admin")

#: Roles a caller may not mint for itself without the operator credential.
PRIVILEGED_ROLES = frozenset({"Reviewer", "Admin"})

#: Sentinel for a route that is deliberately reachable without a token.
PUBLIC = "public"

#: Environment variable holding the operator credential. Presenting it at
#: session-mint time is what allows a privileged role to be issued.
OPERATOR_SECRET_ENV = "HUBCORE_OPERATOR_SECRET"
OPERATOR_HEADER = "X-HubCore-Operator"


def rank(role: str) -> int:
    try:
        return ROLE_LADDER.index(role)
    except ValueError:
        return -1


def satisfies(held: str, required: str) -> bool:
    """True when ``held`` is at least ``required`` on the ladder."""
    required_rank = rank(required)
    if required_rank < 0:
        return False
    return rank(held) >= required_rank


#: (method, path) -> minimum role, or PUBLIC.
#:
#: Public entries are deliberate and each has a reason: the application itself
#: and its liveness probes must answer before anyone has a token; the JWKS is a
#: public verification key; session minting is how a caller obtains a token at
#: all; the shared marketplace catalog and the scope routing table are public by
#: design (Part 12.6). Hub scope inside the catalog route is gated separately, by
#: tenancy, because "public" there depends on a query parameter.
ROUTE_POLICY: Dict[Tuple[str, str], str] = {
    # -- reachable before authentication ----------------------------------
    ("GET", "/"): PUBLIC,
    ("GET", "/index.html"): PUBLIC,
    ("GET", "/health"): PUBLIC,
    ("GET", "/health/live"): PUBLIC,
    ("GET", "/health/ready"): PUBLIC,
    ("GET", "/.well-known/jwks"): PUBLIC,
    ("POST", "/api/v1/session"): PUBLIC,
    ("GET", "/api/v1/marketplace/catalog"): PUBLIC,   # hub scope gated by tenancy
    ("GET", "/api/v1/marketplace/routing"): PUBLIC,

    # -- reads of governed state ------------------------------------------
    ("GET", "/api/v1/status"): "Requester",
    ("GET", "/api/v1/chains"): "Requester",
    ("GET", "/api/v1/modules"): "Requester",
    ("GET", "/api/v1/endpoints"): "Requester",
    ("GET", "/api/v1/mesh/axes"): "Requester",
    ("GET", "/api/v1/annealing"): "Requester",
    ("GET", "/api/v1/spatial"): "Requester",
    ("GET", "/api/v1/spatial/ground-truth"): "Requester",
    ("GET", "/api/v1/federation/mesh"): "Requester",
    ("GET", "/api/v1/expert/ledger"): "Requester",
    ("GET", "/api/v1/advisor/grades"): "Requester",
    ("GET", "/api/v1/advisor/quality"): "Requester",
    ("GET", "/api/v1/advisor/requests"): "Requester",
    ("GET", "/api/v1/advisor/request"): "Requester",
    ("GET", "/api/v1/tasks"): "Requester",
    ("GET", "/api/v1/tasks/audit"): "Requester",
    ("GET", "/api/v1/vadmin"): "Requester",
    ("GET", "/api/v1/vadmin/queue"): "Requester",
    ("GET", "/api/v1/hubs/children"): "Requester",
    ("GET", "/api/v1/hubs/children/tasks"): "Requester",
    ("GET", "/api/v1/marketplace/stats"): "Requester",
    ("GET", "/api/v1/marketplace/royalties"): "Requester",
    ("GET", "/api/v1/marketplace/lakehouse"): "Requester",
    ("GET", "/api/v1/marketplace/hub/catalog"): "Requester",   # plus tenancy
    ("GET", "/api/v1/marketplace/hub/stats"): "Requester",     # plus tenancy
    ("GET", "/api/v1/bus/status"): "Requester",
    ("GET", "/api/v1/directives"): "Requester",
    ("GET", "/api/v1/fleet"): "Reviewer",

    # -- writes a Requester may make --------------------------------------
    ("POST", "/api/v1/ingress"): "Requester",
    ("POST", "/api/v1/tasks"): "Requester",
    ("POST", "/api/v1/tasks/hold"): "Requester",
    ("POST", "/api/v1/advisor/request"): "Requester",
    ("POST", "/api/v1/advisor/query"): "Requester",
    ("POST", "/api/v1/advisor/followup"): "Requester",
    ("POST", "/api/v1/support/ticket"): "Requester",
    ("POST", "/api/v1/federation/gradient"): "Requester",
    ("POST", "/api/v1/hubs/children/register"): "WorkerOperator",
    ("POST", "/api/v1/hubs/children/proposals"): "WorkerOperator",
    ("POST", "/api/v1/bus/relay"): "WorkerOperator",
    ("POST", "/api/v1/directives/feedback"): "WorkerOperator",
    ("POST", "/api/v1/marketplace/gard/shard"): "Requester",
    ("POST", "/api/v1/marketplace/gard/verify"): "Requester",
    ("POST", "/api/v1/marketplace/hub/tokenize"): "Requester",   # plus tenancy
    ("POST", "/api/v1/marketplace/hub/transfer"): "Requester",   # plus tenancy
    ("POST", "/api/v1/marketplace/hub/sync"): "Requester",       # plus tenancy
    ("POST", "/api/v1/spatial/stockopoly-slot"): "Requester",

    # -- reviewer decisions ------------------------------------------------
    ("POST", "/api/v1/tasks/accept"): "Reviewer",
    ("POST", "/api/v1/tasks/reject"): "Reviewer",
    ("POST", "/api/v1/advisor/review"): "Reviewer",
    ("POST", "/api/v1/advisor/grade-override"): "Reviewer",
    ("POST", "/api/v1/advisor/send"): "Reviewer",
    ("POST", "/api/v1/advisor/close"): "Reviewer",
    ("POST", "/api/v1/hubs/children/session"): "Reviewer",
    ("POST", "/api/v1/annealing/drain"): "Reviewer",
    ("POST", "/api/v1/spatial/ground"): "Reviewer",
    ("POST", "/api/v1/dataplane/watermark"): "Reviewer",

    # -- r-ADMIN only: release gates, value, and the synthetic boundary ----
    ("POST", "/api/v1/release/promote"): "Admin",
    ("POST", "/api/v1/advisor/admin-review"): "Admin",
    ("POST", "/api/v1/vadmin/kill-switch"): "Admin",
    ("POST", "/api/v1/vadmin/queue/resolve"): "Admin",
    ("POST", "/api/v1/vadmin/learning-proposal"): "Admin",
    ("POST", "/api/v1/marketplace/tokenize"): "Admin",
    ("POST", "/api/v1/marketplace/trade"): "Admin",
    ("POST", "/api/v1/marketplace/web3/mint"): "Admin",
}


def policy_for(method: str, path: str) -> Optional[str]:
    """The minimum role for a route, or ``None`` when the route is undeclared."""
    return ROUTE_POLICY.get((method.upper(), path))


def assert_policy_covers(routes) -> None:
    """Fail at startup if a served route has no policy entry.

    An undeclared route is the failure this module exists to prevent: it would
    otherwise be reachable with whatever the dispatcher happens to do by default,
    and nobody would find out until someone went looking.
    """
    missing = [f"{method} {path}" for method, path in routes if (method, path) not in ROUTE_POLICY]
    if missing:
        raise PolicyError(
            "these routes are served but have no authorization policy, so their "
            "access level is undefined (V10-SEC-001): " + ", ".join(sorted(missing))
        )


def assert_role(claims, required: str, method: str, path: str) -> None:
    """Raise unless the verified claims satisfy the route's minimum role."""
    if not satisfies(claims.role, required):
        raise PolicyError(
            "role " + repr(claims.role) + " cannot " + method + " " + path
            + "; this route requires " + required + " or higher (V10-SEC-001)"
        )


def assert_may_mint(requested_role: str, presented_secret: str, configured_secret: str) -> None:
    """Gate privileged role minting behind the operator credential.

    Requiring a token everywhere achieves nothing if any caller can mint itself
    an Admin one. A `Requester` token is self-service by design -- that is the
    accountability tier, carrying a subject, a tenancy, and an expiry. Reviewer
    and Admin are decisions about authority and need the operator credential the
    node was started with.
    """
    if requested_role not in PRIVILEGED_ROLES:
        return
    if not configured_secret:
        raise PolicyError(
            "role " + repr(requested_role) + " cannot be issued: "
            + OPERATOR_SECRET_ENV + " is not configured on this node, so there is "
            "nothing to check the request against. Start HubCore with it set"
        )
    import hmac

    if not hmac.compare_digest(presented_secret or "", configured_secret):
        raise TokenError(
            "role " + repr(requested_role) + " requires the operator credential in "
            "the " + OPERATOR_HEADER + " header; a caller cannot grant itself "
            "reviewer or administrator authority (V10-SEC-001)"
        )
