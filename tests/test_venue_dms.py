from types import SimpleNamespace

from risk.venue_dms import VenueDMSController


class _Logger:
    def __init__(self):
        self.messages = []

    def critical(self, message):
        self.messages.append(message)


class _OMS:
    def __init__(self):
        self.state = SimpleNamespace(value="LIVE")
        self._shutdown_requested = False
        self._stopped = False
        self.symbol_guards = {}
        self.venue_guards = {}
        self.strategy_guards = {}
        self.strategy_symbol_guards = {}
        self.mode_constraints = {}
        self.renewals = 0
        self.unhealthy = []
        self.snapshot = {
            "enabled": True,
            "valid": True,
            "reason": "",
        }

    def get_venue_dead_man_switch_snapshot(self):
        return dict(self.snapshot)

    def can_open_new_risk(self):
        return not any(
            (
                self.symbol_guards,
                self.venue_guards,
                self.strategy_guards,
                self.strategy_symbol_guards,
                self.mode_constraints,
            )
        )

    def is_shutdown_started(self):
        return bool(self._shutdown_requested or self._stopped)

    def can_renew_venue_dead_man_switch(self):
        return True

    def request_venue_dead_man_switch_renewal(self):
        self.renewals += 1
        return True

    def handle_venue_dead_man_switch_unhealthy(self, reason):
        self.unhealthy.append(reason)
        return True


def _controller(*, enabled=True, oms=None):
    return VenueDMSController(
        root_config={
            "oms": {"venue_dead_man_switch": {"enabled": enabled}},
        },
        oms=oms,
        logger=_Logger(),
    )


def test_disabled_controller_does_not_require_oms():
    controller = _controller(enabled=False, oms=None)

    assert controller.renew(active=True, kill_switch_triggered=False)
    assert controller.status_snapshot() == {
        "venue_dms_renewal_authorized": True,
        "venue_dms_supervisor_healthy": True,
        "venue_dms_failure_reason": "",
        "last_venue_dms_renewal_result": None,
    }


def test_live_supervisor_failure_latches_and_notifies_oms():
    oms = _OMS()
    controller = _controller(oms=oms)

    controller.set_supervisor_health(False)

    assert not controller.renewal_authorized
    assert not controller.supervisor_healthy
    assert controller.failure_reason == "independent_supervisor_unhealthy"
    assert oms.unhealthy == ["independent_supervisor_unhealthy"]


def test_non_live_supervisor_failure_withholds_without_latching():
    oms = _OMS()
    oms.state.value = "RECOVERING"
    controller = _controller(oms=oms)

    controller.set_supervisor_health(False)

    assert controller.renewal_authorized
    assert not controller.supervisor_healthy
    assert controller.failure_reason == ""


def test_valid_live_snapshot_renews():
    oms = _OMS()
    controller = _controller(oms=oms)

    assert controller.renew(active=True, kill_switch_triggered=False)
    assert controller.last_renewal_result is True
    assert oms.renewals == 1


def test_invalid_snapshot_fails_closed_and_latches_reason():
    oms = _OMS()
    oms.snapshot = {
        "enabled": True,
        "valid": False,
        "reason": "renewal_stale",
    }
    controller = _controller(oms=oms)

    assert not controller.renew(active=True, kill_switch_triggered=False)
    assert not controller.renewal_authorized
    assert controller.last_renewal_result is False
    assert controller.failure_reason == "renewal_stale"
    assert oms.unhealthy == ["renewal_stale"]


def test_existing_risk_guards_withhold_request_without_latching():
    oms = _OMS()
    oms.symbol_guards["BTCUSDT"] = {"market_data": "stale"}
    controller = _controller(oms=oms)

    assert controller.renew(active=True, kill_switch_triggered=False)
    assert controller.renewal_authorized
    assert controller.last_renewal_result is None
    assert oms.renewals == 0
