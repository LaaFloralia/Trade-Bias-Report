"""ポジショニング履歴の初期投入: 過去の scraped_data と CFTC の過去週から記録を作る。

    python scripts/backfill_positioning.py --history /path/output/history/positioning.jsonl \
        --scraped '/path/output/scraped_data_*.json' [--scraped ...]
"""
import argparse
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import COT_TARGETS  # noqa: E402
from scrapers.positioning_history import backfill_cot_history, backfill_from_scraped  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--scraped", action="append", default=[])
    parser.add_argument("--cot-weeks", type=int, default=156)
    args = parser.parse_args()
    files = sorted({Path(f) for pattern in args.scraped for f in glob.glob(pattern)})
    scraped = backfill_from_scraped(files, args.history)
    gold = next(market for label, market in COT_TARGETS if label.startswith("GOLD"))
    cot = backfill_cot_history(gold, args.cot_weeks, args.history)
    print(f"scraped files {len(files)} → {scraped} 件、COT {cot} 週を {args.history} に追加")
    return 0


if __name__ == "__main__":
    sys.exit(main())
