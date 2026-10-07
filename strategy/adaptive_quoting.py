"""Shared helpers for the adaptive market-making models (GLFT and A-S)."""

from __future__ import annotations

import math

from strategy.base import StrategyTemplate


def negative_infinity() -> float:
    return -math.inf


class AdaptiveQuotingStrategy(StrategyTemplate):
    """Config parsing, sizing and markout plumbing common to both models.

    Subclasses own ``portfolio_symbols``, ``adaptive_markout`` and
    ``live_mode``; the helpers below only read them.
    """

    @staticmethod
    def _config_mapping(value, field: str) -> dict:
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        return dict(value)

    @staticmethod
    def _strict_finite(value, field: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be finite") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{field} must be finite")
        return parsed

    @classmethod
    def _ratio_at_least_one(cls, value, field: str) -> float:
        parsed = cls._strict_finite(value, field)
        if parsed < 1.0:
            raise ValueError(f"{field} must be at least one")
        return parsed

    def _parse_portfolio_correlations(
        self,
        raw_correlations,
    ) -> dict[tuple[str, str], float]:
        if not isinstance(raw_correlations, dict):
            raise ValueError("portfolio_risk.correlations must be an object")
        configured_symbols = set(self.portfolio_symbols)
        correlations: dict[tuple[str, str], float] = {}
        for raw_pair, raw_value in raw_correlations.items():
            pair_parts = str(raw_pair or "").split("|")
            if len(pair_parts) != 2:
                raise ValueError(
                    "portfolio_risk correlation keys must use SYMBOL|SYMBOL"
                )
            left, right = (part.strip().upper() for part in pair_parts)
            if not left or not right or left == right:
                raise ValueError(
                    "portfolio_risk correlation keys require two symbols"
                )
            if configured_symbols and (
                left not in configured_symbols or right not in configured_symbols
            ):
                raise ValueError(
                    "portfolio_risk correlation references an unknown symbol"
                )
            correlation = self._strict_finite(
                raw_value,
                f"portfolio_risk.correlations.{raw_pair}",
            )
            if correlation < -1.0 or correlation > 1.0:
                raise ValueError(
                    "portfolio_risk correlations must be between -1 and 1"
                )
            normalized_pair = tuple(sorted((left, right)))
            if normalized_pair in correlations:
                raise ValueError(
                    "portfolio_risk correlation pair is configured twice"
                )
            correlations[normalized_pair] = correlation
        return correlations

    def _calculate_safe_vol(
        self,
        symbol,
        price,
        *,
        side=None,
        current_position=0.0,
        reference_price=None,
    ):
        return self.calculate_quote_volume(
            symbol,
            price,
            side=side,
            current_position=current_position,
            reference_price=reference_price,
        )

    def _scale_safe_volume(
        self,
        symbol: str,
        safe_volume: float,
        multiplier: float,
        price: float,
    ) -> float:
        """Scale a pre-validated volume down without expanding its risk bound."""

        if safe_volume <= 0.0:
            return 0.0
        bounded_multiplier = min(1.0, max(0.0, float(multiplier)))
        if bounded_multiplier >= 1.0:
            return safe_volume
        info = self.reference_data.get_info(symbol)
        if info is None:
            return 0.0
        scaled = self.reference_data.round_qty(
            symbol,
            safe_volume * bounded_multiplier,
        )
        min_qty = max(0.0, float(info.min_qty or 0.0))
        min_notional = max(5.0, float(info.min_notional or 0.0))
        if (
            scaled <= 0.0
            or scaled > safe_volume + 1e-12
            or scaled < min_qty
            or scaled * price + 1e-9 < min_notional
        ):
            return 0.0
        return scaled

    def _record_resolved_paper_markouts(self) -> None:
        resolved = self.adaptive_markout.drain_resolved()
        if not resolved or self.live_mode:
            return
        for observation in resolved:
            self.execution.record_paper_markout(
                {
                    "client_oid": observation.client_oid,
                    "trade_id": observation.trade_id,
                    "symbol": observation.symbol,
                    "side": observation.side.value,
                    "fill_price": observation.fill_price,
                    "horizon_ms": observation.horizon_ms,
                    "mid_price": observation.mid_price,
                    "signed_markout_bps": observation.signed_markout_bps,
                    "fill_observed_monotonic": (
                        observation.fill_observed_monotonic
                    ),
                    "mid_observed_monotonic": (
                        observation.mid_observed_monotonic
                    ),
                    "observation_lag_ms": observation.observation_lag_ms,
                }
            )
