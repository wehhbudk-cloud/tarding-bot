"""Tests for the read-only Binance Spot market-data client."""

from __future__ import annotations

import unittest

from exchange import BINANCE_SPOT_BASE_URL, BinanceMarketDataClient


class ExchangeTests(unittest.TestCase):
    def test_kline_request_uses_requested_interval(self) -> None:
        client = BinanceMarketDataClient()
        captured: dict[str, object] = {}

        def fake_request(path: str, params: dict[str, str]) -> list[list[object]]:
            captured["path"] = path
            captured["params"] = params
            return [[0, "10", "11", "9", "10.5", "100", 899999]]

        client._request = fake_request  # type: ignore[method-assign]
        candles = client.get_klines("SOLUSDT", limit=1, interval="5m")

        self.assertEqual(captured["path"], "/api/v3/klines")
        self.assertEqual(captured["params"]["interval"], "5m")
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].close, 10.5)
        self.assertEqual(
            BINANCE_SPOT_BASE_URL, "https://data-api.binance.vision"
        )

    def test_rejects_empty_interval(self) -> None:
        with self.assertRaises(ValueError):
            BinanceMarketDataClient().get_klines("SOLUSDT", interval=" ")

    def test_rejects_invalid_candle_limit(self) -> None:
        client = BinanceMarketDataClient()
        with self.assertRaises(ValueError):
            client.get_klines("SOLUSDT", limit=0)


if __name__ == "__main__":
    unittest.main()
