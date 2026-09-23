"""Code-only pre-trim (abstract/intro head + conclusion tail) and paragraph records for the Jev filter."""
import json, re, sys
from pathlib import Path
D = Path(__file__).resolve().parent / "docs"  # downloaded PDFs converted with pdftotext; urls in literature_urls.tsv
URLS = dict(l.split("\t") for l in (D / "urls.tsv").read_text().split("\n") if l.strip())
TOPICS = {
    "t1_liquidity": ("stop-loss order clustering and price cascades in currency markets", ["osler2003", "osler2005"]),
    "t2_announcements": ("market reaction to scheduled macroeconomic announcements", ["abdv2003", "chaboud2007"]),
    "t3_gold_macro": ("gold prices, real interest rates and inflation", ["erbharvey2013", "erbharvey2024"]),
    "t4_positions": ("futures trader positions and subsequent price returns", ["tornellyuan", "cftc_study"]),
    "t5_orderflow": ("customer order flow informativeness in foreign exchange", ["bis405"]),
    "t6_llm_bias": ("look-ahead bias and news-based return prediction with language models", ["glasserman2023", "lopezlira2023"]),
}
HEAD, TAIL = int(sys.argv[1]) if len(sys.argv) > 1 else 14000, int(sys.argv[2]) if len(sys.argv) > 2 else 8000
REF = re.compile(r"^\s*(References|REFERENCES|Bibliography|BIBLIOGRAPHY)\s*$", re.M)

def spans(text):
    refs = [m.start() for m in REF.finditer(text)]
    end = refs[-1] if refs and refs[-1] > len(text) * 0.4 else int(len(text) * 0.85)
    head = (0, min(HEAD, end))
    tail = (max(head[1], end - TAIL), end)
    return [head, tail] if tail[0] < tail[1] else [head]

def paragraphs(text, start, end):
    out, pos = [], start
    for block in re.split(r"\n\s*\n", text[start:end]):
        b_start = text.find(block, pos) if block else pos
        pos = b_start + len(block)
        t = re.sub(r"\s+", " ", block).strip()
        if len(t) < 60:            # page numbers, running heads, lone headings
            continue
        if out and len(out[-1]["text"]) + len(t) < 700:
            out[-1]["text"] += " " + t; out[-1]["end"] = pos
        else:
            out.append({"text": t, "start": b_start, "end": pos})
    final = []
    for p in out:
        t = p["text"]
        while len(t) > 1500:
            cut = t.rfind(". ", 0, 1500) + 1 or 1500
            final.append({**p, "text": t[:cut].strip()}); t = t[cut:].strip()
        if t: final.append({**p, "text": t})
    return final

summary = {}
for key, (topic, docs) in TOPICS.items():
    recs, raw_chars, kept_chars = [], 0, 0
    for doc in docs:
        text = (D / f"{doc}.txt").read_text(errors="ignore")
        raw_chars += len(text)
        for s, e in spans(text):
            for i, p in enumerate(paragraphs(text, s, e)):
                recs.append({"id": f"{doc}-{s}-{i:03d}", "text": p["text"], "kind": "passage", "protected": False,
                             "source": {"url": URLS[doc], "doc": doc, "start_char": p["start"], "end_char": p["end"]}})
                kept_chars += len(p["text"])
    out = D.parent / "records" / f"{key}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"records": recs}, ensure_ascii=False))
    summary[key] = dict(topic=topic, docs=docs, full_text_chars=raw_chars, pretrim_chars=kept_chars,
                        records=len(recs), file_bytes=out.stat().st_size)
print(json.dumps(summary, indent=1))
