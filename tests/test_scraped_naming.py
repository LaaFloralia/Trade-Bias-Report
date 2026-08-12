from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 5, 10, 0, tzinfo=tz)


def test_save_scraped_weekly_uses_weekly_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(main, "datetime", FixedDatetime)

    json_path, txt_path = main.save_scraped({"source": {"XAUUSD": {"price": 4000}}}, "text", weekly=True)

    assert json_path == tmp_path / "output" / "scraped_data_weekly_2026-07-05.json"
    assert txt_path == tmp_path / "output" / "scraped_data_weekly_2026-07-05.txt"
    assert json.loads(json_path.read_text(encoding="utf-8"))["source"]["XAUUSD"]["price"] == 4000
    assert txt_path.read_text(encoding="utf-8") == "text"


def test_save_scraped_daily_keeps_legacy_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(main, "datetime", FixedDatetime)

    json_path, txt_path = main.save_scraped({"source": {}}, "daily text", weekly=False)

    assert json_path.name == "scraped_data_2026-07-05.json"
    assert txt_path.name == "scraped_data_2026-07-05.txt"
    assert not json_path.name.startswith("scraped_data_weekly_")


def test_save_scraped_symbol_uses_symbol_prefix(tmp_path, monkeypatch):
    """個別銘柄は scraped_data_{SYM}_ 系列（日付直結 glob から外れる別名）になる。"""
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(main, "datetime", FixedDatetime)

    json_path, txt_path = main.save_scraped({"source": {}}, "t", weekly=False, symbol="USDJPY")

    assert json_path.name == "scraped_data_USDJPY_2026-07-05.json"
    assert txt_path.name == "scraped_data_USDJPY_2026-07-05.txt"


def test_save_scraped_default_symbol_keeps_legacy_prefix(tmp_path, monkeypatch):
    """デフォルト銘柄の明示指定は従来ファイル名（外部契約）を維持する。"""
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(main, "datetime", FixedDatetime)

    from config import DEFAULT_SYMBOL

    json_path, _ = main.save_scraped({"source": {}}, "t", weekly=False, symbol=DEFAULT_SYMBOL)
    assert json_path.name == "scraped_data_2026-07-05.json"
