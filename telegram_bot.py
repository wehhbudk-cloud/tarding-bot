"""Allowlisted Telegram polling, remote pause/resume, status, and trade alerts."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from engine import BotEngine


class TelegramApiError(RuntimeError):
    """Sanitized Telegram API error that never includes the bot token or URL."""


class TelegramBot:
    def __init__(
        self,
        token: str,
        allowed_chat_id: str,
        engine: BotEngine,
        offset_file: Path,
    ) -> None:
        self._token = token.strip()
        self._allowed_chat_id = allowed_chat_id.strip()
        self._engine = engine
        self._offset_file = offset_file
        self._stop = threading.Event()
        self._outbound: queue.Queue[str] = queue.Queue()
        self._offset = self._load_offset()
        if not self._token or not self._allowed_chat_id:
            raise ValueError("Telegram token and allowlisted chat ID are required.")
        try:
            int(self._allowed_chat_id)
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_CHAT_ID must be a numeric chat ID.") from exc
        self._engine.set_event_handler(self.notify_trade)

    @classmethod
    def from_environment(
        cls, engine: BotEngine, offset_file: Path
    ) -> "TelegramBot | None":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
        if not token or not chat_id:
            logging.warning(
                "Telegram remote control is disabled; configure both required "
                "Telegram secrets."
            )
            return None
        return cls(token, chat_id, engine, offset_file)

    def _load_offset(self) -> int | None:
        try:
            raw_value = self._offset_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        try:
            return int(raw_value)
        except ValueError as exc:
            raise RuntimeError(
                "Telegram update offset is invalid; inspect its state file."
            ) from exc

    def _save_offset(self, value: int) -> None:
        self._offset_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._offset_file.with_name(self._offset_file.name + ".tmp")
        temporary.write_text(str(value), encoding="utf-8")
        temporary.replace(self._offset_file)
        self._offset = value

    def _request(
        self,
        method: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout: int = 15,
    ) -> Any:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                response_body = response.read()
        except HTTPError as exc:
            raise TelegramApiError(
                f"Telegram returned HTTP {exc.code}."
            ) from None
        except (URLError, TimeoutError, OSError):
            raise TelegramApiError("Could not reach the Telegram Bot API.") from None
        try:
            result = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramApiError("Telegram returned an unreadable response.") from None
        if not isinstance(result, dict) or not result.get("ok"):
            raise TelegramApiError("Telegram rejected the bot API request.")
        return result.get("result")

    def notify_trade(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        symbol = event.get("symbol", "unknown")
        candle_time = event.get("candle_time_utc", "unknown UTC candle")
        if kind == "entry":
            message = (
                f"Paper trade entry: {symbol}\n"
                f"Entry: {event['entry_price']}\n"
                f"Target (+3%): {event['target_price']}\n"
                f"Stop (-2%): {event['stop_price']}\n"
                f"TradingView candle UTC: {candle_time}"
            )
        elif kind == "target_hunter":
            message = (
                f"[🎯] TARGET HUNTER! {symbol}\n"
                f"Exit: {event['exit_price']}\n"
                f"Net P/L: {event['net_pnl_usd']} USDT "
                f"({float(event['net_pnl_percent']):+.2f}%)\n"
                f"TradingView candle UTC: {candle_time}"
            )
        elif kind == "stop_loss_hunted":
            message = (
                f"[⚠️] STOP LOSS HUNTED! {symbol}\n"
                f"Exit: {event['exit_price']}\n"
                f"Net P/L: {event['net_pnl_usd']} USDT "
                f"({float(event['net_pnl_percent']):+.2f}%)\n"
                f"TradingView candle UTC: {candle_time}"
            )
        else:
            message = (
                f"Paper trade closed: {symbol}\n"
                f"Reason: {event.get('reason', 'closed')}\n"
                f"Exit: {event.get('exit_price', 'unknown')}\n"
                f"Net P/L: {event.get('net_pnl_usd', '0')} USDT\n"
                f"TradingView candle UTC: {candle_time}"
            )
        self._outbound.put(message)

    def _status_message(self) -> str:
        status = self._engine.status()
        lines = [
            f"Paper bot: {'RUNNING' if status['enabled'] else 'PAUSED'}",
            "Exchange orders: disabled (paper mode)",
            f"Signal interval: {status['timeframe']} | HTF filter: 15m",
            f"Open positions: {status['open_positions']}",
            (
                "Total Trades Opened: "
                f"{status['total_trades_opened']} | Wins: "
                f"{status['successful_trades']} | Losses: "
                f"{status['failed_trades']}"
            ),
            (
                "Net Cumulative P/L: "
                f"${float(status['net_cumulative_profit_usd']):.6f} "
                f"({float(status['net_cumulative_profit_percent']):+.2f}%)"
            ),
        ]
        for position in status["positions"]:
            lines.append(
                f"{position['symbol']}: entry {position['entry_price']}, "
                f"target {position['target_price']}, stop {position['stop_price']}"
            )
        return "\n".join(lines)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != self._allowed_chat_id:
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        parts = text.strip().split()
        if not parts:
            return
        command = parts[0].split("@", 1)[0].lower()
        if command == "/start":
            self._engine.set_enabled(True)
            self._outbound.put(
                "Paper bot started. New entries are enabled; open trades remain "
                "monitored for their fixed exits."
            )
        elif command == "/stop":
            self._engine.set_enabled(False)
            self._outbound.put(
                "New signal entries are paused. Open paper trades remain monitored "
                "for their fixed +3% target and -2% stop."
            )
        elif command == "/status":
            self._outbound.put(self._status_message())

    def _poll_once(self) -> list[dict[str, Any]]:
        query = {
            "timeout": "8",
            "allowed_updates": json.dumps(["message"]),
        }
        if self._offset is not None:
            query["offset"] = str(self._offset)
        result = self._request("getUpdates", query=query, timeout=15)
        if not isinstance(result, list):
            raise TelegramApiError("Telegram returned invalid update data.")
        return [item for item in result if isinstance(item, dict)]

    def _flush_outbound(self) -> None:
        while True:
            try:
                message = self._outbound.get_nowait()
            except queue.Empty:
                return
            try:
                self._request(
                    "sendMessage",
                    body={"chat_id": self._allowed_chat_id, "text": message},
                    timeout=15,
                )
            except TelegramApiError as exc:
                logging.warning("Telegram alert could not be sent: %s", exc)
                self._outbound.put(message)
                return

    def run_forever(self) -> None:
        last_error: str | None = None
        logging.info("Telegram remote control is connected for its allowlisted chat.")
        while not self._stop.is_set():
            self._flush_outbound()
            try:
                updates = self._poll_once()
                last_error = None
            except TelegramApiError as exc:
                message = str(exc)
                if message != last_error:
                    logging.warning("Telegram polling issue: %s", message)
                    last_error = message
                self._stop.wait(5)
                continue

            for update in updates:
                update_id = update.get("update_id")
                try:
                    self._handle_update(update)
                except Exception as exc:
                    logging.warning(
                        "Telegram command could not be processed (%s).",
                        type(exc).__name__,
                    )
                if isinstance(update_id, int):
                    self._save_offset(update_id + 1)
            self._flush_outbound()

    def stop(self) -> None:
        self._stop.set()
