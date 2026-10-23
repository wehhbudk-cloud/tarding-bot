"""Poll Binance Spot candles and produce paper-trading signals."""

from __future__ import annotations

import logging
import time
from typing import Any

from config import Config
from engine import BotEngine
from exchange import ExchangeError
from pine_signals import LOOKBACK_LEN, PineSignalSimulator


KLINE_HISTORY_LIMIT = 500
CLOSED_CANDLE_GRACE_MS = 2_000
SCANNER_STATE_VERSION = 1


class SignalScanner:
    def __init__(self, config: Config, market_data: Any, engine: BotEngine) -> None:
        self.config = config
        self.market_data = market_data
        self.engine = engine
        self._tick_sizes: dict[str, float] = {}
        self._simulators: dict[str, PineSignalSimulator] = {}
        self._htf_simulators: dict[str, PineSignalSimulator] = {}
        self._htf_signal_history: dict[str, list[dict[str, Any]]] = {}
        self._last_errors: dict[str, str] = {}
        self._retry_at: dict[str, float] = {}

    def scan_once(self) -> None:
        for symbol in self.config.watchlist:
            if time.monotonic() < self._retry_at.get(symbol, 0.0):
                continue
            try:
                simulator, htf_simulator = self._get_simulators(symbol)
                candles = self.market_data.get_klines(
                    symbol,
                    limit=KLINE_HISTORY_LIMIT,
                    interval=self.config.timeframe,
                )
                htf_candles = self.market_data.get_klines(
                    symbol,
                    limit=KLINE_HISTORY_LIMIT,
                    interval=self.config.htf_timeframe,
                )
                if candles:
                    # Use Binance's live candle timestamp for risk exits so
                    # persisted times match TradingView's UTC chart candles.
                    self.engine.set_current_candle_time(
                        symbol, candles[-1].open_time
                    )
                self._clear_error(symbol)
                now_ms = int(time.time() * 1000) - CLOSED_CANDLE_GRACE_MS
                closed = [
                    candle for candle in candles if candle.close_time < now_ms
                ][-KLINE_HISTORY_LIMIT:]
                closed_htf = [
                    candle for candle in htf_candles if candle.close_time < now_ms
                ][-KLINE_HISTORY_LIMIT:]
                self._update_higher_timeframe(
                    symbol, htf_simulator, closed_htf
                )
                if not closed:
                    self.engine.record_scan(
                        symbol,
                        state="waiting_for_closed_candle",
                        error=None,
                    )
                    continue

                if simulator.last_open_time is None:
                    self._warm_up(symbol, simulator, closed)
                    continue

                if simulator.last_open_time < closed[0].open_time:
                    self._reseed_after_gap(symbol, simulator, closed)
                    continue

                if len(closed) < LOOKBACK_LEN:
                    self._advance_warm_up(symbol, simulator, closed)
                    continue

                new_candles = [
                    candle
                    for candle in closed
                    if candle.open_time > simulator.last_open_time
                ]
                for candle in new_candles:
                    action = simulator.process(candle)
                    if action is not None:
                        htf_bias = self._higher_timeframe_bias_at(
                            symbol, candle.close_time
                        )
                        if htf_bias == action:
                            result = self.engine.handle_signal(
                                symbol,
                                action,
                                candle.close,
                                candle_open_time_ms=candle.open_time,
                            )
                        else:
                            reason = (
                                "higher_timeframe_confirmation_missing"
                                if htf_bias is None
                                else "higher_timeframe_confirmation_mismatch"
                            )
                            result = {"status": "ignored", "reason": reason}
                        self.engine.record_scan(
                            symbol,
                            last_signal={
                                "action": action,
                                "status": result["status"],
                                "reason": result.get("reason"),
                                "candle_open_time": candle.open_time,
                                "candle_time_utc": self.engine.candle_time_utc(
                                    candle.open_time
                                ),
                                "higher_timeframe": self.config.htf_timeframe,
                                "higher_timeframe_bias": htf_bias,
                                "higher_timeframe_confirmed": htf_bias == action,
                            },
                        )
                        logging.info(
                            "%s %s Confirm-only WAIT-flow signal: %s (%s); "
                            "%s bias: %s.",
                            symbol,
                            self.config.timeframe,
                            action.upper(),
                            result["status"],
                            self.config.htf_timeframe,
                            htf_bias or "none",
                        )
                    self.engine.save_indicator_state(
                        symbol, self._serialized_state(symbol)
                    )

                if new_candles:
                    last_open_time = simulator.last_open_time
                    self.engine.record_scan(
                        symbol,
                        state="ready",
                        last_closed_candle=last_open_time,
                        error=None,
                    )
                else:
                    self.engine.record_scan(
                        symbol,
                        state="ready",
                        error=None,
                    )
                self._clear_error(symbol)
            except (ExchangeError, ValueError) as exc:
                self._record_error(symbol, str(exc))
            except Exception as exc:
                self._record_error(
                    symbol,
                    f"Unexpected scanner error ({type(exc).__name__}).",
                )

    def _get_simulators(
        self, symbol: str
    ) -> tuple[PineSignalSimulator, PineSignalSimulator]:
        if symbol in self._simulators:
            return self._simulators[symbol], self._htf_simulators[symbol]

        info = self.market_data.get_symbol_info(symbol)
        if info.get("status") != "TRADING" or info.get("quote_asset") != "USDT":
            raise ExchangeError(
                    f"{symbol} is not an active Binance USDT Spot market."
            )
        tick_size = float(info["tick_size"])
        saved_state = self.engine.get_indicator_state(symbol)
        primary_state = None
        htf_state = None
        htf_history: list[dict[str, Any]] = []
        if (
            saved_state is not None
            and saved_state.get("scanner_state_version") == SCANNER_STATE_VERSION
            and saved_state.get("timeframe") == self.config.timeframe
            and saved_state.get("htf_timeframe") == self.config.htf_timeframe
        ):
            primary_state = saved_state.get("primary")
            htf_state = saved_state.get("higher_timeframe")
            raw_history = saved_state.get("higher_timeframe_signals", [])
            if not isinstance(raw_history, list):
                raise ValueError("Saved higher-timeframe signal history is invalid.")
            for event in raw_history:
                if (
                    not isinstance(event, dict)
                    or event.get("action") not in {"buy", "sell"}
                ):
                    raise ValueError(
                        "Saved higher-timeframe signal history is invalid."
                    )
                try:
                    close_time = int(event["close_time"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError(
                        "Saved higher-timeframe signal history is invalid."
                    ) from None
                htf_history.append(
                    {"close_time": close_time, "action": event["action"]}
                )
        # Legacy indicator-only snapshots lack an interval identity. Rebuild
        # both streams from closed candles rather than mixing intervals.
        simulator = PineSignalSimulator.from_dict(primary_state, tick_size=tick_size)
        htf_simulator = PineSignalSimulator.from_dict(htf_state, tick_size=tick_size)
        self._tick_sizes[symbol] = tick_size
        self._simulators[symbol] = simulator
        self._htf_simulators[symbol] = htf_simulator
        self._htf_signal_history[symbol] = htf_history
        return simulator, htf_simulator

    def _serialized_state(self, symbol: str) -> dict[str, Any]:
        return {
            "scanner_state_version": SCANNER_STATE_VERSION,
            "timeframe": self.config.timeframe,
            "htf_timeframe": self.config.htf_timeframe,
            "primary": self._simulators[symbol].to_dict(),
            "higher_timeframe": self._htf_simulators[symbol].to_dict(),
            "higher_timeframe_signals": self._htf_signal_history[symbol][-1000:],
        }

    def _save_simulators(self, symbol: str) -> None:
        self.engine.save_indicator_state(symbol, self._serialized_state(symbol))

    def _update_higher_timeframe(
        self,
        symbol: str,
        simulator: PineSignalSimulator,
        candles: list[Any],
    ) -> None:
        if not candles:
            return
        history = self._htf_signal_history[symbol]
        if (
            simulator.last_open_time is None
            or simulator.last_open_time < candles[0].open_time
        ):
            simulator = PineSignalSimulator(tick_size=self._tick_sizes[symbol])
            self._htf_simulators[symbol] = simulator
            history = []
            self._htf_signal_history[symbol] = history
            candidates = candles
        else:
            candidates = [
                candle
                for candle in candles
                if candle.open_time > simulator.last_open_time
            ]

        if not candidates:
            return
        for candle in candidates:
            action = simulator.process(candle)
            if action is not None:
                history.append(
                    {"close_time": candle.close_time, "action": action}
                )
        self._htf_signal_history[symbol] = history[-1000:]
        self._save_simulators(symbol)

    def _higher_timeframe_bias_at(
        self, symbol: str, candle_close_time: int
    ) -> str | None:
        bias: str | None = None
        for event in self._htf_signal_history[symbol]:
            if event["close_time"] > candle_close_time:
                break
            bias = event["action"]
        return bias

    def _warm_up(
        self,
        symbol: str,
        simulator: PineSignalSimulator,
        candles: list[Any],
    ) -> None:
        for candle in candles:
            simulator.process(candle, emit_signal=False)
        self._save_simulators(symbol)
        enough_history = len(candles) >= LOOKBACK_LEN
        state = "ready" if enough_history else "warming_up"
        self.engine.record_scan(
            symbol,
            state=state,
            last_closed_candle=simulator.last_open_time,
            error=None,
        )
        logging.info(
            "%s Pine state initialized from %s closed %s candles; no "
            "historical paper entries were placed.",
            symbol,
            len(candles),
            self.config.timeframe,
        )

    def _advance_warm_up(
        self,
        symbol: str,
        simulator: PineSignalSimulator,
        candles: list[Any],
    ) -> None:
        new_candles = [
            candle
            for candle in candles
            if simulator.last_open_time is None
            or candle.open_time > simulator.last_open_time
        ]
        for candle in new_candles:
            simulator.process(candle, emit_signal=False)
        if new_candles:
            self._save_simulators(symbol)
        self.engine.record_scan(
            symbol,
            state="warming_up",
            last_closed_candle=simulator.last_open_time,
            error=None,
        )

    def _reseed_after_gap(
        self,
        symbol: str,
        simulator: PineSignalSimulator,
        candles: list[Any],
    ) -> None:
        replacement = PineSignalSimulator(
            tick_size=self._tick_sizes[symbol]
        )
        for candle in candles:
            replacement.process(candle, emit_signal=False)
        self._simulators[symbol] = replacement
        self._save_simulators(symbol)
        self.engine.record_scan(
            symbol,
            state="reseeded_after_history_gap",
            last_closed_candle=replacement.last_open_time,
            error=None,
        )
        logging.warning(
            "%s Pine state was rebuilt from available %s history after "
            "older candles fell out of the 500-candle API window. No "
            "historical paper entries were placed.",
            symbol,
            self.config.timeframe,
        )

    def _record_error(self, symbol: str, error: str) -> None:
        self._retry_at[symbol] = time.monotonic() + 60
        self.engine.record_scan(
            symbol,
            state="market_data_error",
            error=error,
        )
        if self._last_errors.get(symbol) != error:
            logging.warning("Scanner cannot update %s: %s", symbol, error)
            self._last_errors[symbol] = error

    def _clear_error(self, symbol: str) -> None:
        self._retry_at.pop(symbol, None)
        if symbol in self._last_errors:
            logging.info("Scanner market data recovered for %s.", symbol)
            self._last_errors.pop(symbol, None)
