"""データバリデーション モジュール

format_scraped_data() でClaudeにデータを渡す直前に実行するバリデーション処理。
失敗したデータは「データ異常: [理由]」に置き換える。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

# B-1: PDH/PDL/PWH/PWL の最小レンジ閾値
MIN_RANGE_THRESHOLDS = {
    "XAUUSD": 5.0,
    "USDJPY": 0.05,
    "BTCUSD": 200.0,
    "DXY": 0.1,
}

# B-2: 前日比の異常値閾値 (%)
CHANGE_PCT_THRESHOLDS = {
    "XAUUSD": 10.0,
    "USDJPY": 3.0,
    "BTCUSD": 15.0,
    "DXY": 3.0,
}


def _check_zero_null_negative(value: Optional[float], field_name: str) -> Optional[str]:
    """B-3: 価格のゼロ・NULL・負数チェック。"""
    if value is None:
        return f"{field_name}がNull"
    if value <= 0:
        return f"{field_name}がゼロまたは負数 ({value})"
    return None


def _check_high_low_inversion(high: Optional[float], low: Optional[float], label: str) -> Optional[str]:
    """B-4: High < Low の逆転チェック。"""
    if high is not None and low is not None and high < low:
        return f"{label} High({high}) < Low({low}) 逆転"
    return None


def validate_price_data(symbol: str, data: dict) -> List[str]:
    """価格データの包括的バリデーション。

    Args:
        symbol: 銘柄名 (XAUUSD, USDJPY, BTCUSD, DXY)
        data: 価格データの辞書。以下のキーを期待:
            - current_price / close
            - prev_close / previous_close
            - change_pct / percent_change
            - pdh, pdl, pwh, pwl, pmh, pml

    Returns:
        異常メッセージのリスト。空なら正常。
    """
    issues = []

    # --- B-3: 現在価格のゼロ・NULL・負数チェック ---
    current = data.get("current_price") or data.get("close")
    if current is not None:
        try:
            current = float(current)
        except (ValueError, TypeError):
            current = None
    issue = _check_zero_null_negative(current, "現在価格")
    if issue:
        issues.append(issue)

    prev_close = data.get("prev_close") or data.get("previous_close")
    if prev_close is not None:
        try:
            prev_close = float(prev_close)
        except (ValueError, TypeError):
            prev_close = None
    issue = _check_zero_null_negative(prev_close, "前日終値")
    if issue:
        issues.append(issue)

    # --- B-2: 前日比の異常値チェック ---
    change_pct = data.get("change_pct") or data.get("percent_change")
    if change_pct is not None:
        try:
            change_pct = float(change_pct)
        except (ValueError, TypeError):
            change_pct = None

    if change_pct is not None and symbol in CHANGE_PCT_THRESHOLDS:
        threshold = CHANGE_PCT_THRESHOLDS[symbol]
        if abs(change_pct) > threshold:
            issues.append(f"前日比 {change_pct:+.2f}% が閾値 ±{threshold}% を超過")

    # --- B-1: PDH/PDL/PWH/PWL の最小レンジチェック ---
    threshold = MIN_RANGE_THRESHOLDS.get(symbol, 0)
    for h_key, l_key, label in [("pdh", "pdl", "PDH/PDL"), ("pwh", "pwl", "PWH/PWL"), ("pmh", "pml", "PMH/PML")]:
        h = data.get(h_key)
        l = data.get(l_key)

        if h is not None and l is not None:
            try:
                h, l = float(h), float(l)
            except (ValueError, TypeError):
                continue

            # B-4: High < Low の逆転チェック
            inversion = _check_high_low_inversion(h, l, label)
            if inversion:
                issues.append(inversion)
                continue

            # B-1: レンジチェック
            range_val = h - l
            if range_val < threshold:
                issues.append(f"{label} レンジ {range_val:.4f} が閾値 {threshold} 未満")

        # B-3: 個別のゼロ・NULL・負数チェック
        if h is not None:
            try:
                h_float = float(h)
                issue = _check_zero_null_negative(h_float, f"{label.split('/')[0]}")
                if issue:
                    issues.append(issue)
            except (ValueError, TypeError):
                pass
        if l is not None:
            try:
                l_float = float(l)
                issue = _check_zero_null_negative(l_float, f"{label.split('/')[1]}")
                if issue:
                    issues.append(issue)
            except (ValueError, TypeError):
                pass

    return issues


def validate_twelvedata_instrument(symbol: str, quote: dict, series: List[dict]) -> List[str]:
    """Twelve Data から取得した1銘柄のデータをバリデーションする。

    Args:
        symbol: 銘柄名
        quote: /quote APIのレスポンス
        series: /time_series APIのvaluesリスト

    Returns:
        異常メッセージのリスト。
    """
    issues = []

    if not quote or not quote.get("close"):
        return ["データ未取得"]

    # quoteデータの変換
    data = {
        "current_price": quote.get("close"),
        "prev_close": quote.get("previous_close"),
        "change_pct": quote.get("percent_change"),
    }

    # seriesからPDH/PDL等を抽出
    if len(series) >= 2:
        try:
            data["pdh"] = float(series[1]["high"])
            data["pdl"] = float(series[1]["low"])
        except (KeyError, ValueError, IndexError):
            pass

    if len(series) >= 6:
        try:
            week_data = series[1:6]
            data["pwh"] = max(float(v["high"]) for v in week_data)
            data["pwl"] = min(float(v["low"]) for v in week_data)
        except (KeyError, ValueError):
            pass

    if len(series) >= 23:
        try:
            month_data = series[1:23]
            data["pmh"] = max(float(v["high"]) for v in month_data)
            data["pml"] = min(float(v["low"]) for v in month_data)
        except (KeyError, ValueError):
            pass

    return validate_price_data(symbol, data)


def validate_dxy_data(dxy: dict) -> List[str]:
    """DXYデータのバリデーション。"""
    return validate_price_data("DXY", dxy)


# DGS10 ≒ DFII10 + T10YIE の恒等式許容差。
# FRED は各系列を独立に小数 2 桁で公表するため丸め誤差 ±0.02 程度は正常。
FRED_IDENTITY_TOLERANCE = 0.05


def validate_fred_identity(fred: Optional[dict]) -> List[str]:
    """B-5: FRED 名目金利の恒等式チェック（DGS10 ≒ DFII10 + T10YIE）。

    3 系列すべての value が揃い、かつ as_of_date が一致している場合のみ検査する
    （公表タイミング差で基準日がズレている時の偽陽性を避ける）。
    """
    if not isinstance(fred, dict):
        return []

    entries = {}
    for sid in ("DGS10", "DFII10", "T10YIE"):
        e = fred.get(sid)
        if not isinstance(e, dict) or e.get("value") is None:
            return []  # 系列欠落時は検査対象外（取得失敗は別途 error で報告される）
        entries[sid] = e

    as_of_dates = {e.get("as_of_date") for e in entries.values()}
    if len(as_of_dates) != 1 or None in as_of_dates:
        return []  # 基準日不一致は恒等式の前提が崩れるため検査しない

    try:
        dgs10 = float(entries["DGS10"]["value"])
        dfii10 = float(entries["DFII10"]["value"])
        t10yie = float(entries["T10YIE"]["value"])
    except (TypeError, ValueError):
        return ["FRED 恒等式チェック: 値の数値変換に失敗"]

    diff = abs(dgs10 - (dfii10 + t10yie))
    if diff > FRED_IDENTITY_TOLERANCE:
        return [
            f"FRED 恒等式違反: DGS10 {dgs10:.3f} ≠ DFII10 {dfii10:.3f} + "
            f"T10YIE {t10yie:.3f}（差 {diff:.3f} > 許容 {FRED_IDENTITY_TOLERANCE}）"
        ]
    return []


def validate_all(scraped_data: dict) -> Dict[str, List[str]]:
    """全データを一括バリデーションし、結果をログ出力する。

    Args:
        scraped_data: collect_all_data() の返り値

    Returns:
        銘柄→異常メッセージリストの辞書
    """
    results = {}

    # DXYバリデーション
    dxy = scraped_data.get("dxy")
    if dxy and isinstance(dxy, dict):
        issues = validate_dxy_data(dxy)
        if issues:
            results["DXY"] = issues

    # price_data は既にテキスト化されているため、
    # 元のquote/seriesデータがある場合にバリデーション
    # (main.py が _raw_quote_* / _raw_series_* キーで保持する)
    from config import TWELVEDATA_SYMBOLS  # 銘柄は config.yaml（SSoT）由来

    for symbol in TWELVEDATA_SYMBOLS:
        key = f"_raw_quote_{symbol}"
        series_key = f"_raw_series_{symbol}"
        if key in scraped_data:
            issues = validate_twelvedata_instrument(
                symbol,
                scraped_data[key],
                scraped_data.get(series_key, []),
            )
            if issues:
                results[symbol] = issues

    # FRED 恒等式チェック（DGS10 ≒ DFII10 + T10YIE）
    fred_issues = validate_fred_identity(scraped_data.get("fred"))
    if fred_issues:
        results["FRED"] = fred_issues

    # ログ出力
    if results:
        print("\n  [VALIDATION] バリデーション結果:")
        for symbol, issues in results.items():
            for issue in issues:
                print(f"  [WARN]  {symbol}: {issue}")
    else:
        print("\n  [VALIDATION] 全データ正常")

    return results


def apply_validation(formatted_text: str, validation_results: Dict[str, List[str]]) -> str:
    """バリデーション失敗データをフォーマット済みテキスト内で「データ異常」に置き換える。

    Args:
        formatted_text: format_scraped_data() の出力テキスト
        validation_results: validate_all() の返り値

    Returns:
        バリデーション済みテキスト
    """
    if not validation_results:
        return formatted_text

    lines = formatted_text.split("\n")
    new_lines = []

    for line in lines:
        replaced = False
        for symbol, issues in validation_results.items():
            if f"[{symbol}]" in line:
                # セクションヘッダー — そのまま通す
                new_lines.append(line)
                replaced = True
                break

            # PDH/PDL等の行をチェック
            for issue in issues:
                if "PDH/PDL" in issue and "PDH:" in line and symbol in _find_context_symbol(lines, new_lines):
                    new_lines.append(f"PDH/PDL: データ異常: {issue}")
                    replaced = True
                    break
                if "PWH/PWL" in issue and "PWH:" in line and symbol in _find_context_symbol(lines, new_lines):
                    new_lines.append(f"PWH/PWL: データ異常: {issue}")
                    replaced = True
                    break
                if "PMH/PML" in issue and "PMH:" in line and symbol in _find_context_symbol(lines, new_lines):
                    new_lines.append(f"PMH/PML: データ異常: {issue}")
                    replaced = True
                    break
                if "前日比" in issue and "前日比:" in line and symbol in _find_context_symbol(lines, new_lines):
                    new_lines.append(f"前日比: データ異常: {issue}")
                    replaced = True
                    break
            if replaced:
                break

        if not replaced:
            new_lines.append(line)

    return "\n".join(new_lines)


def _find_context_symbol(all_lines: List[str], processed_lines: List[str]) -> str:
    """直近の [SYMBOL] ヘッダーから現在のコンテキスト銘柄を特定する。"""
    for line in reversed(processed_lines):
        m = re.match(r'\[(\w+)\]', line)
        if m:
            return m.group(1)
    return ""


if __name__ == "__main__":
    # テスト用: サンプルデータでバリデーションを実行
    test_data = {
        "dxy": {
            "current_price": 104.5,
            "prev_close": 104.3,
            "change_pct": 0.19,
            "pdh": 104.6,
            "pdl": 104.2,
            "pwh": 105.0,
            "pwl": 103.8,
            "pmh": 106.0,
            "pml": 103.0,
        }
    }
    results = validate_all(test_data)
    print(f"\nバリデーション結果: {results}")

    # 異常データテスト
    test_bad = {
        "dxy": {
            "current_price": 0,
            "prev_close": 104.3,
            "change_pct": 5.0,
            "pdh": 104.2,
            "pdl": 104.6,  # 逆転
            "pwh": 104.5,
            "pwl": 104.5,  # レンジゼロ
        }
    }
    results_bad = validate_all(test_bad)
    print(f"\n異常データ結果: {results_bad}")
