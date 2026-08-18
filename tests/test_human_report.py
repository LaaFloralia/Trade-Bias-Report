"""human_report（認知負荷対策レンダラ）のパーサ / HTML 生成テスト。

実 API・Playwright は使わない（build_html までを検証、PDF 化は対象外）。
"""

from __future__ import annotations

import html as html_mod
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.human_report import (  # noqa: E402
    ReportData,
    _boxify,
    _decorate_tables,
    _normalize_md,
    _render_detail,
    build_html,
    parse_report,
    svg_price_ladder,
)

DAILY_MD = """# ICT Daily Bias Report — 2026-08-17
データ基準日: 2026-08-17 ｜ 生成: 08/17 18:05 JST ｜ データ充足: 18/18 ｜ XAU-TF: 08/17（0日前・有効）

## セクション0: エグゼクティブサマリー

信頼度: Med ｜ スコア 5 ｜ NO-TRADE: なし

- DXYバイアス: Bearish（99.320、前日比 -0.35%）
- 本日最大のリスク: Managed Money の混雑が巻き戻る反転リスク

## セクション1: 今夜の執行プラン

### 1-1. プランA（本命）

| 項目 | 内容 |
|---|---|
| 銘柄 | XAUUSD（固定） |
| 方向 | Long |
| 注目ゾーン | 4,345-4,366（XAU-TF H4 サポート帯） |
| Draw on Liquidity | BSL 4,415-4,450（PWH 4,449.78 手前、ERL） |
| 無効化レベル | 4,304 割れ |
| 狙う Kill Zone | NY (21:00-01:00 JST) |

### 1-3. 統一信頼度スコア内訳

| # | 項目 | 判定 | 点 |
|---|---|---|---|
| 1 | DXYバイアス整合 | 無相関化により 0 | 0 |
| 2 | リテール整合 | 中立 | 0 |
| 3 | XAU-TF構造整合 | 混在 | 0 |
| 4 | ファンダ大局整合 | 整合 | +1 |
| 5 | 週次アンカー整合 | 整合 | +1 |
| 6 | イベントリスク | なし | 0 |
| 7 | 相関レジーム乖離 | 3 ペア無相関化 | -1 |
| 8 | ETF・機関フロー整合 | 流入 | +1 |

**条件付きシナリオ（Low 時の代替記載）:**
- 4,370-4,391 まで押して回復した場合 → NY で Long を検討。
- 4,304 を割って戻せない場合 → NY で Short を検討。

## セクション3: ポジショニング

### 3-1. サマリーテーブル

| 銘柄 | 現在価格 | 前日比 | バイアス | BSL注目帯 | SSL注目帯 |
|---|---|---|---|---|---|
| XAUUSD | 4,394.98 | +0.440% | Bullish（弱） | 4,393.93 | 4,333.24-4,391.35 |
| DXY | 99.320 | -0.35% | Bearish | 取得不可 | 取得不可 |

### 3-2. リテールポジション & リテール分析

**損益構造:** Short 54.0%（836.29 lots / 6,574 positions、平均 4,143.51）が -6.04% の含み損。Long 46.0%（726.53 lots / 7,128 positions、平均 4,523.39）は -2.87% の含み損。

| 種別 | 価格帯 | volume | side内シェア | 距離 |
|---|---|---|---|---|
| BSL(上) | 4,393.93 – 4,393.93 | 8 | 100.0% | +0.01% |
| SSL(下) | 4,333.24 – 4,391.35 | 195 | 92.0% | -0.05% |

## セクション8: 前回照合 & 自己検証

### 8-1. 前回Daily照合

- 8/14 Daily は XAUUSD Long。Draw 帯の上端付近まで到達 → **当たり**

### 8-2. 自己検証

**セクション0 訂正の反映（確定値）:** 本レポートの確定スコアは **2**、確定信頼度は **Low**、**NO-TRADE: あり**。
"""

