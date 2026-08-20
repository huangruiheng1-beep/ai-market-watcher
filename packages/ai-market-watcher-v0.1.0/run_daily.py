#!/usr/bin/env python3
"""Run one complete daily research workflow safely.

This is the public, agent-independent entry point.  It deliberately accepts
an ND100 CSV as the boundary between data acquisition and report generation:
the CSV is inspected first, the real market date is derived from its contents,
and only then are downstream tools allowed to run.  That makes retries
idempotent and prevents a local clock date from becoming a report date.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
DATE_RE = re.compile(r"20\d{6}")


class WorkflowError(RuntimeError):
    pass


def normalize_market_date(value: str) -> str:
    raw = value.strip()[:10].replace("-", "")
    if not re.fullmatch(r"20\d{6}", raw):
        raise WorkflowError(f"无法识别行情日期: {value!r}")
    datetime.strptime(raw, "%Y%m%d")
    return raw


def csv_market_date(path: Path) -> str:
    """Return one unique daily as-of date from a formal ND100 CSV."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise WorkflowError(f"输入 CSV 为空: {path}")
    daily_values = {
        normalize_market_date(row.get("日线_数据截至", ""))
        for row in rows
        if row.get("日线_数据截至", "").strip()
    }
    if sum(bool(row.get("日线_数据截至", "").strip()) for row in rows) != len(rows):
        raise WorkflowError(f"输入缺少日线_数据截至: {path}")
    if len(daily_values) != 1:
        raise WorkflowError(f"输入包含多个日线行情日 {sorted(daily_values)}: {path}")
    day = daily_values.pop()
    intraday_column = "60min_数据截至"
    if intraday_column in rows[0]:
        intraday_values = {
            normalize_market_date(row.get(intraday_column, ""))
            for row in rows
            if row.get(intraday_column, "").strip()
        }
        if len(intraday_values) != 1 or intraday_values != {day}:
            raise WorkflowError(f"60m 与日线行情日不一致或不完整: {path}")
    return day


def assert_no_existing_formal_report(market_date: str) -> None:
    candidates = [
        OUTPUT / f"nd100_resonance_{market_date}.csv",
        OUTPUT / f"five_rankings_{market_date}_daily.csv",
        OUTPUT / f"skdj_{market_date}_daily.csv",
        OUTPUT / "daily" / market_date,
    ]
    existing = [str(path) for path in candidates if path.exists()]
    if existing:
        raise WorkflowError(
            f"日期门禁：{market_date} 已有正式产物，禁止覆盖: {', '.join(existing)}"
        )


@contextmanager
def single_instance():
    """Cross-platform-ish lock using atomic directory creation."""
    lock = OUTPUT / ".daily-workflow.lock"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise WorkflowError(f"已有日报任务运行中: {lock}") from exc
    try:
        (lock / "pid").write_text(str(os.getpid()), encoding="ascii")
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def run_command(*args: str) -> None:
    print("\n$", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def tagged_file(prefix: str, tag: str, suffix: str) -> Path:
    matches = sorted(OUTPUT.glob(f"{prefix}_*_{tag}{suffix}"))
    if len(matches) != 1:
        raise WorkflowError(
            f"无法唯一定位 {prefix} 产物: {[str(path) for path in matches]}"
        )
    return matches[0]


def rename_tagged(prefix: str, tag: str, market_date: str, suffix: str, final: str) -> Path:
    source = tagged_file(prefix, tag, suffix)
    target = OUTPUT / final
    if target.exists():
        raise WorkflowError(f"目标产物已存在，拒绝覆盖: {target}")
    source.rename(target)
    return target


def patch_manifest(path: Path, market_date: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["report_date"] = market_date
    payload["market_date"] = f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:]}"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_outputs(market_date: str, run_dir: Path) -> None:
    expected = [
        OUTPUT / f"five_rankings_{market_date}_daily.csv",
        OUTPUT / f"five_rankings_{market_date}_daily.html",
        OUTPUT / f"five_rankings_{market_date}_daily_manifest.json",
        OUTPUT / f"skdj_{market_date}_daily.csv",
        OUTPUT / f"skdj_{market_date}_daily.html",
        OUTPUT / f"skdj_{market_date}_daily_manifest.json",
        OUTPUT / f"status_chain_board_{market_date}.html",
        run_dir / f"divergence_td9_{market_date}_t9.csv",
        run_dir / f"divergence_td9_{market_date}_t9.html",
        OUTPUT / "daily" / market_date,
    ]
    missing = [str(path) for path in expected if not path.exists()]
    if missing:
        raise WorkflowError(f"完成验收失败，缺少产物: {', '.join(missing)}")
    for manifest in (
        OUTPUT / f"five_rankings_{market_date}_daily_manifest.json",
        OUTPUT / f"skdj_{market_date}_daily_manifest.json",
        run_dir / "run_manifest.json",
    ):
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("report_date") != market_date:
            raise WorkflowError(f"manifest.report_date 不一致: {manifest} -> {payload.get('report_date')}")


