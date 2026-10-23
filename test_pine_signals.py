"""Tests for the converted 15-minute Confirm-only WAIT-flow signals."""

from __future__ import annotations

import unittest

from exchange import Kline, SIGNAL_INTERVAL_MS
from pine_signals import FLIP_ENTRY_MODE, MIN_HOLD_BARS, PineSignalSimulator


def make_candle(
    index: int,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100.0,
) -> Kline:
    open_time = index * SIGNAL_INTERVAL_MS
    return Kline(
        open_time=open_time,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        close_time=open_time + SIGNAL_INTERVAL_MS - 1,
    )


def wait_buy_setup() -> list[Kline]:
    candles = [
        make_candle(
            index,
            open_price=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
        )
        for index in range(10)
    ]
    candles.extend(
        [
            make_candle(10, open_price=91.0, high=92.0, low=89.0, close=90.0),
            make_candle(11, open_price=90.0, high=92.0, low=89.5, close=91.0),
            make_candle(12, open_price=91.0, high=94.0, low=90.0, close=93.0),
            make_candle(13, open_price=93.0, high=94.0, low=92.0, close=93.0),
            make_candle(14, open_price=93.0, high=95.5, low=92.0, close=95.0),
        ]
    )
    return candles


class PineSignalTests(unittest.TestCase):
    def test_immediate_flip_entries_are_disabled_and_minimum_hold_is_one(self) -> None:
        self.assertIsNone(FLIP_ENTRY_MODE)
        self.assertEqual(MIN_HOLD_BARS, 1)

    def test_buy_is_emitted_only_when_wait_level_is_confirmed(self) -> None:
        simulator = PineSignalSimulator(tick_size=0.01)
        emitted: list[tuple[int, str]] = []
        for candle in wait_buy_setup():
            action = simulator.process(candle)
            if action is not None:
                emitted.append((candle.open_time // SIGNAL_INTERVAL_MS, action))

        self.assertEqual(emitted, [(14, "buy")])

    def test_sell_is_emitted_when_close_breaks_the_wait_buy_low(self) -> None:
        candles = wait_buy_setup()[:13]
        candles.extend(
            [
                make_candle(13, open_price=92.0, high=93.0, low=89.5, close=90.0),
                make_candle(14, open_price=90.0, high=91.0, low=88.5, close=90.0),
                make_candle(15, open_price=89.0, high=89.5, low=87.5, close=88.0),
            ]
        )
        simulator = PineSignalSimulator(tick_size=0.01)
        emitted: list[tuple[int, str]] = []
        for candle in candles:
            action = simulator.process(candle)
            if action is not None:
                emitted.append((candle.open_time // SIGNAL_INTERVAL_MS, action))

        self.assertEqual(emitted, [(15, "sell")])

    def test_warmup_suppresses_historical_signal_emission(self) -> None:
        simulator = PineSignalSimulator(tick_size=0.01)
        outputs = [
            simulator.process(candle, emit_signal=False)
            for candle in wait_buy_setup()
        ]

        self.assertTrue(all(action is None for action in outputs))
        self.assertEqual(simulator.last_open_time, 14 * SIGNAL_INTERVAL_MS)
        self.assertIsNotNone(simulator.position)

    def test_saved_state_continues_with_the_same_next_signal(self) -> None:
        candles = wait_buy_setup()
        first = PineSignalSimulator(tick_size=0.01)
        for candle in candles[:-1]:
            first.process(candle, emit_signal=False)
        restored = PineSignalSimulator.from_dict(
            first.to_dict(),
            tick_size=0.01,
        )

        original_action = first.process(candles[-1])
        restored_action = restored.process(candles[-1])
        self.assertEqual(original_action, "buy")
        self.assertEqual(restored_action, original_action)

    def test_duplicate_candle_is_not_processed_twice(self) -> None:
        simulator = PineSignalSimulator(tick_size=0.01)
        for candle in wait_buy_setup():
            simulator.process(candle)

        self.assertIsNone(simulator.process(wait_buy_setup()[-1]))


if __name__ == "__main__":
    unittest.main()
