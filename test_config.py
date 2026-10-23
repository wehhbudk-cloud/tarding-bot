"""Tests for loading timeframe settings without changing the real environment."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Config


class ConfigTimeframeTests(unittest.TestCase):
    def load_config(self, content: str, environment: dict[str, str]) -> Config:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            (project_dir / ".env").write_text(content, encoding="utf-8")
            with patch("config.PROJECT_DIR", project_dir):
                with patch.dict(os.environ, environment, clear=True):
                    return Config.from_environment()

    def test_default_timeframes(self) -> None:
        config = self.load_config("", {})
        self.assertEqual(config.timeframe, "5m")
        self.assertEqual(config.htf_timeframe, "15m")

    def test_primary_timeframe_loads_but_htf_stays_fixed(self) -> None:
        config = self.load_config("TIMEFRAME=1m\nHTF_TIMEFRAME=15m\n", {})
        self.assertEqual(config.timeframe, "1m")
        self.assertEqual(config.htf_timeframe, "15m")

    def test_environment_takes_precedence_over_dotenv(self) -> None:
        config = self.load_config(
            "TIMEFRAME=1m\nHTF_TIMEFRAME=15m\n",
            {"TIMEFRAME": "3m", "HTF_TIMEFRAME": "15m"},
        )
        self.assertEqual(config.timeframe, "3m")
        self.assertEqual(config.htf_timeframe, "15m")

    def test_non_15m_htf_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed at 15m"):
            self.load_config("", {"HTF_TIMEFRAME": "30m"})

    def test_quoted_timeframes(self) -> None:
        config = self.load_config('TIMEFRAME="5m"\nHTF_TIMEFRAME=\'15m\'\n', {})
        self.assertEqual(config.timeframe, "5m")
        self.assertEqual(config.htf_timeframe, "15m")

    def test_defaults_without_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("config.PROJECT_DIR", Path(directory)):
                with patch.dict(os.environ, {}, clear=True):
                    config = Config.from_environment()
        self.assertEqual(config.timeframe, "5m")
        self.assertEqual(config.htf_timeframe, "15m")


if __name__ == "__main__":
    unittest.main()
