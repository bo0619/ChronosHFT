import ast
from pathlib import Path

import pytest

from infrastructure.binance_account_configuration import (
    AccountConfigurationVerificationError,
    verify_account_configuration,
)


ROOT = Path(__file__).resolve().parents[1]


def _position(symbol, *, side="BOTH", margin_type="isolated", leverage="1"):
    return {
        "symbol": symbol,
        "positionSide": side,
        "marginType": margin_type,
        "leverage": leverage,
    }


def _verify(*, mode=None, positions=None, symbols=None, **overrides):
    arguments = {
        "position_mode_payload": (
            {"dualSidePosition": False} if mode is None else mode
        ),
        "position_risk_payload": (
            [_position("BTCUSDT")] if positions is None else positions
        ),
        "symbols": ["BTCUSDT"] if symbols is None else symbols,
        "target_position_mode": "ONE_WAY",
        "target_margin_type": "ISOLATED",
        "target_leverage": 1,
    }
    arguments.update(overrides)
    return verify_account_configuration(**arguments)


def test_accepts_exact_one_way_isolated_target_leverage_truth():
    assert (
        _verify(
            positions=[
                _position("BTCUSDT"),
                _position("ETHUSDT"),
            ],
            symbols=["btcusdt", "ETHUSDT"],
            target_leverage=1,
        )
        is None
    )


@pytest.mark.parametrize(
    "mode",
    [
        None,
        [],
        {},
        {"dualSidePosition": True},
        {"dualSidePosition": "false"},
        {"dualSidePosition": 0},
    ],
)
def test_rejects_unproven_one_way_mode(mode):
    if mode is None:
        mode = "malformed"
    with pytest.raises(AccountConfigurationVerificationError):
        _verify(mode=mode)


@pytest.mark.parametrize(
    ("positions", "match"),
    [
        ("malformed", "must be a list"),
        ({}, "must be a list"),
        ([None], "must be an object"),
        ([{}], "missing symbol"),
        ([_position("ETHUSDT")], "missing symbols: BTCUSDT"),
        (
            [_position("BTCUSDT"), _position("btcusdt")],
            "duplicate position risk row",
        ),
        ([_position("BTCUSDT", side="LONG")], "positionSide is not BOTH"),
        ([_position("BTCUSDT", margin_type="cross")], "margin is not ISOLATED"),
        ([_position("BTCUSDT", margin_type=True)], "margin is not ISOLATED"),
        ([_position("BTCUSDT", margin_type=None)], "margin is not ISOLATED"),
        ([_position("BTCUSDT", leverage="2")], "does not match target"),
        ([_position("BTCUSDT", leverage="1.0")], "must be a positive integer"),
        ([_position("BTCUSDT", leverage=True)], "must be a positive integer"),
    ],
)
def test_rejects_incomplete_or_mismatched_position_truth(positions, match):
    with pytest.raises(AccountConfigurationVerificationError, match=match):
        _verify(positions=positions)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"target_position_mode": "HEDGE"}, "must be ONE_WAY"),
        ({"target_margin_type": "CROSSED"}, "must be ISOLATED"),
        ({"target_leverage": 0}, "must be a positive integer"),
        ({"target_leverage": "1.0"}, "must be a positive integer"),
        ({"symbols": []}, "at least one symbol"),
        ({"symbols": "BTCUSDT"}, "must be a collection"),
        ({"symbols": [""]}, "empty value"),
    ],
)
def test_rejects_unsafe_or_ambiguous_targets(overrides, match):
    with pytest.raises(AccountConfigurationVerificationError, match=match):
        _verify(**overrides)


def _method_node(path, class_name, method_name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )


def test_rest_verify_queries_are_signed_get_only():
    path = ROOT / "gateway" / "binance" / "rest_api.py"
    for method_name in ("get_position_mode", "get_positions"):
        method = _method_node(path, "BinanceRestApi", method_name)
        request_calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "request"
        ]
        assert len(request_calls) == 1
        call = request_calls[0]
        assert isinstance(call.args[0], ast.Constant)
        assert call.args[0].value == "GET"
        signed = next(keyword for keyword in call.keywords if keyword.arg == "signed")
        assert isinstance(signed.value, ast.Constant)
        assert signed.value.value is True


def test_gateway_verify_only_method_has_no_mutating_rest_call():
    path = ROOT / "gateway" / "binance" / "gateway.py"
    method = _method_node(
        path,
        "BinanceGateway",
        "_verify_account_trading_configuration",
    )
    rest_methods = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "self"
        and node.func.value.attr == "rest"
    }
    assert rest_methods == {
        "get_position_mode",
        "get_positions",
        "response_succeeded",
    }
    assert not any(name.startswith(("set_", "post", "put", "delete")) for name in rest_methods)


def test_oms_wires_account_configuration_mode_to_gateway_statically():
    tree = ast.parse(
        (ROOT / "oms" / "initializer.py").read_text(encoding="utf-8")
    )
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and target.attr == "account_configuration_mode"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    assert ast.unparse(assignments[0].value) == "account_configuration_mode"
