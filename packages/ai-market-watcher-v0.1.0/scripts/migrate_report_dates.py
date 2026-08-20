#!/usr/bin/env python3
"""Migrate formal report references after an audited date correction.

The caller must provide the old and verified market dates explicitly.  This
tool changes report names, report_date/market_date fields and HTML titles. It
deliberately preserves created_at, scan_date and run_id: those are execution
facts, not market dates.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path


TEXT_SUFFIXES = {".html", ".json", ".js"}
REPORT_PREFIXES = ("nd100_resonance", "five_rankings", "skdj", "divergence_td9")


def date8(raw: str) -> str:
    value = raw.replace("-", "")
    datetime.strptime(value, "%Y%m%d")
    return value


def replace_refs(value: str, old: str, new: str) -> str:
    for prefix in REPORT_PREFIXES:
        value = value.replace(f"{prefix}_{old}", f"{prefix}_{new}")
    return value


def rewrite_json(value, old: str, new: str):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            new_key = replace_refs(key, old, new) if isinstance(key, str) else key
            if key in {"report_date", "published_daily_date"} and str(item) == old:
                result[new_key] = new
            elif key == "market_date" and str(item) in {old, f"{old[:4]}-{old[4:6]}-{old[6:]}"}:
                result[new_key] = f"{new[:4]}-{new[4:6]}-{new[6:]}"
            else:
                result[new_key] = rewrite_json(item, old, new)
        return result
    if isinstance(value, list):
        return [rewrite_json(item, old, new) for item in value]
    return replace_refs(value, old, new) if isinstance(value, str) else value


def migrate(path: Path, old: str, new: str, backup_root: Path) -> bool:
    if path.suffix not in TEXT_SUFFIXES:
        return False
    raw = path.read_text(encoding="utf-8")
    old_dash = f"{old[:4]}-{old[4:6]}-{old[6:]}"
    if old not in raw and old_dash not in raw:
        return False
    backup = backup_root / str(path).lstrip("/").replace("/", "__")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    if path.suffix == ".json":
        updated = json.dumps(rewrite_json(json.loads(raw), old, new), ensure_ascii=False, indent=2) + "\n"
    else:
        updated = replace_refs(raw, old, new)
        new_dash = f"{new[:4]}-{new[4:6]}-{new[6:]}"
        updated = re.sub(rf"(报|报告|日报|扫描|观察池[^<]*?· ){old_dash}", rf"\1行情日 {new_dash}", updated)
    path.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移经审计的正式报告行情日期")
    parser.add_argument("--root", action="append", required=True, help="报告根目录，可重复")
    parser.add_argument("--from-date", required=True, help="旧行情日 YYYYMMDD")
    parser.add_argument("--to-date", required=True, help="已核验的新行情日 YYYYMMDD")
    parser.add_argument("--backup-dir", required=True, help="写入迁移前备份的目录")
    parser.add_argument("--apply", action="store_true", help="实际写入；默认只审计")
    args = parser.parse_args()
    old, new = date8(args.from_date), date8(args.to_date)
    if old == new:
        parser.error("新旧日期不能相同")
    roots = [Path(item).expanduser().resolve() for item in args.root]
    backup_root = Path(args.backup_dir).expanduser().resolve()
    candidates = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in TEXT_SUFFIXES:
                raw = path.read_text(encoding="utf-8", errors="ignore")
                if old in raw or f"{old[:4]}-{old[4:6]}-{old[6:]}" in raw:
                    candidates.append(path)
    print(f"[audit] candidates={len(candidates)} from={old} to={new}")
    for path in candidates:
        print(f"  {path}")
    if not args.apply:
        print("[dry-run] 未写入；追加 --apply 执行迁移")
        return 0
    changed = sum(migrate(path, old, new, backup_root) for path in candidates)
    print(f"[applied] changed={changed} backup={backup_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
