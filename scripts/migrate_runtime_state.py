"""Inspect, plan, apply, and verify offline ChronosHFT migrations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from migration.runtime_state import (
    apply_migration_plan,
    build_migration_plan,
    inspect_sources,
    verify_migration_receipt,
)


def _load(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _emit(value: dict, output: str = "") -> None:
    encoded = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if output:
        Path(output).write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sidecar-state")
    parser.add_argument("--journal")
    parser.add_argument("--paper-database")
    parser.add_argument("--config-manifest")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    _source_arguments(inspect_parser)
    inspect_parser.add_argument("--output", default="")

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--inspection", required=True)
    plan_parser.add_argument("--target-root", required=True)
    plan_parser.add_argument("--account-scope-id", default="")
    plan_parser.add_argument("--deployment-id", default="")
    plan_parser.add_argument("--cash-flow-history-complete", action="store_true")
    plan_parser.add_argument("--flat-proof-receipt")
    plan_parser.add_argument("--output", default="")

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--plan", required=True)
    apply_parser.add_argument("--plan-digest", required=True)
    apply_parser.add_argument("--backup-directory", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--receipt", required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        result = inspect_sources(
            sidecar_state=args.sidecar_state,
            journal=args.journal,
            paper_database=args.paper_database,
            config_manifest=args.config_manifest,
        )
        _emit(result, args.output)
        return 0
    if args.command == "plan":
        result = build_migration_plan(
            _load(args.inspection),
            target_root=args.target_root,
            account_scope_id=args.account_scope_id,
            deployment_id=args.deployment_id,
            cash_flow_history_complete=args.cash_flow_history_complete,
            flat_proof_receipt=args.flat_proof_receipt,
        )
        _emit(result, args.output)
        return 0
    if args.command == "apply":
        result = apply_migration_plan(
            _load(args.plan),
            expected_plan_sha256=args.plan_digest,
            backup_directory=args.backup_directory,
        )
        _emit(result)
        return 0
    result = verify_migration_receipt(_load(args.receipt))
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
