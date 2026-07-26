"""Collect fail-closed Binance mainnet evidence without any write endpoint.

Run this script only on the legally permitted deployment host. The production
transport is hard-bound to Binance's official mainnet hosts and a fixed set of
GET endpoints. Tests inject a fake transport and make no network requests.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlencode


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from infrastructure.live_config_guard import (  # noqa: E402
    LIVE_CANARY_ACCOUNT_SOURCE,
    LIVE_CANARY_API_RESTRICTIONS_SOURCE,
    LIVE_CANARY_EVIDENCE_SCHEMA,
    LIVE_CANARY_OPEN_ORDERS_SOURCE,
    LIVE_CANARY_POSITION_MODE_SOURCE,
    LIVE_CANARY_POSITION_RISK_SOURCE,
    LIVE_CANARY_RPI_COMMISSION_SOURCE,
    LIVE_CANARY_RPI_EXCHANGE_INFO_SOURCE,
    resolve_live_canary_evidence_path,
    sign_live_canary_evidence,
    validate_live_api_restrictions_evidence,
    validate_live_canary_evidence_payload,
    validate_live_canary_evidence_destination,
    validate_live_dual_key_account_evidence,
    validate_live_flat_start_evidence,
    validate_live_symbol_configuration_evidence,
)


FUTURES_HOST = "fapi.binance.com"
ACCOUNT_HOST = "api.binance.com"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
RECV_WINDOW_MS = 5000
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,127}$")

EP_SERVER_TIME = "/fapi/v1/time"
EP_EXCHANGE_INFO = "/fapi/v1/exchangeInfo"
EP_COMMISSION_RATE = "/fapi/v1/commissionRate"
EP_ACCOUNT = "/fapi/v3/account"
EP_API_RESTRICTIONS = "/sapi/v1/account/apiRestrictions"
EP_OPEN_ORDERS = "/fapi/v1/openOrders"
EP_POSITION_RISK = "/fapi/v2/positionRisk"
EP_POSITION_MODE = "/fapi/v1/positionSide/dual"

_ENDPOINT_HOSTS = {
    EP_SERVER_TIME: FUTURES_HOST,
    EP_EXCHANGE_INFO: FUTURES_HOST,
    EP_COMMISSION_RATE: FUTURES_HOST,
    EP_ACCOUNT: FUTURES_HOST,
    EP_API_RESTRICTIONS: ACCOUNT_HOST,
    EP_OPEN_ORDERS: FUTURES_HOST,
    EP_POSITION_RISK: FUTURES_HOST,
    EP_POSITION_MODE: FUTURES_HOST,
}
_SIGNED_ENDPOINTS = frozenset(
    {
        EP_COMMISSION_RATE,
        EP_ACCOUNT,
        EP_API_RESTRICTIONS,
        EP_OPEN_ORDERS,
        EP_POSITION_RISK,
        EP_POSITION_MODE,
    }
)

_CREDENTIAL_ENV_PATHS = (
    ("api_key_env",),
    ("api_secret_env",),
    ("risk", "independent_supervisor", "api_key_env"),
    ("risk", "independent_supervisor", "api_secret_env"),
)


class ReadOnlyTransport(Protocol):
    def get_json(
        self,
        endpoint: str,
        *,
        params: Sequence[tuple[str, str]] = (),
        api_key: str | None = None,
    ) -> object: ...


class BinanceMainnetReadOnlyTransport:
    """HTTPS transport with no configurable host, method, or endpoint list."""

    def __init__(self, *, timeout_sec: float = 8.0) -> None:
        if not math.isfinite(timeout_sec) or timeout_sec <= 0 or timeout_sec > 30:
            raise ValueError("timeout_sec must be greater than 0 and at most 30")
        self._timeout_sec = float(timeout_sec)

    def get_json(
        self,
        endpoint: str,
        *,
        params: Sequence[tuple[str, str]] = (),
        api_key: str | None = None,
    ) -> object:
        host = _ENDPOINT_HOSTS.get(endpoint)
        if host is None:
            raise ValueError("endpoint is not in the read-only allowlist")
        query = urlencode(tuple(params))
        target = endpoint if not query else f"{endpoint}?{query}"
        headers = {"Accept": "application/json", "User-Agent": "ChronosHFT/1"}
        if api_key:
            headers["X-MBX-APIKEY"] = api_key

        connection = http.client.HTTPSConnection(
            host,
            port=443,
            timeout=self._timeout_sec,
        )
        try:
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Binance response exceeded the size limit")
            if response.status != 200:
                raise RuntimeError(
                    f"Binance GET {endpoint} returned HTTP {response.status}"
                )
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(f"Binance GET {endpoint} failed") from exc
        finally:
            connection.close()
        try:
            return json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"Binance GET {endpoint} returned invalid JSON"
            ) from exc


@dataclass(frozen=True)
class Credentials:
    primary_key: str
    primary_secret: str
    supervisor_key: str
    supervisor_secret: str


@dataclass(frozen=True)
class OperatorAttestations:
    legal_access: bool
    single_process: bool
    same_futures_account: bool
    supervisor_emergency_permissions: bool
    legacy_state_archived: bool
    fresh_state_generation: bool


class ServerClock:
    def __init__(self, server_time_ms: int) -> None:
        if server_time_ms <= 0:
            raise ValueError("Binance server time must be positive")
        self._server_time_ms = server_time_ms
        self._started_ns = time.monotonic_ns()

    def now_ms(self) -> int:
        elapsed_ms = (time.monotonic_ns() - self._started_ns) // 1_000_000
        return self._server_time_ms + elapsed_ms

    def now_utc_text(self) -> str:
        value = datetime.fromtimestamp(self.now_ms() / 1000, timezone.utc)
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ReadOnlyBinanceClient:
    def __init__(
        self,
        transport: ReadOnlyTransport,
        credentials: Credentials,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        raw_time = transport.get_json(EP_SERVER_TIME)
        if not isinstance(raw_time, Mapping):
            raise ValueError("GET /fapi/v1/time must return an object")
        try:
            server_time_ms = int(raw_time.get("serverTime"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Binance serverTime must be an integer") from exc
        self.clock = ServerClock(server_time_ms)

    def public_get(self, endpoint: str) -> object:
        if endpoint in _SIGNED_ENDPOINTS:
            raise ValueError("signed endpoint requires explicit credentials")
        return self._transport.get_json(endpoint)

    def signed_get(
        self,
        endpoint: str,
        *,
        owner: str,
        params: Sequence[tuple[str, str]] = (),
    ) -> object:
        if endpoint not in _SIGNED_ENDPOINTS:
            raise ValueError("endpoint is not in the signed GET allowlist")
        if owner == "primary":
            api_key = self._credentials.primary_key
            api_secret = self._credentials.primary_secret
        elif owner == "supervisor":
            api_key = self._credentials.supervisor_key
            api_secret = self._credentials.supervisor_secret
        else:
            raise ValueError("credential owner must be primary or supervisor")
        signed_params = [
            *params,
            ("recvWindow", str(RECV_WINDOW_MS)),
            ("timestamp", str(self.clock.now_ms())),
        ]
        canonical = urlencode(tuple(signed_params))
        signature = hmac.new(
            api_secret.encode("utf-8"),
            canonical.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        signed_params.append(("signature", signature))
        return self._transport.get_json(
            endpoint,
            params=signed_params,
            api_key=api_key,
        )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number {value!r} is not allowed")


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=_reject_json_constant)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _nested(config: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = config
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def load_credentials(
    config: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> Credentials:
    source = os.environ if environ is None else environ
    env_names: list[str] = []
    values: list[str] = []
    for path in _CREDENTIAL_ENV_PATHS:
        name = str(_nested(config, path) or "").strip()
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(
                f"{'.'.join(path)} must contain a valid environment variable name"
            )
        value = str(source.get(name, "") or "")
        if not value:
            raise ValueError(f"required credential environment variable {name} is empty")
        env_names.append(name)
        values.append(value)
    if len(set(env_names)) != 4:
        raise ValueError("the four credential environment names must be distinct")
    primary_key, primary_secret, supervisor_key, supervisor_secret = values
    if hmac.compare_digest(primary_key, supervisor_key):
        raise ValueError("primary and supervisor API keys must be distinct")
    return Credentials(
        primary_key=primary_key,
        primary_secret=primary_secret,
        supervisor_key=supervisor_key,
        supervisor_secret=supervisor_secret,
    )


def _require_mapping(value: object, endpoint: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"GET {endpoint} must return an object")
    return value


def _require_list(value: object, endpoint: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"GET {endpoint} must return a list")
    return value


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _account_truth(
    response: Mapping[str, Any],
    *,
    captured_at: str,
    fingerprint: str,
) -> dict[str, Any]:
    return {
        "source": LIVE_CANARY_ACCOUNT_SOURCE,
        "captured_at_utc": captured_at,
        "api_key_fingerprint_sha256": fingerprint,
        "asset": "USDT",
        "canTrade": response.get("canTrade"),
        "multiAssetsMargin": response.get("multiAssetsMargin"),
        "totalWalletBalance": response.get("totalWalletBalance"),
        "totalMarginBalance": response.get("totalMarginBalance"),
        "availableBalance": response.get("availableBalance"),
    }


def _restriction_truth(
    response: Mapping[str, Any],
    *,
    captured_at: str,
    fingerprint: str,
) -> dict[str, Any]:
    return {
        "source": LIVE_CANARY_API_RESTRICTIONS_SOURCE,
        "captured_at_utc": captured_at,
        "api_key_fingerprint_sha256": fingerprint,
        "enableReading": response.get("enableReading"),
        "enableFutures": response.get("enableFutures"),
        "enableWithdrawals": response.get("enableWithdrawals"),
        "ipRestrict": response.get("ipRestrict"),
    }


def _exchange_symbol(
    exchange_info: Mapping[str, Any],
    symbol: str,
) -> Mapping[str, Any]:
    symbols = exchange_info.get("symbols")
    if not isinstance(symbols, list):
        raise ValueError("GET /fapi/v1/exchangeInfo has no symbols list")
    matches = [
        row
        for row in symbols
        if isinstance(row, Mapping)
        and str(row.get("symbol", "") or "").strip().upper() == symbol
    ]
    if len(matches) != 1:
        raise ValueError("exchangeInfo must contain the configured symbol exactly once")
    return matches[0]


def _configured_symbol(config: Mapping[str, Any]) -> str:
    symbols = config.get("symbols")
    if not isinstance(symbols, list) or len(symbols) != 1:
        raise ValueError("collector requires exactly one configured symbol")
    symbol = str(symbols[0] or "").strip().upper()
    if not symbol:
        raise ValueError("configured symbol must not be empty")
    return symbol


def _all_attestations(
    operator: OperatorAttestations,
    *,
    primary_restrictions: Mapping[str, Any],
    supervisor_restrictions: Mapping[str, Any],
) -> dict[str, bool]:
    return {
        "deployment_host_can_reach_binance_mainnet": True,
        "operator_confirmed_exchange_access_allowed": operator.legal_access,
        "single_host_single_process_deployment_confirmed": operator.single_process,
        "primary_api_withdrawals_disabled": (
            primary_restrictions.get("enableWithdrawals") is False
        ),
        "supervisor_api_withdrawals_disabled": (
            supervisor_restrictions.get("enableWithdrawals") is False
        ),
        "primary_api_ip_restricted": primary_restrictions.get("ipRestrict") is True,
        "supervisor_api_ip_restricted": (
            supervisor_restrictions.get("ipRestrict") is True
        ),
        "api_keys_are_distinct": True,
        "api_keys_same_futures_account_confirmed": operator.same_futures_account,
        "primary_api_futures_trading_enabled": (
            primary_restrictions.get("enableFutures") is True
        ),
        "supervisor_api_futures_trading_enabled": (
            supervisor_restrictions.get("enableFutures") is True
        ),
        "supervisor_api_emergency_permissions_confirmed": (
            operator.supervisor_emergency_permissions
        ),
        "credential_environment_populated_on_deployment_host": True,
        "exchange_open_orders_empty": True,
        "exchange_positions_flat": True,
        "legacy_state_archived": operator.legacy_state_archived,
        "fresh_state_generation_selected": operator.fresh_state_generation,
        "isolated_margin_confirmed": True,
        "leverage_confirmed": True,
        "rpi_account_permission_confirmed": True,
    }


def collect_evidence(
    config: Mapping[str, Any],
    credentials: Credentials,
    operator: OperatorAttestations,
    *,
    transport: ReadOnlyTransport,
) -> dict[str, Any]:
    if not all(
        (
            operator.legal_access,
            operator.single_process,
            operator.same_futures_account,
            operator.supervisor_emergency_permissions,
            operator.legacy_state_archived,
            operator.fresh_state_generation,
        )
    ):
        raise ValueError("all six explicit operator attestations are required")

    symbol = _configured_symbol(config)
    client = ReadOnlyBinanceClient(transport, credentials)
    exchange_info = _require_mapping(
        client.public_get(EP_EXCHANGE_INFO),
        EP_EXCHANGE_INFO,
    )
    exchange_symbol = dict(_exchange_symbol(exchange_info, symbol))
    commission = _require_mapping(
        client.signed_get(
            EP_COMMISSION_RATE,
            owner="primary",
            params=(("symbol", symbol),),
        ),
        EP_COMMISSION_RATE,
    )
    primary_account = _require_mapping(
        client.signed_get(EP_ACCOUNT, owner="primary"),
        EP_ACCOUNT,
    )
    supervisor_account = _require_mapping(
        client.signed_get(EP_ACCOUNT, owner="supervisor"),
        EP_ACCOUNT,
    )
    primary_restrictions_raw = _require_mapping(
        client.signed_get(EP_API_RESTRICTIONS, owner="primary"),
        EP_API_RESTRICTIONS,
    )
    supervisor_restrictions_raw = _require_mapping(
        client.signed_get(EP_API_RESTRICTIONS, owner="supervisor"),
        EP_API_RESTRICTIONS,
    )
    open_orders = _require_list(
        client.signed_get(EP_OPEN_ORDERS, owner="primary"),
        EP_OPEN_ORDERS,
    )
    positions = _require_list(
        client.signed_get(EP_POSITION_RISK, owner="primary"),
        EP_POSITION_RISK,
    )
    position_mode = _require_mapping(
        client.signed_get(EP_POSITION_MODE, owner="primary"),
        EP_POSITION_MODE,
    )
    captured_at = client.clock.now_utc_text()
    primary_fingerprint = _fingerprint(credentials.primary_key)
    supervisor_fingerprint = _fingerprint(credentials.supervisor_key)

    primary_restrictions = _restriction_truth(
        primary_restrictions_raw,
        captured_at=captured_at,
        fingerprint=primary_fingerprint,
    )
    supervisor_restrictions = _restriction_truth(
        supervisor_restrictions_raw,
        captured_at=captured_at,
        fingerprint=supervisor_fingerprint,
    )
    symbol_rows = [
        dict(row)
        for row in positions
        if isinstance(row, Mapping)
        and str(row.get("symbol", "") or "").strip().upper() == symbol
    ]
    live_launch = config.get("live_launch")
    if not isinstance(live_launch, Mapping):
        raise ValueError("live_launch must be an object")
    evidence: dict[str, Any] = {
        "schema": LIVE_CANARY_EVIDENCE_SCHEMA,
        "deployment_id": str(live_launch.get("deployment_id", "") or "").strip(),
        "symbol": symbol,
        "operator_attestations": _all_attestations(
            operator,
            primary_restrictions=primary_restrictions,
            supervisor_restrictions=supervisor_restrictions,
        ),
        "primary_api_restrictions": primary_restrictions,
        "supervisor_api_restrictions": supervisor_restrictions,
        "account_truth": _account_truth(
            primary_account,
            captured_at=captured_at,
            fingerprint=primary_fingerprint,
        ),
        "supervisor_account_truth": _account_truth(
            supervisor_account,
            captured_at=captured_at,
            fingerprint=supervisor_fingerprint,
        ),
        "flat_start_truth": {
            "open_orders_source": LIVE_CANARY_OPEN_ORDERS_SOURCE,
            "positions_source": LIVE_CANARY_POSITION_RISK_SOURCE,
            "captured_at_utc": captured_at,
            "api_key_fingerprint_sha256": primary_fingerprint,
            "open_orders": open_orders,
            "positions": positions,
        },
        "symbol_configuration_truth": {
            "position_mode_source": LIVE_CANARY_POSITION_MODE_SOURCE,
            "position_risk_source": LIVE_CANARY_POSITION_RISK_SOURCE,
            "exchange_info_source": LIVE_CANARY_RPI_EXCHANGE_INFO_SOURCE,
            "captured_at_utc": captured_at,
            "api_key_fingerprint_sha256": primary_fingerprint,
            "position_mode": dict(position_mode),
            "symbol_position_rows": symbol_rows,
            "exchange_symbol": exchange_symbol,
        },
        "rpi_truth": {
            "exchange_info_source": LIVE_CANARY_RPI_EXCHANGE_INFO_SOURCE,
            "commission_source": LIVE_CANARY_RPI_COMMISSION_SOURCE,
            "captured_at_utc": captured_at,
            "symbol": symbol,
            "exchange_status": exchange_symbol.get("status"),
            "supports_rpi": "RPI" in _flatten_permissions(
                exchange_symbol.get("permissionSets", [])
            ),
            "makerCommissionRate": commission.get("makerCommissionRate"),
            "takerCommissionRate": commission.get("takerCommissionRate"),
            "rpiCommissionRate": commission.get("rpiCommissionRate"),
        },
    }

    now_utc = datetime.fromtimestamp(client.clock.now_ms() / 1000, timezone.utc)
    validate_live_api_restrictions_evidence(
        config,
        evidence,
        now_utc=now_utc,
        primary_api_key=credentials.primary_key,
        supervisor_api_key=credentials.supervisor_key,
    )
    validate_live_dual_key_account_evidence(
        config,
        evidence,
        now_utc=now_utc,
        primary_api_key=credentials.primary_key,
        supervisor_api_key=credentials.supervisor_key,
    )
    validate_live_flat_start_evidence(
        config,
        evidence,
        now_utc=now_utc,
        primary_api_key=credentials.primary_key,
    )
    validate_live_symbol_configuration_evidence(
        config,
        evidence,
        now_utc=now_utc,
        primary_api_key=credentials.primary_key,
    )
    signed_evidence = sign_live_canary_evidence(
        evidence,
        primary_api_secret=credentials.primary_secret,
        supervisor_api_secret=credentials.supervisor_secret,
    )
    return validate_live_canary_evidence_payload(
        config,
        signed_evidence,
        now_utc=now_utc,
        primary_api_key=credentials.primary_key,
        supervisor_api_key=credentials.supervisor_key,
        primary_api_secret=credentials.primary_secret,
        supervisor_api_secret=credentials.supervisor_secret,
    )


def _flatten_permissions(value: object) -> frozenset[str]:
    pending = [value]
    result: set[str] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            normalized = current.strip().upper()
            if normalized:
                result.add(normalized)
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return frozenset(result)


def resolve_evidence_output_path(
    config: Mapping[str, Any],
    *,
    config_path: Path,
) -> Path:
    return resolve_live_canary_evidence_path(
        config,
        config_path=config_path,
        require_existing_file=False,
    )


def atomic_write_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    validate_live_canary_evidence_destination(path)
    payload = json.dumps(
        evidence,
        indent=2,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
    ) + "\n"
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect short-lived Binance mainnet canary evidence using only a "
            "fixed allowlist of GET endpoints."
        )
    )
    parser.add_argument("--config", required=True, help="Live canary JSON config")
    parser.add_argument("--timeout-sec", type=float, default=8.0)
    parser.add_argument("--confirm-legal-access", action="store_true", required=True)
    parser.add_argument("--confirm-single-process", action="store_true", required=True)
    parser.add_argument(
        "--confirm-same-futures-account",
        action="store_true",
        required=True,
    )
    parser.add_argument(
        "--confirm-supervisor-emergency-permissions",
        action="store_true",
        required=True,
    )
    parser.add_argument(
        "--confirm-legacy-state-archived",
        action="store_true",
        required=True,
    )
    parser.add_argument(
        "--confirm-fresh-state-generation",
        action="store_true",
        required=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = Path(os.path.abspath(args.config))
        config = _read_json_object(config_path, "Live canary config")
        output_path = resolve_evidence_output_path(
            config,
            config_path=config_path,
        )
        credentials = load_credentials(config)
        operator = OperatorAttestations(
            legal_access=args.confirm_legal_access,
            single_process=args.confirm_single_process,
            same_futures_account=args.confirm_same_futures_account,
            supervisor_emergency_permissions=(
                args.confirm_supervisor_emergency_permissions
            ),
            legacy_state_archived=args.confirm_legacy_state_archived,
            fresh_state_generation=args.confirm_fresh_state_generation,
        )
        evidence = collect_evidence(
            config,
            credentials,
            operator,
            transport=BinanceMainnetReadOnlyTransport(
                timeout_sec=args.timeout_sec,
            ),
        )
        atomic_write_evidence(output_path, evidence)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        print(f"Evidence collection failed: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote validated read-only evidence to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
