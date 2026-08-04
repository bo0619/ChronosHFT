import json
from copy import deepcopy
from pathlib import Path

import pytest

from infrastructure.config_scaling import (
    CURRENT_CONFIG_MANIFEST_SCHEMA,
    load_config_document,
    load_root_config,
)
from infrastructure.config_schema import (
    CONFIG_DOCUMENT_VERSION,
    CONFIG_FRAGMENT_SCHEMA,
    CONFIG_UNKNOWN_KEY_POLICY,
    ConfigSchemaError,
    validate_composed_config,
    validate_fragment_document,
    validate_versioned_manifest,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _fragment(fragment, content, *, version=1):
    return {
        "$schema": CONFIG_FRAGMENT_SCHEMA,
        "fragment": fragment,
        "version": version,
        **content,
    }


def _manifest(*includes):
    return {
        "schema": CURRENT_CONFIG_MANIFEST_SCHEMA,
        "config_version": CONFIG_DOCUMENT_VERSION,
        "unknown_keys": CONFIG_UNKNOWN_KEY_POLICY,
        "includes": list(includes),
    }


def _include(path, fragment, *, version=1):
    return {"path": path, "fragment": fragment, "version": version}


def _raw_tracked_config():
    return load_config_document(REPOSITORY_ROOT / "config.json")


def test_tracked_manifest_versions_every_fragment():
    manifest = json.loads((REPOSITORY_ROOT / "config.json").read_text(encoding="utf-8"))

    assert manifest["schema"] == CURRENT_CONFIG_MANIFEST_SCHEMA
    assert manifest["config_version"] == CONFIG_DOCUMENT_VERSION
    assert manifest["unknown_keys"] == CONFIG_UNKNOWN_KEY_POLICY
    assert len(manifest["includes"]) == 30
    assert len({entry["fragment"] for entry in manifest["includes"]}) == 30

    for entry in manifest["includes"]:
        fragment = json.loads(
            (REPOSITORY_ROOT / entry["path"]).read_text(encoding="utf-8")
        )
        assert fragment["$schema"] == CONFIG_FRAGMENT_SCHEMA
        assert fragment["fragment"] == entry["fragment"]
        assert fragment["version"] == entry["version"]


def test_tracked_manifest_is_validated_before_startup_and_metadata_is_stripped():
    raw = _raw_tracked_config()
    configured = load_root_config(REPOSITORY_ROOT / "config.json")

    assert "$schema" not in raw
    assert "fragment" not in raw
    assert "version" not in raw
    assert configured["execution"]["mode"] == "paper"
    assert configured["account"]["initial_balance_usdt"] == 10_000.0
    assert configured["risk"]["limits"]["max_daily_loss"] == 100.0


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (
            lambda manifest: manifest.__setitem__("config_version", 2),
            "config_version",
        ),
        (
            lambda manifest: manifest.__setitem__("config_version", 1.0),
            "config_version",
        ),
        (
            lambda manifest: manifest.__setitem__("unknown_keys", "allow"),
            "unknown_keys",
        ),
        (
            lambda manifest: manifest.__setitem__("future", True),
            "unknown manifest keys",
        ),
        (
            lambda manifest: manifest["includes"][0].__setitem__("version", 2),
            "unsupported",
        ),
        (
            lambda manifest: manifest["includes"][0].__setitem__(
                "fragment", "future.fragment"
            ),
            "unknown",
        ),
    ],
)
def test_v3_manifest_rejects_unknown_policy_and_unsupported_versions(
    mutation,
    match,
):
    manifest = _manifest(_include("execution.json", "execution"))
    mutation(manifest)

    with pytest.raises(ConfigSchemaError, match=match):
        validate_versioned_manifest(manifest)


def test_fragment_rejects_unknown_operational_key_but_allows_string_comments():
    valid = _fragment(
        "execution",
        {
            "_comment_migration": "Operator-only annotation.",
            "execution": {
                "mode": "paper",
                "_comment_mode": "Paper is the tracked profile.",
            },
        },
    )
    assert validate_fragment_document(
        valid,
        expected_fragment="execution",
        expected_version=1,
        source="execution.json",
    ) == {
        "_comment_migration": "Operator-only annotation.",
        "execution": {
            "mode": "paper",
            "_comment_mode": "Paper is the tracked profile.",
        },
    }

    invalid = deepcopy(valid)
    invalid["execution"]["typo_mode"] = "paper"
    with pytest.raises(ConfigSchemaError, match="typo_mode is an unknown field"):
        validate_fragment_document(
            invalid,
            expected_fragment="execution",
            expected_version=1,
            source="execution.json",
        )

    invalid_comment = deepcopy(valid)
    invalid_comment["execution"]["_comment_mode"] = 1
    with pytest.raises(ConfigSchemaError, match="comment must be a string"):
        validate_fragment_document(
            invalid_comment,
            expected_fragment="execution",
            expected_version=1,
            source="execution.json",
        )


