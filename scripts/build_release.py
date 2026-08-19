#!/usr/bin/env python3
"""Build a small, reproducible public release candidate from the source tree."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "packages"
VERSION = "v0.1.0"
PACKAGE_NAME = f"ai-market-watcher-{VERSION}"
PACKAGE_ROOT = PACKAGES / PACKAGE_NAME
ZIP_PATH = PACKAGES / f"{PACKAGE_NAME}.zip"
MANIFEST_PATH = PACKAGES / "SHA256SUMS.txt"

# Explicit allowlist: local state, generated output, credentials and internal
# handoff material are never copied into the public package.
FILES = (
    ".env.example",
    ".gitignore",
    "README.md",
    "requirements.txt",
    "workflow_config.json",
    "demo_data/README.md",
    "demo_data/divergence_td9_demo.csv",
    "demo_data/five_rankings_demo.csv",
    "demo_data/nd100_resonance_demo.csv",
    "demo_data/skdj_demo.csv",
    "demo_data/status_chain_board_real_sample.html",
    "docs/ARCHITECTURE.md",
    "divergence_td9_scanner.py",
    "five_rankings_daily.py",
    "nd100_resonance_scanner.py",
    "run_demo.py",
    "run_live.py",
    "run_nd100_t9_workflow.py",
    "skdj_scanner.py",
    "status_chain_ingest.py",
    "status_chain_rules.py",
    "status_chain_tracker.py",
    "tests/test_skdj_scanner.py",
    "tests/test_status_chain.py",
    "scripts/build_release.py",
    "tests/validate_release.py",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reset_generated_output() -> None:
    PACKAGES.mkdir(exist_ok=True)
    if PACKAGE_ROOT.exists():
        shutil.rmtree(PACKAGE_ROOT)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    if MANIFEST_PATH.exists():
        MANIFEST_PATH.unlink()
    PACKAGE_ROOT.mkdir(parents=True)


def copy_sources() -> None:
    for relative in FILES:
        source = ROOT / relative
        if not source.is_file():
            raise FileNotFoundError(f"missing source file: {relative}")
        target = PACKAGE_ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def write_build_manifest() -> None:
    rows = [
        f"# {PACKAGE_NAME} build manifest",
        "",
        "This directory was generated from an explicit source allowlist.",
        "Local credentials, cache, SQLite, generated output and roadshow handoff files are excluded.",
        "",
        "| File | SHA-256 |",
        "|---|---|",
    ]
    for path in sorted(PACKAGE_ROOT.rglob("*")):
        if path.is_file():
            rows.append(f"| `{path.relative_to(PACKAGE_ROOT)}` | `{sha256(path)}` |")
    (PACKAGE_ROOT / "BUILD_MANIFEST.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def zip_package() -> None:
    with ZipFile(ZIP_PATH, "w", ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE_ROOT.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(PACKAGES))


def write_sha256sums() -> None:
    MANIFEST_PATH.write_text(
        f"{sha256(ZIP_PATH)}  {ZIP_PATH.name}\n",
        encoding="utf-8",
    )


def main() -> int:
    reset_generated_output()
    copy_sources()
    write_build_manifest()
    zip_package()
    write_sha256sums()
    print(f"[package] {PACKAGE_ROOT}")
    print(f"[zip] {ZIP_PATH}")
    print(f"[sha256] {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