WEEKLY_CORRECTION_MD = """# ICT Weekly Bias Report — 2026-08-15
データ基準日: 2026-08-15 | 生成時刻: 2026-08-15 07:50 JST

## セクション0: エグゼクティブサマリー

1. 信頼度: Med-cautious（プラン1 統一スコア 4 点。内訳はセクション8）

## セクション9: 自己検証

**訂正後セクション0-1: 信頼度: Low（プラン1 統一スコア 0 点。プラン非提示、条件付きシナリオのみ）**
"""


def test_daily_corrected_verdict():
    data = parse_report(DAILY_MD)
    v = data.verdict
    assert v.corrected is True
    assert v.confidence == "Low"
    assert v.score == 2
    assert v.no_trade is True
    assert v.original_confidence == "Med"
    assert v.original_score == 5


def test_weekly_correction_pattern():
    data = parse_report(WEEKLY_CORRECTION_MD)
    v = data.verdict
    assert data.kind == "weekly"
    assert v.corrected is True
    assert v.confidence == "Low"
    assert v.score == 0
    assert v.no_trade is True


def test_plan_extraction():
    data = parse_report(DAILY_MD)
    assert len(data.plans) == 1
    p = data.plans[0]
    assert p.direction == "Long"
    assert "4,345-4,366" in p.zone
    assert data.invalidation_price == 4304
    assert data.draw_range == (4415, 4450)
    assert data.draw_kind == "BSL"


def test_score_items():
    data = parse_report(DAILY_MD)
    assert len(data.score_items) == 8
    assert sum(pt for _, _, _, pt in data.score_items) == 2


def test_zones_and_retail():
    data = parse_report(DAILY_MD)
    assert data.current_price == 4394.98
    kinds = {z.kind for z in data.zones}
    assert kinds == {"BSL", "SSL"}
    assert data.retail["short_pct"] == 54.0
    assert data.retail["long_pct"] == 46.0


def test_conditional_scenarios():
    data = parse_report(DAILY_MD)
    assert len(data.conditional_scenarios) == 2
    assert "4,370-4,391" in data.conditional_scenarios[0]


def test_review_verdict():
    data = parse_report(DAILY_MD)
    assert data.review_verdict == "当たり"


def test_ladder_svg():
    data = parse_report(DAILY_MD)
    svg = svg_price_ladder(data)
    assert svg is not None and svg.startswith("<svg")
    assert "現値" in svg and "無効化" in svg


def test_ladder_none_when_no_price():
    assert svg_price_ladder(ReportData()) is None


def test_build_html_end_to_end():
    html = build_html(DAILY_MD, style_css="/* test */")
    # ダッシュボード（確定値）と全文詳細の両方を含む
    assert "自己検証による確定訂正" in html
    assert "様子見" in html
    assert "詳細（全文）" in html
    assert "統一信頼度スコア内訳" in html
    # 生 MD のセクションも詳細側に残る
    assert "エグゼクティブサマリー" in html


def test_build_html_never_crashes_on_minimal_input():
    html = build_html("# 何もない Report\n\n本文のみ。", style_css="")
    assert "詳細（全文）" in html


# ---------------------------------------------------------------------------
# 詳細（全文）レンダリング — 「情報を落とさない」ことの機械検証
# ---------------------------------------------------------------------------

# 区切り記号は「構造」であってレポートの内容ではない。表のパイプや
# ラベルのコロンはレイアウト側（列・タグ）が担うため、両辺から落として比較する。
_SEPARATORS = re.compile(r"[|:：\s]+")


def _md_content_lines(md: str) -> list[str]:
    """MD から構文記号を落とし、内容だけの行に正規化する。"""
    lines: list[str] = []
    for raw in md.splitlines():
        ln = raw.strip()
        if not ln or re.fullmatch(r"[-*_|:\s]+", ln):  # 空行 / 罫線 / 表の区切り行
            continue
        ln = re.sub(r"^#{1,6}\s*", "", ln)
        ln = re.sub(r"^(?:[-*+]|\d+\.)\s+", "", ln)
        ln = ln.replace("*", "").replace("`", "")
        ln = _SEPARATORS.sub("", ln)
        if len(ln) >= 4:
            lines.append(ln)
    return lines