def run_reports(inputs: list[Path], market_date: str, cache_only: bool, run_id: str) -> None:
    py = sys.executable
    tag = f"auto-{run_id}"
    input_args: list[str] = []
    for path in inputs:
        input_args += ["--nd100-input", str(path)]

    run_command(py, "five_rankings_daily.py", *input_args,
                "--output-tag", tag)
    five_csv = rename_tagged("five_rankings", tag, market_date, ".csv", f"five_rankings_{market_date}_daily.csv")
    rename_tagged("five_rankings", tag, market_date, ".html", f"five_rankings_{market_date}_daily.html")
    five_manifest = rename_tagged("five_rankings", tag, market_date, "_manifest.json", f"five_rankings_{market_date}_daily_manifest.json")
    patch_manifest(five_manifest, market_date)

    run_dir = OUTPUT / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    t9_args = [py, "run_nd100_t9_workflow.py", *input_args, "--run-id", run_id]
    if cache_only:
        t9_args.append("--cache-only")
    run_command(*t9_args)
    t9_matches = sorted(run_dir.glob("divergence_td9_*_t9.csv"))
    if len(t9_matches) != 1:
        raise WorkflowError(f"无法唯一定位 T9 CSV: {[str(path) for path in t9_matches]}")
    t9_csv = run_dir / f"divergence_td9_{market_date}_t9.csv"
    if t9_matches[0] != t9_csv:
        t9_matches[0].rename(t9_csv)
    t9_html_matches = sorted(run_dir.glob("divergence_td9_*_t9.html"))
    if len(t9_html_matches) != 1:
        raise WorkflowError(f"无法唯一定位 T9 HTML: {[str(path) for path in t9_html_matches]}")
    t9_html = run_dir / f"divergence_td9_{market_date}_t9.html"
    if t9_html_matches[0] != t9_html:
        t9_html_matches[0].rename(t9_html)
    run_manifest = run_dir / "run_manifest.json"
    if run_manifest.is_file():
        payload = json.loads(run_manifest.read_text(encoding="utf-8"))
        payload["report_date"] = market_date
        payload["market_date"] = f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:]}"
        run_manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    skdj_args = [py, "skdj_scanner.py", *input_args, "--cache-only", "--output-tag", tag]
    if not cache_only:
        skdj_args.remove("--cache-only")
    run_command(*skdj_args)
    skdj_csv = rename_tagged("skdj", tag, market_date, ".csv", f"skdj_{market_date}_daily.csv")
    rename_tagged("skdj", tag, market_date, ".html", f"skdj_{market_date}_daily.html")
    skdj_manifest = rename_tagged("skdj", tag, market_date, "_manifest.json", f"skdj_{market_date}_daily_manifest.json")
    patch_manifest(skdj_manifest, market_date)

    db = OUTPUT / "status_chain.sqlite"
    reports = ",".join(str(path) for path in (*inputs, five_csv, t9_csv, skdj_csv))
    asof = f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:]}"
    run_command(py, "status_chain_tracker.py", "--db", str(db), "ingest",
                "--reports", reports, "--asof", asof)
    run_command(py, "status_chain_tracker.py", "--db", str(db), "update",
                "--asof", asof, "--cache-only")
    run_command(py, "status_chain_tracker.py", "--db", str(db), "report",
                "--out", str(OUTPUT / f"status_chain_board_{market_date}.html"),
                "--asof", asof)
    run_command(py, "publish_daily_reports.py", "--date", market_date, "--t9-run", str(run_dir))
    verify_outputs(market_date, run_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全、可重试、不可覆盖的完整日报入口")
    parser.add_argument("--nd100-input", action="append", required=True,
                        help="已完成的 ND100 CSV，可重复传入多批")
    parser.add_argument("--cache-only", action="store_true",
                        help="下游工具只读缓存，不请求 API")
    parser.add_argument("--retry", type=int, default=1,
                        help="失败后的重试次数；默认不重试")
    parser.add_argument("--retry-delay", type=float, default=60,
                        help="重试间隔秒数")
    args = parser.parse_args(argv)
    if args.retry < 0 or args.retry_delay < 0:
        parser.error("retry 和 retry-delay 不能为负数")

    inputs = [Path(item).expanduser().resolve() for item in args.nd100_input]
    missing = [path for path in inputs if not path.is_file()]
    if missing:
        parser.error("ND100 输入不存在: " + ", ".join(map(str, missing)))
    dates = {csv_market_date(path) for path in inputs}
    if len(dates) != 1:
        raise SystemExit(f"日期门禁：输入批次日期不一致: {sorted(dates)}")
    market_date = dates.pop()

    try:
        with single_instance():
            assert_no_existing_formal_report(market_date)
            run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            for attempt in range(args.retry + 1):
                try:
                    run_reports(inputs, market_date, args.cache_only, run_id)
                    print(f"\n[completed] market_date={market_date} run_id={run_id}")
                    return 0
                except (subprocess.CalledProcessError, WorkflowError) as exc:
                    if attempt >= args.retry:
                        raise
                    print(f"[retry] attempt={attempt + 1} error={exc}; waiting {args.retry_delay}s", file=sys.stderr)
                    time.sleep(args.retry_delay)
    except (WorkflowError, subprocess.CalledProcessError) as exc:
        print(f"[blocked] {exc}", file=sys.stderr)
        return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
