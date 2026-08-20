#!/usr/bin/env python3
"""One-command offline demo. No API key and no network access required."""

from __future__ import annotations

import subprocess
import sys
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output" / "demo"
DB = OUT / "status_chain_demo.sqlite"
ASOF = "2026-08-18"


def run(*args: str) -> None:
    print("\n$", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    # 只清理本 demo 自己生成的文件，避免旧的系统日期文件与固定样例混在一起。
    for path in OUT.glob("*_demo*"):
        if path.is_file():
            path.unlink()

    py = sys.executable
    run(py, "five_rankings_daily.py", "--source", "synthetic",
        "--output-dir", str(OUT), "--output-tag", "demo")
    run(py, "divergence_td9_scanner.py", "--source", "synthetic",
        "--output-dir", str(OUT), "--output-tag", "demo")
    run(py, "skdj_scanner.py", "--source", "synthetic",
        "--output-dir", str(OUT), "--output-tag", "demo")

    reports = ",".join(str(ROOT / "demo_data" / name) for name in (
        "nd100_resonance_demo.csv",
        "five_rankings_demo.csv",
        "divergence_td9_demo.csv",
        "skdj_demo.csv",
    ))
    run(py, "status_chain_tracker.py", "--db", str(DB), "ingest",
        "--reports", reports, "--asof", ASOF)
    board = OUT / "status_chain_demo.html"
    run(py, "status_chain_tracker.py", "--db", str(DB), "report",
        "--out", str(board), "--asof", ASOF)
    real_sample = OUT / "status_chain_real_sample.html"
    shutil.copyfile(
        ROOT / "demo_data" / "status_chain_board_real_sample.html",
        real_sample,
    )

    print("\n[demo ready]")
    print(f"  Rebuilt tracker board: {board}")
    print(f"  Preserved real-data sample: {real_sample}")
    print(f"  Other synthetic reports: {OUT}")
    print("  This demo used no API key and made no market-data request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
