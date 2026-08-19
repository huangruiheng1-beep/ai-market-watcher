#!/usr/bin/env python3
"""ND100 结果 -> 分层筛选 -> T9 的可重复工作流入口。"""

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "workflow_config.json"
DEFAULT_OUTPUT = BASE_DIR / "output" / "runs"
T9_SCRIPT = BASE_DIR / "divergence_td9_scanner.py"


def read_rows(paths):
    rows = []
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows.extend(csv.DictReader(f))
    return rows


def write_csv(path, rows):
    if not rows:
        raise ValueError("ND100 输入结果为空")
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="ND100 -> 筛选 -> T9 工作流")
    ap.add_argument(
        "--nd100-input", required=True, action="append",
        help="ND100 CSV，可重复传入两批；也可用逗号分隔多个路径",
    )
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--run-id", help="本次运行编号；默认使用当前时间")
    ap.add_argument("--cache-only", action="store_true",
                    help="T9 只读现有缓存，不请求 API")
    ap.add_argument("--no-t9", action="store_true", help="只生成筛选名单，不运行 T9")
    args = ap.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    layers = set(config.get("selection_layers", []))
    if not layers:
        raise SystemExit("配置中没有 selection_layers")

    input_paths = []
    for item in args.nd100_input:
        input_paths.extend(Path(p.strip()).resolve() for p in item.split(",") if p.strip())
    missing = [p for p in input_paths if not p.exists()]
    if missing:
        raise SystemExit("ND100 输入文件不存在: " + ", ".join(str(p) for p in missing))

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = DEFAULT_OUTPUT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    all_rows = read_rows(input_paths)
    selected = [row for row in all_rows if row.get("分层", "") in layers]
    if not selected:
        raise SystemExit("按当前筛选规则没有选出标的")
    filtered_path = run_dir / "nd100_filtered.csv"
    write_csv(filtered_path, selected)

    manifest = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input_files": [str(p) for p in input_paths],
        "selection_layers": sorted(layers),
        "nd100_total": len(all_rows),
        "t9_input_count": len(selected),
        "t9_input_tickers": [row.get("ticker", "") for row in selected],
        "filtered_csv": str(filtered_path),
        "t9_output_dir": str(run_dir),
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[工作流] ND100 输入: {len(all_rows)} 只")
    print(f"[工作流] 筛选分层: {', '.join(sorted(layers))}")
    print(f"[工作流] T9 输入: {len(selected)} 只")
    print(f"[工作流] 筛选名单: {filtered_path}")

    if not args.no_t9:
        cmd = [
            sys.executable, str(T9_SCRIPT),
            "--nd100-input", str(filtered_path),
            "--output-dir", str(run_dir),
            "--output-tag", "t9",
        ]
        if args.cache_only or config.get("t9", {}).get("cache_only_default", False):
            cmd.append("--cache-only")
        print("[工作流] 开始运行 T9")
        subprocess.run(cmd, cwd=BASE_DIR, check=True)

    print(f"[工作流] run 目录: {run_dir}")


if __name__ == "__main__":
    main()
