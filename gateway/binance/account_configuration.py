"""Binance account trading-mode configuration and verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from infrastructure.binance_account_configuration import (
    AccountConfigurationVerificationError,
    verify_account_configuration,
)

from .constants import (
    ACCOUNT_CONFIGURATION_MODE_APPLY,
    ACCOUNT_CONFIGURATION_MODE_VERIFY_ONLY,
    ACCOUNT_CONFIGURATION_MODES,
)


@dataclass(frozen=True)
class BinanceAccountConfigurationDependencies:
    rest: Callable[[], object]
    symbols: Callable[[], list[str]]
    venue_name: Callable[[], str]
    log_error: Callable[[str], None]
    log_critical: Callable[[str], None]


class BinanceAccountConfigurationController:
    """Own account-mode targets and apply/verify them before connectivity."""

    def __init__(
        self,
        dependencies: BinanceAccountConfigurationDependencies,
    ) -> None:
        self.dependencies = dependencies
        self.target_leverage = 0
        self.target_margin_type = "CROSSED"
        self.target_position_mode = "ONE_WAY"
        self.mode = ACCOUNT_CONFIGURATION_MODE_APPLY

    def apply(self) -> bool:
        target_leverage = int(self.target_leverage or 0)
        target_margin_type = str(
            self.target_margin_type or "CROSSED"
        ).upper()
        target_position_mode = str(
            self.target_position_mode or "ONE_WAY"
        ).upper()
        venue_name = self.dependencies.venue_name()
        if target_position_mode != "ONE_WAY":
            self.dependencies.log_critical(
                f"[{venue_name}] Refusing unsupported position mode "
                f"{target_position_mode}; OMS ledger is ONE_WAY only"
            )
            return False

        mode = str(self.mode or ACCOUNT_CONFIGURATION_MODE_APPLY).upper()
        if mode not in ACCOUNT_CONFIGURATION_MODES:
            self.dependencies.log_critical(
                f"[{venue_name}] Refusing unsupported account "
                f"configuration mode {mode}"
            )
            return False
        if mode == ACCOUNT_CONFIGURATION_MODE_VERIFY_ONLY:
            return self.verify(
                target_leverage=target_leverage,
                target_margin_type=target_margin_type,
                target_position_mode=target_position_mode,
            )

        rest = self.dependencies.rest()
        response = rest.set_position_mode(target_position_mode)
        if not rest.response_succeeded(
            response,
            accepted_error_codes={"-4059"},
        ):
            self.dependencies.log_error(
                f"[{venue_name}] Failed to set position mode "
                f"{target_position_mode}"
            )
            return False

        for symbol in self.dependencies.symbols():
            response = rest.set_margin_type(symbol, target_margin_type)
            if not rest.response_succeeded(
                response,
                accepted_error_codes={"-4046"},
            ):
                self.dependencies.log_error(
                    f"[{venue_name}] Failed to set margin type "
                    f"{target_margin_type} for {symbol}"
                )
                return False

            if target_leverage > 0:
                response = rest.set_leverage(symbol, target_leverage)
                if not rest.response_succeeded(response):
                    self.dependencies.log_error(
                        f"[{venue_name}] Failed to set leverage "
                        f"{target_leverage} for {symbol}"
                    )
                    return False
        return True

    def verify(
        self,
        *,
        target_leverage: int,
        target_margin_type: str,
        target_position_mode: str,
    ) -> bool:
        rest = self.dependencies.rest()
        try:
            position_mode_response = rest.get_position_mode()
            if not rest.response_succeeded(position_mode_response):
                raise AccountConfigurationVerificationError(
                    "failed to read account position mode"
                )

            position_risk_response = rest.get_positions()
            if not rest.response_succeeded(position_risk_response):
                raise AccountConfigurationVerificationError(
                    "failed to read account position configuration"
                )

            verify_account_configuration(
                position_mode_payload=position_mode_response.json(),
                position_risk_payload=position_risk_response.json(),
                symbols=self.dependencies.symbols(),
                target_position_mode=target_position_mode,
                target_margin_type=target_margin_type,
                target_leverage=target_leverage,
            )
        except Exception as exc:
            self.dependencies.log_critical(
                f"[{self.dependencies.venue_name()}] Account configuration "
                f"verification failed: {exc}"
            )
            return False
        return True
