"""Bias Report MD → 人間向けダッシュボード HTML（認知負荷対策レンダラ）。

設計方針:
- Markdown は「AI が読む正本」。本モジュールはそこから構造を抽出し、
  人間が 30 秒で全体像を掴めるダッシュボード（1〜2 ページ）+ 全文詳細、
  という 3 層構造（結論 → 根拠 → 全文）の HTML を組み立てる。
- 抽出は全て Optional。LLM 生成の揺れでどれかのパターンが取れなくても
  そのウィジェットを描かないだけで、レンダリング全体は決して失敗させない。
  全文詳細は常にレンダリングされるため情報の欠落はない。
- 図は外部ライブラリなしのインライン SVG（Playwright print で確実に出る）。
- 配色は色覚多様性検証済み（dataviz validate_palette.js 全項目 PASS）:
    Bullish/Long  #047857 (+▲) / Bearish/Short #c22f2f (+▼)
    注意/様子見    #d97706 (+◆) / ブランド青    #2547a8
  方向は常に記号・ラベルを併記し、色単独に意味を持たせない。

入力: Daily / Weekly Bias Report の Markdown テキスト
出力: build_html(md_text) -> 完全な HTML 文字列
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import markdown

# ---------------------------------------------------------------------------
# デザイントークン
# ---------------------------------------------------------------------------

C_BULL = "#047857"       # Bullish / Long / 整合(+1)
C_BULL_BG = "#e7f6ef"
C_BEAR = "#c22f2f"       # Bearish / Short / 逆行(-1)
C_BEAR_BG = "#fdeaea"
C_WARN = "#d97706"       # 注意 / 様子見 / Med 系
C_WARN_BG = "#fdf3e3"
C_BRAND = "#2547a8"      # 構造・ブランド
C_BRAND_BG = "#edf1fa"
C_INK = "#1c1c1e"
C_INK2 = "#55555c"
C_MUTED = "#8b8b92"
C_LINE = "#e4e4e8"
C_NEUTRAL_BG = "#f1f1f4"

CONFIDENCE_COLORS = {
    "High": (C_BULL, C_BULL_BG),
    "Med": (C_WARN, C_WARN_BG),
    "Med-cautious": ("#c2410c", "#fdeee3"),
    "Low": (C_BEAR, C_BEAR_BG),
}

# 統一信頼度スコアの 8 項目（番号 → 短縮ラベル）
SCORE_LABELS = {
    1: "DXYバイアス整合",
    2: "リテール逆張り整合",
    3: "XAU-TF構造整合",
    4: "ファンダ大局整合",
    5: "週次アンカー整合",
    6: "イベントリスク",
    7: "相関レジーム",
    8: "ETF・機関フロー",
}


def esc(s: str) -> str:
    return html_mod.escape(str(s), quote=True)


def _trim(s: Optional[str], n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _num(s: str) -> Optional[float]:
    """'4,394.98' / '+0.44%' → float。取れなければ None。"""
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(s))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _fmt_price(v: float) -> str:
    if v >= 1000:
        return f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"
    return f"{v:,.2f}"


# ---------------------------------------------------------------------------
# Markdown 構造パース
# ---------------------------------------------------------------------------

@dataclass
class Section:
    level: int
    title: str
    body: str  # 見出し行を除いた本文（サブセクション含む）


def split_sections(md_text: str) -> list[Section]:
    """`## ` / `### ` 見出しでフラットに分割する（コードフェンス内は無視）。"""
    sections: list[Section] = []
    cur_title, cur_level, cur_lines = "", 0, []
    in_fence = False
    for ln in md_text.splitlines():
        if ln.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            if cur_title or cur_lines:
                sections.append(Section(cur_level, cur_title, "\n".join(cur_lines)))
            cur_level, cur_title, cur_lines = len(m.group(1)), m.group(2).strip(), []
        else:
            cur_lines.append(ln)
    if cur_title or cur_lines:
        sections.append(Section(cur_level, cur_title, "\n".join(cur_lines)))
    return sections


def find_section(sections: list[Section], pattern: str) -> Optional[Section]:
    for s in sections:
        if re.search(pattern, s.title):
            return s
    return None


def find_sections(sections: list[Section], pattern: str) -> list[Section]:
    """パターンに一致する全セクション。親セクション（本文が空）に先にマッチして
    子セクションの表を取り逃がすのを防ぐため、呼び出し側は全件を走査する。"""
    return [s for s in sections if re.search(pattern, s.title)]


def parse_tables(text: str) -> list[list[list[str]]]:
    """テキスト中の Markdown テーブルを全て [rows[cells]] で返す（区切り行除去）。"""
    tables: list[list[list[str]]] = []
    cur: list[list[str]] = []
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue  # 区切り行
            cur.append(cells)
        else:
            if len(cur) >= 2:
                tables.append(cur)
            cur = []
    if len(cur) >= 2:
        tables.append(cur)
    return tables


# ---------------------------------------------------------------------------
# 抽出（すべて Optional / 欠損許容）
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    confidence: Optional[str] = None   # High / Med / Med-cautious / Low
    score: Optional[int] = None
    no_trade: Optional[bool] = None
    corrected: bool = False            # 自己検証による訂正が適用されたか
    original_confidence: Optional[str] = None
    original_score: Optional[int] = None
    original_no_trade: Optional[bool] = None


@dataclass
class Zone:
    kind: str                # "BSL" / "SSL"
    low: float
    high: float
    volume: Optional[float] = None
    share: Optional[str] = None


@dataclass
class PlanCard:
    name: str = ""           # プランA（本命）等
    symbol: Optional[str] = None
    direction: Optional[str] = None   # Long / Short
    zone: Optional[str] = None
    draw: Optional[str] = None
    invalidation: Optional[str] = None
    killzone: Optional[str] = None
    po3: Optional[str] = None


@dataclass
class ReportData:
    kind: str = "daily"                    # daily / weekly
    title: str = ""
    date: str = ""
    meta_chips: list[str] = field(default_factory=list)
    verdict: Verdict = field(default_factory=Verdict)
    s0_bullets: list[str] = field(default_factory=list)
    plans: list[PlanCard] = field(default_factory=list)
    conditional_scenarios: list[str] = field(default_factory=list)
    score_items: list[tuple[int, str, str, int]] = field(default_factory=list)
    positioning_rows: list[dict] = field(default_factory=list)
    retail: dict = field(default_factory=dict)
    zones: list[Zone] = field(default_factory=list)
    current_price: Optional[float] = None
    price_change: Optional[str] = None
    pwh: Optional[float] = None
    pwl: Optional[float] = None
    focus_ranges: list[tuple[float, float]] = field(default_factory=list)
    draw_range: Optional[tuple[float, float]] = None
    draw_kind: Optional[str] = None
    invalidation_price: Optional[float] = None
    events: list[list[str]] = field(default_factory=list)
    week_ahead: Optional[str] = None
    fedwatch: dict = field(default_factory=dict)
    vix_line: Optional[str] = None
    wait_note: Optional[str] = None
    risk_note: Optional[str] = None
    correlations: list[tuple[str, str, str, str]] = field(default_factory=list)
    fundamentals: list[dict] = field(default_factory=list)
    review_verdict: Optional[str] = None
    review_note: Optional[str] = None
    dxy_bias: Optional[str] = None
    dxy_note: Optional[str] = None


CONF_PAT = r"(High|Med-cautious|Med|Low)"


def extract_header(md_text: str, data: ReportData) -> None:
    lines = md_text.splitlines()
    for ln in lines:
        if ln.startswith("# "):
            data.title = ln[2:].strip()
            break
    if "Weekly" in data.title:
        data.kind = "weekly"
    m = re.search(r"—\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", data.title)
    if m:
        data.date = m.group(1)
    # タイトル直後のメタ行（｜ 区切り）
    for ln in lines[1:6]:
        if ("データ基準日" in ln or "生成" in ln) and ("|" in ln or "｜" in ln):
            data.meta_chips = [c.strip() for c in re.split(r"[|｜]", ln) if c.strip()]
            break


def extract_verdict(md_text: str, sections: list[Section], data: ReportData) -> None:
    v = data.verdict
    s0 = find_section(sections, r"セクション0|エグゼクティブサマリー")
    s0_text = s0.body if s0 else md_text[:2000]
    m = re.search(rf"信頼度[:：]\s*\**{CONF_PAT}", s0_text)
    if m:
        v.confidence = m.group(1)
    m = re.search(r"スコア\s*(-?\d+)", s0_text)
    if m:
        v.score = int(m.group(1))
    m = re.search(r"NO-TRADE[:：]\s*\**(あり|なし)", s0_text)
    if m:
        v.no_trade = m.group(1) == "あり"

    # 自己検証セクションの確定訂正（あればこちらが正）
    m = re.search(rf"確定信頼度は?\s*\**{CONF_PAT}", md_text)
    conf_fix = m.group(1) if m else None
    m = re.search(r"確定スコアは?\s*\**(-?\d+)", md_text)
    score_fix = int(m.group(1)) if m else None
    m = re.search(r"確定.{0,20}?NO-TRADE[:：]\s*\**(あり|なし)", md_text)
    nt_fix = (m.group(1) == "あり") if m else None
    # Weekly 形式の訂正: 「訂正後セクション0-1: 信頼度: Low（... スコア 0 点 ... プラン非提示 ...）」
    m = re.search(
        rf"訂正後セクション0[^\n]*?信頼度[:：]\s*\**{CONF_PAT}([^\n]*)", md_text)
    if m:
        conf_fix = conf_fix or m.group(1)
        rest = m.group(2)
        m2 = re.search(r"スコア\s*(-?\d+)\s*点", rest)
        if score_fix is None and m2:
            score_fix = int(m2.group(1))
        if nt_fix is None and "プラン非提示" in rest:
            nt_fix = True
    if any(x is not None for x in (conf_fix, score_fix, nt_fix)):
        changed = (
            (conf_fix is not None and conf_fix != v.confidence)
            or (score_fix is not None and score_fix != v.score)
            or (nt_fix is not None and nt_fix != v.no_trade)
        )
        if changed:
            v.corrected = True
            v.original_confidence, v.original_score, v.original_no_trade = (
                v.confidence, v.score, v.no_trade,
            )
        v.confidence = conf_fix or v.confidence
        v.score = score_fix if score_fix is not None else v.score
        v.no_trade = nt_fix if nt_fix is not None else v.no_trade

    # S0 箇条書き（- または 1. 形式）
    if s0:
        for ln in s0.body.splitlines():
            m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)$", ln)
            if m and m.group(1).strip():
                data.s0_bullets.append(m.group(1).strip())


def extract_plans(sections: list[Section], data: ReportData) -> None:
    for s in sections:
        if not re.search(r"プラン[AB12１２]|プラン[０-９]", s.title):
            continue
        card = PlanCard(name=re.sub(r"^[\d\-\.]+\s*", "", s.title))
        for tbl in parse_tables(s.body):
            for row in tbl:
                if len(row) < 2:
                    continue
                key, val = row[0], row[1]
                if "銘柄" in key:
                    card.symbol = val
                elif key.strip() == "方向":
                    card.direction = val
                elif "注目ゾーン" in key:
                    card.zone = val
                elif "Draw" in key:
                    card.draw = val
                elif "無効化" in key:
                    card.invalidation = val
                elif "Kill" in key or "KZ" in key:
                    card.killzone = val
                elif "PO3" in key:
                    card.po3 = val
        # テーブルなし（箇条書き形式のプランB）
        if card.direction is None:
            for ln in s.body.splitlines():
                for attr, pat in (
                    ("direction", r"方向[:：]\s*(.+)"),
                    ("zone", r"注目ゾーン[:：]\s*(.+)"),
                    ("draw", r"Draw on Liquidity[:：]\s*(.+)"),
                    ("invalidation", r"無効化レベル?[:：]\s*(.+)"),
                    ("killzone", r"狙う\s*KZ[:：]\s*(.+)"),
                ):
                    m = re.search(pat, ln)
                    if m and getattr(card, attr) is None:
                        setattr(card, attr, m.group(1).strip())
        if card.direction or card.zone:
            data.plans.append(card)

    # 条件付きシナリオ:
    #   1. 「（Low 時の代替記載）」付きの箇条書き（Daily の NO-TRADE 形式）を最優先
    #   2. 「条件付きシナリオ...:」見出し直後の箇条書き（Weekly 形式）
    #   3. 1 行インライン形式（数値を含む実質的な内容のみ採用）
    for pat in (
        r"条件付きシナリオ（Low 時の代替記載）[^\n]*\n((?:\s*[-*].*\n?)+)",
        r"条件付きシナリオ[^\n]*[:：]\s*\n+((?:\s*[-*].*\n?)+)",
    ):
        for s in sections:
            m = re.search(pat, s.body)
            if m:
                data.conditional_scenarios = [
                    re.sub(r"^\s*[-*]\s*", "", ln).strip()
                    for ln in m.group(1).splitlines() if ln.strip()
                ]
                return
    for s in sections:
        m = re.search(r"\**条件付きシナリオ[:：]\**\s*(.+)", s.body)
        if m:
            text = m.group(1).strip()
            if len(text) >= 25 and re.search(r"\d", text):
                data.conditional_scenarios = [text]
                return


def extract_scores(sections: list[Section], data: ReportData) -> None:
    """統一スコア 8 項目表（ヘッダに # と 点 を含む）を最初に見つけた場所から取る。
    Daily は「1-3. 統一信頼度スコア内訳」、Weekly は「8-1. プラン1」内にある。"""
    # スコア内訳系のタイトルを優先し、なければ全セクション走査
    candidates = find_sections(sections, r"スコア内訳|スコア再計算") + sections
    for s in candidates:
        for tbl in parse_tables(s.body):
            header = tbl[0]
            if not (any("点" in c for c in header) and any("#" in c for c in header)):
                continue
            items: list[tuple[int, str, str, int]] = []
            for row in tbl[1:]:
                if len(row) < 4:
                    continue
                n = _num(row[0])
                pt = _num(row[-1])
                if n is None or pt is None or "合計" in row[0]:
                    continue
                num = int(n)
                label = SCORE_LABELS.get(num, row[1][:12])
                items.append((num, label, row[-2] if len(row) >= 4 else "", int(pt)))
            if len(items) >= 4:
                data.score_items = items
                return


def extract_positioning(sections: list[Section], data: ReportData) -> None:
    for s in sections:
        for tbl in parse_tables(s.body):
            header = tbl[0]
            if not any(("現在価格" in c or "現在値" in c) for c in header):
                continue
            idx = {c: i for i, c in enumerate(header)}
            for row in tbl[1:]:
                if len(row) < 3:
                    continue
                d = {header[i]: row[i] for i in range(min(len(header), len(row)))}
                d["_symbol"] = row[0]
                data.positioning_rows.append(d)
            break
        if data.positioning_rows:
            break
    for row in data.positioning_rows:
        if "XAU" in row.get("_symbol", ""):
            for k, val in row.items():
                if "現在" in k:
                    data.current_price = _num(val)
                if ("前日比" in k or "変動" in k) and "要因" not in k:
                    data.price_change = _trim(re.sub(r"[（(].*", "", val).strip(), 14)
        if "DXY" in row.get("_symbol", ""):
            for k, val in row.items():
                if "バイアス" in k:
                    data.dxy_bias = val


def extract_retail(md_text: str, sections: list[Section], data: ReportData) -> None:
    s = find_section(sections, r"リテールポジション")
    if s is None:
        return
    r: dict = {}
    m = re.search(
        r"Short\s*([\d.]+)%（([\d,\.]+)\s*lots[^）]*平均\s*([\d,\.]+)）\s*が\s*([+\-‐−][\d.]+)%",
        s.body,
    )
    if m:
        r["short_pct"], r["short_lots"], r["short_avg"], r["short_pnl"] = (
            float(m.group(1)), m.group(2), m.group(3), m.group(4))
    m = re.search(
        r"Long\s*([\d.]+)%（([\d,\.]+)\s*lots[^）]*平均\s*([\d,\.]+)）\s*は?\s*([+\-‐−][\d.]+)%",
        s.body,
    )
    if m:
        r["long_pct"], r["long_lots"], r["long_avg"], r["long_pnl"] = (
            float(m.group(1)), m.group(2), m.group(3), m.group(4))
    data.retail = r
    # BSL/SSL ゾーンテーブル
    for tbl in parse_tables(s.body):
        header = tbl[0]
        if not any("価格帯" in c for c in header):
            continue
        for row in tbl[1:]:
            if len(row) < 2:
                continue
            kind = "BSL" if "BSL" in row[0] else ("SSL" if "SSL" in row[0] else None)
            if kind is None:
                continue
            rng = re.findall(r"\d[\d,]{2,}(?:\.\d+)?", row[1])
            if not rng:
                continue
            vals = [_num(x) for x in rng]
            vals = [v for v in vals if v is not None]
            if not vals:
                continue
            vol = _num(row[2]) if len(row) > 2 else None
            share = row[3] if len(row) > 3 else None
            data.zones.append(Zone(kind, min(vals), max(vals), vol, share))
        break


def _extract_range(text: str) -> Optional[tuple[float, float]]:
    m = re.search(r"(\d[\d,]{2,}(?:\.\d+)?)\s*[-–〜]\s*(\d[\d,]{2,}(?:\.\d+)?)", text)
    if not m:
        v = _num(text)
        return (v, v) if v is not None else None
    a, b = _num(m.group(1)), _num(m.group(2))
    if a is None or b is None:
        return None
    return (min(a, b), max(a, b))


def extract_levels(md_text: str, data: ReportData) -> None:
    """価格ラダー用のレベル（Draw / 無効化 / 注目ゾーン / PWH / PWL）を抽出。"""
    plan = data.plans[0] if data.plans else None
    if plan:
        if plan.draw:
            data.draw_range = _extract_range(plan.draw)
            data.draw_kind = "BSL" if "BSL" in plan.draw else ("SSL" if "SSL" in plan.draw else None)
        if plan.invalidation:
            data.invalidation_price = _num(plan.invalidation)
        if plan.zone:
            for m in re.finditer(r"(\d[\d,]{2,}(?:\.\d+)?)\s*[-–〜]\s*(\d[\d,]{2,}(?:\.\d+)?)", plan.zone):
                a, b = _num(m.group(1)), _num(m.group(2))
                if a is not None and b is not None:
                    data.focus_ranges.append((min(a, b), max(a, b)))
    m = re.search(r"PWH\s*(\d[\d,]*\.?\d*)", md_text)
    if m:
        data.pwh = _num(m.group(1))
    m = re.search(r"PWL\s*(\d[\d,]*\.?\d*)", md_text)
    if m:
        data.pwl = _num(m.group(1))
    if data.current_price is None:
        m = re.search(r"現値\s*(\d[\d,]*\.\d+)", md_text)
        if m:
            data.current_price = _num(m.group(1))


def extract_events(sections: list[Section], data: ReportData) -> None:
    for s in find_sections(sections, r"Killzone重複|ハイインパクト指標|イベント"):
        for tbl in parse_tables(s.body):
            if any("指標" in c for c in tbl[0]):
                data.events = tbl
                break
        if data.events:
            break
    for sec in sections:
        m = re.search(r"今週残り[:：]\s*(.+)", sec.body)
        if m:
            data.week_ahead = m.group(1).strip()
            break


def extract_fedwatch(sections: list[Section], data: ReportData) -> None:
    s = find_section(sections, r"FedWatch")
    if s is None:
        return
    fw: dict = {}
    m = re.search(r"次回FOMC日?（あと\s*(\d+)\s*日）?\s*\|?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", s.body)
    if m:
        fw["days"], fw["date"] = m.group(1), m.group(2)
    else:
        m = re.search(r"([0-9]{4}-[0-9]{2}-[0-9]{2})", s.body)
        if m:
            fw["date"] = m.group(1)
        m = re.search(r"あと\s*(\d+)\s*日", s.body)
        if m:
            fw["days"] = m.group(1)
    probs = re.findall(r"(\d\.\d{2}-\d\.\d{2})\s*(\d{1,2}(?:\.\d)?)%", s.body)
    if probs:
        fw["probs"] = [(rng, float(p)) for rng, p in probs[: 4]]
    data.fedwatch = fw


def extract_vix(sections: list[Section], data: ReportData) -> None:
    for s in sections:
        m = re.search(r"VIX\s*[\d.]+.*", s.body)
        if m and ("ボラ" in s.title or "リスクレジーム" in s.title or "セッション統計" in s.title):
            data.vix_line = re.sub(r"\*+", "", m.group(0).strip())
            break
    s = find_section(sections, r"ボラ環境|セッション統計")
    if s:
        for ln in s.body.splitlines():
            if "待" in ln and ("追いかけ" in ln or "初動" in ln):
                data.wait_note = ln.strip()
                break


def extract_notes(md_text: str, sections: list[Section], data: ReportData) -> None:
    for b in data.s0_bullets:
        if "リスク" in b and data.risk_note is None:
            note = re.sub(r"^(本日|来週)?最大のリスク[:：]\s*", "", b)
            data.risk_note = re.sub(r"\*+", "", note)
    for s in sections:
        m = re.search(r"\**待ちの妥当性[:：]?\**\s*(.+)", s.body)
        if m:
            data.wait_note = m.group(1).strip()
            break


def extract_correlations(sections: list[Section], data: ReportData) -> None:
    s = find_section(sections, r"相関レジーム|相関チェック")
    if s is None:
        return
    for tbl in parse_tables(s.body):
        header = tbl[0]
        if not any("20" in c and "r" in c.lower() for c in header):
            continue
        for row in tbl[1:]:
            if len(row) >= 4:
                pair = re.sub(r"XAUUSD\s*vs\s*", "", row[0])
                data.correlations.append((pair, row[1], row[2], row[3]))
        break


def extract_fundamentals(sections: list[Section], data: ReportData) -> None:
    s = find_section(sections, r"ファンダメンタル大局|ファンダ大局バイアステーブル")
    if s is None:
        return
    for tbl in parse_tables(s.body):
        header = tbl[0]
        if not any("大局" in c or "バイアス" in c for c in header):
            continue
        for row in tbl[1:]:
            if len(row) >= 3:
                data.fundamentals.append({
                    "symbol": row[0], "bias": row[1],
                    "driver": row[2], "conf": row[3] if len(row) > 3 else "",
                })
        break


def extract_review(sections: list[Section], data: ReportData) -> None:
    for s in find_sections(sections, r"前回Daily照合|前回バイアス vs 実際|前回照合"):
        m = re.search(r"→\s*\**(当たり|外れ|部分的中|未決着|一部的中)\**", s.body)
        if not m:
            continue
        data.review_verdict = m.group(1)
        for ln in s.body.splitlines():
            ln = ln.strip().lstrip("-* ")
            if ln and ("前回" in ln or "8/" in ln or "Draw" in ln):
                data.review_note = re.sub(r"\*+", "", ln)[:150]
                break
        return


def extract_dxy(sections: list[Section], data: ReportData) -> None:
    for s in find_sections(sections, r"DXY"):
        m = re.search(r"バイアスは\s*\**(Bullish|Bearish|Neutral)\**", s.body)
        if m:
            data.dxy_bias = data.dxy_bias or m.group(1)
        m = re.search(r"XAUUSD への影響[:：]\s*(.+)", s.body)
        if m:
            data.dxy_note = data.dxy_note or m.group(1).strip()[:180]
    if data.dxy_bias is None:
        # S0 箇条書きからのフォールバック（Weekly: 「DXY 週次バイアス: Bearish — ...」）
        for b in data.s0_bullets:
            m = re.search(r"DXY[^:：]*バイアス[:：]\s*\**(Bullish|Bearish|Neutral)", b)
            if m:
                data.dxy_bias = m.group(1)
                break


def parse_report(md_text: str) -> ReportData:
    data = ReportData()
    sections = split_sections(md_text)
    extract_header(md_text, data)
    extract_verdict(md_text, sections, data)
    extract_plans(sections, data)
    extract_scores(sections, data)
    extract_positioning(sections, data)
    extract_retail(md_text, sections, data)
    extract_levels(md_text, data)
    extract_events(sections, data)
    extract_fedwatch(sections, data)
    extract_vix(sections, data)
    extract_notes(md_text, sections, data)
    extract_correlations(sections, data)
    extract_fundamentals(sections, data)
    extract_review(sections, data)
    extract_dxy(sections, data)
    return data


# ---------------------------------------------------------------------------
# SVG 図
# ---------------------------------------------------------------------------

def _nice_step(rng: float) -> float:
    for step in (10, 20, 25, 50, 100, 200, 500):
        if rng / step <= 8:
            return step
    return 500


def _fmt_int(v: float) -> str:
    return f"{v:,.0f}"


def _fmt_share(share: Optional[str]) -> str:
    if not share:
        return ""
    s = share.replace(".0%", "%")
    return f"（{s}）"


def svg_price_ladder(data: ReportData, width: int = 320, height: int = 400) -> Optional[str]:
    """価格レベルマップ: 現値・BSL/SSL・注目ゾーン・Draw・無効化を縦軸で図解。"""
    if data.current_price is None:
        return None
    levels: list[float] = [data.current_price]
    for z in data.zones:
        levels += [z.low, z.high]
    for lo, hi in data.focus_ranges:
        levels += [lo, hi]
    if data.draw_range:
        levels += list(data.draw_range)
    if data.invalidation_price:
        levels.append(data.invalidation_price)
    for v in (data.pwh, data.pwl):
        if v is not None:
            levels.append(v)
    # 現値から大きく外れた値（別銘柄の混入等）は除外
    cp = data.current_price
    levels = [v for v in levels if cp * 0.9 <= v <= cp * 1.1]
    if len(levels) < 3:
        return None
    lo, hi = min(levels), max(levels)
    pad = (hi - lo) * 0.06 or 1.0
    lo, hi = lo - pad, hi + pad

    top, bottom = 18, height - 14
    track_l, track_r = 62, 172
    label_x = track_r + 8

    def y(p: float) -> float:
        return top + (hi - p) / (hi - lo) * (bottom - top)

    parts: list[str] = []
    parts.append(
        f'<svg class="ladder" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="価格レベルマップ">'
    )
    # 価格グリッド
    step = _nice_step(hi - lo)
    g = (int(lo // step) + 1) * step
    while g < hi:
        gy = y(g)
        parts.append(
            f'<line x1="{track_l - 4}" y1="{gy:.1f}" x2="{track_r}" y2="{gy:.1f}" '
            f'stroke="{C_LINE}" stroke-width="1"/>'
            f'<text x="{track_l - 8}" y="{gy + 3:.1f}" text-anchor="end" '
            f'font-size="9" fill="{C_MUTED}">{_fmt_int(g)}</text>'
        )
        g += step

    # リクイディティゾーン（BSL 緑 / SSL 赤）
    for z in data.zones:
        zy1, zy2 = y(z.high), y(z.low)
        if zy2 - zy1 < 4:
            mid = (zy1 + zy2) / 2
            zy1, zy2 = mid - 2, mid + 2
        color, bg = (C_BULL, C_BULL_BG) if z.kind == "BSL" else (C_BEAR, C_BEAR_BG)
        parts.append(
            f'<rect x="{track_l}" y="{zy1:.1f}" width="{track_r - track_l}" '
            f'height="{zy2 - zy1:.1f}" fill="{bg}" stroke="{color}" '
            f'stroke-width="1" rx="2"/>'
        )
    # 注目ゾーン（青破線枠）
    for lo_f, hi_f in data.focus_ranges:
        fy1, fy2 = y(hi_f), y(lo_f)
        if fy2 - fy1 < 4:
            mid = (fy1 + fy2) / 2
            fy1, fy2 = mid - 2, mid + 2
        parts.append(
            f'<rect x="{track_l - 3}" y="{fy1:.1f}" width="{track_r - track_l + 6}" '
            f'height="{fy2 - fy1:.1f}" fill="none" stroke="{C_BRAND}" '
            f'stroke-width="1.4" stroke-dasharray="4 3" rx="3"/>'
        )
    # 無効化ライン
    if data.invalidation_price and lo <= data.invalidation_price <= hi:
        iy = y(data.invalidation_price)
        parts.append(
            f'<line x1="{track_l - 4}" y1="{iy:.1f}" x2="{track_r + 4}" y2="{iy:.1f}" '
            f'stroke="{C_BEAR}" stroke-width="1.6" stroke-dasharray="6 3"/>'
        )
    # 現値ライン
    cy = y(cp)
    parts.append(
        f'<line x1="{track_l - 6}" y1="{cy:.1f}" x2="{track_r + 6}" y2="{cy:.1f}" '
        f'stroke="{C_INK}" stroke-width="2.2"/>'
        f'<circle cx="{track_l - 6}" cy="{cy:.1f}" r="3" fill="{C_INK}"/>'
    )

    # 右側ラベル（衝突回避付き）
    @dataclass
    class Lbl:
        price: float
        text: str
        color: str
        bold: bool = False

    labels: list[Lbl] = [Lbl(cp, f"現値 {_fmt_price(cp)}", C_INK, True)]
    if data.draw_range:
        dlo, dhi = data.draw_range
        if lo <= dhi <= hi:
            arrow = "▲" if (data.draw_kind or "BSL") == "BSL" else "▼"
            rng = _fmt_int(dlo) if dlo == dhi else f"{_fmt_int(dlo)}-{_fmt_int(dhi)}"
            labels.append(Lbl((dlo + dhi) / 2, f"{arrow} Draw {rng}",
                              C_BULL if arrow == "▲" else C_BEAR, True))
    for z in data.zones:
        rng = _fmt_int(z.low) if abs(z.high - z.low) < 1 else f"{_fmt_int(z.low)}-{_fmt_int(z.high)}"
        labels.append(Lbl((z.low + z.high) / 2,
                          f"{z.kind} {rng}{_fmt_share(z.share)}",
                          C_BULL if z.kind == "BSL" else C_BEAR))
    if data.invalidation_price and lo <= data.invalidation_price <= hi:
        labels.append(Lbl(data.invalidation_price,
                          f"無効化 {_fmt_int(data.invalidation_price)}", C_BEAR, True))
    if data.pwh and lo <= data.pwh <= hi:
        labels.append(Lbl(data.pwh, f"PWH {_fmt_int(data.pwh)}", C_MUTED))
    if data.pwl and lo <= data.pwl <= hi:
        labels.append(Lbl(data.pwl, f"PWL {_fmt_int(data.pwl)}", C_MUTED))

    labels.sort(key=lambda l: -l.price)
    min_gap = 13.0
    ys = [y(l.price) for l in labels]
    for i in range(1, len(ys)):          # 上から下へ押し下げ
        if ys[i] < ys[i - 1] + min_gap:
            ys[i] = ys[i - 1] + min_gap
    overflow = ys[-1] - (height - 8)     # はみ出したら全体を押し上げ
    if overflow > 0:
        ys = [v - overflow for v in ys]
        for i in range(len(ys) - 2, -1, -1):
            if ys[i] > ys[i + 1] - min_gap:
                ys[i] = ys[i + 1] - min_gap
    for l, ly in zip(labels, ys):
        real_y = y(l.price)
        if abs(real_y - ly) > 2:  # リーダー線
            parts.append(
                f'<line x1="{track_r + 4}" y1="{real_y:.1f}" x2="{label_x - 2}" '
                f'y2="{ly:.1f}" stroke="{C_LINE}" stroke-width="1"/>'
            )
        weight = ' font-weight="700"' if l.bold else ""
        parts.append(
            f'<text x="{label_x}" y="{ly + 3:.1f}" font-size="10"{weight} '
            f'fill="{l.color}">{esc(l.text)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_score_gauge(score: int, width: int = 640, height: int = 64) -> str:
    """統一信頼度スコアのゲージ（-8〜+8、閾値バンド付き）。"""
    lo, hi = -8, 8
    left, right, bar_y, bar_h = 16, width - 16, 26, 12

    def x(v: float) -> float:
        return left + (v - lo) / (hi - lo) * (right - left)

    bands = [
        (-8, 2.5, C_NEUTRAL_BG, "Low ≤2（様子見）", C_INK2),
        (2.5, 4.5, "#fdeee3", "Med-cautious 3-4", "#c2410c"),
        (4.5, 6.5, C_WARN_BG, "Med 5-6", C_WARN),
        (6.5, 8, C_BULL_BG, "High ≥7", C_BULL),
    ]
    parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="統一信頼度スコア {score}">'
    ]
    for b_lo, b_hi, bg, label, fg in bands:
        parts.append(
            f'<rect x="{x(b_lo):.1f}" y="{bar_y}" width="{x(b_hi) - x(b_lo):.1f}" '
            f'height="{bar_h}" fill="{bg}" stroke="#ffffff" stroke-width="1.5"/>'
            f'<text x="{(x(b_lo) + x(b_hi)) / 2:.1f}" y="{bar_y + bar_h + 12}" '
            f'text-anchor="middle" font-size="8.5" fill="{fg}">{esc(label)}</text>'
        )
    for v in (-8, -4, 0, 4, 8):
        parts.append(
            f'<text x="{x(v):.1f}" y="{bar_y - 12}" text-anchor="middle" '
            f'font-size="8" fill="{C_MUTED}">{v:+d}</text>'
            f'<line x1="{x(v):.1f}" y1="{bar_y - 8}" x2="{x(v):.1f}" '
            f'y2="{bar_y}" stroke="{C_LINE}" stroke-width="1"/>'
        )
    mx = x(max(lo, min(hi, score)))
    parts.append(
        f'<path d="M {mx:.1f} {bar_y - 2} l -6 -10 l 12 0 z" fill="{C_INK}"/>'
        f'<text x="{mx:.1f}" y="{bar_y - 15}" text-anchor="middle" font-size="11" '
        f'font-weight="700" fill="{C_INK}">{score:+d}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def svg_share_bar(short_pct: float, long_pct: float, width: int = 620, height: int = 34) -> str:
    """リテール Short/Long 比率の 100% バー。"""
    left, right, bar_y, bar_h = 4, width - 4, 6, 18
    total = short_pct + long_pct or 100
    sw = (right - left) * short_pct / total
    parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="リテール Short {short_pct}% / Long {long_pct}%">',
        f'<rect x="{left}" y="{bar_y}" width="{sw:.1f}" height="{bar_h}" '
        f'fill="{C_BEAR}" rx="3"/>',
        f'<rect x="{left + sw + 2:.1f}" y="{bar_y}" '
        f'width="{right - left - sw - 2:.1f}" height="{bar_h}" fill="{C_BULL}" rx="3"/>',
        f'<text x="{left + 8}" y="{bar_y + 13}" font-size="10" font-weight="700" '
        f'fill="#ffffff">▼ Short {short_pct:.1f}%</text>',
        f'<text x="{right - 8}" y="{bar_y + 13}" font-size="10" font-weight="700" '
        f'fill="#ffffff" text-anchor="end">▲ Long {long_pct:.1f}%</text>',
        "</svg>",
    ]
    return "".join(parts)


def svg_prob_bar(probs: list[tuple[str, float]], width: int = 620, height: int = 46) -> str:
    """FedWatch レートレンジ確率の 100% スタックバー（順序ランプ: 青系）。"""
    left, right, bar_y, bar_h = 4, width - 4, 4, 20
    total = sum(p for _, p in probs) or 100
    ramp = ["#2a78d6", "#86b6ef", "#cde2fb", "#e8eef8"]
    parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="FedWatch 金利確率">'
    ]
    xcur = float(left)
    for i, (rng, p) in enumerate(probs):
        w = (right - left) * p / total
        color = ramp[min(i, len(ramp) - 1)]
        txt_fill = "#ffffff" if i == 0 else C_INK
        parts.append(
            f'<rect x="{xcur:.1f}" y="{bar_y}" width="{max(w - 2, 1):.1f}" '
            f'height="{bar_h}" fill="{color}" rx="3"/>'
        )
        label = f"{rng}%: {p:.1f}%"
        if w > 90:
            parts.append(
                f'<text x="{xcur + 8:.1f}" y="{bar_y + 14}" font-size="9.5" '
                f'font-weight="600" fill="{txt_fill}">{esc(label)}</text>'
            )
        else:
            parts.append(
                f'<text x="{xcur + w / 2:.1f}" y="{bar_y + bar_h + 14}" font-size="8.5" '
                f'text-anchor="middle" fill="{C_INK2}">{esc(label)}</text>'
            )
        xcur += w
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# HTML 部品
# ---------------------------------------------------------------------------

def _dir_badge(direction: Optional[str]) -> str:
    d = (direction or "").strip()
    if re.search(r"Long|Bullish", d, re.IGNORECASE):
        arrow, color, bg = "▲", C_BULL, C_BULL_BG
    elif re.search(r"Short|Bearish", d, re.IGNORECASE):
        arrow, color, bg = "▼", C_BEAR, C_BEAR_BG
    else:
        arrow, color, bg = "◆", C_INK2, C_NEUTRAL_BG
    label = esc(d) if d else "—"
    return (f'<span class="badge" style="color:{color};background:{bg};'
            f'border-color:{color}">{arrow} {label}</span>')


def _conf_badge(conf: Optional[str], score: Optional[int]) -> str:
    color, bg = CONFIDENCE_COLORS.get(conf or "", (C_INK2, C_NEUTRAL_BG))
    stxt = f" ｜ スコア {score:+d}" if score is not None else ""
    return (f'<span class="badge" style="color:{color};background:{bg};'
            f'border-color:{color}">{esc(conf or "—")}{stxt}</span>')


def _kpi_card(label: str, value_html: str, sub: str = "", accent: str = C_LINE) -> str:
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return (
        f'<div class="kpi" style="border-top-color:{accent}">'
        f'<div class="kpi-label">{esc(label)}</div>'
        f'<div class="kpi-value">{value_html}</div>{sub_html}</div>'
    )


def build_kpi_row(data: ReportData) -> str:
    v = data.verdict
    cards = []
    # 1. 総合判定
    if v.no_trade:
        cards.append(_kpi_card(
            "本日の判定" if data.kind == "daily" else "来週の判定",
            f'<span style="color:{C_WARN}">◆ 様子見</span>',
            "NO-TRADE（高確度セットアップなし）", C_WARN))
    else:
        plan = data.plans[0] if data.plans else None
        d = plan.direction if plan else None
        sym = (plan.symbol or "XAUUSD") if plan else "XAUUSD"
        sym_clean = re.sub(r"[（(].*", "", sym).strip()
        sub = f"{esc(sym_clean)} ｜ KZ: {esc(plan.killzone)}" if plan and plan.killzone else ""
        cards.append(_kpi_card(
            "本日の判定" if data.kind == "daily" else "来週の判定",
            _dir_badge(d), sub,
            C_BULL if d and "Long" in d else C_BEAR if d and "Short" in d else C_LINE))
    # 2. 信頼度
    conf_color = CONFIDENCE_COLORS.get(v.confidence or "", (C_INK2, C_NEUTRAL_BG))[0]
    sub = "自己検証で訂正済み" if v.corrected else "統一 8 項目スコア"
    cards.append(_kpi_card("信頼度", _conf_badge(v.confidence, v.score), sub, conf_color))
    # 3. XAUUSD 現値
    if data.current_price:
        chg = esc(data.price_change or "")
        chg_color = C_BULL if chg.startswith("+") else C_BEAR if chg.startswith("-") else C_INK2
        cards.append(_kpi_card(
            "XAUUSD 現値",
            f'{_fmt_price(data.current_price)} '
            f'<span style="font-size:8.5pt;color:{chg_color}">{chg}</span>',
            "", C_BRAND))
    # 4. DXY
    if data.dxy_bias:
        cards.append(_kpi_card("DXY バイアス", _dir_badge(data.dxy_bias),
                               esc(_trim(data.dxy_note, 42)), C_BRAND))
    return f'<div class="kpi-row">{"".join(cards)}</div>'


def build_correction_callout(data: ReportData) -> str:
    v = data.verdict
    if not v.corrected:
        return ""
    def _nt(x):
        return "あり" if x else "なし" if x is not None else "—"
    return (
        f'<div class="callout callout-warn"><span class="callout-icon">⚠</span>'
        f'<div><strong>自己検証による確定訂正</strong> — セクション0 の初期値 '
        f'「{esc(v.original_confidence or "—")} / スコア {v.original_score if v.original_score is not None else "—"} / '
        f'NO-TRADE {_nt(v.original_no_trade)}」は、スコア再集計により '
        f'「<strong>{esc(v.confidence or "—")} / スコア {v.score if v.score is not None else "—"} / '
        f'NO-TRADE {_nt(v.no_trade)}</strong>」へ訂正。本ダッシュボードは確定値を表示している。'
        f'</div></div>'
    )


def build_scenario_callout(data: ReportData) -> str:
    """様子見判定 or 条件付きシナリオのハイライト（メイングリッド右カラム用）。"""
    v = data.verdict
    if not data.conditional_scenarios:
        return ""
    rows = "".join(f"<li>{esc(s)}</li>" for s in data.conditional_scenarios)
    if v.no_trade:
        when = "本日" if data.kind == "daily" else "来週"
        return (
            f'<div class="callout callout-warn" style="margin-bottom:3mm">'
            f'<span class="callout-icon">◆</span><div><strong>{when}は様子見（NO-TRADE）</strong>'
            f' — 執行判断材料は以下の条件付きシナリオのみ。<ul class="cond-list">{rows}</ul>'
            f'</div></div>'
        )
    return (f'<div class="note-box" style="margin-bottom:3mm"><strong>条件付きシナリオ</strong>'
            f'<ul class="cond-list">{rows}</ul></div>')


def build_plan_cards(data: ReportData) -> str:
    """プラン A/B カードの 2 カラム行。"""
    v = data.verdict
    parts: list[str] = []
    for p in data.plans[:2]:
        is_long = bool(p.direction and "Long" in p.direction)
        color = C_BULL if is_long else C_BEAR
        muted = ' plan-muted' if v.no_trade else ""
        rows = []
        for label, val in (("注目ゾーン", p.zone), ("Draw", p.draw),
                           ("無効化", p.invalidation), ("KZ", p.killzone)):
            if val:
                rows.append(
                    f'<div class="plan-row"><span class="plan-key">{label}</span>'
                    f'<span class="plan-val">{esc(_trim(val, 96))}</span></div>')
        status = '<span class="plan-status">撤回（参考）</span>' if v.no_trade else ""
        parts.append(
            f'<div class="plan-card{muted}" style="border-left-color:{color}">'
            f'<div class="plan-head"><span class="plan-name">{esc(p.name)}</span>'
            f'{_dir_badge(p.direction)}{status}</div>{"".join(rows)}</div>'
        )
    if not parts:
        return ""
    return f'<div class="plans-row">{"".join(parts)}</div>'


def build_score_panel(data: ReportData) -> str:
    if not data.score_items:
        return ""
    rows = []
    for num, label, judgment, pt in data.score_items:
        if pt > 0:
            chip = f'<span class="score-chip" style="background:{C_BULL}">+{pt}</span>'
        elif pt < 0:
            chip = f'<span class="score-chip" style="background:{C_BEAR}">{pt}</span>'
        else:
            chip = f'<span class="score-chip score-chip-zero">0</span>'
        reason = re.sub(r"\*+", "", judgment)
        reason = reason if len(reason) <= 62 else reason[:60] + "…"
        rows.append(
            f'<div class="score-row"><span class="score-label">{num}. {esc(label)}</span>'
            f'{chip}<span class="score-reason">{esc(reason)}</span></div>')
    total = data.verdict.score
    gauge = svg_score_gauge(total) if total is not None else ""
    return (
        f'<section class="panel"><h3 class="panel-title">統一信頼度スコア内訳</h3>'
        f'{"".join(rows)}<div class="gauge-wrap">{gauge}</div></section>'
    )


def build_retail_panel(data: ReportData) -> str:
    r = data.retail
    if not r.get("short_pct"):
        return ""
    bar = svg_share_bar(r["short_pct"], r.get("long_pct", 100 - r["short_pct"]))
    cells = []
    for side, key_avg, key_pnl, key_lots, color in (
        ("Short", "short_avg", "short_pnl", "short_lots", C_BEAR),
        ("Long", "long_avg", "long_pnl", "long_lots", C_BULL),
    ):
        if r.get(key_avg):
            pnl = r.get(key_pnl, "")
            cells.append(
                f'<div class="retail-cell"><span style="color:{color};font-weight:700">'
                f'{"▼" if side == "Short" else "▲"} {side}</span> 平均 {esc(r[key_avg])} ｜ '
                f'含み損益 <strong>{esc(pnl)}%</strong> ｜ {esc(r.get(key_lots, "—"))} lots</div>')
    note = ""
    sp = r.get("short_pnl")
    if sp is not None and _num(sp) is not None and _num(sp) < -3:
        note = (f'<div class="mini-note">Short 側の深い含み損（{esc(sp)}%）の損切りが'
                f'上方 BSL の燃料になっている。</div>')
    return (
        f'<section class="panel"><h3 class="panel-title">リテールポジショニング</h3>'
        f'{bar}<div class="retail-grid">{"".join(cells)}</div>{note}</section>'
    )


def build_macro_panel(data: ReportData) -> str:
    blocks = []
    fw = data.fedwatch
    if fw.get("probs"):
        head = "FedWatch"
        if fw.get("date"):
            days = f"（あと {fw['days']} 日）" if fw.get("days") else ""
            head += f' — 次回 FOMC {esc(fw["date"])}{days}'
        blocks.append(f'<div class="macro-head">{head}</div>{svg_prob_bar(fw["probs"])}')
    if data.vix_line:
        blocks.append(f'<div class="mini-note">{esc(data.vix_line[:120])}</div>')
    if not blocks:
        return ""
    return (f'<section class="panel"><h3 class="panel-title">マクロ・ボラ環境</h3>'
            f'{"".join(blocks)}</section>')


def build_events_panel(data: ReportData) -> str:
    if not data.events and not data.week_ahead:
        return ""
    rows_html = ""
    if data.events:
        header, *rows = data.events
        keep = min(len(header), 6)
        h = "".join(f"<th>{esc(c)}</th>" for c in header[:keep])
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row[:keep]) + "</tr>"
            for row in rows[:6])
        rows_html = f'<table class="mini-table"><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>'
    ahead = (f'<div class="mini-note"><strong>今週残り:</strong> {esc(data.week_ahead)}</div>'
             if data.week_ahead else "")
    return (f'<section class="panel"><h3 class="panel-title">イベント（今後24時間〜今週）</h3>'
            f'{rows_html}{ahead}</section>')


def build_correlation_panel(data: ReportData) -> str:
    if not data.correlations:
        return ""
    chips = []
    for pair, r20, r60, judge in data.correlations[:4]:
        broken = "無相関" in judge or "断絶" in judge
        color, bg = (C_INK2, C_NEUTRAL_BG) if broken else (C_BRAND, C_BRAND_BG)
        chips.append(
            f'<div class="corr-chip" style="border-color:{color};background:{bg}">'
            f'<div class="corr-pair">{esc(pair[:26])}</div>'
            f'<div class="corr-vals">r20 {esc(r20)} ｜ r60 {esc(r60)}</div>'
            f'<div class="corr-judge" style="color:{color}">{esc(judge[:14])}</div></div>')
    return (f'<section class="panel"><h3 class="panel-title">相関レジーム</h3>'
            f'<div class="corr-row">{"".join(chips)}</div></section>')


def build_fundamental_panel(data: ReportData) -> str:
    if not data.fundamentals:
        return ""
    cards = []
    for f in data.fundamentals[:3]:
        driver = re.sub(r"\*+", "", f["driver"])
        driver = driver if len(driver) <= 150 else driver[:148] + "…"
        conf = f'<span class="mini-tag">確度 {esc(f["conf"])}</span>' if f.get("conf") else ""
        cards.append(
            f'<div class="fund-card"><div class="fund-head">'
            f'<strong>{esc(f["symbol"])}</strong>{_dir_badge(f["bias"])}{conf}</div>'
            f'<div class="fund-driver">{esc(driver)}</div></div>')
    return (f'<section class="panel"><h3 class="panel-title">ファンダ大局バイアス（数週間〜数ヶ月）</h3>'
            f'{"".join(cards)}</section>')


def build_review_panel(data: ReportData) -> str:
    if not data.review_verdict:
        return ""
    color, bg = {
        "当たり": (C_BULL, C_BULL_BG), "部分的中": (C_WARN, C_WARN_BG),
        "一部的中": (C_WARN, C_WARN_BG), "未決着": (C_INK2, C_NEUTRAL_BG),
        "外れ": (C_BEAR, C_BEAR_BG),
    }.get(data.review_verdict, (C_INK2, C_NEUTRAL_BG))
    note = f'<span class="review-note">{esc(data.review_note or "")}</span>'
    return (
        f'<section class="panel"><h3 class="panel-title">前回照合</h3>'
        f'<div class="review-line"><span class="badge" style="color:{color};'
        f'background:{bg};border-color:{color}">{esc(data.review_verdict)}</span>'
        f'{note}</div></section>'
    )


def build_dashboard(data: ReportData) -> str:
    """1〜2 ページのダッシュボード HTML を組み立てる。"""
    chips = "".join(f'<span class="meta-chip">{esc(c)}</span>' for c in data.meta_chips)
    header = (
        f'<header class="dash-header"><div>'
        f'<h1 class="dash-title">{esc(data.title or "Bias Report")}</h1>'
        f'<div class="meta-row">{chips}</div></div></header>'
    )
    ladder = svg_price_ladder(data)
    ladder_html = (
        f'<div class="ladder-col"><h3 class="panel-title">価格レベルマップ</h3>'
        f'{ladder}<div class="ladder-legend">'
        f'<span style="color:{C_BULL}">■ BSL（上方流動性）</span>'
        f'<span style="color:{C_BEAR}">■ SSL（下方流動性）</span>'
        f'<span style="color:{C_BRAND}">▢ 注目ゾーン</span>'
        f'<span style="color:{C_BEAR}">- - 無効化</span></div></div>'
        if ladder else ""
    )
    scenario = build_scenario_callout(data)
    wait = ""
    if data.wait_note and data.kind == "daily":
        wait = (f'<div class="note-box"><strong>待ちの妥当性</strong> '
                f'{esc(_trim(data.wait_note, 220))}</div>')
    main_grid = ""
    if ladder_html or scenario:
        main_grid = (f'<div class="main-grid">{ladder_html}'
                     f'<div>{scenario}{wait}</div></div>')
    elif wait:
        main_grid = wait
    risk = ""
    if data.risk_note:
        risk = (f'<div class="callout callout-risk"><span class="callout-icon">⚠</span>'
                f'<div><strong>{"本日" if data.kind == "daily" else "来週"}最大のリスク</strong> — '
                f'{esc(data.risk_note)}</div></div>')

    page1 = (
        f'<div class="dash-page">{header}{build_correction_callout(data)}'
        f'{build_kpi_row(data)}{risk}{main_grid}{build_plan_cards(data)}</div>'
    )

    panels2 = [
        build_score_panel(data),
        build_retail_panel(data),
        build_macro_panel(data),
        build_events_panel(data),
        build_correlation_panel(data),
        build_fundamental_panel(data),
        build_review_panel(data),
    ]
    panels2 = [p for p in panels2 if p]
    page2 = f'<div class="dash-page2">{"".join(panels2)}</div>' if panels2 else ""
    return page1 + page2


# ---------------------------------------------------------------------------
# 全文詳細 + ドキュメント全体
# ---------------------------------------------------------------------------

def _md_to_html(md_text: str) -> str:
    return markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        output_format="html5",
    )


def build_html(md_text: str, generated: Optional[str] = None,
               style_css: Optional[str] = None) -> str:
    """レポート MD から完全な HTML ドキュメントを生成する。"""
    data = parse_report(md_text)
    dashboard = build_dashboard(data)
    detail_body = _md_to_html(md_text)
    if style_css is None:
        css_path = Path(__file__).resolve().parent.parent / "templates" / "style_human.css"
        style_css = css_path.read_text(encoding="utf-8")
    from datetime import datetime
    gen = generated or datetime.now().strftime("%Y-%m-%d %H:%M JST")
    doc_label = "ICT Weekly Bias Report" if data.kind == "weekly" else "ICT Daily Bias Report"
    style_css = style_css.replace("{{DOC_LABEL}}", doc_label).replace("{{DATE_JST}}", gen)
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{esc(data.title or doc_label)}</title>
<style>{style_css}</style>
</head>
<body>
{dashboard}
<div class="detail">
<div class="detail-divider"><span>詳細（全文）</span>
<span class="detail-sub">ここから下は AI 生成レポートの全文。結論・根拠はダッシュボードに要約済み。</span></div>
{detail_body}
</div>
<footer class="footer">Generated by fundamental-macro-analysis / human-first renderer ｜ {esc(gen)}</footer>
</body>
</html>
"""
