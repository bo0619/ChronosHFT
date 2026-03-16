import sys
import types
import unittest
from datetime import datetime
from unittest.mock import patch

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

from event.type import LifecycleState, Side, TradeData
from strategy.ml_sniper.ml_sniper import MLSniperStrategy


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class DummyOMS:
    def __init__(self):
        self.state = LifecycleState.LIVE
        self.config = {
            "backtest": {
                "maker_fee": 0.0,
                "taker_fee": 0.0,
            }
        }
        self.exposure = types.SimpleNamespace(net_positions={})
        self.frozen_strategies = []

    def cancel_order(self, client_oid):
        return None

    def cancel_all_orders(self, symbol):
        return None

    def freeze_strategy(self, strategy_id, reason, symbol="", cancel_active_orders=True):
        self.frozen_strategies.append((strategy_id, reason, symbol, cancel_active_orders))


class FakeAlphaProcess:
    def __init__(self, snapshots=None, healthy=True, unhealthy_symbols=None):
        self.enabled = True
        self.snapshots = list(snapshots or [])
        self.healthy = healthy
        self.unhealthy_symbols = set(unhealthy_symbols or [])
        self.orderbooks = []
        self.trades = []
        self.stopped = False

    def start(self):
        return True

    def stop(self):
        self.stopped = True

    def submit_orderbook(self, orderbook):
        self.orderbooks.append(orderbook.symbol)
        return True

    def submit_trade(self, trade):
        self.trades.append(trade.trade_id)
        return True

    def poll(self):
        pending = list(self.snapshots)
        self.snapshots.clear()
        return pending

    def is_healthy(self):
        return self.healthy

    def get_metrics_snapshot(self):
        return {"alive": self.healthy, "deferred_depth": 0}

    def get_unhealthy_symbols(self):
        return set(self.unhealthy_symbols)


