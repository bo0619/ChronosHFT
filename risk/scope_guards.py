"""Symbol and venue guard ownership for RiskManager."""

from __future__ import annotations

from collections import defaultdict

from infrastructure.logger import logger


class ScopeGuardField:
    """Expose one declared guard state field through the compatibility facade."""

    def __init__(self, attribute: str):
        self.attribute = attribute

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.scope_guards, self.attribute)

    def __set__(self, instance, value) -> None:
        setattr(instance.scope_guards, self.attribute, value)


class ScopeGuardMethod:
    """Bind a RiskManager method to its scope-guard controller."""

    def __init__(self, method_name: str | None = None):
        self.method_name = method_name

    def __set_name__(self, owner, name: str) -> None:
        if self.method_name is None:
            self.method_name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.scope_guards, self.method_name)

    def __set__(self, instance, value) -> None:
        setattr(instance.scope_guards, self.method_name, value)


class RiskScopeGuardController:
    """Own symbol/venue guard state and compare-and-clear recovery."""

    def __init__(
        self,
        *,
        oms,
        gateway,
        log_warn,
        trigger_kill_switch,
        tracked_symbols,
        symbol_recovery_updates: int,
        venue_recovery_updates: int,
        max_frozen_symbols_before_kill: int,
    ) -> None:
        self.oms = oms
        self.gateway = gateway
        self._log_warn = log_warn
        self.trigger_kill_switch = trigger_kill_switch
        self._tracked_symbols = tracked_symbols
        self.symbol_freeze_recovery_updates = symbol_recovery_updates
        self.venue_freeze_recovery_updates = venue_recovery_updates
        self.max_frozen_symbols_before_kill = max_frozen_symbols_before_kill
        self.latency_recovery_by_symbol = defaultdict(int)
        self.divergence_recovery_by_symbol = defaultdict(int)
        self.frozen_symbols = {}
        self.symbol_freeze_epochs = {}
        self.symbol_freeze_owners = {}
        self.frozen_venues = {}
        self.venue_freeze_epochs = {}
        self.venue_recovery_by_venue = defaultdict(int)

    @staticmethod
    def _risk_symbol_guard_owner(reason: str) -> str:
        return str(reason or "").split(":", 1)[0]

    def _refresh_local_symbol_guard(self, symbol: str) -> None:
        owners = self.symbol_freeze_owners.get(symbol, {})
        if not owners:
            self.symbol_freeze_owners.pop(symbol, None)
            self.frozen_symbols.pop(symbol, None)
            self.symbol_freeze_epochs.pop(symbol, None)
            return
        newest = max(
            owners.values(),
            key=lambda record: int(record.get("epoch", 0) or 0),
        )
        self.frozen_symbols[symbol] = str(newest.get("reason", "") or "")
        self.symbol_freeze_epochs[symbol] = int(newest.get("epoch", 0) or 0)

    def _owned_symbol_reason(self, symbol: str, prefix: str) -> str:
        symbol = str(symbol or "").upper()
        owner = self._risk_symbol_guard_owner(prefix)
        record = self.symbol_freeze_owners.get(symbol, {}).get(owner, {})
        return str(record.get("reason", "") or "")

    def _freeze_symbol(self, symbol: str, reason: str):
        if not symbol:
            return

        symbol = symbol.upper()
        owner = self._risk_symbol_guard_owner(reason)
        owners = self.symbol_freeze_owners.setdefault(symbol, {})
        existing_reason = str(
            (owners.get(owner) or {}).get("reason", "") or ""
        )
        # A live latency figure changes on every update. Re-freezing the same
        # owner for each numeric variation repeats cancellation, journaling,
        # logging and event publication, amplifying an already stale queue.
        # Keep the first durable guard until its guarded recovery clears it.
        if existing_reason:
            return

        logger.error(f"[Risk] Symbol circuit breaker {symbol}: {reason}")
        self._log_warn(f"Symbol frozen {symbol}: {reason}")
        if self.oms and hasattr(self.oms, "freeze_symbol"):
            try:
                epoch = self.oms.freeze_symbol(
                    symbol,
                    reason,
                    cancel_active_orders=True,
                )
                if epoch is None:
                    get_owners = getattr(
                        self.oms,
                        "get_symbol_freeze_owners",
                        None,
                    )
                    if callable(get_owners):
                        remote_owner = (get_owners(symbol) or {}).get(owner, {})
                        if remote_owner.get("reason") == reason:
                            epoch = remote_owner.get("epoch")
                if epoch is None:
                    get_epoch = getattr(self.oms, "get_symbol_freeze_epoch", None)
                    get_reason = getattr(self.oms, "get_symbol_freeze_reason", None)
                    if callable(get_epoch) and (
                        not callable(get_reason) or get_reason(symbol) == reason
                    ):
                        epoch = get_epoch(symbol)
            except Exception as exc:
                logger.error(f"[Risk] oms.freeze_symbol({symbol}) failed: {exc}")
                epoch = None
        else:
            epoch = max(
                (int(record.get("epoch", 0) or 0) for record in owners.values()),
                default=0,
            ) + 1
        owners[owner] = {
            "reason": reason,
            "epoch": int(epoch or 0),
        }
        self._refresh_local_symbol_guard(symbol)
        self._maybe_escalate_symbol_freeze(reason)

    def _clear_owned_symbol_freeze(
        self,
        symbol: str,
        expected_reason: str,
        recovery_reason: str,
    ) -> bool:
        symbol = str(symbol or "").upper()
        if not symbol:
            return False
        owner = self._risk_symbol_guard_owner(expected_reason)
        owners = self.symbol_freeze_owners.setdefault(symbol, {})
        owner_record = owners.get(owner)
        if owner_record is None:
            legacy_reason = self.frozen_symbols.get(symbol, "")
            if legacy_reason == expected_reason:
                owner_record = {
                    "reason": expected_reason,
                    "epoch": int(self.symbol_freeze_epochs.get(symbol, 0) or 0),
                }
                owners[owner] = owner_record
        if not self.oms or not hasattr(self.oms, "clear_symbol_freeze"):
            owners.pop(owner, None)
            self._refresh_local_symbol_guard(symbol)
            return True

        expected_epoch = (
            int(owner_record.get("epoch", 0) or 0)
            if owner_record is not None
            else None
        )
        get_reason = getattr(self.oms, "get_symbol_freeze_reason", None)
        get_epoch = getattr(self.oms, "get_symbol_freeze_epoch", None)
        get_owners = getattr(self.oms, "get_symbol_freeze_owners", None)
        current_reason = get_reason(symbol) if callable(get_reason) else expected_reason
        if (expected_epoch is None or expected_epoch <= 0) and callable(get_owners):
            remote_record = (get_owners(symbol) or {}).get(owner, {})
            if remote_record.get("reason") == expected_reason:
                expected_epoch = int(remote_record.get("epoch", 0) or 0)
                owners[owner] = {
                    "reason": expected_reason,
                    "epoch": expected_epoch,
                }
        if (
            (expected_epoch is None or expected_epoch <= 0)
            and current_reason == expected_reason
            and callable(get_epoch)
        ):
            expected_epoch = int(get_epoch(symbol))
            owners[owner] = {
                "reason": expected_reason,
                "epoch": expected_epoch,
            }
        if expected_epoch is None or expected_epoch <= 0:
            logger.error(
                "[Risk] Refusing unguarded symbol recovery: "
                f"symbol={symbol} reason={expected_reason} epoch unavailable"
            )
            return False

        try:
            cleared = bool(
                self.oms.clear_symbol_freeze(
                    symbol,
                    reason=recovery_reason,
                    expected_epoch=int(expected_epoch),
                    expected_reason=expected_reason,
                )
            )
        except Exception as exc:
            logger.error(f"[Risk] oms.clear_symbol_freeze({symbol}) failed: {exc}")
            return False

        if not cleared:
            if callable(get_owners):
                remote_record = (get_owners(symbol) or {}).get(owner)
                if (
                    not remote_record
                    or remote_record.get("reason") != expected_reason
                    or int(remote_record.get("epoch", 0) or 0)
                    != int(expected_epoch)
                ):
                    owners.pop(owner, None)
                    self._refresh_local_symbol_guard(symbol)
                    return True
            current_reason = get_reason(symbol) if callable(get_reason) else expected_reason
            if current_reason and current_reason != expected_reason:
                logger.warning(
                    "[Risk] Symbol recovery ownership superseded; stale clear ignored: "
                    f"symbol={symbol} expected={expected_reason} current={current_reason}"
                )
                owners.pop(owner, None)
                self._refresh_local_symbol_guard(symbol)
                return True
            if not current_reason:
                owners.pop(owner, None)
                self._refresh_local_symbol_guard(symbol)
                return True
            return False

        owners.pop(owner, None)
        self._refresh_local_symbol_guard(symbol)
        return True

    def _recover_symbol_if_stable(self, symbol: str, prefix: str):
        frozen_reason = self._owned_symbol_reason(symbol, prefix)
        if not frozen_reason.startswith(prefix):
            return

        if prefix == "latency:":
            self.latency_recovery_by_symbol[symbol] += 1
            stable_updates = self.latency_recovery_by_symbol[symbol]
        else:
            self.divergence_recovery_by_symbol[symbol] += 1
            stable_updates = self.divergence_recovery_by_symbol[symbol]

        if stable_updates < self.symbol_freeze_recovery_updates:
            return

        recovery_reason = (
            f"{prefix.rstrip(':')} recovered after "
            f"{stable_updates} healthy updates"
        )
        if not self._clear_owned_symbol_freeze(
            symbol,
            frozen_reason,
            recovery_reason,
        ):
            return
        logger.info(f"[Risk] Symbol circuit breaker cleared {symbol}: {prefix}recovered")
        self._log_warn(f"Symbol restored {symbol}: {prefix}recovered")

        self.latency_recovery_by_symbol[symbol] = 0
        self.divergence_recovery_by_symbol[symbol] = 0

    def _current_venue(self):
        venue = ""
        if self.gateway:
            venue = getattr(self.gateway, "gateway_name", "") or venue
        if not venue and self.oms:
            venue = getattr(getattr(self.oms, "gateway", None), "gateway_name", "") or venue
        return (venue or "UNKNOWN").upper()

    def _freeze_venue(self, venue: str, reason: str):
        if not venue:
            return

        venue = venue.upper()
        existing_reason = self.frozen_venues.get(venue, "")
        existing_owner = self._risk_symbol_guard_owner(existing_reason)
        next_owner = self._risk_symbol_guard_owner(reason)
        if existing_reason and existing_owner == next_owner:
            return
        self.frozen_venues[venue] = reason
        self.venue_recovery_by_venue[venue] = 0

        logger.error(f"[Risk] Venue circuit breaker {venue}: {reason}")
        self._log_warn(f"Venue frozen {venue}: {reason}")
        if self.oms and hasattr(self.oms, "freeze_venue"):
            try:
                freeze_epoch = self.oms.freeze_venue(
                    venue,
                    reason,
                    cancel_active_orders=False,
                )
                if freeze_epoch is None:
                    current_reason = getattr(
                        self.oms,
                        "get_venue_freeze_reason",
                        lambda *_args, **_kwargs: "",
                    )(venue)
                    get_epoch = getattr(
                        self.oms,
                        "get_venue_freeze_epoch",
                        None,
                    )
                    if current_reason == reason and callable(get_epoch):
                        freeze_epoch = get_epoch(venue)
                if freeze_epoch is not None:
                    self.venue_freeze_epochs[venue] = int(freeze_epoch)
                else:
                    self.venue_freeze_epochs.pop(venue, None)
                    logger.error(
                        f"[Risk] Venue freeze epoch unavailable for {venue}; "
                        "recovery will remain fail-closed"
                    )
            except Exception as exc:
                self.venue_freeze_epochs.pop(venue, None)
                logger.error(f"[Risk] oms.freeze_venue({venue}) failed: {exc}")

    def _recover_venue_if_stable(self, venue: str, prefix: str):
        venue = (venue or "UNKNOWN").upper()
        frozen_reason = self.frozen_venues.get(venue, "")
        if not frozen_reason.startswith(prefix):
            return

        self.venue_recovery_by_venue[venue] += 1
        stable_updates = self.venue_recovery_by_venue[venue]
        if stable_updates < self.venue_freeze_recovery_updates:
            return

        expected_epoch = self.venue_freeze_epochs.pop(venue, None)
        self.frozen_venues.pop(venue, None)
        self.venue_recovery_by_venue[venue] = 0
        cleared = False
        if self.oms and hasattr(self.oms, "clear_venue_freeze"):
            if expected_epoch is None:
                logger.error(
                    f"[Risk] Refusing unguarded venue recovery for {venue}: "
                    "freeze epoch unavailable"
                )
            else:
                try:
                    cleared = bool(
                        self.oms.clear_venue_freeze(
                            venue,
                            reason=(
                                f"{prefix.rstrip(':')} recovered after "
                                f"{stable_updates} healthy updates"
                            ),
                            expected_epoch=expected_epoch,
                            expected_reason=frozen_reason,
                        )
                    )
                except Exception as exc:
                    logger.error(
                        f"[Risk] oms.clear_venue_freeze({venue}) failed: {exc}"
                    )

        if cleared:
            logger.info(
                f"[Risk] Venue circuit breaker cleared {venue}: {prefix}recovered"
            )
            self._log_warn(f"Venue restored {venue}: {prefix}recovered")
        else:
            logger.warning(
                f"[Risk] Local venue breaker recovered for {venue}, but the "
                "OMS guard was retained or already replaced"
            )

    def _maybe_escalate_symbol_freeze(self, trigger_reason: str):
        tracked_symbols = self._tracked_symbols()
        if not tracked_symbols:
            return

        threshold = self.max_frozen_symbols_before_kill
        if threshold <= 0:
            threshold = len(tracked_symbols) if len(tracked_symbols) > 1 else 0
        if threshold <= 0:
            return

        frozen_count = len({symbol for symbol in tracked_symbols if symbol in self.frozen_symbols})
        if frozen_count >= threshold:
            self.trigger_kill_switch(
                f"Symbol circuit breakers exhausted ({frozen_count}/{len(tracked_symbols)}): {trigger_reason}"
            )
