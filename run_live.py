#!/usr/bin/env python3
"""Small live roadshow workflow using the user's private Twelve Data key."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

from nd100_resonance_scanner import load_twelve_data_key
from run_daily import csv_market_date, daily_report_complete


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
RESUME_MARKER = OUT / ".live-workflow-resume.json"


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
    ap.add_argument("--retry", type=int, default=1, help="完整日报失败后的自动重试次数")
    ap.add_argument("--retry-delay", type=float, default=60, help="重试间隔秒数")
    args = ap.parse_args()

    if args.limit is not None and args.limit <= 0 or args.retry < 0 or args.retry_delay < 0:
        ap.error("limit 必须大于 0；retry 和 retry-delay 不能为负数")

    if not load_twelve_data_key():
        raise SystemExit("未找到本地 API Key。请按 README 配置 .env 或项目外凭据文件。")

    OUT.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    # A sleep, terminal close or Agent restart may leave the canonical ND100
    # input after acquisition. Resume that exact input before making another
    # API request.
    if RESUME_MARKER.is_file():
        try:
            state = json.loads(RESUME_MARKER.read_text(encoding="utf-8"))
            pending = Path(state["nd100_input"]).expanduser().resolve()
            pending_date = csv_market_date(pending)
        except (OSError, KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError):
            RESUME_MARKER.unlink(missing_ok=True)
        else:
            if daily_report_complete(pending_date):
                print(f"[reuse] {pending_date} 正式日报已完成，清理续跑标记。")
                RESUME_MARKER.unlink(missing_ok=True)
                return 0
            print(f"[resume] 接管中断后的 ND100 输入: {pending}")
            run(py, "run_daily.py", "--nd100-input", str(pending),
                "--retry", str(args.retry), "--retry-delay", str(args.retry_delay))
            RESUME_MARKER.unlink(missing_ok=True)
            return 0

    resonance = None
    market_date = None
    for attempt in range(args.retry + 1):
        run_tag = f"{args.tag}-{datetime.now(ZoneInfo('America/New_York')).strftime('%Y%m%d-%H%M%S')}-{attempt}"
        resonance_args = [py, "nd100_resonance_scanner.py"]
        if args.tickers:
            resonance_args += ["--tickers", args.tickers]
        else:
            resonance_args += ["--limit", str(args.limit)]
        resonance_args += ["--output-tag", run_tag]
        try:
            run(*resonance_args)
            resonance_matches = sorted(OUT.glob(f"nd100_resonance_*_{run_tag}.csv"))
            if len(resonance_matches) != 1:
                raise RuntimeError(f"无法唯一定位 ND100 输出: {[str(p) for p in resonance_matches]}")
            resonance = resonance_matches[0]
            market_date = csv_market_date(resonance)
            break
        except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
            if attempt >= args.retry:
                raise SystemExit(f"行情获取连续失败: {exc}") from exc
            print(f"[retry] 行情日期/缓存异常，{attempt + 1}/{args.retry} 后自动重试: {exc}", file=sys.stderr)
            time.sleep(args.retry_delay)
    assert resonance is not None and market_date is not None
    print(f"[date gate] market_date={market_date}")
    if ((OUT / "daily" / market_date).is_dir() or
            (OUT / f"five_rankings_{market_date}_daily.csv").exists()):
        print(f"[reuse] {market_date} 正式日报已存在，跳过重跑。")
        return 0
    formal_resonance = OUT / f"nd100_resonance_{market_date}.csv"
    formal_resonance_html = OUT / f"nd100_resonance_{market_date}.html"
    formal_resonance_manifest = OUT / f"nd100_resonance_{market_date}_manifest.json"
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
    RESUME_MARKER.write_text(
        json.dumps({"nd100_input": str(formal_resonance), "market_date": market_date}, indent=2) + "\n",
        encoding="utf-8",
    )
    run(py, "run_daily.py", "--nd100-input", str(formal_resonance),
        "--retry", str(args.retry), "--retry-delay", str(args.retry_delay))
    RESUME_MARKER.unlink(missing_ok=True)

    print("\n[live roadshow ready]")
    print(f"  market_date: {market_date}")
    print(f"  Daily reports: {OUT / 'daily' / market_date}")
    print("  API key remained local and was not written to reports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