class MLSniperAdaptationTests(unittest.TestCase):
    def setUp(self):
        self.engine = DummyEngine()
        self.oms = DummyOMS()
        self.strategy = MLSniperStrategy(self.engine, self.oms)
        self.strategy.min_warmup_sec = 0.0

    def test_warmup_is_tracked_per_symbol(self):
        btc_predictor = self.strategy._get_predictor("BTCUSDT")
        eth_predictor = self.strategy._get_predictor("ETHUSDT")

        for model in btc_predictor.models.values():
            model.n_updates = 2

        self.assertTrue(self.strategy._check_warmup("BTCUSDT"))
        self.assertFalse(self.strategy._check_warmup("ETHUSDT"))
        self.assertTrue(self.strategy.symbol_warmup_ready["BTCUSDT"])
        self.assertFalse(self.strategy.symbol_warmup_ready["ETHUSDT"])

    def test_comment_entries_are_filtered_out_of_weights(self):
        self.assertEqual(set(self.strategy.weights.keys()), {"1s", "10s", "30s"})
        self.assertTrue(all(isinstance(weight, float) for weight in self.strategy.weights.values()))

    @patch("strategy.ml_sniper.ml_sniper.ref_data_manager.round_price", side_effect=lambda symbol, price: price)
    def test_consensus_filter_blocks_conflicts_and_allows_alignment(self, _round_price):
        sym = "BTCUSDT"
        self.strategy.base_velocity_threshold = 10.0
        self.strategy.base_taker_entry_threshold = 50.0
        self.strategy.base_maker_entry_threshold = 1.0
        self.strategy.net_edge_buffer_bps = 0.0
        self.strategy.maker_spread_weight = 0.0
        self.strategy.maker_fee_bps = 0.0
        self.strategy.taker_fee_bps = 0.0

        with patch.object(self.strategy, "_calc_vol", return_value=1.0), patch.object(
            self.strategy, "_tick_size", return_value=0.1
        ), patch.object(self.strategy, "send_intent", return_value="entry-1"):
            self.strategy.latest_preds[sym] = {"1s": 1.0, "10s": 3.0, "30s": -2.5}
            self.strategy._run_fsm(
                sym,
                mid=100.0,
                bid_1=99.9,
                ask_1=100.1,
                signal=1.2,
                velocity=0.0,
                now=1.0,
            )

        self.assertIsNone(self.strategy.entry_oid[sym])
        self.assertEqual(self.strategy.state[sym], "FLAT")

        with patch.object(self.strategy, "_calc_vol", return_value=1.0), patch.object(
            self.strategy, "_tick_size", return_value=0.1
        ), patch.object(self.strategy, "send_intent", return_value="entry-2"):
            self.strategy.latest_preds[sym] = {"1s": 1.0, "10s": 3.0, "30s": 2.5}
            self.strategy._run_fsm(
                sym,
                mid=100.0,
                bid_1=99.9,
                ask_1=100.1,
                signal=4.0,
                velocity=0.0,
                now=2.0,
            )

        self.assertEqual(self.strategy.entry_oid[sym], "entry-2")
        self.assertEqual(self.strategy.entry_mode[sym], "GTX")
        self.assertEqual(self.strategy.state[sym], "ENTERING")
        self.assertEqual(self.strategy.order_context["entry-2"]["role"], "entry")

    @patch("strategy.ml_sniper.ml_sniper.ref_data_manager.round_price", side_effect=lambda symbol, price: price)
    def test_net_edge_gating_blocks_weak_alpha_and_allows_strong_alpha(self, _round_price):
        sym = "BTCUSDT"
        self.strategy.base_velocity_threshold = 10.0
        self.strategy.base_taker_entry_threshold = 50.0
        self.strategy.base_maker_entry_threshold = 1.0
        self.strategy.net_edge_buffer_bps = 0.0
        self.strategy.maker_spread_weight = 1.0
        self.strategy.maker_fee_bps = 0.0
        self.strategy.taker_fee_bps = 0.0
        self.strategy.latest_preds[sym] = {"1s": 1.0, "10s": 4.0, "30s": 3.0}

        with patch.object(self.strategy, "_calc_vol", return_value=1.0), patch.object(
            self.strategy, "_tick_size", return_value=0.1
        ), patch.object(self.strategy, "send_intent", return_value="entry-weak"):
            self.strategy._run_fsm(
                sym,
                mid=100.0,
                bid_1=99.9,
                ask_1=100.1,
                signal=5.0,
                velocity=0.0,
                now=1.0,
            )

        self.assertIsNone(self.strategy.entry_oid[sym])
        self.assertEqual(self.strategy.state[sym], "FLAT")

        with patch.object(self.strategy, "_calc_vol", return_value=1.0), patch.object(
            self.strategy, "_tick_size", return_value=0.1
        ), patch.object(self.strategy, "send_intent", return_value="entry-strong"):
            self.strategy._run_fsm(
                sym,
                mid=100.0,
                bid_1=99.9,
                ask_1=100.1,
                signal=25.0,
                velocity=0.0,
                now=2.0,
            )

        self.assertEqual(self.strategy.entry_oid[sym], "entry-strong")
        self.assertEqual(self.strategy.state[sym], "ENTERING")

    def test_trade_feedback_tightens_adaptive_thresholds(self):
        sym = "BTCUSDT"
        base_threshold = self.strategy._adaptive_entry_threshold(sym, "GTX")
        self.strategy.min_closed_trades_for_adaptation = 1

        self.strategy.order_context["entry-1"] = {
            "symbol": sym,
            "side": Side.BUY,
            "mode": "GTX",
            "role": "entry",
            "limit_price": 100.0,
            "mid": 100.0,
            "signal": 4.0,
            "velocity": 0.0,
            "entry_price": 100.0,
            "submit_ts": 1.0,
            "exit_pnl_sum": 0.0,
            "exit_qty": 0.0,
        }
        self.strategy.on_trade(
            TradeData(
                symbol=sym,
                order_id="entry-1",
                trade_id="t-entry",
                side="BUY",
                price=100.2,
                volume=1.0,
                datetime=datetime.utcnow(),
            )
        )

        self.strategy.order_context["exit-1"] = {
            "symbol": sym,
            "side": Side.SELL,
            "mode": "IOC",
            "role": "exit",
            "limit_price": 99.0,
            "mid": 100.0,
            "signal": -2.0,
            "velocity": 0.0,
            "entry_price": 100.0,
            "submit_ts": 2.0,
            "exit_pnl_sum": 0.0,
            "exit_qty": 0.0,
        }
        self.strategy.on_trade(
            TradeData(
                symbol=sym,
                order_id="exit-1",
                trade_id="t-exit",
                side="SELL",
                price=99.0,
                volume=1.0,
                datetime=datetime.utcnow(),
            )
        )
        self.strategy._finalize_exit_feedback(sym, "exit-1")

        feedback = self.strategy.execution_feedback[sym]
        tightened_threshold = self.strategy._adaptive_entry_threshold(sym, "GTX")

        self.assertLess(feedback["maker_edge_ewma"], 0.0)
        self.assertLess(feedback["exit_pnl_ewma"], 0.0)
        self.assertLess(feedback["win_rate_ewma"], 0.5)
        self.assertEqual(feedback["closed_trades"], 1)
        self.assertGreater(tightened_threshold, base_threshold)


    @patch("strategy.ml_sniper.ml_sniper.ref_data_manager.round_price", side_effect=lambda symbol, price: price)
    def test_low_confidence_regime_blocks_entry(self, _round_price):
        sym = "BTCUSDT"
        self.strategy.base_velocity_threshold = 10.0
        self.strategy.base_taker_entry_threshold = 50.0
        self.strategy.base_maker_entry_threshold = 1.0
        self.strategy.net_edge_buffer_bps = 0.0
        self.strategy.maker_spread_weight = 0.0
        self.strategy.maker_fee_bps = 0.0
        self.strategy.taker_fee_bps = 0.0
        self.strategy.latest_preds[sym] = {"1s": 1.0, "10s": 3.0, "30s": 2.0}

        with patch.object(self.strategy, "_calc_vol", return_value=1.0), patch.object(
            self.strategy, "_tick_size", return_value=0.1
        ), patch.object(self.strategy, "send_intent", return_value="entry-blocked"):
            self.strategy._run_fsm(
                sym,
                mid=100.0,
                bid_1=99.9,
                ask_1=100.1,
                signal=1.2,
                velocity=0.0,
                confidence=0.05,
                now=1.0,
            )

        self.assertIsNone(self.strategy.entry_oid[sym])
        self.assertEqual(self.strategy.state[sym], "FLAT")
        self.assertEqual(self.strategy.latest_regime[sym], "low_conf")

    @patch("strategy.ml_sniper.ml_sniper.ref_data_manager.round_price", side_effect=lambda symbol, price: price)
    def test_holding_requotes_stale_exit_order(self, _round_price):
        sym = "BTCUSDT"
        self.oms.exposure.net_positions[sym] = 1.0
        self.strategy.state[sym] = "HOLDING"
        self.strategy.entry_price[sym] = 100.0
        self.strategy.pos_entry_ts[sym] = 1.0
        self.strategy.exit_oid[sym] = "exit-1"
        self.strategy.active_orders["exit-1"] = object()
        self.strategy.order_context["exit-1"] = {
            "limit_price": 101.0,
            "submit_ts": 0.0,
            "role": "exit",
            "entry_price": 100.0,
        }

        with patch.object(self.strategy, "_tick_size", return_value=0.1), patch.object(
            self.strategy, "cancel_order"
        ) as cancel_order:
            self.strategy._run_fsm(
                sym,
                mid=100.0,
                bid_1=99.9,
                ask_1=100.1,
                signal=0.0,
                velocity=0.0,
                confidence=1.0,
                now=5.0,
            )

        cancel_order.assert_called_once_with("exit-1")

    def test_alpha_process_snapshot_drives_publish_and_fsm(self):
        sym = "BTCUSDT"
        self.strategy.alpha_process = FakeAlphaProcess(
            snapshots=[
                {
                    "kind": "alpha_snapshot",
                    "symbol": sym,
                    "now": 10.0,
                    "bid_1": 99.9,
                    "ask_1": 100.1,
                    "mid": 100.0,
                    "preds": {"1s": 2.0, "10s": 3.0, "30s": 4.0},
                    "spread_bps": 20.0,
                    "sigma_bps": 12.0,
                    "diagnostics": {
                        "1s": {"confidence": 0.8},
                        "10s": {"confidence": 0.6},
                        "30s": {"confidence": 0.5},
                    },
                    "weights_1s": [0.1] * 9,
                    "warmup_progress": {"1s": 2, "10s": 2, "30s": 2},
                    "predictor_warmed_up": True,
                }
            ]
        )
        self.strategy.alpha_process.enabled = True

        with patch.object(self.strategy, "_publish_state") as publish_state, patch.object(
            self.strategy, "_run_fsm"
        ) as run_fsm:
            self.strategy.on_orderbook(
                types.SimpleNamespace(
                    symbol=sym,
                    bids={99.9: 1.0},
                    asks={100.1: 1.0},
                )
            )

        publish_state.assert_called_once()
        run_fsm.assert_called_once()
        self.assertEqual(self.strategy.latest_sigma_bps[sym], 12.0)
        self.assertEqual(self.strategy.latest_preds[sym]["10s"], 3.0)

    def test_unhealthy_alpha_process_freezes_strategy(self):
        self.strategy.alpha_process = FakeAlphaProcess(healthy=False, unhealthy_symbols={"BTCUSDT"})
        self.strategy.alpha_process.enabled = True

        self.strategy.poll_async_workers()

        self.assertEqual(self.oms.frozen_strategies[-1][0], self.strategy.name)
        self.assertEqual(self.oms.frozen_strategies[-1][1], "alpha_process_unhealthy")
        self.assertEqual(self.oms.frozen_strategies[-1][2], "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
