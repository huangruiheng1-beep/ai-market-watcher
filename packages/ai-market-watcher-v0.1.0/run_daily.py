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
EXPECTED_ND100_COUNT = 102


class WorkflowError(RuntimeError):
    pass


def input_tickers(paths: list[Path]) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        current = [str(row.get("ticker") or "").strip().upper() for row in rows]
        current = [ticker for ticker in current if ticker]
        if len(current) != len(set(current)):
            raise WorkflowError(f"输入文件存在重复 ticker: {path}")
        for ticker in current:
            if ticker in seen:
                raise WorkflowError(f"批次之间存在重复 ticker: {ticker}")
            seen.add(ticker)
            tickers.append(ticker)
    if not tickers:
        raise WorkflowError("ND100 输入没有可用 ticker")
    if len(tickers) != EXPECTED_ND100_COUNT:
        raise WorkflowError(
            f"ND100 输入不完整：当前 {len(tickers)}/{EXPECTED_ND100_COUNT} 只；"
            "partial universe 只能继续扫描，禁止进入正式日报"
        )
    return tickers


def validate_existing_scope(market_date: str, expected: set[str]) -> None:
    checks = [
        (OUTPUT / f"five_rankings_{market_date}_daily_manifest.json", "五榜单", "ticker_count", "tickers"),
        (OUTPUT / f"skdj_{market_date}_daily_manifest.json", "SKDJ", "input_ticker_count", "input_tickers"),
    ]
    for manifest, label, count_key, tickers_key in checks:
        if not manifest.is_file():
            continue
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        actual = {str(value).strip().upper() for value in payload.get(tickers_key, []) if str(value).strip()}
        if payload.get(count_key) != len(expected) or actual != expected:
            raise WorkflowError(
                f"{label}与本次 ND100 输入范围不一致：应为 {len(expected)} 只，"
                f"实际为 {payload.get(count_key)} 只；请不要复用旧报告，先清理/归档旧派生报告后重跑。"
            )


def daily_expected_outputs(market_date: str) -> list[Path]:
    return [
        OUTPUT / f"five_rankings_{market_date}_daily.csv",
        OUTPUT / f"five_rankings_{market_date}_daily.html",
        OUTPUT / f"five_rankings_{market_date}_daily_manifest.json",
        OUTPUT / f"skdj_{market_date}_daily.csv",
        OUTPUT / f"skdj_{market_date}_daily.html",
        OUTPUT / f"skdj_{market_date}_daily_manifest.json",
        OUTPUT / f"status_chain_board_{market_date}.html",
        OUTPUT / "daily" / market_date,
        OUTPUT / "daily" / market_date / f"divergence_td9_{market_date}.csv",
        OUTPUT / "daily" / market_date / f"divergence_td9_{market_date}.html",
        OUTPUT / "daily" / market_date / f"divergence_td9_{market_date}_manifest.json",
    ]


def daily_report_complete(market_date: str) -> bool:
    if not all(path.exists() for path in daily_expected_outputs(market_date)):
        return False
    for manifest in (
        OUTPUT / f"five_rankings_{market_date}_daily_manifest.json",
        OUTPUT / f"skdj_{market_date}_daily_manifest.json",
    ):
        try:
            if json.loads(manifest.read_text(encoding="utf-8")).get("report_date") != market_date:
                return False
        except (OSError, json.JSONDecodeError):
            return False
    return True


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
    if daily_report_complete(market_date):
        raise WorkflowError(f"日期门禁：{market_date} 已有完整正式日报，自动复用而不是覆盖")


@contextmanager
def single_instance():
    """Cross-platform-ish lock using atomic directory creation."""
    lock = OUTPUT / ".daily-workflow.lock"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError as exc:
        pid_path = lock / "pid"
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
            os.kill(pid, 0)
        except (FileNotFoundError, ValueError, ProcessLookupError):
            shutil.rmtree(lock, ignore_errors=True)
            lock.mkdir()
            print(f"[resume] 清理过期日报锁: {lock}")
        except PermissionError:
            raise WorkflowError(f"无法确认日报锁状态，拒绝并发运行: {lock}") from exc
        else:
            raise WorkflowError(f"已有日报任务运行中: {lock} pid={pid}") from exc
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
    expected = daily_expected_outputs(market_date) + [
        run_dir / f"divergence_td9_{market_date}_t9.csv",
        run_dir / f"divergence_td9_{market_date}_t9.html",
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

    five_csv = OUTPUT / f"five_rankings_{market_date}_daily.csv"
    five_parts = [
        five_csv,
        OUTPUT / f"five_rankings_{market_date}_daily.html",
        OUTPUT / f"five_rankings_{market_date}_daily_manifest.json",
    ]
    if all(path.exists() for path in five_parts):
        print(f"[resume] 五榜单已完成，跳过: {market_date}")
    else:
        five_args = [*input_args, "--output-tag", tag]
        if cache_only:
            five_args.append("--cache-only")
        run_command(py, "five_rankings_daily.py", *five_args)
        five_csv = rename_tagged("five_rankings", tag, market_date, ".csv", f"five_rankings_{market_date}_daily.csv")
        rename_tagged("five_rankings", tag, market_date, ".html", f"five_rankings_{market_date}_daily.html")
        five_manifest = rename_tagged("five_rankings", tag, market_date, "_manifest.json", f"five_rankings_{market_date}_daily_manifest.json")
        patch_manifest(five_manifest, market_date)

    run_dir = OUTPUT / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    t9_args = [py, "run_nd100_t9_workflow.py", *input_args, "--run-id", run_id]
    if cache_only:
        t9_args.append("--cache-only")
    existing_t9 = sorted(
        path for path in (OUTPUT / "runs").glob("*")
        if path.is_dir()
        and (path / f"divergence_td9_{market_date}_t9.csv").is_file()
        and (path / f"divergence_td9_{market_date}_t9.html").is_file()
    )
    if existing_t9:
        run_dir = existing_t9[-1]
        t9_csv = run_dir / f"divergence_td9_{market_date}_t9.csv"
        print(f"[resume] T9 已完成，继续使用: {run_dir}")
    else:
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
    skdj_csv = OUTPUT / f"skdj_{market_date}_daily.csv"
    skdj_parts = [
        skdj_csv,
        OUTPUT / f"skdj_{market_date}_daily.html",
        OUTPUT / f"skdj_{market_date}_daily_manifest.json",
    ]
    if all(path.exists() for path in skdj_parts):
        print(f"[resume] SKDJ 已完成，跳过: {market_date}")
    else:
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
    expected_tickers = set(input_tickers(inputs))

    try:
        with single_instance():
            validate_existing_scope(market_date, expected_tickers)
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
