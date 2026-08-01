from risk.sidecar_durable_state import SidecarDurableState


def _finite_float(value, _label):
    return float(value)


class _Owner:
    def __init__(self):
        self.state_path = ""
        self.state_required = False
        self.state_fsync = False
        self.state_generation = 0
        self.state_recovered = False
        self.state_load_error = ""
        self.state_persist_error = ""
        self._last_persisted_fingerprint = None
        self.kill_latched = False
        self.kill_reason = ""
        self.stage = "ARMED"
        self.failures = []
        self.fingerprint = ("stable",)

    @staticmethod
    def _state_checksum(payload):
        return SidecarDurableState.checksum(payload)

    def _durable_fingerprint(self):
        return self.fingerprint

    def _fail_closed_on_state_error(self, reason):
        self.failures.append(reason)
        self.kill_latched = True
        self.kill_reason = reason
        self.stage = "FAILED"


def _state(owner):
    return SidecarDurableState(
        owner,
        _finite_float,
        lambda _source, _destination: None,
    )


def test_checksum_is_stable_across_mapping_insertion_order():
    assert SidecarDurableState.checksum(
        {"b": 2, "a": 1}
    ) == SidecarDurableState.checksum({"a": 1, "b": 2})


def test_required_missing_state_path_fails_closed():
    owner = _Owner()
    owner.state_required = True

    assert not _state(owner).persist("test")

    assert owner.state_persist_error == "state_path_missing"
    assert owner.failures == ["state_path_missing"]
    assert owner.kill_latched
    assert owner.stage == "FAILED"


def test_unchanged_fingerprint_skips_state_file_rewrite():
    owner = _Owner()
    owner.state_path = "must-not-be-created.json"
    owner._last_persisted_fingerprint = owner.fingerprint
    replacements = []
    state = SidecarDurableState(
        owner,
        _finite_float,
        lambda source, destination: replacements.append(
            (source, destination)
        ),
    )

    assert state.persist("unchanged")
    assert replacements == []
    assert owner.state_generation == 0
