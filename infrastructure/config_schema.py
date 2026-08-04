"""Strict v3 schema contracts for fragmented ChronosHFT configuration."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping

from governance.contracts import (
    CONFIG_DOCUMENT_VERSION,
    CONFIG_FRAGMENT_SCHEMA,
    CONFIG_MANIFEST_SCHEMA,
    CONFIG_UNKNOWN_KEY_POLICY,
)

LEGACY_CONFIG_MANIFEST_SCHEMA = "chronoshft.config_manifest.v1"
VERSIONED_CONFIG_MANIFEST_SCHEMA = CONFIG_MANIFEST_SCHEMA
CONFIG_FRAGMENT_METADATA_KEYS = frozenset({"$schema", "fragment", "version"})


class ConfigSchemaError(ValueError):
    """Raised when versioned configuration violates its declared contract."""


@dataclass(frozen=True)
class FragmentInclude:
    path: str
    fragment: str
    version: int


@dataclass(frozen=True)
class BooleanSpec:
    pass


@dataclass(frozen=True)
class NumberSpec:
    integer: bool = False
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    exclusive_maximum: bool = False


@dataclass(frozen=True)
class StringSpec:
    choices: frozenset[str] | None = None
    min_length: int = 0
    pattern: str | None = None
    trimmed: bool = False


@dataclass(frozen=True)
class ArraySpec:
    items: object
    min_items: int = 0
    max_items: int | None = None
    unique: bool = False


@dataclass(frozen=True)
class MappingSpec:
    values: object
    key_pattern: str | None = None
    min_items: int = 0


@dataclass(frozen=True)
class ObjectSpec:
    fields: Mapping[str, object]
    required: frozenset[str]
    allow_comments: bool = True


def _object(
    fields: Mapping[str, object],
    *,
    optional: tuple[str, ...] = (),
    allow_comments: bool = True,
) -> ObjectSpec:
    return ObjectSpec(
        fields=dict(fields),
        required=frozenset(fields).difference(optional),
        allow_comments=allow_comments,
    )


def _number(
    minimum: float | None = None,
    maximum: float | None = None,
    *,
    exclusive_minimum: bool = False,
    exclusive_maximum: bool = False,
) -> NumberSpec:
    return NumberSpec(
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive_minimum,
        exclusive_maximum=exclusive_maximum,
    )


def _integer(
    minimum: int | None = None,
    maximum: int | None = None,
    *,
    exclusive_minimum: bool = False,
) -> NumberSpec:
    return NumberSpec(
        integer=True,
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive_minimum,
    )


def _string(
    *,
    choices: tuple[str, ...] | None = None,
    min_length: int = 0,
    pattern: str | None = None,
    trimmed: bool = False,
) -> StringSpec:
    return StringSpec(
        choices=frozenset(choices) if choices is not None else None,
        min_length=min_length,
        pattern=pattern,
        trimmed=trimmed,
    )


BOOL = BooleanSpec()
NONNEGATIVE = _number(0.0)
POSITIVE = _number(0.0, exclusive_minimum=True)
PROBABILITY = _number(0.0, 1.0)
POSITIVE_INT = _integer(1)
NONNEGATIVE_INT = _integer(0)
NONEMPTY_TEXT = _string(min_length=1, trimmed=True)
SYMBOL_TEXT = _string(
    min_length=5,
    pattern=r"^[A-Z0-9]{5,30}$",
    trimmed=True,
)
SYMBOL_KEYED_RATE = MappingSpec(
    values=PROBABILITY,
    key_pattern=r"^[A-Z0-9]{5,30}$",
)


def _portfolio_risk_schema() -> ObjectSpec:
    return _object(
        {
            "enabled": BOOL,
            "require_full_universe": BOOL,
            "max_state_age_sec": POSITIVE,
            "correlations": MappingSpec(
                values=_number(-1.0, 1.0),
                key_pattern=(r"^[A-Z0-9]{5,30}\|[A-Z0-9]{5,30}$"),
            ),
        }
    )


def _markout_schema() -> ObjectSpec:
    return _object(
        {
            "horizons_ms": ArraySpec(
                POSITIVE_INT,
                min_items=1,
                max_items=32,
                unique=True,
            ),
            "min_samples": POSITIVE_INT,
            "confidence_z": POSITIVE,
            "max_pending": POSITIVE_INT,
            "window_size": POSITIVE_INT,
        }
    )


def _hawkes_schema() -> ObjectSpec:
    return _object(
        {
            "decay_rate_per_s": POSITIVE,
            "self_excitation": NONNEGATIVE,
            "cross_excitation": NONNEGATIVE,
            "max_multiplier": _number(1.0),
        }
    )


def _stale_quote_guard_schema() -> ObjectSpec:
    return _object({"enabled": BOOL, "min_depth_bps": NONNEGATIVE})


def _dynamic_covariance_schema() -> ObjectSpec:
    return _object(
        {
            "sample_interval_s": POSITIVE,
            "max_state_age_s": POSITIVE,
            "max_sync_skew_s": NONNEGATIVE,
            "ewma_alpha": _number(0.0, 1.0, exclusive_minimum=True),
            "diagonal_shrinkage": PROBABILITY,
            "min_samples": POSITIVE_INT,
        }
    )


def _queue_latency_schema() -> ObjectSpec:
    return _object(
        {
            "rate_ewma_alpha": _number(
                0.0,
                1.0,
                exclusive_minimum=True,
            ),
            "default_service_rate_qty_per_s": POSITIVE,
            "max_queue_delay_s": POSITIVE,
            "network_latency_ms": NONNEGATIVE,
            "queue_risk_time_weight": NONNEGATIVE,
            "confidence_z": NONNEGATIVE,
        }
    )


def _size_optimization_schema() -> ObjectSpec:
    return _object(
        {
            "candidate_multipliers": ArraySpec(
                NONNEGATIVE,
                min_items=1,
                max_items=32,
                unique=True,
            ),
            "utility_horizon_s": POSITIVE,
            "size_penalty_bps": NONNEGATIVE,
        }
    )


def _model_readiness_counts() -> ObjectSpec:
    return _object(
        {
            "min_volatility_samples": POSITIVE_INT,
            "min_model_samples": POSITIVE_INT,
        }
    )


FRAGMENT_SCHEMAS: dict[str, dict[int, ObjectSpec]] = {
    "account": {
        1: _object(
            {
                "account": _object(
                    {
                        "leverage": _integer(1, 125),
                        "margin_type": _string(choices=("ISOLATED",)),
                        "position_mode": _string(choices=("ONE_WAY",)),
                        "use_bnb_fees": BOOL,
                    }
                )
            }
        )
    },
    "alerts": {
        1: _object(
            {
                "alert": _object(
                    {
                        "active": BOOL,
                        "telegram_token": _string(),
                        "telegram_chat_id": _string(),
                    }
                )
            }
        )
    },
    "backtest": {
        1: _object(
            {
                "backtest": _object(
                    {
                        "taker_fee": PROBABILITY,
                        "maker_fee": PROBABILITY,
                        "rpi_commission_rate": PROBABILITY,
                        "rpi_commission_rates": SYMBOL_KEYED_RATE,
                        "latency_base_ms": NONNEGATIVE,
                        "latency_sigma": NONNEGATIVE,
                        "cancel_ahead_prob": PROBABILITY,
                        "chaos": _object(
                            {
                                "packet_loss_rate": PROBABILITY,
                                "order_reject_rate": PROBABILITY,
                            }
                        ),
                    }
                )
            }
        )
    },
    "data_recording": {
        1: _object(
            {
                "record_data": BOOL,
                "data_recorder": _object(
                    {
                        "save_path": NONEMPTY_TEXT,
                        "flush_threshold": POSITIVE_INT,
                        "queue_capacity": POSITIVE_INT,
                        "close_timeout_sec": POSITIVE,
                        "min_free_bytes": NONNEGATIVE_INT,
                        "process_niceness": _integer(-20, 19),
                    }
                ),
            }
        )
    },
    "execution": {
        1: _object(
            {
                "execution": _object(
                    {
                        "mode": _string(choices=("paper", "live")),
                    }
                )
            }
        )
    },
    "oms": {
        1: _object(
            {
                "oms": _object(
                    {
                        "reconcile_min_interval_sec": NONNEGATIVE,
                        "reconcile_api_failure_threshold": POSITIVE_INT,
                        "reconcile_api_cooldown_sec": NONNEGATIVE,
                        "trade_recovery_lookback_ms": POSITIVE_INT,
                        "trade_recovery_overlap_ms": NONNEGATIVE_INT,
                        "trade_recovery_id_overlap": NONNEGATIVE_INT,
                        "trade_tail_verification_delay_sec": NONNEGATIVE,
                        "trade_tail_verification_retry_sec": POSITIVE,
                        "trade_tail_verification_attempts": POSITIVE_INT,
                        "outbound_gate_drain_timeout_sec": POSITIVE,
                        "shutdown_cancel_timeout_sec": POSITIVE,
                        "shutdown_empty_snapshots_required": POSITIVE_INT,
                        "shutdown_cancel_settle_interval_sec": POSITIVE,
                        "ack_timeout_sec": POSITIVE,
                        "ack_timeout_recheck_sec": POSITIVE,
                        "monitor_check_interval_sec": POSITIVE,
                        "risk_rejection_log_interval_sec": POSITIVE,
                        "event_log_max": POSITIVE_INT,
                        "journal_min_free_bytes": NONNEGATIVE_INT,
                        "journal_space_check_interval_sec": POSITIVE,
                        "tombstone_max": NONNEGATIVE_INT,
                    }
                )
            }
        )
    },
    "paper_trade": {
        1: _object(
            {
                "paper_trade": _object(
                    {
                        "enabled": BOOL,
                        "market_data_environment": _string(
                            choices=("production", "testnet")
                        ),
                        "maker_fee": PROBABILITY,
                        "taker_fee": PROBABILITY,
                        "rpi_commission_rate": PROBABILITY,
                        "rpi_commission_rates": SYMBOL_KEYED_RATE,
                        "rpi_fill_model": _string(choices=("public_trade_proxy",)),
                        "reset_on_start": BOOL,
                        "cancel_ahead_fraction": PROBABILITY,
                        "maintenance_margin_rate": PROBABILITY,
                        "mark_rest_poll_interval_sec": POSITIVE,
                        "mark_ws_stale_after_sec": POSITIVE,
                        "mark_rest_request_timeout_sec": POSITIVE,
                        "command_timeout_sec": POSITIVE,
                        "command_queue_size": POSITIVE_INT,
                        "max_order_history": POSITIVE_INT,
                        "max_trade_history": POSITIVE_INT,
                    }
                )
            }
        )
    },
    "paper_trade_database": {
        1: _object(
            {
                "paper_trade_database": _object(
                    {
                        "enabled": BOOL,
                        "path": NONEMPTY_TEXT,
                        "sqlite_timeout_sec": POSITIVE,
                        "queue_capacity": _integer(100),
                        "write_batch_size": _integer(1, 256),
                        "min_free_bytes": NONNEGATIVE_INT,
                        "space_check_interval_sec": POSITIVE,
                        "close_timeout_sec": POSITIVE,
                        "strategy_sample_interval_sec": _number(0.1),
                        "account_sample_interval_sec": _number(0.1),
                        "market_sample_interval_sec": _number(0.1),
                    }
                )
            }
        )
    },
    "symbols": {
        1: _object(
            {
                "symbols": ArraySpec(
                    SYMBOL_TEXT,
                    min_items=1,
                    max_items=128,
                    unique=True,
                )
            }
        )
    },
    "risk.black_swan": {
        1: _object(
            {
                "risk": _object(
                    {"black_swan": _object({"volatility_halt_threshold": PROBABILITY})}
                )
            }
        )
    },
    "risk.core": {1: _object({"risk": _object({"active": BOOL})})},
    "risk.limits": {
        1: _object(
            {"risk": _object({"limits": _object({"max_drawdown_pct": PROBABILITY})})}
        )
    },
    "risk.price_sanity": {
        1: _object(
            {
                "risk": _object(
                    {
                        "price_sanity": _object(
                            {
                                "max_deviation_pct": PROBABILITY,
                                "max_spread_pct": PROBABILITY,
                            }
                        )
                    }
                )
            }
        )
    },
    "risk.technical_health": {
        1: _object(
            {
                "risk": _object(
                    {
                        "tech_health": _object(
                            {
                                "max_latency_ms": POSITIVE,
                                "max_order_count_per_sec": POSITIVE_INT,
                                "consecutive_error_limit": POSITIVE_INT,
                            }
                        )
                    }
                )
            }
        )
    },
    "strategy.avellaneda_stoikov": {
        1: _object(
            {
                "strategy": _object(
                    {
                        "avellaneda_stoikov": _object(
                            {
                                "gamma": POSITIVE,
                                "k": POSITIVE,
                                "vol_window": POSITIVE_INT,
                                "cycle_interval": POSITIVE,
                                "horizon_s": POSITIVE,
                                "min_sigma_bps": POSITIVE,
                                "max_tick_gap_sec": POSITIVE,
                                "min_spread_ratio": NONNEGATIVE,
                                "portfolio_risk": _portfolio_risk_schema(),
                                "adaptive": _object(
                                    {
                                        "enabled": BOOL,
                                        "side_intensity": _object(
                                            {
                                                "base_A_per_s": POSITIVE,
                                                "bid_A_multiplier": POSITIVE,
                                                "ask_A_multiplier": POSITIVE,
                                                "bid_k_multiplier": POSITIVE,
                                                "ask_k_multiplier": POSITIVE,
                                            }
                                        ),
                                        "hawkes": _hawkes_schema(),
                                        "markout": _markout_schema(),
                                        "flow_toxicity": _object(
                                            {
                                                "enabled": BOOL,
                                                "half_life_s": POSITIVE,
                                                "ewma_alpha": _number(
                                                    0.0,
                                                    1.0,
                                                    exclusive_minimum=True,
                                                ),
                                                "trade_imbalance_cost_bps": NONNEGATIVE,
                                                "microprice_weight": NONNEGATIVE,
                                                "max_adverse_cost_bps": NONNEGATIVE,
                                            }
                                        ),
                                        "stale_quote_guard": (
                                            _stale_quote_guard_schema()
                                        ),
                                        "dynamic_covariance": (
                                            _dynamic_covariance_schema()
                                        ),
                                        "robust": _object(
                                            {
                                                "k_ratio": _number(1.0),
                                                "volatility_ratio": _number(1.0),
                                            }
                                        ),
                                        "queue_latency": _queue_latency_schema(),
                                        "size_optimization": (
                                            _size_optimization_schema()
                                        ),
                                    }
                                ),
                            }
                        )
                    }
                )
            }
        )
    },
    "strategy.capital_scaling": {
        1: _object(
            {
                "strategy": _object(
                    {
                        "capital_multiplier": POSITIVE,
                        "capital_scaling": _object(
                            {
                                "enabled": BOOL,
                                "reference_capital_usdt": POSITIVE,
                                "target_order_notional": POSITIVE,
                                "order_notional_limit_factor": _number(1.0),
                                "target_total_risk_notional": POSITIVE,
                                "target_concurrent_symbols": POSITIVE_INT,
                                "target_daily_loss": NONNEGATIVE,
                                "max_order_qty": POSITIVE,
                                "budget_asset_weights": MappingSpec(
                                    values=POSITIVE,
                                    key_pattern=r"^[A-Z0-9]{2,12}$",
                                    min_items=1,
                                ),
                                "position_buffer_orders": _number(1.0),
                                "reference_min_notional": POSITIVE,
                                "notional_buffer": _number(1.0),
                            }
                        ),
                    }
                )
            }
        )
    },
    "strategy.core": {
        1: _object(
            {
                "strategy": _object(
                    {
                        "name": _string(
                            choices=(
                                "GLFT_MultiScale",
                                "AvellanedaStoikov",
                            )
                        ),
                        "primary_model": _string(
                            choices=("glft", "avellaneda_stoikov")
                        ),
                        "registered_models": ArraySpec(
                            _string(choices=("glft", "avellaneda_stoikov")),
                            min_items=1,
                            max_items=2,
                            unique=True,
                        ),
                        "execution_policy": _string(choices=("single_primary",)),
                        "use_rpi": BOOL,
                        "use_rpi_for_avellaneda_stoikov": BOOL,
                        "use_rpi_for_glft": BOOL,
                        "use_rpi_for_passive_exit": BOOL,
                        "rpi_fallback_to_gtx": BOOL,
                        "rpi_live_policy": _object({"require_zero_commission": BOOL}),
                    }
                )
            }
        )
    },
    "strategy.glft": {
        1: _object(
            {
                "strategy": _object(
                    {
                        "glft": _object(
                            {
                                "gamma": POSITIVE,
                                "cycle_interval": POSITIVE,
                                "paper_cycle_interval": POSITIVE,
                                "alpha": _object({"enabled": BOOL}),
                                "target_inventory_notional_usdt": NONNEGATIVE,
                                "portfolio_risk": _portfolio_risk_schema(),
                                "adaptive": _object(
                                    {
                                        "enabled": BOOL,
                                        "finite_horizon_s": POSITIVE,
                                        "side_intensity": _object(
                                            {
                                                "bid_A_multiplier": POSITIVE,
                                                "ask_A_multiplier": POSITIVE,
                                                "bid_k_multiplier": POSITIVE,
                                                "ask_k_multiplier": POSITIVE,
                                            }
                                        ),
                                        "hawkes": _hawkes_schema(),
                                        "markout": _markout_schema(),
                                        "flow_toxicity": _object(
                                            {
                                                "enabled": BOOL,
                                                "half_life_s": POSITIVE,
                                                "trade_imbalance_cost_bps": NONNEGATIVE,
                                                "microprice_weight": NONNEGATIVE,
                                                "max_adverse_cost_bps": NONNEGATIVE,
                                            }
                                        ),
                                        "stale_quote_guard": (
                                            _stale_quote_guard_schema()
                                        ),
                                        "dynamic_covariance": (
                                            _dynamic_covariance_schema()
                                        ),
                                        "robust": _object(
                                            {
                                                "intensity_ratio": _number(1.0),
                                                "k_ratio": _number(1.0),
                                                "volatility_ratio": _number(1.0),
                                            }
                                        ),
                                        "queue_latency": _queue_latency_schema(),
                                        "size_optimization": (
                                            _size_optimization_schema()
                                        ),
                                    }
                                ),
                                "rpi_intensity": _object(
                                    {
                                        "min_sample_count": POSITIVE_INT,
                                        "min_depth_level_count": POSITIVE_INT,
                                        "min_total_exposure_seconds": POSITIVE,
                                        "min_fill_count": POSITIVE_INT,
                                        "min_depth_span_bps": POSITIVE,
                                        "min_k_per_bps": POSITIVE,
                                        "max_k_per_bps": POSITIVE,
                                    }
                                ),
                                "calibrator": _object(
                                    {
                                        "window": POSITIVE_INT,
                                        "min_samples": POSITIVE_INT,
                                        "initial_sigma_bps": POSITIVE,
                                        "initial_A": POSITIVE,
                                        "initial_k": POSITIVE,
                                        "learning_rate": POSITIVE,
                                        "sigma_max_bps": POSITIVE,
                                        "sigma_ema_alpha": _number(
                                            0.0,
                                            1.0,
                                            exclusive_minimum=True,
                                        ),
                                        "max_tick_gap_sec": POSITIVE,
                                    }
                                ),
                                "execution": _object(
                                    {
                                        "min_spread_bps": NONNEGATIVE,
                                        "paper_min_spread_bps": NONNEGATIVE,
                                    }
                                ),
                            }
                        )
                    }
                )
            }
        )
    },
    "strategy.model_readiness": {
        1: _object(
            {
                "strategy": _object(
                    {
                        "model_readiness": _object(
                            {
                                "enabled": BOOL,
                                "min_volatility_samples": POSITIVE_INT,
                                "min_model_samples": POSITIVE_INT,
                                "models": _object(
                                    {
                                        "glft": _model_readiness_counts(),
                                        "avellaneda_stoikov": (
                                            _model_readiness_counts()
                                        ),
                                    }
                                ),
                                "live_approval": _object(
                                    {
                                        "manifest_path": _string(),
                                        "min_data_duration_sec": POSITIVE,
                                        "min_oos_samples": POSITIVE_INT,
                                        "trusted_signers": MappingSpec(
                                            values=_object(
                                                {
                                                    "algorithm": _string(
                                                        choices=("ED25519",)
                                                    ),
                                                    "public_key_base64": (
                                                        NONEMPTY_TEXT
                                                    ),
                                                },
                                                allow_comments=False,
                                            ),
                                            key_pattern=(
                                                r"^[A-Za-z0-9]"
                                                r"[A-Za-z0-9._:-]{2,127}$"
                                            ),
                                        ),
                                    },
                                    optional=("trusted_signers",),
                                ),
                            }
                        )
                    }
                )
            }
        )
    },
    "strategy.order_sizing": {
        1: _object(
            {
                "strategy": _object(
                    {"order_sizing": _object({"mode": _string(choices=("notional",))})}
                )
            }
        )
    },
    "system.admin_control": {
        1: _object(
            {
                "system": _object(
                    {
                        "admin_control": _object(
                            {
                                "path": NONEMPTY_TEXT,
                                "command_ttl_sec": POSITIVE,
                                "session_max_age_sec": POSITIVE,
                                "max_retained_results": POSITIVE_INT,
                                "max_retained_archives": POSITIVE_INT,
                                "retention_max_age_sec": POSITIVE,
                            }
                        )
                    }
                )
            }
        )
    },
    "system.dashboard": {
        1: _object(
            {
                "system": _object(
                    {
                        "web_dashboard": _object(
                            {
                                "enabled": BOOL,
                                "host": _string(
                                    choices=("127.0.0.1", "localhost", "::1")
                                ),
                                "port": _integer(1, 65535),
                                "open_browser": BOOL,
                                "refresh_interval_ms": POSITIVE_INT,
                                "max_request_threads": POSITIVE_INT,
                                "request_timeout_sec": POSITIVE,
                                "history_limit": POSITIVE_INT,
                                "orders_limit": POSITIVE_INT,
                                "trades_limit": POSITIVE_INT,
                                "logs_limit": POSITIVE_INT,
                                "telemetry_mailbox_capacity": _integer(1, 1),
                                "telemetry_stop_timeout_sec": POSITIVE,
                            }
                        )
                    }
                )
            }
        )
    },
    "system.shutdown": {
        1: _object(
            {
                "system": _object(
                    {
                        "shutdown": _object(
                            {
                                "max_attempts": POSITIVE_INT,
                                "retry_interval_sec": _number(0.0, 5.0),
                            }
                        )
                    }
                )
            }
        )
    },
    "system.event_engine": {
        1: _object(
            {
                "system": _object(
                    {
                        "event_engine": _object(
                            {
                                "queue_capacity": _object(
                                    {
                                        "market": POSITIVE_INT,
                                        "execution": POSITIVE_INT,
                                        "cold": POSITIVE_INT,
                                    }
                                ),
                                "queue_warn_depth": _object(
                                    {
                                        "market": NONNEGATIVE_INT,
                                        "execution": NONNEGATIVE_INT,
                                        "cold": NONNEGATIVE_INT,
                                    }
                                ),
                                "backlog_warn_ms": _object(
                                    {
                                        "market": POSITIVE,
                                        "execution": POSITIVE,
                                        "cold": POSITIVE,
                                    }
                                ),
                                "handler_slow_ms": _object(
                                    {
                                        "market": POSITIVE,
                                        "execution": POSITIVE,
                                        "cold": POSITIVE,
                                    }
                                ),
                                "alert_interval_sec": POSITIVE,
                                "shutdown_drain_timeout_sec": POSITIVE,
                            }
                        )
                    }
                )
            }
        )
    },
    "system.logging": {
        1: _object(
            {
                "system": _object(
                    {
                        "log_level": _string(
                            choices=(
                                "DEBUG",
                                "INFO",
                                "WARNING",
                                "ERROR",
                                "CRITICAL",
                            )
                        ),
                        "log_path": NONEMPTY_TEXT,
                        "log_console": BOOL,
                        "log_queue_capacity": POSITIVE_INT,
                        "log_max_bytes": POSITIVE_INT,
                        "log_backup_count": NONNEGATIVE_INT,
                        "log_close_timeout_sec": POSITIVE,
                        "web_port": _integer(1, 65535),
                    }
                )
            }
        )
    },
    "system.market_data": {
        1: _object(
            {
                "system": _object(
                    {
                        "market_data": _object(
                            {
                                "environment": _string(
                                    choices=("production", "testnet")
                                ),
                                "testnet": BOOL,
                                "public_only": BOOL,
                                "publish_depth_levels": POSITIVE_INT,
                                "emit_full_orderbook_events": BOOL,
                                "max_book_buffer": POSITIVE_INT,
                                "max_orderbook_levels_per_side": POSITIVE_INT,
                                "max_delta_levels_per_side": POSITIVE_INT,
                                "max_book_recovery_threads": POSITIVE_INT,
                                "book_recovery_join_timeout_sec": POSITIVE,
                                "book_resync_max_attempts": POSITIVE_INT,
                                "book_resync_retry_sec": NONNEGATIVE,
                                "stream_ready_timeout_sec": POSITIVE,
                                "max_market_event_ingress_age_ms": POSITIVE,
                            }
                        )
                    }
                )
            }
        )
    },
    "system.rate_limit": {
        1: _object(
            {
                "system": _object(
                    {
                        "binance_rest_rate_limit": _object(
                            {
                                "enabled": BOOL,
                                "request_weight_limit": POSITIVE_INT,
                                "trading_reserve": NONNEGATIVE_INT,
                                "emergency_reserve": NONNEGATIVE_INT,
                                "state_path": NONEMPTY_TEXT,
                                "sqlite_timeout_sec": POSITIVE,
                                "full_open_orders_audit_interval_sec": POSITIVE,
                            }
                        )
                    }
                )
            }
        )
    },
    "system.resource_monitor": {
        1: _object(
            {
                "system": _object(
                    {
                        "resource_monitor": _object(
                            {
                                "enabled": BOOL,
                                "sample_interval_sec": POSITIVE,
                                "rss_warn_bytes": POSITIVE_INT,
                                "rss_freeze_bytes": POSITIVE_INT,
                                "max_main_threads": POSITIVE_INT,
                                "max_main_fds": POSITIVE_INT,
                                "max_total_threads": POSITIVE_INT,
                                "max_total_fds": POSITIVE_INT,
                                "max_processes": POSITIVE_INT,
                                "cpu_warn_percent_one_core": POSITIVE,
                                "breach_checks": POSITIVE_INT,
                                "recovery_checks": POSITIVE_INT,
                                "history_samples": POSITIVE_INT,
                                "require_available_on_linux": BOOL,
                                "require_complete_process_tree_on_linux": BOOL,
                            }
                        )
                    }
                )
            }
        )
    },
    "system.strategy_runtime": {
        1: _object(
            {
                "system": _object(
                    {
                        "strategy_runtime": _object(
                            {
                                "control_queue_capacity": POSITIVE_INT,
                                "queue_warn_depth": NONNEGATIVE_INT,
                                "warn_queue_depth": NONNEGATIVE_INT,
                                "freeze_queue_depth": POSITIVE_INT,
                                "warn_backlog_ms": POSITIVE,
                                "freeze_backlog_ms": POSITIVE,
                                "async_worker_warn_deferred": NONNEGATIVE_INT,
                                "async_worker_freeze_deferred": POSITIVE_INT,
                                "recovery_checks": POSITIVE_INT,
                                "slow_handler_ms": POSITIVE,
                                "alert_interval_sec": POSITIVE,
                                "shutdown_timeout_sec": POSITIVE,
                            }
                        )
                    }
                )
            }
        )
    },
    "system.time_sync": {
        1: _object(
            {
                "system": _object(
                    {
                        "time_sync": _object(
                            {
                                "startup_required": BOOL,
                                "require_healthy_for_trading": BOOL,
                                "sample_count": POSITIVE_INT,
                                "min_successful_samples": POSITIVE_INT,
                                "low_rtt_sample_count": POSITIVE_INT,
                                "sample_spacing_ms": NONNEGATIVE,
                                "request_timeout_sec": POSITIVE,
                                "connection_warmup_timeout_sec": POSITIVE,
                                "max_initial_offset_ms": POSITIVE,
                                "max_phase_error_ms": POSITIVE,
                                "halt_phase_error_ms": POSITIVE,
                                "max_rtt_ms": POSITIVE,
                                "max_uncertainty_ms": POSITIVE,
                                "max_offset_dispersion_ms": POSITIVE,
                                "max_sync_age_sec": POSITIVE,
                                "max_wall_clock_step_ms": POSITIVE,
                                "health_poll_interval_sec": POSITIVE,
                                "freeze_breach_threshold": POSITIVE_INT,
                                "halt_breach_threshold": POSITIVE_INT,
                                "recovery_success_threshold": POSITIVE_INT,
                                "sync_interval_sec": POSITIVE,
                                "unhealthy_retry_sec": POSITIVE,
                                "max_consecutive_failures": POSITIVE_INT,
                            }
                        )
                    }
                )
            }
        )
    },
}


def validate_versioned_manifest(payload: Mapping[str, object]) -> list[FragmentInclude]:
    """Validate and decode a v3 manifest without resolving filesystem paths."""
    expected_keys = {"schema", "config_version", "unknown_keys", "includes"}
    unknown = sorted(set(payload).difference(expected_keys))
    missing = sorted(expected_keys.difference(payload))
    violations = []
    if unknown:
        violations.append(f"unknown manifest keys: {unknown}")
    if missing:
        violations.append(f"missing manifest keys: {missing}")
    if payload.get("schema") != VERSIONED_CONFIG_MANIFEST_SCHEMA:
        violations.append(f"schema must be {VERSIONED_CONFIG_MANIFEST_SCHEMA!r}")
    config_version = payload.get("config_version")
    if (
        isinstance(config_version, bool)
        or not isinstance(config_version, int)
        or config_version != CONFIG_DOCUMENT_VERSION
    ):
        violations.append(
            f"config_version must be the supported integer {CONFIG_DOCUMENT_VERSION}"
        )
    if payload.get("unknown_keys") != CONFIG_UNKNOWN_KEY_POLICY:
        violations.append(f"unknown_keys must be {CONFIG_UNKNOWN_KEY_POLICY!r}")

    raw_includes = payload.get("includes")
    if not isinstance(raw_includes, list) or not raw_includes:
        violations.append("includes must be a non-empty array")
        raw_includes = []
    elif len(raw_includes) > 128:
        violations.append("includes must contain no more than 128 entries")

    includes: list[FragmentInclude] = []
    seen_fragments: set[str] = set()
    for index, raw_include in enumerate(raw_includes):
        label = f"includes[{index}]"
        if not isinstance(raw_include, Mapping):
            violations.append(f"{label} must be an object")
            continue
        include_keys = {"path", "fragment", "version"}
        extra = sorted(set(raw_include).difference(include_keys))
        absent = sorted(include_keys.difference(raw_include))
        if extra:
            violations.append(f"{label} has unknown keys: {extra}")
        if absent:
            violations.append(f"{label} is missing keys: {absent}")
        path = raw_include.get("path")
        fragment = raw_include.get("fragment")
        version = raw_include.get("version")
        if not isinstance(path, str) or not path or path != path.strip():
            violations.append(f"{label}.path must be a non-empty trimmed string")
        if not isinstance(fragment, str) or not fragment:
            violations.append(f"{label}.fragment must be a non-empty string")
            continue
        if fragment in seen_fragments:
            violations.append(f"duplicate fragment identity: {fragment!r}")
        seen_fragments.add(fragment)
        supported_versions = FRAGMENT_SCHEMAS.get(fragment)
        if supported_versions is None:
            violations.append(f"{label}.fragment is unknown: {fragment!r}")
            continue
        if isinstance(version, bool) or not isinstance(version, int):
            violations.append(f"{label}.version must be an integer")
            continue
        if version not in supported_versions:
            supported = sorted(supported_versions)
            violations.append(
                f"{label}.version {version} is unsupported for {fragment!r}; "
                f"supported versions: {supported}"
            )
            continue
        if isinstance(path, str) and path and path == path.strip():
            includes.append(FragmentInclude(path, fragment, version))

    if violations:
        raise ConfigSchemaError(
            "config manifest schema validation failed: " + "; ".join(violations)
        )
    return includes


def validate_fragment_document(
    payload: Mapping[str, object],
    *,
    expected_fragment: str,
    expected_version: int,
    source: str,
) -> dict:
    """Validate a fragment envelope and return only its configuration fields."""
    violations: list[str] = []
    if payload.get("$schema") != CONFIG_FRAGMENT_SCHEMA:
        violations.append(f"$schema must be {CONFIG_FRAGMENT_SCHEMA!r}")
    fragment = payload.get("fragment")
    if fragment != expected_fragment:
        violations.append(
            f"fragment must match manifest identity {expected_fragment!r}"
        )
    version = payload.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != expected_version
    ):
        violations.append(f"version must match manifest version {expected_version}")

    schema = FRAGMENT_SCHEMAS.get(expected_fragment, {}).get(expected_version)
    if schema is None:
        violations.append(
            f"unsupported fragment contract {expected_fragment!r} v{expected_version}"
        )
    content = {
        key: value
        for key, value in payload.items()
        if key not in CONFIG_FRAGMENT_METADATA_KEYS
    }
    if schema is not None:
        _validate_value(content, schema, "config", violations)
    if violations:
        raise ConfigSchemaError(
            f"config fragment {expected_fragment!r} at {source} failed schema "
            "validation: " + "; ".join(violations)
        )
    return content


def _validate_value(
    value: object,
    spec: object,
    path: str,
    violations: list[str],
) -> None:
    if isinstance(spec, BooleanSpec):
        if not isinstance(value, bool):
            violations.append(f"{path} must be a JSON boolean")
        return
    if isinstance(spec, NumberSpec):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            kind = "integer" if spec.integer else "number"
            violations.append(f"{path} must be a finite {kind}")
            return
        if spec.integer and not isinstance(value, int):
            violations.append(f"{path} must be an integer")
            return
        parsed = float(value)
        if not math.isfinite(parsed):
            violations.append(f"{path} must be finite")
            return
        if spec.minimum is not None:
            invalid = (
                parsed <= spec.minimum
                if spec.exclusive_minimum
                else parsed < spec.minimum
            )
            if invalid:
                relation = "greater than" if spec.exclusive_minimum else "at least"
                violations.append(f"{path} must be {relation} {spec.minimum:g}")
        if spec.maximum is not None:
            invalid = (
                parsed >= spec.maximum
                if spec.exclusive_maximum
                else parsed > spec.maximum
            )
            if invalid:
                relation = "less than" if spec.exclusive_maximum else "at most"
                violations.append(f"{path} must be {relation} {spec.maximum:g}")
        return
    if isinstance(spec, StringSpec):
        if not isinstance(value, str):
            violations.append(f"{path} must be a string")
            return
        if len(value) < spec.min_length:
            violations.append(
                f"{path} must contain at least {spec.min_length} characters"
            )
        if spec.trimmed and value != value.strip():
            violations.append(f"{path} must not have surrounding whitespace")
        if spec.choices is not None and value not in spec.choices:
            violations.append(f"{path} must be one of {sorted(spec.choices)!r}")
        if spec.pattern is not None and re.fullmatch(spec.pattern, value) is None:
            violations.append(f"{path} has an invalid format")
        return
    if isinstance(spec, ArraySpec):
        if not isinstance(value, list):
            violations.append(f"{path} must be an array")
            return
        if len(value) < spec.min_items:
            violations.append(f"{path} must contain at least {spec.min_items} items")
        if spec.max_items is not None and len(value) > spec.max_items:
            violations.append(
                f"{path} must contain no more than {spec.max_items} items"
            )
        if spec.unique and any(
            item in value[:index] for index, item in enumerate(value)
        ):
            violations.append(f"{path} must contain unique items")
        for index, item in enumerate(value):
            _validate_value(item, spec.items, f"{path}[{index}]", violations)
        return
    if isinstance(spec, MappingSpec):
        if not isinstance(value, Mapping):
            violations.append(f"{path} must be an object")
            return
        if len(value) < spec.min_items:
            violations.append(f"{path} must contain at least {spec.min_items} fields")
        for key, item in value.items():
            if not isinstance(key, str) or (
                spec.key_pattern is not None
                and re.fullmatch(spec.key_pattern, key) is None
            ):
                violations.append(f"{path} has an invalid key {key!r}")
                continue
            _validate_value(item, spec.values, f"{path}.{key}", violations)
        return
    if isinstance(spec, ObjectSpec):
        if not isinstance(value, Mapping):
            violations.append(f"{path} must be an object")
            return
        missing = sorted(spec.required.difference(value))
        if missing:
            violations.append(f"{path} is missing required fields: {missing}")
        for key, item in value.items():
            child_spec = spec.fields.get(key)
            if child_spec is not None:
                _validate_value(item, child_spec, f"{path}.{key}", violations)
            elif (
                spec.allow_comments
                and isinstance(key, str)
                and key.startswith("_comment")
            ):
                if not isinstance(item, str):
                    violations.append(f"{path}.{key} comment must be a string")
            else:
                violations.append(f"{path}.{key} is an unknown field")
        return
    raise TypeError(f"unsupported schema specification at {path}: {spec!r}")


def validate_composed_config(config: Mapping[str, object]) -> None:
    """Validate invariants spanning separately owned v3 fragments."""
    violations: list[str] = []
    strategy = _mapping(config.get("strategy"))
    scaling = _mapping(strategy.get("capital_scaling"))
    risk = _mapping(config.get("risk"))
    limits = _mapping(risk.get("limits"))
    symbols = config.get("symbols")

    registered = strategy.get("registered_models")
    primary = strategy.get("primary_model")
    if isinstance(registered, list) and primary is not None:
        if primary not in registered:
            violations.append(
                "strategy.primary_model must be present in strategy.registered_models"
            )
        for model in registered:
            if model not in strategy:
                violations.append(
                    f"strategy.registered_models requires strategy.{model}"
                )
        expected_name = {
            "glft": "GLFT_MultiScale",
            "avellaneda_stoikov": "AvellanedaStoikov",
        }.get(primary)
        if expected_name is not None and strategy.get("name") != expected_name:
            violations.append(
                "strategy.name must match strategy.primary_model's stable strategy ID"
            )

    if scaling and isinstance(symbols, list):
        concurrent = scaling.get("target_concurrent_symbols")
        if isinstance(concurrent, int) and concurrent > len(symbols):
            violations.append(
                "strategy.capital_scaling.target_concurrent_symbols must not "
                "exceed the number of symbols"
            )
        order = _finite_number(scaling.get("target_order_notional"))
        factor = _finite_number(scaling.get("order_notional_limit_factor"))
        total = _finite_number(scaling.get("target_total_risk_notional"))
        reference = _finite_number(scaling.get("reference_capital_usdt"))
        daily_loss = _finite_number(scaling.get("target_daily_loss"))
        drawdown = _finite_number(limits.get("max_drawdown_pct"))
        if None not in (order, factor, total) and order * factor > total:
            violations.append(
                "scaled order-notional limit must not exceed "
                "strategy.capital_scaling.target_total_risk_notional"
            )
        if None not in (total, reference) and total > reference:
            violations.append(
                "strategy.capital_scaling.target_total_risk_notional must not "
                "exceed reference_capital_usdt"
            )
        if (
            None not in (daily_loss, reference, drawdown)
            and daily_loss > reference * drawdown
        ):
            violations.append(
                "strategy.capital_scaling.target_daily_loss must not exceed "
                "reference_capital_usdt * risk.limits.max_drawdown_pct"
            )
        weights = scaling.get("budget_asset_weights")
        if isinstance(weights, Mapping):
            quote_assets = {_quote_asset(symbol) for symbol in symbols}
            if "" in quote_assets:
                violations.append(
                    "symbols must use a supported quote asset for capital budgets"
                )
            elif set(weights) != quote_assets:
                violations.append(
                    "strategy.capital_scaling.budget_asset_weights must contain "
                    "exactly the quote assets used by symbols"
                )

    readiness = _mapping(strategy.get("model_readiness"))
    readiness_models = _mapping(readiness.get("models"))
    global_volatility = readiness.get("min_volatility_samples")
    global_model = readiness.get("min_model_samples")
    for model, model_config in readiness_models.items():
        configured = _mapping(model_config)
        if (
            isinstance(global_volatility, int)
            and configured.get("min_volatility_samples", 0) < global_volatility
        ):
            violations.append(
                f"strategy.model_readiness.models.{model}.min_volatility_samples "
                "must not be below the global minimum"
            )
        if (
            isinstance(global_model, int)
            and configured.get("min_model_samples", 0) < global_model
        ):
            violations.append(
                f"strategy.model_readiness.models.{model}.min_model_samples "
                "must not be below the global minimum"
            )

    system = _mapping(config.get("system"))
    rate_limit = _mapping(system.get("binance_rest_rate_limit"))
    if rate_limit:
        reserves = rate_limit.get("trading_reserve", 0) + rate_limit.get(
            "emergency_reserve", 0
        )
        if reserves >= rate_limit.get("request_weight_limit", 0):
            violations.append(
                "system.binance_rest_rate_limit reserves must leave positive "
                "general request capacity"
            )

    event_engine = _mapping(system.get("event_engine"))
    capacities = _mapping(event_engine.get("queue_capacity"))
    warnings = _mapping(event_engine.get("queue_warn_depth"))
    for lane in ("market", "execution", "cold"):
        if lane in capacities and warnings.get(lane, 0) > capacities[lane]:
            violations.append(
                f"system.event_engine.queue_warn_depth.{lane} must not exceed "
                f"queue_capacity.{lane}"
            )

    runtime = _mapping(system.get("strategy_runtime"))
    if runtime:
        queue_capacity = runtime.get("control_queue_capacity", 0)
        warn_depth = runtime.get("warn_queue_depth", 0)
        queue_warn_depth = runtime.get("queue_warn_depth", 0)
        freeze_depth = runtime.get("freeze_queue_depth", 0)
        if not warn_depth <= queue_warn_depth <= freeze_depth <= queue_capacity:
            violations.append(
                "system.strategy_runtime queue thresholds must satisfy "
                "warn_queue_depth <= queue_warn_depth <= freeze_queue_depth "
                "<= control_queue_capacity"
            )
        if runtime.get("warn_backlog_ms", 0) > runtime.get("freeze_backlog_ms", 0):
            violations.append(
                "system.strategy_runtime.warn_backlog_ms must not exceed "
                "freeze_backlog_ms"
            )
        if runtime.get("async_worker_warn_deferred", 0) > runtime.get(
            "async_worker_freeze_deferred", 0
        ):
            violations.append(
                "system.strategy_runtime async worker warning threshold must "
                "not exceed the freeze threshold"
            )

    resources = _mapping(system.get("resource_monitor"))
    if resources:
        for warning, freeze in (
            ("rss_warn_bytes", "rss_freeze_bytes"),
            ("max_main_threads", "max_total_threads"),
            ("max_main_fds", "max_total_fds"),
        ):
            if resources.get(warning, 0) > resources.get(freeze, 0):
                violations.append(
                    f"system.resource_monitor.{warning} must not exceed {freeze}"
                )
        if resources.get("recovery_checks", 0) < resources.get("breach_checks", 0):
            violations.append(
                "system.resource_monitor.recovery_checks must not be below "
                "breach_checks"
            )

    time_sync = _mapping(system.get("time_sync"))
    if time_sync:
        low_rtt = time_sync.get("low_rtt_sample_count", 0)
        successful = time_sync.get("min_successful_samples", 0)
        sample_count = time_sync.get("sample_count", 0)
        if not low_rtt <= successful <= sample_count:
            violations.append(
                "system.time_sync sample counts must satisfy "
                "low_rtt_sample_count <= min_successful_samples <= sample_count"
            )
        if time_sync.get("max_phase_error_ms", 0) >= time_sync.get(
            "halt_phase_error_ms", 0
        ):
            violations.append(
                "system.time_sync.max_phase_error_ms must be below halt_phase_error_ms"
            )
        if time_sync.get("freeze_breach_threshold", 0) > time_sync.get(
            "halt_breach_threshold", 0
        ):
            violations.append(
                "system.time_sync.freeze_breach_threshold must not exceed "
                "halt_breach_threshold"
            )
        if time_sync.get("max_offset_dispersion_ms", math.inf) > time_sync.get(
            "max_uncertainty_ms", -math.inf
        ):
            violations.append(
                "system.time_sync.max_offset_dispersion_ms must not exceed "
                "max_uncertainty_ms"
            )
        if time_sync.get("max_uncertainty_ms", math.inf) > time_sync.get(
            "max_rtt_ms", -math.inf
        ):
            violations.append(
                "system.time_sync.max_uncertainty_ms must not exceed max_rtt_ms"
            )

    paper = _mapping(config.get("paper_trade"))
    market_data = _mapping(system.get("market_data"))
    execution = _mapping(config.get("execution"))
    if execution and paper:
        mode_is_paper = execution.get("mode") == "paper"
        if paper.get("enabled") is not mode_is_paper:
            violations.append(
                "paper_trade.enabled must be true exactly when execution.mode='paper'"
            )
    if paper and market_data:
        environment = market_data.get("environment")
        if paper.get("market_data_environment") != environment:
            violations.append(
                "paper_trade.market_data_environment must equal "
                "system.market_data.environment"
            )
        if market_data.get("testnet") is not (environment == "testnet"):
            violations.append(
                "system.market_data.testnet must match market_data.environment"
            )
        if market_data.get("public_only") is not True:
            violations.append(
                "Paper market data must set system.market_data.public_only=true"
            )
        if paper.get("mark_rest_poll_interval_sec", math.inf) > paper.get(
            "mark_ws_stale_after_sec", -math.inf
        ):
            violations.append(
                "paper_trade.mark_rest_poll_interval_sec must not exceed "
                "mark_ws_stale_after_sec"
            )
    if market_data:
        published_depth = market_data.get("publish_depth_levels", math.inf)
        max_levels = market_data.get("max_orderbook_levels_per_side", -math.inf)
        delta_levels = market_data.get("max_delta_levels_per_side", math.inf)
        if published_depth > max_levels:
            violations.append(
                "system.market_data.publish_depth_levels must not exceed "
                "max_orderbook_levels_per_side"
            )
        if delta_levels > max_levels:
            violations.append(
                "system.market_data.max_delta_levels_per_side must not exceed "
                "max_orderbook_levels_per_side"
            )

    tech_health = _mapping(risk.get("tech_health"))
    if market_data and tech_health:
        if market_data.get("max_market_event_ingress_age_ms", math.inf) > (
            tech_health.get("max_latency_ms", -math.inf)
        ):
            violations.append(
                "system.market_data.max_market_event_ingress_age_ms must not "
                "exceed risk.tech_health.max_latency_ms"
            )
    price_sanity = _mapping(risk.get("price_sanity"))
    black_swan = _mapping(risk.get("black_swan"))
    if price_sanity and price_sanity.get("max_spread_pct", math.inf) > price_sanity.get(
        "max_deviation_pct", -math.inf
    ):
        violations.append(
            "risk.price_sanity.max_spread_pct must not exceed max_deviation_pct"
        )
    if (
        price_sanity
        and black_swan
        and price_sanity.get("max_deviation_pct", math.inf)
        > black_swan.get("volatility_halt_threshold", -math.inf)
    ):
        violations.append(
            "risk.price_sanity.max_deviation_pct must not exceed "
            "risk.black_swan.volatility_halt_threshold"
        )

    dashboard = _mapping(system.get("web_dashboard"))
    if dashboard.get("enabled") is True and dashboard.get("port") == system.get(
        "web_port"
    ):
        violations.append("system.web_dashboard.port must differ from system.web_port")
    admin = _mapping(system.get("admin_control"))
    if admin and admin.get("session_max_age_sec", math.inf) > admin.get(
        "command_ttl_sec", -math.inf
    ):
        violations.append(
            "system.admin_control.session_max_age_sec must not exceed command_ttl_sec"
        )
    alert = _mapping(config.get("alert"))
    if alert.get("active") is True and (
        not str(alert.get("telegram_token", "")).strip()
        or not str(alert.get("telegram_chat_id", "")).strip()
    ):
        violations.append(
            "active alerts require non-empty telegram_token and telegram_chat_id"
        )

    database = _mapping(config.get("paper_trade_database"))
    if database and database.get("write_batch_size", 0) > database.get(
        "queue_capacity", 0
    ):
        violations.append(
            "paper_trade_database.write_batch_size must not exceed queue_capacity"
        )
    recorder = _mapping(config.get("data_recorder"))
    if recorder and recorder.get("flush_threshold", 0) > recorder.get(
        "queue_capacity", 0
    ):
        violations.append(
            "data_recorder.flush_threshold must not exceed queue_capacity"
        )
    oms = _mapping(config.get("oms"))
    if oms and oms.get("trade_recovery_overlap_ms", 0) > oms.get(
        "trade_recovery_lookback_ms", 0
    ):
        violations.append(
            "oms.trade_recovery_overlap_ms must not exceed trade_recovery_lookback_ms"
        )

    for model_name in ("glft", "avellaneda_stoikov"):
        model = _mapping(strategy.get(model_name))
        adaptive = _mapping(model.get("adaptive"))
        markout = _mapping(adaptive.get("markout"))
        horizons = markout.get("horizons_ms")
        if isinstance(horizons, list) and horizons != sorted(horizons):
            violations.append(
                f"strategy.{model_name}.adaptive.markout.horizons_ms must be sorted"
            )
        sizing = _mapping(adaptive.get("size_optimization"))
        candidates = sizing.get("candidate_multipliers")
        if isinstance(candidates, list) and candidates != sorted(candidates):
            violations.append(
                f"strategy.{model_name}.adaptive.size_optimization."
                "candidate_multipliers must be sorted"
            )
    glft = _mapping(strategy.get("glft"))
    rpi_intensity = _mapping(glft.get("rpi_intensity"))
    if rpi_intensity and rpi_intensity.get("min_k_per_bps", 0) >= (
        rpi_intensity.get("max_k_per_bps", 0)
    ):
        violations.append(
            "strategy.glft.rpi_intensity.min_k_per_bps must be below max_k_per_bps"
        )
    calibrator = _mapping(glft.get("calibrator"))
    if calibrator and calibrator.get("min_samples", 0) > calibrator.get("window", 0):
        violations.append("strategy.glft.calibrator.min_samples must not exceed window")

    if violations:
        raise ConfigSchemaError(
            "cross-fragment configuration validation failed: " + "; ".join(violations)
        )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _quote_asset(value: object) -> str:
    symbol = str(value or "")
    for suffix in ("USDT", "USDC", "BUSD", "FDUSD"):
        if symbol.endswith(suffix):
            return suffix
    return ""
