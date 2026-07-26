"""Symbol, venue and strategy guard ownership for the OMS."""

from __future__ import annotations

from infrastructure.logger import logger

from .component import OMSComponent


class OMSGuardManager(OMSComponent):
    """Own layered trading guards and guarded recovery transitions."""

    @staticmethod
    def _symbol_guard_owner(reason: str) -> str:
        reason = str(reason or "").strip()
        parts = reason.split(":")
        prefix = parts[0] if parts else ""
        if prefix in {"latency", "divergence", "stale_market_data"}:
            return prefix
        if prefix == "truth_plane" and len(parts) > 1:
            return ":".join(parts[:2])
        if prefix == "system_health" and len(parts) > 2:
            if parts[1] in {"FATAL_GAP", "ORDERBOOK_RESYNC_FAILED"}:
                # One current order-book integrity owner per symbol. A newer
                # recovery token replaces the older token, while its epoch and
                # reason prevent the older worker's CLEAR from succeeding.
                return "system_health:orderbook"
            return ":".join(parts[:2])
        if prefix == "order_truth" and len(parts) > 2:
            return f"order_truth:{parts[-1]}"
        return reason

    def _ensure_symbol_guard_records_locked(self, symbol: str) -> dict:
        if not hasattr(self, "symbol_guard_records"):
            self.symbol_guard_records = {}
        if not hasattr(self, "symbol_guard_epoch_counters"):
            self.symbol_guard_epoch_counters = {}
        records = self.symbol_guard_records.get(symbol)
        if records is not None:
            return records
        records = {}
        legacy_reason = self.symbol_guards.get(symbol, "")
        if legacy_reason:
            epoch = max(
                1,
                int(self.symbol_guard_epochs.get(symbol, 0) or 0),
            )
            records[self._symbol_guard_owner(legacy_reason)] = {
                "reason": legacy_reason,
                "epoch": epoch,
            }
            self.symbol_guard_epoch_counters[symbol] = max(
                epoch,
                int(self.symbol_guard_epoch_counters.get(symbol, 0) or 0),
            )
        self.symbol_guard_records[symbol] = records
        return records

    def _refresh_symbol_guard_effective_locked(self, symbol: str) -> None:
        records = self.symbol_guard_records.get(symbol, {})
        if not records:
            self.symbol_guard_records.pop(symbol, None)
            self.symbol_guards.pop(symbol, None)
            self.symbol_guard_epochs.pop(symbol, None)
            return
        _, newest = max(
            records.items(),
            key=lambda item: int(item[1].get("epoch", 0) or 0),
        )
        self.symbol_guards[symbol] = str(newest.get("reason", "") or "")
        self.symbol_guard_epochs[symbol] = int(newest.get("epoch", 0) or 0)

    def _install_symbol_guard_locked(
        self,
        symbol: str,
        reason: str,
    ) -> tuple[int, str]:
        owner = self._symbol_guard_owner(reason)
        previous_reason = self.symbol_guards.get(symbol, "")
        records = self._ensure_symbol_guard_records_locked(symbol)
        previous_owner_reason = str(
            (records.get(owner) or {}).get("reason", "") or ""
        )
        epoch = max(
            [
                int(self.symbol_guard_epoch_counters.get(symbol, 0) or 0),
                *(
                    int(record.get("epoch", 0) or 0)
                    for record in records.values()
                ),
            ]
        ) + 1
        audit_kind = (
            "symbol_frozen"
            if previous_owner_reason != reason
            else "symbol_freeze_reasserted"
        )
        self._audit(
            audit_kind,
            symbol=symbol,
            reason=reason,
            previous_reason=previous_reason,
            previous_owner_reason=previous_owner_reason,
            owner=owner,
            epoch=epoch,
        )
        self.symbol_guard_epoch_counters[symbol] = epoch
        records[owner] = {"reason": reason, "epoch": epoch}
        self._refresh_symbol_guard_effective_locked(symbol)
        self._refresh_outbound_gate_locked(f"symbol_frozen:{symbol}:{reason}")
        return epoch, previous_owner_reason

    def _enforce_symbol_guard(
        self,
        symbol: str,
        reason: str,
        epoch: int,
        cancel_active_orders: bool,
    ) -> None:
        if cancel_active_orders:
            self._cancel_all_orders_unchecked(
                symbol,
                source=f"symbol_freeze_immediate:{epoch}",
                bypass_message_budget=True,
            )
        drained = self._wait_for_outbound_risk_sends(
            f"symbol_freeze:{symbol}",
            symbol=symbol,
        )
        if cancel_active_orders:
            self._cancel_all_orders_unchecked(
                symbol,
                source=f"symbol_freeze_post_drain:{epoch}",
                bypass_message_budget=True,
            )
        if not drained:
            self.halt_system(
                "Outbound symbol risk-send drain timed out during freeze: "
                f"{symbol}"
            )
        with self.lock:
            self._refresh_outbound_gate_locked(f"symbol_guarded:{symbol}")

    def freeze_symbol(
        self,
        symbol: str,
        reason: str,
        cancel_active_orders: bool = True,
    ):
        if not symbol:
            return None

        symbol = symbol.upper()
        reason = str(reason or "symbol_guarded")
        with self.lock:
            epoch, previous_owner_reason = self._install_symbol_guard_locked(
                symbol,
                reason,
            )

        if previous_owner_reason != reason:
            logger.error(f"[OMS] Symbol frozen {symbol}: {reason}")
        self._enforce_symbol_guard(
            symbol,
            reason,
            epoch,
            cancel_active_orders,
        )
        return epoch

    def clear_symbol_freeze(
        self,
        symbol: str,
        reason: str = "",
        expected_epoch: int | None = None,
        expected_reason: str = "",
        expected_owner: str = "",
    ):
        if not symbol:
            return False

        symbol = symbol.upper()
        with self.lock:
            records = self._ensure_symbol_guard_records_locked(symbol)
            current_reason = self.symbol_guards.get(symbol, "")
            current_epoch = int(self.symbol_guard_epochs.get(symbol, 0) or 0)
            target_owner = str(expected_owner or "")
            if target_owner:
                candidates = [(target_owner, records.get(target_owner))]
            else:
                candidates = list(records.items())
            candidates = [
                (owner, record)
                for owner, record in candidates
                if record
                and (
                    not expected_reason
                    or str(record.get("reason", "") or "") == expected_reason
                )
                and (
                    expected_epoch is None
                    or int(record.get("epoch", 0) or 0) == int(expected_epoch)
                )
            ]
            if not expected_owner and not expected_reason and expected_epoch is None:
                if len(records) == 1:
                    candidates = list(records.items())
                else:
                    candidates = []
            if len(candidates) != 1:
                self._audit(
                    "symbol_unfreeze_stale_ignored",
                    symbol=symbol,
                    reason=reason,
                    expected_epoch=(
                        int(expected_epoch)
                        if expected_epoch is not None
                        else None
                    ),
                    current_epoch=current_epoch,
                    expected_reason=expected_reason,
                    current_reason=current_reason,
                    expected_owner=expected_owner,
                    active_owners=sorted(records),
                )
                return False
            target_owner, target_record = candidates[0]
            previous_reason = str(target_record.get("reason", "") or "")
            cleared_epoch = int(target_record.get("epoch", 0) or 0)
            remaining_reasons = [
                str(record.get("reason", "") or "")
                for candidate_owner, record in records.items()
                if candidate_owner != target_owner
            ]
            audit_kind = (
                "symbol_guard_cleared"
                if remaining_reasons
                else "symbol_unfrozen"
            )
            self._audit(
                audit_kind,
                symbol=symbol,
                reason=reason or previous_reason,
                previous_reason=previous_reason,
                owner=target_owner,
                epoch=cleared_epoch,
                remaining_reasons=remaining_reasons,
            )
            records.pop(target_owner, None)
            self._refresh_symbol_guard_effective_locked(symbol)
            self._refresh_outbound_gate_locked(
                reason or previous_reason or f"symbol_unfrozen:{symbol}"
            )
        if not previous_reason:
            return False

        if remaining_reasons:
            logger.info(
                f"[OMS] Symbol guard owner cleared {symbol}: "
                f"{reason or previous_reason}; remaining={remaining_reasons}"
            )
            audit_kind = "symbol_guard_cleared"
        else:
            logger.info(f"[OMS] Symbol restored {symbol}: {reason or previous_reason}")
        return True

    def get_symbol_freeze_reason(self, symbol: str) -> str:
        if not symbol:
            return ""
        with self.lock:
            return self.symbol_guards.get(symbol.upper(), "")

    def get_symbol_freeze_epoch(self, symbol: str) -> int:
        if not symbol:
            return 0
        with self.lock:
            return int(self.symbol_guard_epochs.get(symbol.upper(), 0) or 0)

    def get_symbol_freeze_owners(self, symbol: str) -> dict:
        if not symbol:
            return {}
        symbol = symbol.upper()
        with self.lock:
            records = self._ensure_symbol_guard_records_locked(symbol)
            return {
                owner: {
                    "reason": str(record.get("reason", "") or ""),
                    "epoch": int(record.get("epoch", 0) or 0),
                }
                for owner, record in records.items()
            }

    def clear_orderbook_freeze(
        self,
        symbol: str,
        recovery_token: str,
        reason: str = "orderbook_resynced",
    ) -> bool:
        symbol = str(symbol or "").upper()
        recovery_token = str(recovery_token or "").strip()
        if not symbol or not recovery_token:
            return False
        expected_reasons = {
            f"system_health:FATAL_GAP:{recovery_token}",
            f"system_health:ORDERBOOK_RESYNC_FAILED:{recovery_token}",
        }
        with self.lock:
            records = self._ensure_symbol_guard_records_locked(symbol)
            matches = [
                (owner, record)
                for owner, record in records.items()
                if str(record.get("reason", "") or "") in expected_reasons
            ]
            if len(matches) != 1:
                self._audit(
                    "orderbook_unfreeze_stale_ignored",
                    symbol=symbol,
                    recovery_token=recovery_token,
                    current_reason=self.symbol_guards.get(symbol, ""),
                    active_reasons=[
                        str(record.get("reason", "") or "")
                        for record in records.values()
                    ],
                )
                return False
            owner, record = matches[0]
            current_reason = str(record.get("reason", "") or "")
            current_epoch = int(record.get("epoch", 0) or 0)
        return self.clear_symbol_freeze(
            symbol,
            reason=reason,
            expected_epoch=current_epoch,
            expected_reason=current_reason,
            expected_owner=owner,
        )

    @staticmethod
    def _venue_guard_owner(reason: str) -> str:
        reason = str(reason or "").strip()
        parts = reason.split(":")
        prefix = parts[0] if parts else ""
        if prefix == "system_health":
            if len(parts) > 1 and parts[1] == "TRANSPORT":
                return "system_health:transport"
            if len(parts) > 1 and parts[1].startswith(
                (
                    "WS_",
                    "USER_STREAM_",
                    "MARKET_DATA_",
                )
            ):
                return "system_health:transport"
            return ":".join(parts[:2]) if len(parts) > 1 else reason
        if prefix == "truth_plane" and len(parts) > 1:
            return ":".join(parts[:2])
        return prefix or reason

    def _ensure_venue_guard_records_locked(self, venue: str) -> dict:
        if not hasattr(self, "venue_guard_records"):
            self.venue_guard_records = {}
        if not hasattr(self, "venue_guard_epoch_counters"):
            self.venue_guard_epoch_counters = {}
        records = self.venue_guard_records.get(venue)
        if records is not None:
            return records
        records = {}
        legacy_reason = self.venue_guards.get(venue, "")
        if legacy_reason:
            epoch = max(1, int(self.venue_guard_epochs.get(venue, 0) or 0))
            records[self._venue_guard_owner(legacy_reason)] = {
                "reason": legacy_reason,
                "epoch": epoch,
            }
            self.venue_guard_epoch_counters[venue] = max(
                epoch,
                int(self.venue_guard_epoch_counters.get(venue, 0) or 0),
            )
        self.venue_guard_records[venue] = records
        return records

    def _refresh_venue_guard_effective_locked(self, venue: str) -> None:
        records = self.venue_guard_records.get(venue, {})
        if not records:
            self.venue_guard_records.pop(venue, None)
            self.venue_guards.pop(venue, None)
            self.venue_guard_epochs.pop(venue, None)
            return
        _, newest = max(
            records.items(),
            key=lambda item: int(item[1].get("epoch", 0) or 0),
        )
        self.venue_guards[venue] = str(newest.get("reason", "") or "")
        self.venue_guard_epochs[venue] = int(newest.get("epoch", 0) or 0)

    def freeze_venue(self, venue: str, reason: str, cancel_active_orders: bool = True):
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        reason = str(reason or "venue_guarded")
        owner = self._venue_guard_owner(reason)
        with self.lock:
            previous_reason = self.venue_guards.get(venue, "")
            records = self._ensure_venue_guard_records_locked(venue)
            previous_owner_reason = str(
                (records.get(owner) or {}).get("reason", "") or ""
            )
            epoch = max(
                [
                    int(self.venue_guard_epoch_counters.get(venue, 0) or 0),
                    *(
                        int(record.get("epoch", 0) or 0)
                        for record in records.values()
                    ),
                ]
            ) + 1
            audit_kind = (
                "venue_frozen"
                if previous_owner_reason != reason
                else "venue_freeze_reasserted"
            )
            self._audit(
                audit_kind,
                venue=venue,
                reason=reason,
                previous_reason=previous_reason,
                previous_owner_reason=previous_owner_reason,
                owner=owner,
                epoch=epoch,
            )
            self.venue_guard_epoch_counters[venue] = epoch
            records[owner] = {"reason": reason, "epoch": epoch}
            self._refresh_venue_guard_effective_locked(venue)
            self._refresh_outbound_gate_locked(f"venue_frozen:{venue}:{reason}")

        if previous_owner_reason != reason:
            logger.error(f"[OMS] Venue frozen {venue}: {reason}")

        drained = self._wait_for_outbound_risk_sends(f"venue_freeze:{venue}")
        if not cancel_active_orders:
            return epoch

        try:
            for symbol in self._account_cancel_symbols():
                self._cancel_all_orders_unchecked(
                    symbol,
                    source="venue_freeze",
                )
        except Exception:
            pass
        if not drained:
            self.halt_system(
                f"Outbound risk-send drain timed out during venue freeze: {venue}"
            )
        return epoch

    def clear_venue_freeze(
        self,
        venue: str,
        reason: str = "",
        expected_epoch: int | None = None,
        expected_reason: str | None = None,
        expected_owner: str = "",
    ):
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        with self.lock:
            records = self._ensure_venue_guard_records_locked(venue)
            current_epoch = int(self.venue_guard_epochs.get(venue, 0) or 0)
            current_reason = self.venue_guards.get(venue, "")
            target_owner = str(expected_owner or "")
            if target_owner:
                candidates = [(target_owner, records.get(target_owner))]
            else:
                candidates = list(records.items())
            candidates = [
                (owner, record)
                for owner, record in candidates
                if record
                and (
                    expected_reason is None
                    or str(record.get("reason", "") or "")
                    == str(expected_reason)
                )
                and (
                    expected_epoch is None
                    or int(record.get("epoch", 0) or 0) == int(expected_epoch)
                )
            ]
            if not expected_owner and expected_reason is None and expected_epoch is None:
                candidates = list(records.items()) if len(records) == 1 else []
            if len(candidates) != 1:
                self._audit(
                    "venue_unfreeze_stale_ignored",
                    venue=venue,
                    reason=reason,
                    expected_epoch=(
                        int(expected_epoch)
                        if expected_epoch is not None
                        else None
                    ),
                    current_epoch=current_epoch,
                    expected_reason=expected_reason,
                    current_reason=current_reason,
                    expected_owner=expected_owner,
                    active_owners=sorted(records),
                )
                return False
            target_owner, target_record = candidates[0]
            previous_reason = str(target_record.get("reason", "") or "")
            cleared_epoch = int(target_record.get("epoch", 0) or 0)
            remaining_reasons = [
                str(record.get("reason", "") or "")
                for candidate_owner, record in records.items()
                if candidate_owner != target_owner
            ]
            audit_kind = (
                "venue_guard_cleared"
                if remaining_reasons
                else "venue_unfrozen"
            )
            self._audit(
                audit_kind,
                venue=venue,
                reason=reason or previous_reason,
                previous_reason=previous_reason,
                owner=target_owner,
                epoch=cleared_epoch,
                remaining_reasons=remaining_reasons,
            )
            records.pop(target_owner, None)
            self._refresh_venue_guard_effective_locked(venue)
            self._refresh_outbound_gate_locked(
                reason or previous_reason or f"venue_unfrozen:{venue}"
            )
        if not previous_reason:
            return False

        if remaining_reasons:
            logger.info(
                f"[OMS] Venue guard owner cleared {venue}: "
                f"{reason or previous_reason}; remaining={remaining_reasons}"
            )
            audit_kind = "venue_guard_cleared"
        else:
            logger.info(f"[OMS] Venue restored {venue}: {reason or previous_reason}")
        return True

    def get_venue_freeze_reason(self, venue: str = "") -> str:
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        with self.lock:
            return self.venue_guards.get(venue, "")

    def get_venue_freeze_epoch(self, venue: str = "") -> int:
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        with self.lock:
            return int(self.venue_guard_epochs.get(venue, 0) or 0)

    def get_venue_freeze_owners(self, venue: str = "") -> dict:
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        with self.lock:
            records = self._ensure_venue_guard_records_locked(venue)
            return {
                owner: {
                    "reason": str(record.get("reason", "") or ""),
                    "epoch": int(record.get("epoch", 0) or 0),
                }
                for owner, record in records.items()
            }

    def request_venue_recovery_verification(
        self,
        venue: str = "",
        reason: str = "transport_recovered",
        expected_owner: str = "",
        expected_epoch: int | None = None,
        expected_reason: str | None = None,
    ) -> bool:
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        with self.lock:
            if self._shutdown_requested or self._stopped:
                return False
            records = self._ensure_venue_guard_records_locked(venue)
            owner = str(expected_owner or "")
            candidates = (
                [(owner, records.get(owner))]
                if owner
                else list(records.items())
            )
            candidates = [
                (candidate_owner, record)
                for candidate_owner, record in candidates
                if record
                and (
                    expected_epoch is None
                    or int(record.get("epoch", 0) or 0)
                    == int(expected_epoch)
                )
                and (
                    expected_reason is None
                    or str(record.get("reason", "") or "")
                    == str(expected_reason)
                )
            ]
            if len(candidates) != 1:
                self._audit(
                    "venue_recovery_verification_stale_ignored",
                    venue=venue,
                    reason=reason,
                    expected_owner=expected_owner,
                    expected_epoch=expected_epoch,
                    expected_reason=expected_reason,
                    active_owners=sorted(records),
                )
                return False
            owner, record = candidates[0]
            guard_reason = str(record.get("reason", "") or "")
            epoch = int(record.get("epoch", 0) or 0)
        if not guard_reason or epoch <= 0:
            return False
        self._audit(
            "venue_recovery_verification_requested",
            venue=venue,
            epoch=epoch,
            owner=owner,
            reason=reason,
            guard_reason=guard_reason,
        )
        return bool(
            self.trigger_reconcile(
                f"Venue recovery verification: {venue}: {reason}",
                recovery_venue=venue,
                recovery_epoch=epoch,
                recovery_owner=owner,
                recovery_reason=guard_reason,
            )
        )

    def freeze_strategy(
        self,
        strategy_id: str,
        reason: str,
        symbol: str = "",
        cancel_active_orders: bool = True,
    ):
        strategy_id = (strategy_id or "").strip()
        if not strategy_id:
            return

        symbol = symbol.upper() if symbol else ""
        with self.lock:
            if symbol:
                key = (strategy_id, symbol)
                previous_reason = self.strategy_symbol_guards.get(key, "")
                self.strategy_symbol_guards[key] = reason
                payload = {
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "reason": reason,
                    "previous_reason": previous_reason,
                }
                log_message = f"[OMS] Strategy frozen {strategy_id}/{symbol}: {reason}"
                audit_kind = "strategy_symbol_frozen"
            else:
                previous_reason = self.strategy_guards.get(strategy_id, "")
                self.strategy_guards[strategy_id] = reason
                payload = {
                    "strategy_id": strategy_id,
                    "reason": reason,
                    "previous_reason": previous_reason,
                }
                log_message = f"[OMS] Strategy frozen {strategy_id}: {reason}"
                audit_kind = "strategy_frozen"
            self._refresh_outbound_gate_locked(
                f"strategy_frozen:{strategy_id}:{symbol}:{reason}"
            )

        if previous_reason != reason:
            logger.error(log_message)
            self._audit(audit_kind, **payload)
        else:
            self._audit("strategy_freeze_reasserted", **payload)

        self._wait_for_outbound_risk_sends(
            f"strategy_freeze:{strategy_id}:{symbol}"
        )
        if cancel_active_orders:
            self._cancel_orders_matching(
                lambda order: order.intent.strategy_id == strategy_id
                and (not symbol or order.intent.symbol == symbol)
            )
        with self.lock:
            self._refresh_outbound_gate_locked(
                f"strategy_guarded:{strategy_id}:{symbol}"
            )

    def clear_strategy_freeze(self, strategy_id: str, symbol: str = "", reason: str = ""):
        strategy_id = (strategy_id or "").strip()
        if not strategy_id:
            return False

        symbol = symbol.upper() if symbol else ""
        with self.lock:
            if symbol:
                previous_reason = self.strategy_symbol_guards.pop((strategy_id, symbol), "")
            else:
                previous_reason = self.strategy_guards.pop(strategy_id, "")
            self._refresh_outbound_gate_locked(
                reason
                or previous_reason
                or f"strategy_unfrozen:{strategy_id}:{symbol}"
            )
        if not previous_reason:
            return False

        payload = {
            "strategy_id": strategy_id,
            "reason": reason or previous_reason,
            "previous_reason": previous_reason,
        }
        if symbol:
            payload["symbol"] = symbol
            logger.info(f"[OMS] Strategy restored {strategy_id}/{symbol}: {reason or previous_reason}")
        else:
            logger.info(f"[OMS] Strategy restored {strategy_id}: {reason or previous_reason}")
        self._audit("strategy_unfrozen", **payload)
        return True

    def get_strategy_freeze_reason(self, strategy_id: str, symbol: str = "") -> str:
        strategy_id = (strategy_id or "").strip()
        if not strategy_id:
            return ""

        symbol = symbol.upper() if symbol else ""
        if symbol:
            scoped_reason = self.strategy_symbol_guards.get((strategy_id, symbol), "")
            if scoped_reason:
                return scoped_reason
        return self.strategy_guards.get(strategy_id, "")

    def _capture_guard_cleanup_snapshot_locked(self, prefixes=()) -> dict:
        prefixes = tuple(prefixes or ())

        def selected(guard_reason: str) -> bool:
            return not prefixes or any(
                guard_reason.startswith(prefix) for prefix in prefixes
            )

        symbol_snapshots = []
        for symbol in set(self.symbol_guards) | set(self.symbol_guard_records):
            records = self._ensure_symbol_guard_records_locked(symbol)
            for owner, record in records.items():
                guard_reason = str(record.get("reason", "") or "")
                if selected(guard_reason):
                    symbol_snapshots.append(
                        (
                            symbol,
                            owner,
                            guard_reason,
                            int(record.get("epoch", 0) or 0),
                        )
                    )
        return {
            "symbols": symbol_snapshots,
            "venues": [
                (
                    venue,
                    guard_reason,
                    int(self.venue_guard_epochs.get(venue, 0) or 0),
                )
                for venue, guard_reason in self.venue_guards.items()
                if selected(guard_reason)
            ],
            "strategies": [
                (strategy_id, guard_reason)
                for strategy_id, guard_reason in self.strategy_guards.items()
                if selected(guard_reason)
            ],
            "strategy_symbols": [
                (strategy_id, symbol, guard_reason)
                for (strategy_id, symbol), guard_reason
                in self.strategy_symbol_guards.items()
                if selected(guard_reason)
            ],
        }

    def _clear_guard_cleanup_snapshot(self, snapshot: dict, reason: str) -> int:
        cleared = 0
        # The outer RLock makes the decision and all exact-owner clears one
        # atomic recovery commit. A new fault can only arrive afterwards and
        # therefore cannot be mistaken for a guard proven healthy here.
        with self.lock:
            for symbol, owner, guard_reason, epoch in snapshot.get("symbols", []):
                if self.clear_symbol_freeze(
                    symbol,
                    reason=reason,
                    expected_epoch=epoch,
                    expected_reason=guard_reason,
                    expected_owner=owner,
                ):
                    cleared += 1
            for venue, guard_reason, epoch in snapshot.get("venues", []):
                if self.clear_venue_freeze(
                    venue,
                    reason=reason,
                    expected_epoch=epoch,
                    expected_reason=guard_reason,
                ):
                    cleared += 1
            for strategy_id, guard_reason in snapshot.get("strategies", []):
                if self.strategy_guards.get(strategy_id, "") != guard_reason:
                    continue
                if self.clear_strategy_freeze(strategy_id, reason=reason):
                    cleared += 1
            for strategy_id, symbol, guard_reason in snapshot.get(
                "strategy_symbols", []
            ):
                if (
                    self.strategy_symbol_guards.get((strategy_id, symbol), "")
                    != guard_reason
                ):
                    continue
                if self.clear_strategy_freeze(
                    strategy_id,
                    symbol=symbol,
                    reason=reason,
                ):
                    cleared += 1
        return cleared

    def clear_transient_guards(
        self,
        prefixes=("truth_plane:",),
        guard_snapshot: dict | None = None,
    ):
        prefixes = tuple(prefixes or ())
        if not prefixes:
            return 0
        with self.lock:
            snapshot = guard_snapshot or self._capture_guard_cleanup_snapshot_locked(
                prefixes
            )
        return self._clear_guard_cleanup_snapshot(
            snapshot,
            reason="transient guard cleared after truth verification",
        )

    def _clear_recovered_guards_if_pending(self, reason: str = ""):
        if not self.recovered_guard_cleanup_pending:
            return 0

        with self.lock:
            snapshot = self._recovered_guard_cleanup_snapshot or {
                "symbols": [],
                "venues": [],
                "strategies": [],
                "strategy_symbols": [],
            }
            cleared = self._clear_guard_cleanup_snapshot(
                snapshot,
                reason=reason or "recovered_guard_cleared",
            )
            self.recovered_guard_cleanup_pending = False
            self._recovered_guard_cleanup_snapshot = None
        if cleared:
            self._audit("recovered_guards_cleared", reason=reason or "recovered_guard_cleared", count=cleared)
        return cleared

    def is_symbol_tradeable(self, symbol: str) -> bool:
        return self.can_open_new_risk() and not self.get_symbol_freeze_reason(symbol)

    def can_submit_for_strategy(self, strategy_id: str, symbol: str = "") -> bool:
        return self._get_order_block_reason(strategy_id, symbol) == ""

    def get_order_block_reason(self, strategy_id: str = "", symbol: str = "") -> str:
        return self._get_order_block_reason(strategy_id, symbol)

    def _cancel_orders_matching(self, predicate):
        with self.lock:
            client_oids = [
                order.client_oid
                for order in self.orders.values()
                if order.is_active() and predicate(order)
            ]
        for client_oid in client_oids:
            self.cancel_order(client_oid)

