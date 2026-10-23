"""Tests for allowlisted remote bot control without network access."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from config import Config
from engine import BotEngine
from telegram_bot import TelegramBot


class FakeMarketData:
    def __init__(self) -> None:
        self.prices = {"SOLUSDT": 100.0}

    def get_price(self, symbol: str) -> float:
        return self.prices[symbol]


class TelegramBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        config = Config(
            watchlist=("SOLUSDT",),
            quote_amount=Decimal("10"),
            total_budget=Decimal("30"),
            max_positions=3,
            host="127.0.0.1",
            port=8000,
            poll_seconds=5,
            state_file=base / "state.json",
            history_file=base / "trading_history.csv",
        )
        self.market_data = FakeMarketData()
        self.engine = BotEngine(config, self.market_data)
        self.bot = TelegramBot(
            "test-token",
            "12345",
            self.engine,
            base / "telegram_offset.txt",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_only_allowlisted_chat_can_start_or_stop_the_bot(self) -> None:
        self.bot._handle_update(
            {"message": {"chat": {"id": 7}, "text": "/stop"}}
        )
        self.assertTrue(self.engine.is_enabled())

        self.bot._handle_update(
            {"message": {"chat": {"id": 12345}, "text": "/stop"}}
        )
        self.assertFalse(self.engine.is_enabled())
        self.bot._handle_update(
            {"message": {"chat": {"id": 12345}, "text": "/start"}}
        )
        self.assertTrue(self.engine.is_enabled())

    def test_stop_acknowledges_open_trade_monitoring_continues(self) -> None:
        self.engine.handle_signal("SOLUSDT", "buy", 100.0)
        self.assertIn("Paper trade entry", self.bot._outbound.get_nowait())
        self.bot._handle_update(
            {"message": {"chat": {"id": 12345}, "text": "/stop"}}
        )

        self.assertFalse(self.engine.is_enabled())
        self.assertEqual(self.engine.status()["open_positions"], 1)
        self.assertIn("remain monitored", self.bot._outbound.get_nowait())

    def test_status_reports_summary_and_paper_mode(self) -> None:
        self.bot._handle_update(
            {"message": {"chat": {"id": 12345}, "text": "/status"}}
        )
        message = self.bot._outbound.get_nowait()

        self.assertIn("paper mode", message.lower())
        self.assertIn("Total Trades Opened: 0", message)
        self.assertIn("HTF filter: 15m", message)

    def test_entry_target_and_stop_alerts_are_queued(self) -> None:
        self.engine.handle_signal("SOLUSDT", "buy", 100.0)
        self.assertIn("Paper trade entry", self.bot._outbound.get_nowait())

        self.market_data.prices["SOLUSDT"] = 103.0
        self.engine.check_positions_once(candle_open_time_ms=1_761_186_000_000)
        self.assertIn(
            "[🎯] TARGET HUNTER!",
            self.bot._outbound.get_nowait(),
        )

        self.engine.handle_signal("SOLUSDT", "buy", 100.0)
        self.bot._outbound.get_nowait()  # entry alert for the second trade
        self.market_data.prices["SOLUSDT"] = 98.0
        self.engine.check_positions_once(candle_open_time_ms=1_761_186_300_000)
        self.assertIn(
            "[⚠️] STOP LOSS HUNTED!",
            self.bot._outbound.get_nowait(),
        )


if __name__ == "__main__":
    unittest.main()
