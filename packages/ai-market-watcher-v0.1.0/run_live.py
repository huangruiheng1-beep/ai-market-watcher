#!/usr/bin/env python3
"""Small live roadshow workflow using the user's private Twelve Data key."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

from nd100_resonance_scanner import load_twelve_data_key


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"


def run(*args: str) -> None:
    print("\n$", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def market_asof(path: Path) -> str:
    dates = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            raw = (row.get("日线_数据截至") or "").strip()
            if raw:
                dates.append(raw[:10])
    if not dates:
        raise RuntimeError("无法从共振报告确定行情截至日期")
    return min(dates)


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 投研市场观察助手·可配置股票池的真实行情运行入口")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--tickers",
                       help="逗号分隔的股票代码；可传入本批股票，路演演示可用 5–10 只")
    group.add_argument("--limit", type=int, default=50,
                       help="未指定 --tickers 时，扫描当前默认股票池的前 N 只；正式运行默认每批 50 只，路演可显式缩小")
    ap.add_argument("--tag", default="roadshow", help="输出文件名标记")
    args = ap.parse_args()

    if args.limit is not None and args.limit <= 0:
        ap.error("--limit 必须大于 0")

    if not load_twelve_data_key():
        raise SystemExit("未找到本地 API Key。请按 README 配置 .env 或项目外凭据文件。")

    OUT.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    resonance = OUT / f"nd100_resonance_{today}_{args.tag}.csv"

    resonance_args = [py, "nd100_resonance_scanner.py"]
    if args.tickers:
        resonance_args += ["--tickers", args.tickers]
    else:
        resonance_args += ["--limit", str(args.limit)]
    resonance_args += ["--output-tag", args.tag]
    run(*resonance_args)
    run(py, "five_rankings_daily.py", "--nd100-input", str(resonance),
        "--output-tag", args.tag)
    run(py, "divergence_td9_scanner.py", "--nd100-input", str(resonance),
        "--output-dir", str(OUT), "--output-tag", args.tag)
    run(py, "skdj_scanner.py", "--nd100-input", str(resonance),
        "--output-dir", str(OUT), "--output-tag", args.tag)

    asof = market_asof(resonance)
    reports = ",".join(str(OUT / name) for name in (
        f"nd100_resonance_{today}_{args.tag}.csv",
        f"five_rankings_{today}_{args.tag}.csv",
        f"divergence_td9_{today}_{args.tag}.csv",
        f"skdj_{today}_{args.tag}.csv",
    ))
    db = OUT / "status_chain.sqlite"
    run(py, "status_chain_tracker.py", "--db", str(db), "ingest",
        "--reports", reports, "--asof", asof)

    update_cutoff = (date.fromisoformat(asof) + timedelta(days=1)).isoformat()
    run(py, "status_chain_tracker.py", "--db", str(db), "update",
        "--asof", update_cutoff, "--cache-only")
    board = OUT / f"status_chain_board_{today}_{args.tag}.html"
    run(py, "status_chain_tracker.py", "--db", str(db), "report",
        "--out", str(board), "--asof", asof)

    print("\n[live roadshow ready]")
    print(f"  Bottom status tracker: {board}")
    print("  API key remained local and was not written to reports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
