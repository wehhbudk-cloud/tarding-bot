"""Tests for paper entries, exits, budgets, and saved state."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from config import Config
from engine import BotEngine


class FakeMarketData:
    def __init__(self) -> None:
        self.prices = {
            "SOLUSDT": 100.0,
            "FETUSDT": 50.0,
            "MAVUSDT": 25.0,
            "DASHUSDT": 10.0,
        }

    def get_price(self, symbol: str) -> float:
        return self.prices[symbol]


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = Config(
            watchlist=("SOLUSDT", "FETUSDT", "MAVUSDT", "DASHUSDT"),
            quote_amount=Decimal("10"),
            total_budget=Decimal("30"),
            max_positions=3,
            host="127.0.0.1",
            port=8000,
            poll_seconds=5,
            state_file=Path(self.temp_dir.name) / "state.json",
        )
        self.market_data = FakeMarketData()
        self.engine = BotEngine(self.config, self.market_data)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_buy_uses_ten_usdt_and_cannot_exceed_three_positions(self) -> None:
        results = [
            self.engine.handle_signal(symbol, "buy", self.market_data.prices[symbol])
            for symbol in ("SOLUSDT", "FETUSDT", "MAVUSDT", "DASHUSDT")
        ]

        self.assertEqual([item["status"] for item in results], [
            "opened",
            "opened",
            "opened",
            "ignored",
        ])
        self.assertEqual(results[0]["quote_amount"], "10")
        self.assertEqual(self.engine.status()["open_positions"], 3)
        self.assertEqual(self.engine.status()["current_exposure_usdt"], "30")
        self.assertFalse(self.engine.status()["order_submission_enabled"])
        table = self.engine.format_status_table()
        for heading in (
            "Symbol",
            "Entry Price",
            "Target (+3%)",
            "Stop Loss (-2%)",
            "Status",
            "Net PnL",
            "🟢 ACTIVE",
        ):
            self.assertIn(heading, table)

    def test_sell_signal_closes_the_matching_paper_position(self) -> None:
        self.engine.handle_signal("SOLUSDT", "buy", 100.0)
        result = self.engine.handle_signal("SOLUSDT", "sell", 101.0)

        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["reason"], "indicator_sell_signal")
        self.assertEqual(self.engine.status()["open_positions"], 0)

    def test_take_profit_and_stop_loss_close_positions(self) -> None:
        self.engine.handle_signal(
            "SOLUSDT", "buy", 100.0, candle_open_time_ms=1_761_186_000_000
        )
        self.market_data.prices["SOLUSDT"] = 104.0
        with self.assertLogs(level="INFO") as target_logs:
            self.engine.check_positions_once(candle_open_time_ms=1_761_186_300_000)
        self.assertEqual(self.engine.status()["open_positions"], 0)
        target_trade = self.engine.history.all_trades()[0]
        self.assertEqual(Decimal(target_trade["exit_price"]), Decimal("103"))
        self.assertEqual(Decimal(target_trade["gross_pnl_usd"]), Decimal("0.3"))
        self.assertEqual(
            Decimal(target_trade["binance_fees_usd"]), Decimal("0.015225")
        )
        self.assertEqual(Decimal(target_trade["net_pnl_usd"]), Decimal("0.284775"))
        self.assertEqual(target_trade["exit_candle_time_ms"], "1761186300000")
        self.assertTrue(
            any("[🎯] TARGET HUNTER!" in message for message in target_logs.output)
        )
        closed_details = next(
            message for message in target_logs.output
            if "Closed paper trade SOLUSDT" in message
        )
        for detail in (
            "Entry Price:",
            "Exit Price:",
            "Gross Profit/Loss:",
            "Binance Trading Fees (0.075% each side):",
            "Net Profit/Loss:",
            "TradingView candle UTC:",
        ):
            self.assertIn(detail, closed_details)

        self.engine.handle_signal(
            "FETUSDT", "buy", 50.0, candle_open_time_ms=1_761_186_000_000
        )
        self.market_data.prices["FETUSDT"] = 48.0
        with self.assertLogs(level="WARNING") as stop_logs:
            self.engine.check_positions_once(candle_open_time_ms=1_761_186_300_000)
        self.assertEqual(self.engine.status()["open_positions"], 0)
        self.assertTrue(
            any("[⚠️] STOP LOSS HUNTED!" in message for message in stop_logs.output)
        )
        stop_trade = self.engine.history.all_trades()[1]
        self.assertEqual(Decimal(stop_trade["exit_price"]), Decimal("49"))
        self.assertEqual(Decimal(stop_trade["gross_pnl_usd"]), Decimal("-0.2"))
        self.assertEqual(
            Decimal(stop_trade["binance_fees_usd"]), Decimal("0.01485")
        )
        self.assertEqual(
            Decimal(stop_trade["net_pnl_usd"]), Decimal("-0.21485")
        )
        self.assertIn("🔴 END", self.engine.format_status_table())
        summary = self.engine.history_summary()
        self.assertEqual(summary["total_trades_opened"], 2)
        self.assertEqual(summary["successful_trades"], 1)
        self.assertEqual(summary["failed_trades"], 1)

    def test_indicator_state_and_positions_are_persisted(self) -> None:
        self.engine.handle_signal(
            "SOLUSDT", "buy", 100.0, candle_open_time_ms=1_761_186_000_000
        )
        self.engine.save_indicator_state("SOLUSDT", {"version": 1, "bar_index": 12})

        restored = BotEngine(self.config, self.market_data)
        self.assertEqual(restored.status()["open_positions"], 1)
        self.assertEqual(
            restored.get_indicator_state("SOLUSDT"),
            {"version": 1, "bar_index": 12},
        )

    def test_csv_history_survives_removing_json_state(self) -> None:
        self.engine.handle_signal(
            "SOLUSDT", "buy", 100.0, candle_open_time_ms=1_761_186_000_000
        )
        history_path = self.config.state_file.parent / "trading_history.csv"
        before = history_path.read_text(encoding="utf-8")
        self.config.state_file.unlink()

        restored = BotEngine(self.config, self.market_data)
        self.assertEqual(restored.status()["open_positions"], 1)
        self.assertEqual(history_path.read_text(encoding="utf-8"), before)
        self.assertEqual(restored.status()["total_trades_opened"], 1)

    def test_budget_guard_prevents_more_than_configured_exposure(self) -> None:
        limited_config = Config(
            watchlist=("SOLUSDT", "FETUSDT", "MAVUSDT"),
            quote_amount=Decimal("10"),
            total_budget=Decimal("20"),
            max_positions=3,
            host="127.0.0.1",
            port=8000,
            poll_seconds=5,
            state_file=Path(self.temp_dir.name) / "limited.json",
        )
        limited = BotEngine(limited_config, self.market_data)
        limited.handle_signal("SOLUSDT", "buy", 100.0)
        limited.handle_signal("FETUSDT", "buy", 50.0)
        result = limited.handle_signal("MAVUSDT", "buy", 25.0)

        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "total_budget_reached")
        self.assertEqual(limited.status()["current_exposure_usdt"], "20")


if __name__ == "__main__":
    unittest.main()
