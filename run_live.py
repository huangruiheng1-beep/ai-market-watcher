#!/usr/bin/env python3
"""Small live roadshow workflow using the user's private Twelve Data key."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

from nd100_resonance_scanner import load_twelve_data_key
from run_daily import csv_market_date


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"


def run(*args: str) -> None:
    print("\n$", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 投研市场观察助手·可配置股票池的真实行情运行入口")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--tickers",
                       help="逗号分隔的股票代码；可传入本批股票，路演演示可用 5–10 只")
    group.add_argument("--limit", type=int, default=50,
                       help="未指定 --tickers 时，扫描当前默认股票池的前 N 只；正式运行默认每批 50 只，路演可显式缩小")
    ap.add_argument("--tag", default="roadshow", help="本次输入的临时标记")
    args = ap.parse_args()

    if args.limit is not None and args.limit <= 0:
        ap.error("--limit 必须大于 0")

    if not load_twelve_data_key():
        raise SystemExit("未找到本地 API Key。请按 README 配置 .env 或项目外凭据文件。")

    OUT.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    run_tag = f"{args.tag}-{datetime.now(ZoneInfo('America/New_York')).strftime('%Y%m%d-%H%M%S')}"

    resonance_args = [py, "nd100_resonance_scanner.py"]
    if args.tickers:
        resonance_args += ["--tickers", args.tickers]
    else:
        resonance_args += ["--limit", str(args.limit)]
    resonance_args += ["--output-tag", run_tag]
    run(*resonance_args)
    resonance_matches = sorted(OUT.glob(f"nd100_resonance_*_{run_tag}.csv"))
    if len(resonance_matches) != 1:
        raise SystemExit(f"无法唯一定位 ND100 输出: {[str(p) for p in resonance_matches]}")
    resonance = resonance_matches[0]
    market_date = csv_market_date(resonance)
    print(f"[date gate] market_date={market_date}")
    if ((OUT / "daily" / market_date).is_dir() or
            (OUT / f"five_rankings_{market_date}_daily.csv").exists()):
        print(f"[reuse] {market_date} 正式日报已存在，跳过重跑。")
        return 0
    formal_resonance = OUT / f"nd100_resonance_{market_date}_{args.tag}.csv"
    formal_resonance_html = OUT / f"nd100_resonance_{market_date}_{args.tag}.html"
    formal_resonance_manifest = OUT / f"nd100_resonance_{market_date}_{args.tag}_manifest.json"
    for source, target in (
        (resonance, formal_resonance),
        (resonance.with_suffix(".html"), formal_resonance_html),
        (resonance.with_name(resonance.stem + "_manifest.json"), formal_resonance_manifest),
    ):
        if target.exists():
            raise SystemExit(f"拒绝覆盖已有 ND100 产物: {target}")
        source.rename(target)
    manifest = json.loads(formal_resonance_manifest.read_text(encoding="utf-8"))
    manifest["report_date"] = market_date
    manifest["market_date"] = f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:]}"
    formal_resonance_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    run(py, "run_daily.py", "--nd100-input", str(formal_resonance))

    print("\n[live roadshow ready]")
    print(f"  market_date: {market_date}")
    print(f"  Daily reports: {OUT / 'daily' / market_date}")
    print("  API key remained local and was not written to reports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
