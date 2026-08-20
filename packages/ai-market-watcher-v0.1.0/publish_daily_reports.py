#!/usr/bin/env python3
"""Consolidate one day's reports into the user-facing daily layer.

The original run artifacts remain under output/runs/. This script only copies
formal reports into output/daily/YYYYMMDD/ and never scans symbols or calls an
API. T9 is renamed there without the internal ``_t9`` suffix so the Daily
dashboard can treat it like the other formal daily reports.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
OUTPUT = BASE_DIR / "output"


def normalize_date(raw: str) -> str:
    raw = raw.replace("-", "")
    if not re.fullmatch(r"20\d{6}", raw):
        raise SystemExit(f"日期格式错误: {raw}，应为 YYYYMMDD")
    datetime.strptime(raw, "%Y%m%d")
    return raw


def copy_if_exists(source: Path, target: Path, copied: list[str]) -> None:
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(target))


def newest_t9_run(report_date: str) -> Path | None:
    runs = sorted(
        path for path in (OUTPUT / "runs").glob(f"{report_date}-*")
        if path.is_dir() and (path / f"divergence_td9_{report_date}_t9.html").is_file()
    )
    return runs[-1] if runs else None


def publish(report_date: str, t9_run: Path | None = None) -> Path:
    target_dir = OUTPUT / "daily" / report_date
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    names = [
        f"nd100_resonance_{report_date}.csv",
        f"nd100_resonance_{report_date}.html",
        f"nd100_resonance_{report_date}_manifest.json",
        f"nd100_resonance_{report_date}_batch02.csv",
        f"nd100_resonance_{report_date}_batch02.html",
        f"nd100_resonance_{report_date}_batch02_manifest.json",
        f"five_rankings_{report_date}_daily.csv",
        f"five_rankings_{report_date}_daily.html",
        f"five_rankings_{report_date}_daily_manifest.json",
        f"skdj_{report_date}_daily.csv",
        f"skdj_{report_date}_daily.html",
        f"skdj_{report_date}_daily_manifest.json",
        f"status_chain_board_{report_date}.html",
    ]
    for name in names:
        if name == f"status_chain_board_{report_date}.html":
            exact = OUTPUT / name
            status_sources = [exact] if exact.is_file() else sorted(
                OUTPUT.glob("status_chain_board_*.html"), reverse=True
            )[:1]
            for source in status_sources:
                copy_if_exists(source, target_dir / source.name, copied)
        else:
            copy_if_exists(OUTPUT / name, target_dir / name, copied)

    t9_run = t9_run or newest_t9_run(report_date)
    if t9_run:
        copy_if_exists(
            t9_run / f"divergence_td9_{report_date}_t9.csv",
            target_dir / f"divergence_td9_{report_date}.csv",
            copied,
        )
        copy_if_exists(
            t9_run / f"divergence_td9_{report_date}_t9.html",
            target_dir / f"divergence_td9_{report_date}.html",
            copied,
        )
        run_manifest = t9_run / "run_manifest.json"
        if run_manifest.is_file():
            manifest = json.loads(run_manifest.read_text(encoding="utf-8"))
            manifest["published_daily_date"] = report_date
            manifest["source_run_dir"] = str(t9_run)
            (target_dir / f"divergence_td9_{report_date}_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            copied.append(str(target_dir / f"divergence_td9_{report_date}_manifest.json"))

    if not copied:
        raise SystemExit(f"没有找到 {report_date} 可整理的正式报告")
    print(f"[daily-publish] date={report_date}")
    print(f"[daily-publish] target={target_dir}")
    print(f"[daily-publish] copied={len(copied)}")
    print(f"[daily-publish] t9_run={t9_run or '未找到'}")
    return target_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="整理某日正式报告到 output/daily/YYYYMMDD")
    parser.add_argument("--date", required=True, help="报告日期，格式 YYYYMMDD")
    parser.add_argument("--t9-run", help="指定 T9 原始运行目录；默认选择该日期最新运行")
    args = parser.parse_args()
    t9_run = Path(args.t9_run).resolve() if args.t9_run else None
    publish(normalize_date(args.date), t9_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
