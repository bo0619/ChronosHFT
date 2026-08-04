"""Binance exchange adapter owned exclusively by the independent sidecar."""

import time

from infrastructure.binance_rate_limit_budget import BinanceRateLimitBudget
from risk.binance_sidecar_clock import BinanceSidecarClock
from risk.binance_sidecar_emergency import BinanceSidecarEmergencyActions
from risk.binance_risk_http import BinanceRiskHttpClient
from risk.binance_sidecar_settings import BinanceSidecarExchangeConfiguration
from risk.binance_sidecar_truth import BinanceSidecarTruthReader
from risk.exchange_port import ActionResult, SnapshotPurpose, TruthResult
from risk.sidecar_snapshot_worker import RiskSnapshotWorker
from risk.sidecar_transport import SidecarTransport
from risk.sidecar_values import finite_float


class BinanceRiskSidecarExchange:
    """Minimal authenticated exchange channel owned by the risk sidecar."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool,
        settings: dict = None,
        *,
        rest_factory=None,
        rate_limit_factory=None,
        session_factory=None,
        wall_time=time.time,
        monotonic=time.perf_counter,
        sleep=time.sleep,
    ):
        import requests

        if rest_factory is None:
            rest_factory = BinanceRiskHttpClient
        if rate_limit_factory is None:
            rate_limit_factory = BinanceRateLimitBudget.from_config

        settings = settings or {}
        rate_limit_settings = (
            BinanceSidecarExchangeConfiguration.validated_rate_limit_settings(
                settings
            )
        )
        self.rate_limit_budget = rate_limit_factory(rate_limit_settings)
        self.session = (
            requests.Session() if session_factory is None else session_factory()
        )
        self.session.headers.update({"Content-Type": "application/json"})
        self.rest = rest_factory(
            api_key,
            api_secret,
            self.session,
            testnet=testnet,
            rate_limit_budget=self.rate_limit_budget,
        )
        self.rest.clock_resync_callback = self.sync_exchange_clock
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._sleep = sleep
        self.account_scope_id = str(
            settings.get(
                "account_scope_id",
                settings.get("account_key_fingerprint", ""),
            )
            or ""
        )
        self._truth_sequence = 0
        configuration = BinanceSidecarExchangeConfiguration.from_settings(
            settings,
            finite_float,
            rate_limit_settings=rate_limit_settings,
        )
        configuration.initialize_owner(self)
        if hasattr(self.rest, "timestamp_provider"):
            self.rest.timestamp_provider = self._signed_timestamp_ms

    def _signed_timestamp_ms(self) -> int:
        corrected = self._corrected_epoch_at(self._monotonic())
        if corrected is None:
            raise RuntimeError("risk_exchange_clock_anchor_unavailable")
        return int(float(corrected) * 1000)

    def _collect_clock_samples(self, *, emergency: bool = False):
        return self._clock().collect_samples(
            emergency=emergency
        )

    def sync_exchange_clock(self, *, emergency: bool = False):
        return self._clock().sync(emergency=emergency)

    def _ensure_exchange_clock(self, force: bool = False):
        return self._clock().ensure(force)

    def _clock(self) -> BinanceSidecarClock:
        return BinanceSidecarClock(
            self,
            finite_float,
            wall_time=getattr(self, "_wall_time", None),
            monotonic=getattr(self, "_monotonic", None),
            sleep=getattr(self, "_sleep", None),
        )

    def check_account_channel(self):
        return BinanceSidecarTruthReader(self).check_account_channel()

    @staticmethod
    def _response_payload(response, expected_type, label: str):
        return BinanceSidecarTruthReader.response_payload(
            response,
            expected_type,
            label,
        )

    @staticmethod
    def _position_risk_fingerprint(positions) -> tuple:
        return BinanceSidecarTruthReader.position_risk_fingerprint(
            positions
        )

    def _corrected_epoch_at(self, observed_monotonic: float):
        return self._clock().corrected_epoch_at(observed_monotonic)

    def _get_funding_observations(self):
        return BinanceSidecarTruthReader(self).get_funding_observations()

    def get_risk_snapshot(self):
        return BinanceSidecarTruthReader(self).get_risk_snapshot()

    def read_account_truth(
        self,
        purpose: SnapshotPurpose,
    ) -> TruthResult:
        return BinanceSidecarTruthReader(self).read_account_truth(purpose)

    @staticmethod
    def _income_identity(row: dict) -> str:
        return BinanceSidecarTruthReader.income_identity(row)

    def _get_daily_external_cash_flow(self):
        return BinanceSidecarTruthReader(
            self
        ).get_daily_external_cash_flow()

    def _get_cached_daily_external_cash_flow(self):
        return BinanceSidecarTruthReader(
            self
        ).get_cached_daily_external_cash_flow()

    def _get_cached_external_cash_flow_truth(self):
        return BinanceSidecarTruthReader(
            self
        ).get_cached_external_cash_flow_truth()

    def _get_open_orders_snapshot(self):
        return BinanceSidecarTruthReader(self).get_open_orders_snapshot()

    def _remember_open_order_symbols(self, rows):
        return BinanceSidecarTruthReader(self).remember_open_order_symbols(
            rows
        )

    def emergency_cancel(self, symbols, countdown_time_ms: int):
        return BinanceSidecarEmergencyActions(self).cancel(
            symbols,
            countdown_time_ms,
        )

    def cancel_all_account_orders(self, action_id: str) -> ActionResult:
        ok, reason = self.emergency_cancel(
            self.symbols,
            1_000,
        )
        return ActionResult(bool(ok), str(action_id), str(reason or ""))

    def emergency_flatten(self):
        return BinanceSidecarEmergencyActions(self).flatten()

    def flatten_all_account_positions(self, action_id: str) -> ActionResult:
        ok, submitted, reason = self.emergency_flatten()
        return ActionResult(
            bool(ok),
            str(action_id),
            str(reason or ""),
            int(submitted or 0),
        )

    def close(self):
        self.session.close()


class _RiskSnapshotWorker(RiskSnapshotWorker):
    """Snapshot worker wired to the sidecar's latest-only result queue."""

    def __init__(self, exchange):
        super().__init__(
            exchange,
            put_latest=SidecarTransport.put_latest,
            perf_counter=time.perf_counter,
            wall_time=time.time,
        )
