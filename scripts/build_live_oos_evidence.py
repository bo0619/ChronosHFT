"""Reconstruct GLFT/RPI OOS evidence from sealed local journals only."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.oos_reconstruction import (  # noqa: E402
    OOS_RECONSTRUCTION_SCHEMA,
    OOSReconstructionError,
    OOSReconstructionRequirements,
    load_raw_oos_evidence,
    reconstruct_oos_evidence,
)
from infrastructure.config_scaling import (  # noqa: E402
    normalize_root_config_preapproval,
)
from infrastructure.single_writer_fence import (  # noqa: E402
    SingleWriterFence,
)
from strategy.model_readiness import (  # noqa: E402
    deployment_config_sha256,
    implementation_sha256_for_model,
    oos_evidence_sha256,
)


OUTPUT_SCHEMA = "chronoshft.glft_rpi_oos_reconstruction_output.v1"


def _reject_json_constant(value: str):
    raise OOSReconstructionError(
        f"non-finite JSON constant is not allowed: {value}"
    )


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OOSReconstructionError(
                f"duplicate JSON key is not allowed: {key!r}"
            )
        result[key] = value
    return result


def _read_config(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            raw = json.load(
                handle,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_object_without_duplicate_keys,
            )
    except (
        OSError,
        json.JSONDecodeError,
        OOSReconstructionError,
    ) as exc:
        raise OOSReconstructionError(
            f"cannot read strict deployment config {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise OOSReconstructionError(
            "deployment config must be a JSON object"
        )
    try:
        config = normalize_root_config_preapproval(raw)
    except (TypeError, ValueError) as exc:
        raise OOSReconstructionError(
            f"deployment config normalization failed: {exc}"
        ) from exc

    execution = config.get("execution", {})
    paper = config.get("paper_trade", {})
    if (
        not isinstance(execution, Mapping)
        or str(execution.get("mode", "") or "").lower() != "live"
        or not isinstance(paper, Mapping)
        or paper.get("enabled") is not False
        or config.get("testnet") is not False
    ):
        raise OOSReconstructionError(
            "OOS reconstruction requires an explicit production Live config"
        )
    return config


def _resolve(config_path: Path, value: object, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise OOSReconstructionError(f"{field} must be configured")
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return candidate.resolve()


def _source_paths(
    config: Mapping,
    config_path: Path,
) -> tuple[Path, Path, tuple[Path, ...]]:
    system = config.get("system", {})
    system = system if isinstance(system, Mapping) else {}
    evidence = system.get("evidence_recorder", {})
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if evidence.get("enabled") is not True:
        raise OOSReconstructionError(
            "system.evidence_recorder.enabled must be true"
        )
    evidence_path = _resolve(
        config_path,
        evidence.get("path"),
        "system.evidence_recorder.path",
    )
    evidence_fence = evidence.get("single_writer_fence", {})
    evidence_fence = (
        evidence_fence if isinstance(evidence_fence, Mapping) else {}
    )
    if evidence_fence.get("enabled") is not True:
        raise OOSReconstructionError(
            "evidence recorder single-writer fence must be enabled"
        )
    evidence_fence_path = _resolve(
        config_path,
        evidence_fence.get("path") or f"{evidence.get('path')}.lock",
        "system.evidence_recorder.single_writer_fence.path",
    )

    oms = config.get("oms", {})
    oms = oms if isinstance(oms, Mapping) else {}
    oms_path = _resolve(
        config_path,
        oms.get("journal_path"),
        "oms.journal_path",
    )
    oms_fence = oms.get("single_writer_fence", {})
    oms_fence = oms_fence if isinstance(oms_fence, Mapping) else {}
    if oms_fence.get("enabled") is not True:
        raise OOSReconstructionError(
            "OMS single-writer fence must be enabled"
        )
    oms_fence_path = _resolve(
        config_path,
        oms_fence.get("path") or f"{oms.get('journal_path')}.lock",
        "oms.single_writer_fence.path",
    )

    source_paths = {evidence_path, oms_path}
    fence_paths = {evidence_fence_path, oms_fence_path}
    if len(source_paths) != 2 or len(fence_paths) != 2:
        raise OOSReconstructionError(
            "OMS and market evidence journals/fences must be disjoint"
        )
    if source_paths.intersection(fence_paths):
        raise OOSReconstructionError(
            "journal paths cannot also be writer-fence paths"
        )
    return evidence_path, oms_path, tuple(sorted(fence_paths))


def _acquire_fences(
    paths: Sequence[Path],
    *,
    deployment_id: str,
) -> list[SingleWriterFence]:
    acquired = []
    try:
        for path in paths:
            fence = SingleWriterFence(
                str(path),
                owner_metadata={
                    "component": "ChronosHFT.OOSReconstruction",
                    "deployment_id": deployment_id,
                },
            )
            fence.acquire()
            acquired.append(fence)
    except BaseException:
        for fence in reversed(acquired):
            fence.release()
        raise
    return acquired


def _release_fences(fences: Sequence[SingleWriterFence]) -> None:
    for fence in reversed(fences):
        fence.release()


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_json(
    path: Path,
    value: Mapping,
    *,
    overwrite: bool,
) -> None:
    if not overwrite and os.path.lexists(path):
        raise OOSReconstructionError(
            f"output already exists: {path}; pass --overwrite explicitly"
        )
    try:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
    except (TypeError, ValueError) as exc:
        raise OOSReconstructionError(
            "reconstructed OOS output is not strict JSON"
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary_path = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary_path, path)
        else:
            try:
                os.link(temporary_path, path)
            except FileExistsError as exc:
                raise OOSReconstructionError(
                    f"output already exists: {path}"
                ) from exc
            temporary_path.unlink()
        temporary_path = None
        _sync_directory(path.parent)
    except OOSReconstructionError:
        raise
    except OSError as exc:
        raise OOSReconstructionError(
            f"cannot atomically write OOS evidence {path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _positive_decimal(value: str, field: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OOSReconstructionError(
            f"{field} must be a positive decimal"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise OOSReconstructionError(
            f"{field} must be a positive decimal"
        )
    return parsed


def _parse_utc_argument(value: str, field: str) -> datetime:
    text = str(value or "").strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise OOSReconstructionError(
            f"{field} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OOSReconstructionError(
            f"{field} must include a UTC offset"
        )
    return parsed.astimezone(timezone.utc)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct maker/RPI truth, exact fees/funding, PnL, drawdown, "
            "and 1s/5s markout from sealed local journals. This tool performs "
            "no network, Gateway, OMS, or order operation."
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--training-ended-at", required=True)
    parser.add_argument("--oos-started-at", required=True)
    parser.add_argument("--oos-ended-at", required=True)
    parser.add_argument("--max-markout-lag-ms", type=int, default=2_000)
    parser.add_argument("--min-utc-day-clusters", type=int, default=5)
    parser.add_argument("--flat-tolerance", default="0.000000001")
    parser.add_argument(
        "--pnl-crosscheck-tolerance-usdt",
        default="0.000001",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly replace an existing output atomically",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    fences = []
    try:
        config_path = Path(args.config).resolve()
        output_path = Path(args.output).resolve()
        config = _read_config(config_path)
        live_launch = config.get("live_launch", {})
        live_launch = (
            live_launch if isinstance(live_launch, Mapping) else {}
        )
        deployment_id = str(
            live_launch.get("deployment_id", "") or ""
        ).strip()
        if not deployment_id:
            raise OOSReconstructionError(
                "live_launch.deployment_id is required"
            )
        symbols = tuple(
            str(symbol or "").strip().upper()
            for symbol in config.get("symbols", ())
        )
        if len(symbols) != 1 or not symbols[0]:
            raise OOSReconstructionError(
                "Live OOS reconstruction currently requires one symbol"
            )
        evidence_path, oms_path, fence_paths = _source_paths(
            config,
            config_path,
        )
        if output_path in {config_path, evidence_path, oms_path, *fence_paths}:
            raise OOSReconstructionError(
                "output must be distinct from config, journals, and fences"
            )
        config_digest = deployment_config_sha256(config)
        fences = _acquire_fences(
            fence_paths,
            deployment_id=deployment_id,
        )
        raw = load_raw_oos_evidence(
            oms_journal_path=oms_path,
            market_evidence_path=evidence_path,
            deployment_id=deployment_id,
            deployment_config_sha256=config_digest,
            symbols=symbols,
        )
        requirements = OOSReconstructionRequirements(
            max_markout_lag_ms=args.max_markout_lag_ms,
            min_utc_day_clusters=args.min_utc_day_clusters,
            flat_tolerance=_positive_decimal(
                args.flat_tolerance,
                "flat_tolerance",
            ),
            pnl_crosscheck_tolerance_usdt=_positive_decimal(
                args.pnl_crosscheck_tolerance_usdt,
                "pnl_crosscheck_tolerance_usdt",
            ),
        )
        oos = reconstruct_oos_evidence(
            raw,
            deployment_id=deployment_id,
            deployment_config_sha256=config_digest,
            symbols=symbols,
            training_ended_at=_parse_utc_argument(
                args.training_ended_at,
                "training-ended-at",
            ),
            started_at=_parse_utc_argument(
                args.oos_started_at,
                "oos-started-at",
            ),
            ended_at=_parse_utc_argument(
                args.oos_ended_at,
                "oos-ended-at",
            ),
            requirements=requirements,
        )
        document = {
            "schema": OUTPUT_SCHEMA,
            "reconstruction_schema": OOS_RECONSTRUCTION_SCHEMA,
            "deployment_id": deployment_id,
            "symbols": list(symbols),
            "deployment_config_sha256": config_digest,
            "implementation_sha256": implementation_sha256_for_model(
                "glft"
            ),
            "oos_evidence_sha256": oos_evidence_sha256(oos),
            "oos": oos,
        }
        _atomic_write_json(
            output_path,
            document,
            overwrite=args.overwrite,
        )
    except (
        OOSReconstructionError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    finally:
        _release_fences(fences)

    print(
        f"WROTE {output_path} "
        f"({symbols[0]}, {oos['sample_count']} marks, "
        f"{oos['fill_count']} fills)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