def test_every_tracked_runtime_object_rejects_unknown_fields():
    manifest = json.loads((REPOSITORY_ROOT / "config.json").read_text(encoding="utf-8"))
    for include in manifest["includes"]:
        source = REPOSITORY_ROOT / include["path"]
        fragment = json.loads(source.read_text(encoding="utf-8"))
        object_paths = []

        def collect(value, path=()):
            if not isinstance(value, dict):
                return
            object_paths.append(path)
            for key, item in value.items():
                if key not in {"$schema", "fragment", "version"}:
                    collect(item, (*path, key))

        collect(fragment)
        for object_path in object_paths:
            changed = deepcopy(fragment)
            target = changed
            for key in object_path:
                target = target[key]
            target["__unknown_runtime_key__"] = True
            with pytest.raises(ConfigSchemaError, match="unknown|invalid key"):
                validate_fragment_document(
                    changed,
                    expected_fragment=include["fragment"],
                    expected_version=include["version"],
                    source=str(source),
                )


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ({"execution": {"mode": 1}}, "must be a string"),
        ({"execution": {"mode": "simulation"}}, "must be one of"),
        ({"execution": {}}, "missing required fields"),
    ],
)
def test_fragment_rejects_wrong_types_ranges_and_missing_fields(content, match):
    with pytest.raises(ConfigSchemaError, match=match):
        validate_fragment_document(
            _fragment("execution", content),
            expected_fragment="execution",
            expected_version=1,
            source="execution.json",
        )


def test_fragment_rejects_numeric_values_outside_declared_range():
    content = {
        "account": {
            "leverage": 126,
            "margin_type": "ISOLATED",
            "position_mode": "ONE_WAY",
            "use_bnb_fees": False,
        }
    }

    with pytest.raises(ConfigSchemaError, match="leverage must be at most 125"):
        validate_fragment_document(
            _fragment("account", content),
            expected_fragment="account",
            expected_version=1,
            source="account.json",
        )


def test_loader_rejects_manifest_fragment_identity_mismatch(tmp_path):
    (tmp_path / "execution.json").write_text(
        json.dumps(_fragment("alerts", {"execution": {"mode": "paper"}})),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "config.json"
    manifest_path.write_text(
        json.dumps(_manifest(_include("execution.json", "execution"))),
        encoding="utf-8",
    )

    with pytest.raises(ConfigSchemaError, match="must match manifest identity"):
        load_config_document(manifest_path)


def test_legacy_v1_manifest_is_rejected_at_runtime(tmp_path):
    (tmp_path / "legacy.json").write_text(
        json.dumps({"legacy_extension": {"still": "accepted"}}),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "config.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "chronoshft.config_manifest.v1",
                "includes": ["legacy.json"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="obsolete config manifest schema"):
        load_config_document(manifest_path)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda config: config["strategy"]["capital_scaling"].__setitem__(
                "target_daily_loss", 5.01
            ),
            "target_daily_loss",
        ),
        (
            lambda config: config["strategy"]["capital_scaling"].__setitem__(
                "budget_asset_weights", {"USDC": 1.0}
            ),
            "budget_asset_weights",
        ),
        (
            lambda config: config["system"]["binance_rest_rate_limit"].__setitem__(
                "trading_reserve", 2_100
            ),
            "reserves",
        ),
        (
            lambda config: config["system"]["event_engine"][
                "queue_warn_depth"
            ].__setitem__("market", 2_049),
            "queue_warn_depth.market",
        ),
        (
            lambda config: config["system"]["time_sync"].__setitem__(
                "min_successful_samples", 8
            ),
            "sample counts",
        ),
        (
            lambda config: config["paper_trade"].__setitem__(
                "market_data_environment", "testnet"
            ),
            "market_data_environment",
        ),
        (
            lambda config: config["strategy"].__setitem__(
                "registered_models", ["avellaneda_stoikov"]
            ),
            "primary_model",
        ),
        (
            lambda config: config["strategy"].__setitem__("name", "AvellanedaStoikov"),
            "strategy.name",
        ),
        (
            lambda config: config["execution"].__setitem__("mode", "live"),
            "paper_trade.enabled",
        ),
        (
            lambda config: config["risk"]["price_sanity"].__setitem__(
                "max_spread_pct", 0.04
            ),
            "max_spread_pct",
        ),
        (
            lambda config: config["system"]["time_sync"].__setitem__(
                "max_uncertainty_ms", 201.0
            ),
            "max_uncertainty_ms",
        ),
    ],
)
def test_cross_fragment_drift_fails_closed(mutate, match):
    config = _raw_tracked_config()
    mutate(config)

    with pytest.raises(ConfigSchemaError, match=match):
        validate_composed_config(config)
