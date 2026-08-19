#!/usr/bin/env python3
"""Validate the generated release candidate without network access."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "packages" / "ai-market-watcher-v0.1.0"
ZIP_PATH = ROOT / "packages" / "ai-market-watcher-v0.1.0.zip"

FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "cache",
    "output",
    "roadshow",
}
FORBIDDEN_TEXT = re.compile(
    r"/Users/|(?:^|[=:\s])sk-[A-Za-z0-9]{20,}|(?:^|[=:\s])ghp_[A-Za-z0-9]{20,}|(?:^|[=:\s])AIza[A-Za-z0-9_-]{20,}|-----BEGIN .* PRIVATE KEY-----",
    re.IGNORECASE,
)


def fail(message: str) -> None:
    raise AssertionError(message)


def validate_tree() -> None:
    if not PACKAGE_ROOT.is_dir():
        fail("release directory is missing; run scripts/build_release.py first")
    if not ZIP_PATH.is_file():
        fail("release zip is missing; run scripts/build_release.py first")

    files = [path for path in PACKAGE_ROOT.rglob("*") if path.is_file()]
    if not files:
        fail("release directory is empty")
    for path in files:
        relative_parts = set(path.relative_to(PACKAGE_ROOT).parts)
        if relative_parts & FORBIDDEN_PARTS:
            fail(f"forbidden path in release: {path.relative_to(PACKAGE_ROOT)}")
        if path.suffix in {".sqlite", ".sqlite3", ".key", ".pem"}:
            fail(f"forbidden file type in release: {path.relative_to(PACKAGE_ROOT)}")
        if path.name == "validate_release.py":
            # This file necessarily contains the forbidden-pattern regex itself.
            continue
        if path.suffix in {".md", ".py", ".json", ".example", ".txt"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if FORBIDDEN_TEXT.search(text):
                fail(f"possible private path or credential in: {path.relative_to(PACKAGE_ROOT)}")


def validate_zip() -> None:
    with ZipFile(ZIP_PATH) as archive:
        names = archive.namelist()
        if not names:
            fail("release zip is empty")
        for name in names:
            parts = set(Path(name).parts)
            if parts & FORBIDDEN_PARTS:
                fail(f"forbidden path in zip: {name}")
            if name.endswith((".sqlite", ".sqlite3", ".key", ".pem")):
                fail(f"forbidden file type in zip: {name}")


def main() -> int:
    validate_tree()
    validate_zip()
    print(f"[ok] release candidate: {PACKAGE_ROOT}")
    print(f"[ok] archive: {ZIP_PATH}")
    print("[ok] no cache/output/SQLite/roadshow/private-path artifacts found")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"[FAIL] {error}", file=sys.stderr)
        raise SystemExit(1)
