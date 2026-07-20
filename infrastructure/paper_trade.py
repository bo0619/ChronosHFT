from copy import deepcopy


PAPER_EXECUTION_MODE = "paper"
LIVE_EXECUTION_MODE = "live"
RISK_MANAGER_HEARTBEAT_SOURCE = "risk_manager"
PAPER_OMS_JOURNAL_PATH = "storage/paper/oms_journal.jsonl"
PAPER_OMS_FENCE_PATH = "storage/paper/oms_journal.jsonl.lock"


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _explicit_execution_mode(config: dict) -> str:
    execution = config.get("execution", {})
    if not isinstance(execution, dict):
        execution = {}
    raw_mode = str(execution.get("mode", "") or "").strip().lower()
    if not raw_mode:
        return ""
    if raw_mode in {"paper", "paper_trade", "simulation", "sim"}:
        return PAPER_EXECUTION_MODE
    if raw_mode in {"live", "production", "real"}:
        return LIVE_EXECUTION_MODE
    raise ValueError(f"Unsupported execution.mode: {raw_mode}")


def _legacy_paper_enabled(config: dict) -> bool:
    paper_trade = config.get("paper_trade", {})
    paper_enabled = (
        _as_bool(paper_trade.get("enabled", False))
        if isinstance(paper_trade, dict)
        else _as_bool(paper_trade)
    )
    system = config.get("system", {})
    dry_run = (
        _as_bool(system.get("dry_run", False))
        if isinstance(system, dict)
        else False
    )
    return paper_enabled or dry_run


def is_paper_trade(config: dict) -> bool:
    """Return whether execution is paper-only, rejecting contradictory modes."""
    if not isinstance(config, dict):
        return False

    explicit_mode = _explicit_execution_mode(config)
    legacy_enabled = _legacy_paper_enabled(config)
    if explicit_mode == LIVE_EXECUTION_MODE and legacy_enabled:
        raise ValueError(
            "Conflicting execution configuration: execution.mode=live with paper mode enabled"
        )
    if explicit_mode:
        return explicit_mode == PAPER_EXECUTION_MODE
    return legacy_enabled


def apply_paper_trade_mode(config: dict) -> dict:
    """Normalize Paper Trade to production public data with no private credentials."""
    if not isinstance(config, dict):
        return {}

    configured = deepcopy(config)
    if not is_paper_trade(configured):
        return configured

    execution = configured.get("execution")
    if not isinstance(execution, dict):
        execution = {}
        configured["execution"] = execution
    execution["mode"] = PAPER_EXECUTION_MODE

    paper_trade = configured.get("paper_trade")
    if not isinstance(paper_trade, dict):
        paper_trade = {}
        configured["paper_trade"] = paper_trade
    paper_trade["enabled"] = True
    paper_trade["market_data_environment"] = "production"
    if not _as_bool(paper_trade.get("reset_on_start", True)):
        raise ValueError(
            "Paper Trade venue persistence is not implemented; "
            "paper_trade.reset_on_start must be true"
        )
    paper_trade["reset_on_start"] = True

    # `testnet` historically controls every Binance connection. Paper execution
    # intentionally uses production public market data and owns no private venue
    # connection, so normalize this legacy switch to production.
    configured["testnet"] = False

    system = configured.get("system")
    if not isinstance(system, dict):
        system = {}
        configured["system"] = system
    system["dry_run"] = True
    market_data = system.get("market_data")
    if not isinstance(market_data, dict):
        market_data = {}
        system["market_data"] = market_data
    market_data["environment"] = "production"
    market_data["testnet"] = False
    market_data["public_only"] = True

    # Keep simulated execution state completely separate from live OMS state.
    # This prevents a Paper bootstrap/replay or lock acquisition from touching
    # the production journal and writer fence.
    oms = configured.get("oms")
    if not isinstance(oms, dict):
        oms = {}
        configured["oms"] = oms
    oms["journal_path"] = PAPER_OMS_JOURNAL_PATH
    # The local venue ledger is intentionally reset for every process. Never
    # replay an older OMS projection against that fresh ledger.
    oms["replay_journal_on_startup"] = False
    single_writer_fence = oms.get("single_writer_fence")
    if not isinstance(single_writer_fence, dict):
        single_writer_fence = {}
        oms["single_writer_fence"] = single_writer_fence
    single_writer_fence["path"] = PAPER_OMS_FENCE_PATH

    # Prevent later secret resolution from rehydrating credentials from the
    # process environment. Paper runtime must not even hand credentials to a
    # component that could accidentally gain private API behavior.
    for field in ("api_key", "api_secret", "api_key_env", "api_secret_env"):
        configured[field] = ""

    risk = configured.get("risk")
    if not isinstance(risk, dict):
        risk = {}
        configured["risk"] = risk

    independent_supervisor = risk.get("independent_supervisor")
    if not isinstance(independent_supervisor, dict):
        independent_supervisor = {}
        risk["independent_supervisor"] = independent_supervisor
    independent_supervisor["enabled"] = False
    independent_supervisor["flatten_enabled"] = False
    independent_supervisor["daily_loss_enabled"] = False
    for field in ("api_key", "api_secret", "api_key_env", "api_secret_env"):
        independent_supervisor[field] = ""

    cash_flow_truth = risk.get("cash_flow_truth")
    if not isinstance(cash_flow_truth, dict):
        cash_flow_truth = {}
        risk["cash_flow_truth"] = cash_flow_truth
    cash_flow_truth["enabled"] = False
    cash_flow_truth["require_snapshot"] = False

    heartbeat = risk.get("risk_control_heartbeat")
    if not isinstance(heartbeat, dict):
        heartbeat = {}
        risk["risk_control_heartbeat"] = heartbeat
    heartbeat["enabled"] = True
    # RiskManager publishes this exact source from its live safety loop.
    heartbeat["required_source"] = RISK_MANAGER_HEARTBEAT_SOURCE

    return configured
