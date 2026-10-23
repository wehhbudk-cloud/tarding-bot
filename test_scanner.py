"""Tests for candle warm-up and automatic signal-to-paper execution."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from config import Config
from engine import BotEngine
from exchange import Kline
from scanner import SignalScanner
from test_pine_signals import make_candle, wait_buy_setup


FIVE_MINUTES_MS = 5 * 60 * 1000


class FakeMarketData:
    def __init__(
        self,
        candles: list[Kline],
        htf_candles: list[Kline] | None = None,
    ) -> None:
        self.candles_by_interval = {
            "5m": list(candles),
            "15m": list(htf_candles or []),
        }
        self.requested_intervals: list[str] = []

    def get_symbol_info(self, symbol: str) -> dict[str, str | float]:
        return {
            "status": "TRADING",
            "quote_asset": "USDT",
            "tick_size": 0.01,
        }

    def get_klines(
        self,
        symbol: str,
        limit: int = 500,
        interval: str = "15m",
    ) -> list[Kline]:
        self.requested_intervals.append(interval)
        return self.candles_by_interval.get(interval, [])[-limit:]

    def get_price(self, symbol: str) -> float:
        return 100.0


class ScannerTests(unittest.TestCase):
    @staticmethod
    def _config(state_file: Path) -> Config:
        return Config(
            watchlist=("SOLUSDT",),
            quote_amount=Decimal("10"),
            total_budget=Decimal("30"),
            max_positions=3,
            host="127.0.0.1",
            port=8000,
            poll_seconds=5,
            state_file=state_file,
            timeframe="5m",
            htf_timeframe="15m",
        )

    @staticmethod
    def _rescale(
        candles: list[Kline], interval_ms: int, *, offset_ms: int = 0
    ) -> list[Kline]:
        return [
            Kline(
                open_time=offset_ms + index * interval_ms,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                close_time=offset_ms + (index + 1) * interval_ms - 1,
            )
            for index, candle in enumerate(candles)
        ]

    def test_buy_requires_a_closed_matching_higher_timeframe_signal(self) -> None:
        setup = wait_buy_setup()
        # The higher-timeframe buy closes before this later 5m setup begins.
        primary_prefix = [
            make_candle(
                index,
                open_price=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
            )
            for index in range(45)
        ]
        primary_candles = self._rescale(primary_prefix, FIVE_MINUTES_MS)
        primary_setup = self._rescale(
            setup,
            FIVE_MINUTES_MS,
            offset_ms=45 * FIVE_MINUTES_MS,
        )
        market_data = FakeMarketData(
            primary_candles + primary_setup[:-1],
            htf_candles=setup,
        )

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory) / "state.json")
            engine = BotEngine(config, market_data)
            scanner = SignalScanner(config, market_data, engine)

            scanner.scan_once()
            self.assertEqual(engine.status()["open_positions"], 0)
            self.assertEqual(market_data.requested_intervals, ["5m", "15m"])

            # Reload both simulator streams and the higher-timeframe bias.
            market_data.candles_by_interval["5m"].append(primary_setup[-1])
            resumed_engine = BotEngine(config, market_data)
            resumed_scanner = SignalScanner(config, market_data, resumed_engine)
            resumed_scanner.scan_once()

            self.assertEqual(resumed_engine.status()["open_positions"], 1)
            self.assertEqual(
                resumed_engine.status()["positions"][0]["symbol"], "SOLUSDT"
            )
            self.assertEqual(
                resumed_engine.status()["scans"]["SOLUSDT"]["last_signal"]["action"],
                "buy",
            )
            self.assertTrue(
                resumed_engine.status()["scans"]["SOLUSDT"]["last_signal"][
                    "higher_timeframe_confirmed"
                ]
            )

    def test_primary_signal_is_ignored_without_higher_timeframe_bias(self) -> None:
        primary_prefix = [
            make_candle(
                index,
                open_price=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
            )
            for index in range(45)
        ]
        primary_candles = self._rescale(primary_prefix, FIVE_MINUTES_MS)
        primary_setup = self._rescale(
            wait_buy_setup(),
            FIVE_MINUTES_MS,
            offset_ms=45 * FIVE_MINUTES_MS,
        )
        flat_htf = [
            make_candle(
                index,
                open_price=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
            )
            for index in range(30)
        ]
        market_data = FakeMarketData(
            primary_candles + primary_setup[:-1],
            htf_candles=flat_htf,
        )

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory) / "state.json")
            engine = BotEngine(config, market_data)
            scanner = SignalScanner(config, market_data, engine)
            scanner.scan_once()
            market_data.candles_by_interval["5m"].append(primary_setup[-1])
            scanner.scan_once()

            status = engine.status()
            self.assertEqual(status["open_positions"], 0)
            signal = status["scans"]["SOLUSDT"]["last_signal"]
            self.assertEqual(signal["status"], "ignored")
            self.assertEqual(
                signal["reason"], "higher_timeframe_confirmation_missing"
            )

    def test_primary_signal_is_ignored_when_higher_timeframe_disagrees(self) -> None:
        primary_prefix = [
            make_candle(
                index,
                open_price=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
            )
            for index in range(51)
        ]
        primary_candles = self._rescale(primary_prefix, FIVE_MINUTES_MS)
        primary_setup = self._rescale(
            wait_buy_setup(),
            FIVE_MINUTES_MS,
            offset_ms=51 * FIVE_MINUTES_MS,
        )
        htf_sell_setup = wait_buy_setup()[:13] + [
            make_candle(13, open_price=92.0, high=93.0, low=89.5, close=90.0),
            make_candle(14, open_price=90.0, high=91.0, low=88.5, close=90.0),
            make_candle(15, open_price=89.0, high=89.5, low=87.5, close=88.0),
        ]
        market_data = FakeMarketData(
            primary_candles + primary_setup[:-1],
            htf_candles=htf_sell_setup,
        )

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory) / "state.json")
            engine = BotEngine(config, market_data)
            scanner = SignalScanner(config, market_data, engine)
            scanner.scan_once()
            market_data.candles_by_interval["5m"].append(primary_setup[-1])
            scanner.scan_once()

            status = engine.status()
            self.assertEqual(status["open_positions"], 0)
            signal = status["scans"]["SOLUSDT"]["last_signal"]
            self.assertEqual(signal["status"], "ignored")
            self.assertEqual(
                signal["reason"], "higher_timeframe_confirmation_mismatch"
            )


if __name__ == "__main__":
    unittest.main()
