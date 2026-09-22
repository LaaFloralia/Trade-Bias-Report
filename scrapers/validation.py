"""データバリデーション モジュール

format_scraped_data() でClaudeにデータを渡す直前に実行するバリデーション処理。
失敗したデータは「データ異常: [理由]」に置き換える。
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _to_finite_number(value: Any) -> Optional[float]:
    """API由来の数値文字列を許容し、bool・NaN・無限大を拒否する。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return converted if math.isfinite(converted) else None


def _check_zero_null_negative(value: Any, field_name: str) -> Optional[str]:
    """B-3: 価格が有限数かつ正数であることを確認する。"""
    if value is None:
        return f"{field_name}がNull"
    converted = _to_finite_number(value)
    if converted is None:
        return f"{field_name}が数値不正 ({value!r})"
    if converted <= 0:
        return f"{field_name}がゼロまたは負数 ({converted})"
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
    current_raw = data["current_price"] if "current_price" in data else data.get("close")
    issue = _check_zero_null_negative(current_raw, "現在価格")
    if issue:
        issues.append(issue)

    prev_close_raw = data["prev_close"] if "prev_close" in data else data.get("previous_close")
    issue = _check_zero_null_negative(prev_close_raw, "前日終値")
    if issue:
        issues.append(issue)

    # --- B-2: 前日比の異常値チェック ---
    change_raw = data["change_pct"] if "change_pct" in data else data.get("percent_change")
    change_pct = _to_finite_number(change_raw)
    if change_raw is not None and change_pct is None:
        issues.append(f"前日比が数値不正 ({change_raw!r})")

    # Twelve Data の変化額は負値が正常なので、有限数かだけを検証する。
    change_amount = data.get("change")
    if change_amount is not None and _to_finite_number(change_amount) is None:
        issues.append(f"変化額が数値不正 ({change_amount!r})")

    # 当日 OHLC は正の有限数で、始値・終値を日中レンジが包含する必要がある。
    ohlc = {}
    for key, label in (("open", "当日始値"), ("high", "当日高値"), ("low", "当日安値")):
        raw_value = data.get(key)
        if raw_value is None:
            continue
        issue = _check_zero_null_negative(raw_value, label)
        if issue:
            issues.append(issue)
            continue
        ohlc[key] = _to_finite_number(raw_value)
    current_number = _to_finite_number(current_raw)
    if all(key in ohlc for key in ("open", "high", "low")) and current_number is not None:
        if ohlc["high"] < max(ohlc["open"], current_number):
            issues.append("当日OHLC整合性不正: 高値が始値または終値を下回る")
        if ohlc["low"] > min(ohlc["open"], current_number):
            issues.append("当日OHLC整合性不正: 安値が始値または終値を上回る")
        if ohlc["high"] < ohlc["low"]:
            issues.append("当日OHLC整合性不正: 高値が安値を下回る")

    if change_pct is not None and symbol in CHANGE_PCT_THRESHOLDS:
        threshold = CHANGE_PCT_THRESHOLDS[symbol]
        if abs(change_pct) > threshold:
            issues.append(f"前日比 {change_pct:+.2f}% が閾値 ±{threshold}% を超過")

    # --- B-1: PDH/PDL/PWH/PWL の最小レンジチェック ---
    threshold = MIN_RANGE_THRESHOLDS.get(symbol, 0)
    for h_key, l_key, label in [("pdh", "pdl", "PDH/PDL"), ("pwh", "pwl", "PWH/PWL"), ("pmh", "pml", "PMH/PML")]:
        h = data.get(h_key)
        l = data.get(l_key)

        h_number = _to_finite_number(h)
        l_number = _to_finite_number(l)

        if h is not None and h_number is None:
            issues.append(f"{label.split('/')[0]}が数値不正 ({h!r})")
        if l is not None and l_number is None:
            issues.append(f"{label.split('/')[1]}が数値不正 ({l!r})")

        if h_number is not None and l_number is not None:
            h, l = h_number, l_number

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
        if h_number is not None:
            issue = _check_zero_null_negative(h_number, f"{label.split('/')[0]}")
            if issue:
                issues.append(issue)
        if l_number is not None:
            issue = _check_zero_null_negative(l_number, f"{label.split('/')[1]}")
            if issue:
                issues.append(issue)

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

    if not isinstance(quote, dict):
        return ["現在価格がNull", "前日終値がNull"]

    # quoteデータの変換
    data = {
        "current_price": quote.get("close"),
        "prev_close": quote.get("previous_close"),
        "change_pct": quote.get("percent_change"),
        "change": quote.get("change"),
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
    }

    def _raw_extreme(rows: List[dict], key: str, *, maximum: bool) -> Any:
        """集計前に全要素を検証し、不正値はそのまま異常判定へ渡す。"""
        values: List[float] = []
        for row in rows:
            if not isinstance(row, dict) or key not in row:
                return "<missing>"
            raw_value = row[key]
            number = _to_finite_number(raw_value)
            if number is None:
                return raw_value
            values.append(number)
        if not values:
            return "<missing>"
        return max(values) if maximum else min(values)

    # seriesからPDH/PDL等を抽出。不正値をfloatへ先変換して隠さない。
    if len(series) >= 2:
        previous = series[1]
        if isinstance(previous, dict):
            data["pdh"] = previous.get("high", "<missing>")
            data["pdl"] = previous.get("low", "<missing>")

    if len(series) >= 6:
        week_data = series[1:6]
        data["pwh"] = _raw_extreme(week_data, "high", maximum=True)
        data["pwl"] = _raw_extreme(week_data, "low", maximum=False)

    if len(series) >= 23:
        month_data = series[1:23]
        data["pmh"] = _raw_extreme(month_data, "high", maximum=True)
        data["pml"] = _raw_extreme(month_data, "low", maximum=False)

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
    new_lines: List[str] = []
    skip_price_section = False

    fatal_by_symbol = {
        symbol: [
            issue for issue in issues
            if issue.startswith((
                "現在価格",
                "前日終値",
                "変化額",
                "当日始値",
                "当日高値",
                "当日安値",
                "当日OHLC",
            ))
        ]
        for symbol, issues in validation_results.items()
    }

    for line in lines:
        if skip_price_section and (
            line.startswith("[") or line.startswith("### ") or line.startswith("=== ")
        ):
            skip_price_section = False

        header = re.match(r"^\[([A-Za-z0-9_]+)(?:[^]]*)\]$", line)
        if header:
            skip_price_section = False
            symbol = header.group(1)
            fatal_issues = fatal_by_symbol.get(symbol) or []
            if fatal_issues:
                new_lines.append(line)
                safe_issues = [issue.split(" (", 1)[0] for issue in fatal_issues]
                new_lines.append(
                    "価格セクション除外（データ異常: " + " / ".join(safe_issues) + "）"
                )
                skip_price_section = True
                continue

        if skip_price_section:
            continue

        replaced = False
        context_symbol = _find_context_symbol(lines, new_lines)
        for symbol, issues in validation_results.items():
            if symbol != context_symbol:
                continue
            for issue in issues:
                if ("PDH/PDL" in issue or issue.startswith(("PDH", "PDL"))) and "PDH:" in line:
                    new_lines.append(f"PDH/PDL: データ異常: {issue}")
                    replaced = True
                    break
                if ("PWH/PWL" in issue or issue.startswith(("PWH", "PWL"))) and "PWH:" in line:
                    new_lines.append(f"PWH/PWL: データ異常: {issue}")
                    replaced = True
                    break
                if ("PMH/PML" in issue or issue.startswith(("PMH", "PML"))) and "PMH:" in line:
                    new_lines.append(f"PMH/PML: データ異常: {issue}")
                    replaced = True
                    break
                if "前日比" in issue and "前日比:" in line:
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
        m = re.match(r'^\[([A-Za-z0-9_]+)', line)
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
