"""HubCore v10 operator CLI.

    python -m services.hubcore.cli doctor
    python -m services.hubcore.cli hal
    python -m services.hubcore.cli health [--strict]
    python -m services.hubcore.cli audit-bindings
    python -m services.hubcore.cli tenant provision --org ORG --repo REPO [...]
    python -m services.hubcore.cli tenant list
    python -m services.hubcore.cli token mint --org ORG --repo REPO --subject SUB [...]
    python -m services.hubcore.cli token verify --token TOKEN
    python -m services.hubcore.cli anneal export-state --output PACK
    python -m services.hubcore.cli anneal import-state --input PACK
    python -m services.hubcore.cli gate screen --file ARTIFACT.json --kind KIND

Every subcommand prints JSON on success and exits non-zero with a policy reason
on refusal, so it composes into CI gates and PowerShell health checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, airlock, allocations, config, endpoints, hal, statepack
from .errors import HubCoreError


def _emit(payload: Any) -> int:
    print(json.dumps(payload, indent=2, default=str))
    return 0


def _tenancy(args: argparse.Namespace, workspace: Dict[str, Any]) -> allocations.TenancyTuple:
    return allocations.TenancyTuple(
        org_id=allocations.validate_id(args.org, "org_id"),
        site_id=allocations.validate_id(args.site or workspace.get("siteId", "site-default"), "site_id"),
        device_id=allocations.validate_id(
            args.device or workspace.get("deviceId", "dev-default"), "device_id"
        ),
        repo_id=allocations.slugify_repo(args.repo),
        user_id=allocations.validate_id(args.user, "user_id"),
    )


def _engine(workspace: Dict[str, Any]) -> allocations.AllocationEngine:
    return allocations.AllocationEngine(config.library_root(workspace), workspace)


# -- commands --------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """One-shot posture report: configuration, hardware, fleet, bindings."""
    documents = config.load_all()
    workspace = documents["workspace"]
    profile = hal.probe(workspace)
    fleet = endpoints.status_report(workspace, timeout=args.timeout)
    engine = _engine(workspace)
    return _emit(
        {
            "hubcoreVersion": __version__,
            "spec": config.SCHEMA_VERSION,
            "org": {
                "orgId": documents["org"]["orgId"],
                "orgName": documents["org"].get("orgName"),
                "hubName": documents["org"].get("hubName"),
                # A placeholder here means setup never finished and the Hub UI
                # would show '{{hubName}}' where a name belongs.
                "unresolvedIdentity": config.unresolved_tokens(documents["org"]),
                "chains": len(documents["org"]["chains"]),
                "federationEnabled": documents["org"].get("federation", {}).get("enabled", False),
                "consentTier": documents["org"].get("federation", {}).get("consentTier", 0),
            },
            "modules": len(documents["modules"]["modules"]),
            "hardware": profile.to_dict(),
            "fleet": fleet,
            "allocations": {
                "root": str(engine.allocations_root),
                "tenants": list(engine.list_tenants()),
            },
        }
    )


def cmd_hal(args: argparse.Namespace) -> int:
    workspace = config.load("workspace")
    return _emit(hal.probe(workspace).to_dict())


def cmd_health(args: argparse.Namespace) -> int:
    workspace = config.load("workspace")
    if args.strict:
        endpoints.assert_startup_health(workspace, timeout=args.timeout)
    return _emit(endpoints.status_report(workspace, timeout=args.timeout))


def cmd_audit_bindings(args: argparse.Namespace) -> int:
    workspace = config.load("workspace")
    findings = endpoints.audit_bindings(workspace)
    _emit({"requirement": "V10-END-001", "findings": findings, "compliant": not findings})
    return 1 if findings and args.strict else 0


def cmd_tenant_provision(args: argparse.Namespace) -> int:
    workspace = config.load("workspace")
    tenancy = _tenancy(args, workspace)
    engine = _engine(workspace)
    record = engine.provision(tenancy, quota_mb=args.quota_mb)
    record["usageMb"] = engine.usage_mb(tenancy)
    return _emit(record)


def cmd_tenant_list(args: argparse.Namespace) -> int:
    workspace = config.load("workspace")
    return _emit({"tenants": list(_engine(workspace).list_tenants())})


def cmd_token_mint(args: argparse.Namespace) -> int:
    from .token_broker import TokenBroker, endpoint_manifest

    workspace = config.load("workspace")
    tenancy = _tenancy(args, workspace)
    engine = _engine(workspace)
    # V10-ALC-002: no token is minted before the allocation space exists.
    engine.provision(tenancy)
    broker = TokenBroker()
    token = broker.mint(
        tenancy,
        subject=args.subject,
        role=args.role,
        capabilities=args.cap or [],
        ttl_seconds=args.ttl,
    )
    claims = broker.verify(token)
    return _emit({"token": token, "manifest": endpoint_manifest(workspace, claims)})


def cmd_token_verify(args: argparse.Namespace) -> int:
    from .token_broker import TokenBroker

    claims = TokenBroker().verify(args.token)
    return _emit(
        {
            "subject": claims.subject,
            "tenancy": claims.tenancy.to_dict(),
            "role": claims.role,
            "allowedCaps": sorted(claims.allowed_caps),
            "allocationSpaceId": claims.allocation_space_id,
            "expiresAt": claims.expires_at,
        }
    )


def _state_directory(workspace: Dict[str, Any]) -> Path:
    relative = workspace.get("selfAnnealing", {}).get("stateDirectory", "Data/hub/self_annealing")
    return config.library_root(workspace) / relative


def cmd_anneal_export(args: argparse.Namespace) -> int:
    workspace = config.load("workspace")
    signer = None
    if args.sign:
        from .token_broker import TokenBroker

        signer = TokenBroker()._load_private()  # noqa: SLF001 - internal by design
    return _emit(statepack.export_state(_state_directory(workspace), args.output, signer=signer))


def cmd_anneal_import(args: argparse.Namespace) -> int:
    workspace = config.load("workspace")
    return _emit(statepack.import_state(args.input, _state_directory(workspace)))


def cmd_anneal_inspect(args: argparse.Namespace) -> int:
    return _emit(statepack.inspect(args.input))


def cmd_gate_screen(args: argparse.Namespace) -> int:
    org = config.load("org")
    tier = int(args.tier if args.tier is not None else org.get("federation", {}).get("consentTier", 0))
    artifact = json.loads(Path(args.file).read_text(encoding="utf-8"))
    gate = airlock.ContaminationGate(
        field_whitelist=args.allow or None,
        forbidden_strings=airlock.forbidden_strings_from_env(),
    )
    report = gate.screen(artifact, args.kind, tier)
    _emit(report.to_dict())
    return 0 if report.passed else 1


def cmd_generate_hub(args: argparse.Namespace) -> int:
    from . import generator
    res = generator.compile_and_stage_hub()
    return _emit(res)


def cmd_setup(args: argparse.Namespace) -> int:
    from . import wizard
    wiz = wizard.SetupWizard()
    step_inputs = json.loads(args.inputs) if args.inputs else {}
    res = wiz.run_step(args.step, step_inputs)
    return _emit(res)


def cmd_modules_list(args: argparse.Namespace) -> int:
    from . import modules
    reg = modules.ModuleRegistry()
    return _emit({"modules": [m.to_dict() for m in reg.list_all()], "byChain": reg.by_chain()})


def cmd_toolforge_scaffold(args: argparse.Namespace) -> int:
    from . import toolforge
    tf = toolforge.ToolForge()
    res = tf.scaffold(
        module_id=args.id,
        name=args.name,
        chain=args.chain,
        version=args.version or "1.0.0",
        description=args.description or "",
    )
    return _emit(res)


def cmd_promote(args: argparse.Namespace) -> int:
    from . import releaser
    engine = releaser.ReleaseEngine()
    res = engine.promote(
        artifact_rel_path=args.artifact,
        actor_id=args.actor or "releaser",
        notes=args.notes or "",
    )
    return _emit(res)


def cmd_advisor_query(args: argparse.Namespace) -> int:
    from . import advisor
    pipe = advisor.AdvisorPipeline()
    res = pipe.query(args.query, user_id=args.user or "cli-user")
    return _emit(res.to_dict())


def cmd_migrate_supabase(args: argparse.Namespace) -> int:
    from . import dataplane
    engine = dataplane.DataPlaneEngine()
    res = engine.migrate_supabase_to_native()
    return _emit(res)


def cmd_federation_status(args: argparse.Namespace) -> int:
    from . import federation
    coord = federation.FederationCoordinator()
    return _emit(coord.status())


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hubcore", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version="hubcore " + __version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="full posture report")
    doctor.add_argument("--timeout", type=float, default=endpoints.DEFAULT_TIMEOUT)
    doctor.set_defaults(func=cmd_doctor)

    hal_cmd = subparsers.add_parser("hal", help="probe hardware and show the Conductor tier")
    hal_cmd.set_defaults(func=cmd_hal)

    health = subparsers.add_parser("health", help="probe the endpoint fleet")
    health.add_argument("--strict", action="store_true", help="exit non-zero if required endpoints are down")
    health.add_argument("--timeout", type=float, default=endpoints.DEFAULT_TIMEOUT)
    health.set_defaults(func=cmd_health)

    audit = subparsers.add_parser("audit-bindings", help="check V10-END-001 loopback binding")
    audit.add_argument("--strict", action="store_true")
    audit.set_defaults(func=cmd_audit_bindings)

    gen_hub = subparsers.add_parser("generate-hub", help="compile and stage Hub_v10.html")
    gen_hub.set_defaults(func=cmd_generate_hub)

    setup_cmd = subparsers.add_parser("setup", help="run setup wizard step")
    setup_cmd.add_argument("--step", type=int, required=True)
    setup_cmd.add_argument("--inputs", default="{}")
    setup_cmd.set_defaults(func=cmd_setup)

    mod_cmd = subparsers.add_parser("module", help="module registry and toolforge")
    mod_sub = mod_cmd.add_subparsers(dest="mod_command", required=True)
    mod_list = mod_sub.add_parser("list", help="list registered modules")
    mod_list.set_defaults(func=cmd_modules_list)
    mod_scaffold = mod_sub.add_parser("scaffold", help="scaffold a new module in UAT")
    mod_scaffold.add_argument("--id", required=True)
    mod_scaffold.add_argument("--name", required=True)
    mod_scaffold.add_argument("--chain", required=True)
    mod_scaffold.add_argument("--version", default="1.0.0")
    mod_scaffold.add_argument("--description", default="")
    mod_scaffold.set_defaults(func=cmd_toolforge_scaffold)

    promote_cmd = subparsers.add_parser("promote", help="promote UAT candidate to PROD")
    promote_cmd.add_argument("--artifact", required=True)
    promote_cmd.add_argument("--actor", default="releaser")
    promote_cmd.add_argument("--notes", default="")
    promote_cmd.set_defaults(func=cmd_promote)

    advisor_cmd = subparsers.add_parser("advisor", help="query Ask Advisor pipeline")
    advisor_cmd.add_argument("--query", required=True)
    advisor_cmd.add_argument("--user", default="cli-user")
    advisor_cmd.set_defaults(func=cmd_advisor_query)

    mig_cmd = subparsers.add_parser("migrate-supabase", help="migrate data plane to native WAL + DuckDB")
    mig_cmd.set_defaults(func=cmd_migrate_supabase)

    fed_cmd = subparsers.add_parser("federate", help="federation coordinator status")
    fed_cmd.set_defaults(func=cmd_federation_status)

    tenant = subparsers.add_parser("tenant", help="allocation space management")
    tenant_sub = tenant.add_subparsers(dest="tenant_command", required=True)

    provision = tenant_sub.add_parser("provision", help="create or refresh an allocation space")
    _add_tenancy_args(provision)
    provision.add_argument("--quota-mb", type=int, default=None)
    provision.set_defaults(func=cmd_tenant_provision)

    tenant_list = tenant_sub.add_parser("list", help="list provisioned allocation spaces")
    tenant_list.set_defaults(func=cmd_tenant_list)

    token = subparsers.add_parser("token", help="scoped token broker")
    token_sub = token.add_subparsers(dest="token_command", required=True)

    mint = token_sub.add_parser("mint", help="mint a short-lived scoped tenant token")
    _add_tenancy_args(mint)
    mint.add_argument("--subject", required=True)
    mint.add_argument("--role", default="Requester")
    mint.add_argument("--cap", action="append", help="repeatable capability grant")
    mint.add_argument("--ttl", type=int, default=900)
    mint.set_defaults(func=cmd_token_mint)

    verify = token_sub.add_parser("verify", help="verify a token and show its claims")
    verify.add_argument("--token", required=True)
    verify.set_defaults(func=cmd_token_verify)

    anneal = subparsers.add_parser("anneal", help="self-annealing state packs")
    anneal_sub = anneal.add_subparsers(dest="anneal_command", required=True)

    export = anneal_sub.add_parser("export-state", help="freeze and package annealing state")
    export.add_argument("--output", required=True)
    export.add_argument("--sign", action="store_true", help="sign the manifest with the broker key")
    export.set_defaults(func=cmd_anneal_export)

    restore = anneal_sub.add_parser("import-state", help="verify and restore a state pack")
    restore.add_argument("--input", required=True)
    restore.set_defaults(func=cmd_anneal_import)

    inspect_cmd = anneal_sub.add_parser("inspect", help="read a pack manifest")
    inspect_cmd.add_argument("--input", required=True)
    inspect_cmd.set_defaults(func=cmd_anneal_inspect)

    gate = subparsers.add_parser("gate", help="ContaminationGate")
    gate_sub = gate.add_subparsers(dest="gate_command", required=True)
    screen = gate_sub.add_parser("screen", help="screen an artifact for egress")
    screen.add_argument("--file", required=True)
    screen.add_argument("--kind", required=True)
    screen.add_argument("--tier", type=int, default=None)
    screen.add_argument("--allow", action="append", help="repeatable whitelisted field name")
    screen.set_defaults(func=cmd_gate_screen)

    return parser


def _add_tenancy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--org", required=True, help="orgId")
    parser.add_argument("--repo", required=True, help="repository reference, e.g. loadopoly/VS-Code")
    parser.add_argument("--user", default="user-default", help="userId")
    parser.add_argument("--site", default=None, help="siteId (defaults to the workspace siteId)")
    parser.add_argument("--device", default=None, help="deviceId (defaults to the workspace deviceId)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except HubCoreError as exc:
        print(
            json.dumps({"error": type(exc).__name__, "code": exc.code, "detail": str(exc)}, indent=2),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
