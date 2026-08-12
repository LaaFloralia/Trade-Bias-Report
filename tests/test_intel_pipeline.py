"""G4/G5 回帰テスト: intel.py ヘッドレスパイプライン（claude 実走なし・mock runner）"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import intel  # noqa: E402

VALID_OBJ = {
    "bias": -0.4,
    "no_trade": False,
    "no_trade_reason": None,
    "risk_events_next_24h": ["21:30 JST 米 CPI"],
    "positioning_summary": "リテールはゴールドロングに偏り、Top Trader はショート優勢。",
    "confidence": 0.7,
}


# ---------------------------------------------------------------------------
# スキーマバリデーション
# ---------------------------------------------------------------------------

def test_schema_accepts_valid_object():
    assert intel.validate_intel_json(VALID_OBJ) == []


def test_schema_rejects_violations():
    cases = [
        ({**VALID_OBJ, "bias": 1.5}, "bias"),                      # 範囲外
        ({**VALID_OBJ, "bias": "bullish"}, "bias"),                # 型違い
        ({**VALID_OBJ, "no_trade": "yes"}, "no_trade"),            # 型違い
        ({**VALID_OBJ, "confidence": -0.1}, "confidence"),         # 範囲外
        ({**VALID_OBJ, "risk_events_next_24h": [1]}, "risk_events"),  # 要素型
        ({**VALID_OBJ, "positioning_summary": " "}, "positioning"),   # 空文字
        ({**VALID_OBJ, "no_trade": True, "no_trade_reason": None}, "no_trade_reason"),
    ]
    for obj, key_hint in cases:
        errors = intel.validate_intel_json(obj)
        assert errors, f"{key_hint} の違反が検出されない"

    missing = {k: v for k, v in VALID_OBJ.items() if k != "bias"}
    assert any("bias" in e for e in intel.validate_intel_json(missing))


def test_normalize_drops_extra_keys_and_orders():
    obj = {**VALID_OBJ, "extra_field": "should be dropped"}
    normalized = intel.normalize_intel_json(obj)
    assert list(normalized.keys()) == list(intel.INTEL_JSON_KEYS)
    assert "extra_field" not in normalized


def test_fallback_json_passes_schema_and_is_safe():
    fb = intel.fallback_intel_json("テスト理由")
    assert intel.validate_intel_json(fb) == []
    assert fb["no_trade"] is True
    assert fb["bias"] == 0.0
    assert fb["confidence"] == 0.0


# ---------------------------------------------------------------------------
# JSON 抽出（コードフェンス剥がし）
# ---------------------------------------------------------------------------

def test_extract_json_object_handles_fences_and_prose():
    payload = json.dumps(VALID_OBJ, ensure_ascii=False)
    assert intel.extract_json_object(payload) == VALID_OBJ
    assert intel.extract_json_object(f"```json\n{payload}\n```") == VALID_OBJ
    assert intel.extract_json_object(f"以下が結果です。\n{payload}\nご確認ください。") == VALID_OBJ
    assert intel.extract_json_object("JSON はありません") is None
    assert intel.extract_json_object("") is None


# ---------------------------------------------------------------------------
# 機械用 JSON 生成のリトライ → フォールバック
# ---------------------------------------------------------------------------

def test_generate_machine_json_success_first_try():
    runner = lambda prompt: json.dumps(VALID_OBJ, ensure_ascii=False)  # noqa: E731
    obj, fallback, calls = intel.generate_machine_json("# report", runner)
    assert fallback is False
    assert obj["bias"] == -0.4
    assert len(calls) == 1


def test_generate_machine_json_retry_then_success():
    responses = ["これは JSON ではない", json.dumps(VALID_OBJ, ensure_ascii=False)]
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return responses[len(prompts) - 1]

    obj, fallback, calls = intel.generate_machine_json("# report", runner)
    assert fallback is False
    assert len(calls) == 2
    # リトライプロンプトに前回の問題点フィードバックが含まれる
    assert "前回出力の問題点" in prompts[1]


def test_generate_machine_json_double_failure_falls_back_safe():
    bad = json.dumps({**VALID_OBJ, "bias": 9.9}, ensure_ascii=False)  # スキーマ違反
    obj, fallback, calls = intel.generate_machine_json("# report", lambda p: bad)
    assert fallback is True
    assert obj["no_trade"] is True
    assert intel.validate_intel_json(obj) == []
    assert len(calls) == 2  # リトライは 1 回まで


# ---------------------------------------------------------------------------
# --quick: 直近 scraped_data の再利用（新規取得スキップ）
# ---------------------------------------------------------------------------

def test_find_latest_scraped_picks_newest_by_date(tmp_path, monkeypatch):
    monkeypatch.setattr(intel, "OUTPUT_DIR", tmp_path)
    assert intel.find_latest_scraped() is None

    for d in ("2026-06-01", "2026-06-10", "2026-05-30"):
        (tmp_path / f"scraped_data_{d}.txt").write_text("data", encoding="utf-8")
    (tmp_path / "other_file.txt").write_text("noise", encoding="utf-8")

    latest = intel.find_latest_scraped()
    assert latest is not None
    assert latest.name == "scraped_data_2026-06-10.txt"


def test_collect_data_quick_uses_latest_without_scraping(tmp_path, monkeypatch):
    monkeypatch.setattr(intel, "OUTPUT_DIR", tmp_path)
    (tmp_path / "scraped_data_2026-06-09.txt").write_text("old", encoding="utf-8")
    (tmp_path / "scraped_data_2026-06-11.txt").write_text("new", encoding="utf-8")

    def fail_run(*args, **kwargs):
        raise AssertionError("--quick なのに main.py が起動された")

    monkeypatch.setattr(intel.subprocess, "run", fail_run)

    # 当日ファイルが存在しなくても直近（日付不問）を使う
    path = intel.collect_data(False, False, "2026-06-12", quick=True)
    assert path.name == "scraped_data_2026-06-11.txt"


def test_collect_data_quick_fails_clearly_when_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr(intel, "OUTPUT_DIR", tmp_path)
    try:
        intel.collect_data(False, False, "2026-06-12", quick=True)
    except RuntimeError as exc:
        assert "--quick" in str(exc)
    else:
        raise AssertionError("scraped_data が無いのに RuntimeError にならない")


# ---------------------------------------------------------------------------
# エンジンシーム (INTEL_ENGINE: claude | codex)
# ---------------------------------------------------------------------------

def test_run_llm_defaults_to_claude(monkeypatch):
    monkeypatch.delenv("INTEL_ENGINE", raising=False)
    monkeypatch.setattr(intel, "run_claude",
                        lambda prompt, timeout=None, allow_tools=False: f"claude:{prompt}")
    assert intel.run_llm("hi") == "claude:hi"


def test_run_llm_dispatches_to_codex(monkeypatch):
    monkeypatch.setenv("INTEL_ENGINE", "codex")
    monkeypatch.setattr(intel, "run_codex", lambda prompt, timeout=None: f"codex:{prompt}")
    assert intel.run_llm("hi") == "codex:hi"


def test_run_llm_unknown_engine_falls_back_to_claude(monkeypatch):
    monkeypatch.setenv("INTEL_ENGINE", "gpt9")
    monkeypatch.setattr(intel, "run_claude",
                        lambda prompt, timeout=None, allow_tools=False: "claude-out")
    assert intel.run_llm("hi") == "claude-out"


def test_run_codex_missing_binary_advises_claude_switch(monkeypatch):
    monkeypatch.setenv("INTEL_CODEX_BIN", "/no/such/codex-binary")
    try:
        intel.run_codex("hi", timeout=5)
    except RuntimeError as exc:
        msg = str(exc)
        assert "codex CLI が見つかりません" in msg
        assert "INTEL_ENGINE=claude" in msg  # claude への切替案内
    else:
        raise AssertionError("codex CLI が無いのに RuntimeError にならない")


# ---------------------------------------------------------------------------
# プロンプト組み立て / 出力先
# ---------------------------------------------------------------------------

def test_report_prompt_embeds_master_prompt_and_data():
    md, prompt = intel.generate_report_md(
        "daily",
        "=== Price Data ===\nXAUUSD 4000",
        lambda p: "# ICT Daily Bias Report",
        "2026-06-11",
        "2026-06-11",
    )
    assert md.startswith("# ICT Daily Bias Report")
    assert "ICT Daily Bias Report" in prompt          # master_prompt.md 本文
    assert "XAUUSD 4000" in prompt                    # scraped データ
    assert "取得済みデータ (最優先で使用すること)" in prompt
    assert "データ基準日: 2026-06-11（パイプライン実行日: 2026-06-11）" in prompt
    # 2026-08-13: ニュースセクションを埋めるため検索系だけ許可し、ファイル操作系は禁止
    assert "ファイル操作・コマンド実行系のツール（Read / Write / Edit / Bash）は使わない" in prompt
    assert "WebSearch / WebFetch は利用可能" in prompt


def test_save_outputs_use_expected_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_PATH", str(tmp_path / "Brain"))
    monkeypatch.setattr(intel, "INTEL_DIR", tmp_path / "output" / "intel")
    monkeypatch.setattr(intel, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(intel, "JSONL_PATH", tmp_path / "logs" / "intel_runs.jsonl")

    md_path = intel.save_md_to_brain("# report", "daily", "2026-06-11")
    assert md_path == tmp_path / "Brain" / "Calendar" / "Daily-Bias" / "Daily_Bias_Report_2026-06-11.md"
    assert md_path.read_text(encoding="utf-8") == "# report"

    json_path = intel.save_intel_json(VALID_OBJ, "daily", "2026-06-11")
    assert json_path == tmp_path / "output" / "intel" / "intel_daily_2026-06-11.json"
    assert json.loads(json_path.read_text(encoding="utf-8"))["confidence"] == 0.7

    intel.append_run_log({"run_id": "test123", "ok": True})
    lines = (tmp_path / "logs" / "intel_runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["run_id"] == "test123"
