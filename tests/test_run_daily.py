from __future__ import annotations

import csv
from pathlib import Path

import pytest

import run_daily


def write_csv(path: Path, dates: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "60min_数据截至", "日线_数据截至"])
        writer.writeheader()
        for index, value in enumerate(dates):
            writer.writerow({"ticker": f"T{index}", "60min_数据截至": value, "日线_数据截至": value})


def test_csv_market_date_reads_real_asof(tmp_path: Path) -> None:
    path = tmp_path / "input.csv"
    write_csv(path, ["2026-08-19", "2026-08-19 16:00 ET"])
    assert run_daily.csv_market_date(path) == "20260819"


def test_csv_market_date_rejects_mixed_dates(tmp_path: Path) -> None:
    path = tmp_path / "input.csv"
    write_csv(path, ["2026-08-19", "2026-08-18"])
    with pytest.raises(run_daily.WorkflowError, match="多个日线行情日"):
        run_daily.csv_market_date(path)


def test_csv_market_date_rejects_partial_intraday_refresh(tmp_path: Path) -> None:
    path = tmp_path / "input.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "60min_数据截至", "日线_数据截至"])
        writer.writeheader()
        writer.writerow({"ticker": "AAPL", "60min_数据截至": "2026-08-19", "日线_数据截至": "2026-08-19"})
        writer.writerow({"ticker": "NVDA", "60min_数据截至": "2026-08-18", "日线_数据截至": "2026-08-19"})
    with pytest.raises(run_daily.WorkflowError, match="60m"):
        run_daily.csv_market_date(path)


def test_single_instance_blocks_second_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_daily, "OUTPUT", tmp_path)
    with run_daily.single_instance():
        with pytest.raises(run_daily.WorkflowError, match="已有日报任务"):
            with run_daily.single_instance():
                pass


def test_partial_formal_report_is_resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_daily, "OUTPUT", tmp_path)
    (tmp_path / "five_rankings_20260819_daily.csv").write_text("existing", encoding="utf-8")
    run_daily.assert_no_existing_formal_report("20260819")


def test_complete_formal_report_is_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_daily, "OUTPUT", tmp_path)
    for path in run_daily.daily_expected_outputs("20260819"):
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.name.endswith("manifest.json"):
                path.write_text('{"report_date": "20260819"}', encoding="utf-8")
            else:
                path.write_text("complete", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
    with pytest.raises(run_daily.WorkflowError, match="完整正式日报"):
        run_daily.assert_no_existing_formal_report("20260819")
