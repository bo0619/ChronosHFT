"""Order fee and commission accounting helpers."""

from __future__ import annotations

from event.type import ExchangeOrderUpdate
from infrastructure.commission_truth import resolve_passive_fee_rate
from infrastructure.logger import logger

from .component import OMSComponent
from .order import Order


class OMSOrderAccounting(OMSComponent):
    """Own quote-asset resolution and execution fee estimates."""

    OWNER_READS = frozenset({"config"})

    def _extract_quote_asset(self, symbol: str) -> str:
        symbol = str(symbol or "").upper()
        for suffix in (
            "USDT",
            "USDC",
            "BUSD",
            "FDUSD",
            "BTC",
            "ETH",
            "BNB",
        ):
            if symbol.endswith(suffix):
                return suffix
        return ""

    def _tracked_quote_assets(self, symbols) -> set[str]:
        assets = {self._extract_quote_asset(symbol) for symbol in symbols or []}
        assets.discard("")
        return assets or {"USDT", "USDC", "BUSD", "FDUSD"}

    def _get_fill_commission(
        self,
        update: ExchangeOrderUpdate,
        order: Order,
        fill_notional: float,
    ) -> float:
        if update.commission is None:
            return fill_notional * self._get_fee_rate(
                order,
                is_maker=update.is_maker,
            )

        asset = (
            update.commission_asset or self._extract_quote_asset(order.intent.symbol)
        ).upper()
        if asset in {"", "USDT", "USDC", "BUSD", "FDUSD"}:
            return update.commission

        logger.warning(
            f"[OMS] Unsupported commission asset {asset}; "
            "falling back to configured fee model"
        )
        return fill_notional * self._get_fee_rate(
            order,
            is_maker=update.is_maker,
        )

    def _get_fee_rate(self, order: Order, is_maker: bool = None) -> float:
        fee_config = self.config.get("backtest", {})
        if order.intent.is_rpi:
            return resolve_passive_fee_rate(
                maker_rate=fee_config.get("maker_fee", 0.0),
                symbol=order.intent.symbol,
                is_rpi=True,
                rpi_commission_rates=fee_config.get(
                    "rpi_commission_rates",
                    {},
                ),
                default_rpi_commission_rate=fee_config.get(
                    "rpi_commission_rate",
                    0.0,
                ),
            )
        maker_fee = float(fee_config.get("maker_fee", 0.0))
        if is_maker is True:
            return maker_fee
        if is_maker is False:
            return fee_config.get("taker_fee", 0.0005)
        if order.intent.is_post_only:
            return maker_fee
        return fee_config.get("taker_fee", 0.0005)