def _html_text(html: str) -> str:
    """本文テキストだけを取り出す。装飾記号（<span class="mark">▲</span>）は除く。"""
    body = re.sub(r'<span class="mark">.*?</span>', " ", html, flags=re.DOTALL)
    return _SEPARATORS.sub("", html_mod.unescape(re.sub(r"<[^>]+>", " ", body)))


def _assert_no_information_lost(md: str, detail_html: str) -> None:
    text = _html_text(detail_html)
    missing = [ln for ln in _md_content_lines(md) if ln not in text]
    assert not missing, f"詳細から欠落した内容: {missing[:3]}"


def test_detail_preserves_every_source_line():
    """視覚化のための変換で本文が 1 行も失われないこと（最重要の契約）。"""
    _assert_no_information_lost(DAILY_MD, _render_detail(DAILY_MD))
    _assert_no_information_lost(WEEKLY_CORRECTION_MD, _render_detail(WEEKLY_CORRECTION_MD))


def test_detail_preserves_fixture_report():
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "sample_report.md"
    if not fixture.exists():
        return
    md = fixture.read_text(encoding="utf-8")
    _assert_no_information_lost(md, _render_detail(md))


def test_normalize_md_rescues_list_after_bold_label():
    """空行なしで `**ラベル:**` の直後に続く箇条書きがハイフンのまま出ないこと。"""
    md = "**条件付きシナリオ:**\n- 4,370 まで押した場合\n- 4,304 を割った場合\n"
    assert "\n\n- 4,370" in _normalize_md(md)
    html = _render_detail(f"# t\n\n## セクション1: x\n\n{md}")
    assert "<li>" in html and "- 4,370" not in _html_text(html)


def test_labeled_paragraph_becomes_box():
    html = _boxify("<p><strong>損益構造:</strong> Short 54.0% が含み損。</p>")
    assert 'class="labeled"' in html
    # ラベルは区切り記号ごと原文のまま、本文も原文のまま
    assert "損益構造:" in html and "Short 54.0% が含み損。" in html


def test_all_bold_paragraph_becomes_statement():
    html = _boxify("<p><strong>本日は高確度のセットアップなし。</strong></p>")
    assert 'class="statement"' in html
    assert "本日は高確度のセットアップなし。" in html


def test_plain_bold_lead_without_colon_is_untouched():
    src = "<p><strong>強調</strong> のあとに続く文章。</p>"
    assert _boxify(src) == src


def test_table_cells_get_badges_and_alignment():
    html = _decorate_tables(
        "<table>\n<thead>\n<tr>\n<th>項目</th>\n<th>判定</th>\n<th>点</th>\n</tr>\n</thead>\n"
        "<tbody>\n<tr>\n<td>DXY</td>\n<td>Bearish</td>\n<td>-1</td>\n</tr>\n</tbody>\n</table>"
    )
    assert 'class="cell-badge"' in html and "▼" in html and "Bearish" in html
    assert 'class="score-chip"' in html and "-1" in html
    assert "DXY" in html


def test_correlation_cell_gets_mini_bar():
    html = _decorate_tables(
        "<table>\n<thead>\n<tr>\n<th>ペア</th>\n<th>20日 r</th>\n</tr>\n</thead>\n"
        "<tbody>\n<tr>\n<td>XAU vs DXY</td>\n<td>-0.23</td>\n</tr>\n</tbody>\n</table>"
    )
    assert "mini-bar" in html and "-0.23" in html


def test_wide_table_gets_smaller_class():
    header = "".join(f"<th>c{i}</th>" for i in range(8))
    row = "".join(f"<td>v{i}</td>" for i in range(8))
    html = _decorate_tables(
        f"<table>\n<thead>\n<tr>{header}</tr>\n</thead>\n<tbody>\n<tr>{row}</tr>\n</tbody>\n</table>"
    )
    assert 'class="wide-table"' in html


def test_section_gets_kicker_and_index():
    html = _render_detail(DAILY_MD)
    # 「セクション3: ポジショニング」→ キッカー + タイトルに分割しても語は残る
    assert 'class="sec-kicker"' in html
    assert "セクション3" in html and "ポジショニング" in html
    assert 'class="sec-index"' in html
