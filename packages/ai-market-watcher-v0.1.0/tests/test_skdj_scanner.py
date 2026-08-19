#!/usr/bin/env python3
"""
SKDJ 超跌观察池扫描器单元测试  ·  test_skdj_scanner.py
================================================================
覆盖 SKDJ_PHASE1_IMPLEMENTATION_PLAN_v0.1.md 的 10 条最低测试要求：
  1. 手算小样本验证 LOWV/HIGHV/RAW/RSV/K/D
  2. 上穿只在交叉当根为 true，不连续重复
  3. 下穿只在交叉当根为 true
  4. 20、80 边界（<= 20 / >= 80）
  5. HIGHV == LOWV 不产生假信号
  6. 数据不足不产生信号
  7. 缺列/非数字数据 fail closed
  8. --cache-only 不发网络请求
  9. synthetic 数据稳定覆盖三类展示
  10. CSV/manifest 含公式版本、参数、data_asof
"""

import json
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(BASE))
import skdj_scanner as skdj  # noqa: E402


class TestCalcSkdj(unittest.TestCase):
    """1. 手算小样本验证 LOWV/HIGHV/RAW/RSV/K/D"""

    def test_hand_computed_small_sample(self):
        # n=3, m=2, d_window=2；手算预期：
        #   LOWV  = [nan,nan, 9,10,11]
        #   HIGHV = [nan,nan,13,14,15]
        #   rng   = [nan,nan, 4, 4, 4]
        #   RAW   = [nan,nan,75,75,75]   (100*(C-LOWV)/rng)
        #   RSV   = [nan,nan,75,75,75]   (EMA2, adjust=False, 首值=自身)
        #   K     = [nan,nan,75,75,75]   (EMA2 of RSV)
        #   D     = [nan,nan,nan,75,75]  (MA(K,2))
        high = [11, 12, 13, 14, 15]
        low = [9, 10, 11, 12, 13]
        close = [10, 11, 12, 13, 14]
        out = skdj.calc_skdj(high, low, close, n=3, m=2,
                             ema_adjust=False, d_window=2)
        # LOWV / HIGHV
        self.assertTrue(np.isnan(out["raw"].iloc[0]))
        self.assertTrue(np.isnan(out["raw"].iloc[1]))
        self.assertAlmostEqual(out["raw"].iloc[2], 75.0)
        self.assertAlmostEqual(out["raw"].iloc[3], 75.0)
        self.assertAlmostEqual(out["raw"].iloc[4], 75.0)
        # RSV / K
        self.assertAlmostEqual(out["rsv"].iloc[2], 75.0)
        self.assertAlmostEqual(out["k"].iloc[2], 75.0)
        # D（窗口 2，从 idx3 起）
        self.assertTrue(np.isnan(out["d"].iloc[2]))
        self.assertAlmostEqual(out["d"].iloc[3], 75.0)
        self.assertAlmostEqual(out["k"].iloc[-1], 75.0)
        self.assertAlmostEqual(out["d"].iloc[-1], 75.0)


class TestCrossUpOnlyOnce(unittest.TestCase):
    """2. 上穿只在交叉当根为 true，不连续重复"""

    def test_cross_up_single_bar(self):
        k = [1, 2, 3, 3, 2]
        d = [2, 2, 2, 4, 4]
        cross = skdj.detect_cross(k, d)
        # 上穿只在 idx2 出现一次；idx3 转为下穿（不重复上穿），idx4 不再交叉
        self.assertEqual(cross.iloc[0], "")
        self.assertEqual(cross.iloc[1], "")
        self.assertEqual(cross.iloc[2], "上穿")
        self.assertNotEqual(cross.iloc[3], "上穿")   # 上穿不连续重复到下一根
        self.assertEqual(cross.iloc[4], "")
        self.assertEqual(list(cross).count("上穿"), 1)


class TestCrossDownOnlyOnce(unittest.TestCase):
    """3. 下穿只在交叉当根为 true"""

    def test_cross_down_single_bar(self):
        k = [1, 2, 3, 3, 2]
        d = [2, 2, 2, 4, 4]
        cross = skdj.detect_cross(k, d)
        self.assertEqual(cross.iloc[3], "下穿")
        self.assertEqual(list(cross).count("下穿"), 1)
        # idx4 不应重复下穿（prev_k=3 < d_prev=4 不成立，3>=4 False）
        self.assertEqual(cross.iloc[4], "")


class TestBoundaries(unittest.TestCase):
    """4. 20、80 边界（按 PPT 文字 <= 20 / >= 80）"""

    @staticmethod
    def _flat_df(close_val, n=65):
        # high/low 恒定且不同 -> rng 恒非零，RAW 恒定
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        df = pd.DataFrame(index=idx)
        df["High"] = [110.0] * n
        df["Low"] = [90.0] * n
        df["Close"] = [close_val] * n
        df["Open"] = [close_val] * n
        return df

    def test_boundary_20_is_oversold(self):
        # close=94 -> RAW=(94-90)/(110-90)*100=20 -> K=D=20 -> both_le_20
        df = self._flat_df(94.0)
        r = skdj.analyze_skdj("TEST", df, skdj.DEFAULT_PROFILE)
        self.assertEqual(r["data_status"], "ok")
        self.assertTrue(r["k_le_20"])
        self.assertTrue(r["d_le_20"])
        self.assertTrue(r["both_le_20"])
        # 20 是上闭界：等于 20 必须算超卖（验证 <= 而非 <）
        self.assertEqual(r["k"], 20.0)

    def test_boundary_80_is_overbought(self):
        # close=106 -> RAW=(106-90)/(110-90)*100=80 -> K=D=80 -> both_ge_80
        df = self._flat_df(106.0)
        r = skdj.analyze_skdj("TEST", df, skdj.DEFAULT_PROFILE)
        self.assertEqual(r["data_status"], "ok")
        self.assertTrue(r["k_ge_80"])
        self.assertTrue(r["d_ge_80"])
        self.assertTrue(r["both_ge_80"])
        self.assertEqual(r["k"], 80.0)


class TestHighvEqualsLowv(unittest.TestCase):
    """5. HIGHV == LOWV（一字板）不产生假信号"""

    def test_flat_board_no_signal(self):
        n = 65
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        df = pd.DataFrame(index=idx)
        v = 100.0
        df["High"] = [v] * n
        df["Low"] = [v] * n
        df["Close"] = [v] * n
        df["Open"] = [v] * n
        r = skdj.analyze_skdj("TEST", df, skdj.DEFAULT_PROFILE)
        # RAW 全为 NaN -> K/D 为 NaN -> fail closed，不产生信号
        self.assertEqual(r["data_status"], "数据不足")
        self.assertEqual(r["scenario"], "普通区")
        self.assertFalse(r["needs_human_confirmation"])
        self.assertFalse(r["both_le_20"])
        self.assertFalse(r["both_ge_80"])
        self.assertEqual(r["cross"], "无")


class TestInsufficientData(unittest.TestCase):
    """6. 数据不足不产生信号"""

    def test_too_few_bars(self):
        n = 10  # 低于 max(60, n+2m+10)=60
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        df = pd.DataFrame(index=idx)
        df["High"] = np.linspace(110, 100, n)
        df["Low"] = np.linspace(90, 95, n)
        df["Close"] = np.linspace(100, 98, n)
        df["Open"] = df["Close"]
        r = skdj.analyze_skdj("TEST", df, skdj.DEFAULT_PROFILE)
        self.assertEqual(r["data_status"], "数据不足")
        self.assertEqual(r["scenario"], "普通区")
        self.assertFalse(r["needs_human_confirmation"])
        self.assertIsNone(r["k"])


class TestFailClosed(unittest.TestCase):
    """7. 缺列/非数字 fail closed"""

    def test_missing_high_column(self):
        n = 65
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        df = pd.DataFrame(index=idx)
        df["Low"] = [90.0] * n
        df["Close"] = [100.0] * n
        r = skdj.analyze_skdj("TEST", df, skdj.DEFAULT_PROFILE)
        self.assertEqual(r["data_status"], "数据不足")

    def test_non_numeric_high(self):
        n = 65
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        df = pd.DataFrame(index=idx)
        df["High"] = ["x"] * n   # 非数字
        df["Low"] = [90.0] * n
        df["Close"] = [100.0] * n
        df["Open"] = [100.0] * n
        r = skdj.analyze_skdj("TEST", df, skdj.DEFAULT_PROFILE)
        self.assertEqual(r["data_status"], "数据不足")
        self.assertFalse(r["needs_human_confirmation"])

    def test_recent_calculation_window_with_missing_value(self):
        n = 65
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        df = pd.DataFrame(index=idx)
        df["High"] = [110.0] * n
        df["Low"] = [90.0] * n
        df["Close"] = pd.Series([100.0] * n, index=idx, dtype=object)
        df.loc[idx[-2], "Close"] = ""
        r = skdj.analyze_skdj("TEST", df, skdj.DEFAULT_PROFILE)
        self.assertEqual(r["data_status"], "数据不足")
        self.assertIsNone(r["k"])
        self.assertFalse(r["needs_human_confirmation"])


class TestCompletedDailyBar(unittest.TestCase):
    """当前美东自然日的日 K 不参与收盘结论。"""

    def test_current_day_bar_is_excluded(self):
        n = 65
        now = pd.Timestamp.now(tz="America/New_York")
        idx = pd.date_range(
            end=now.normalize() - pd.Timedelta(days=1),
            periods=n - 1,
            freq="B",
            tz="America/New_York",
        ).append(pd.DatetimeIndex([now.normalize()]))
        df = pd.DataFrame(index=idx)
        df["High"] = [110.0] * n
        df["Low"] = [90.0] * n
        df["Close"] = [100.0] * n
        df.loc[df.index[-1], "Close"] = 110.0
        r = skdj.analyze_skdj("TEST", df, skdj.DEFAULT_PROFILE)
        self.assertEqual(r["data_status"], "ok")
        self.assertEqual(r["k"], 50.0)
        self.assertNotIn(str(now.date()), r["data_asof"])


class TestHtmlSourceLabel(unittest.TestCase):
    def test_source_label_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            skdj.gen_html([], skdj.DEFAULT_PROFILE, out, source_label="真实/缓存")
            html = out.read_text(encoding="utf-8")
            self.assertIn("真实/缓存", html)
            self.assertNotIn("演示数据 · synthetic", html)


class TestCacheOnlyNoNetwork(unittest.TestCase):
    """8. --cache-only 不发网络请求"""

    def test_cache_only_skips_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("skdj_scanner.load_batch_or_download") as m:
                skdj.main(["--tickers", "AAPL", "--cache-only",
                           "--output-dir", tmp, "--output-tag", "t8"])
                # cache-only 必须只读缓存，绝不调用批量下载（即不请求 API）
                m.assert_not_called()

    def test_multiple_nd100_inputs_are_combined(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "nd100-01.csv"
            second = Path(tmp) / "nd100-02.csv"
            pd.DataFrame([{"ticker": "AAA", "分层": "偏多"}]).to_csv(first, index=False)
            pd.DataFrame([{"ticker": "BBB", "分层": "偏空"}]).to_csv(second, index=False)
            skdj.main(["--nd100-input", str(first), "--nd100-input", str(second),
                       "--cache-only", "--output-dir", tmp, "--output-tag", "multi"])
            manifest = json.loads(
                (Path(tmp) / f"skdj_{pd.Timestamp.now():%Y%m%d}_multi_manifest.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["input_ticker_count"], 2)
            self.assertEqual(manifest["input_tickers"], ["AAA", "BBB"])


class TestSyntheticCoversThreeCategories(unittest.TestCase):
    """9. synthetic 数据稳定覆盖三类展示"""

    def test_three_categories_present(self):
        rows = skdj.scan_synthetic(skdj.DEFAULT_PROFILE)
        scenarios = {r["scenario"] for r in rows}
        self.assertIn("下跌超跌", scenarios)
        self.assertIn("上升回调", scenarios)
        self.assertIn("顶部超买", scenarios)
        # 同时覆盖普通区与数据不足
        self.assertIn("普通区", scenarios)
        self.assertIn("数据不足", {r["data_status"] for r in rows})
        # AAPL 应在超卖区（both_le_20）
        aapl = next(r for r in rows if r["ticker"] == "AAPL")
        self.assertTrue(aapl["both_le_20"])
        self.assertEqual(aapl["scenario"], "下跌超跌")
        # MSFT 应在超买区
        msft = next(r for r in rows if r["ticker"] == "MSFT")
        self.assertTrue(msft["both_ge_80"])
        self.assertEqual(msft["scenario"], "顶部超买")
        # NVDA 应出现低位上穿
        nvda = next(r for r in rows if r["ticker"] == "NVDA")
        self.assertEqual(nvda["cross"], "上穿")
        self.assertEqual(nvda["scenario"], "上升回调")
        # GOOGL 应出现高位下穿
        googl = next(r for r in rows if r["ticker"] == "GOOGL")
        self.assertEqual(googl["cross"], "下穿")
        self.assertEqual(googl["scenario"], "顶部超买")


class TestCsvManifestMetadata(unittest.TestCase):
    """10. CSV/manifest 含公式版本、参数、data_asof"""

    def test_outputs_contain_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            skdj.main(["--source", "synthetic", "--output-dir", tmp,
                       "--output-tag", "t10"])
            today = pd.Timestamp.now().strftime("%Y%m%d")
            csv_path = Path(tmp) / f"skdj_{today}_t10.csv"
            man_path = Path(tmp) / f"skdj_{today}_t10_manifest.json"
            self.assertTrue(csv_path.exists())
            self.assertTrue(man_path.exists())

            df = pd.read_csv(csv_path, encoding="utf-8-sig")
            # CSV 必须含公式与关键审计字段
            for col in ("formula_profile", "n", "m", "data_asof",
                        "both_le_20", "both_ge_80", "scenario",
                        "needs_human_confirmation", "data_status"):
                self.assertIn(col, df.columns, f"CSV 缺少列 {col}")

            manifest = json.loads(man_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["formula_profile"], "candidate_skdj_9_3_v1")
            self.assertEqual(manifest["formula_status"], "candidate_unverified")
            self.assertIn("formula_params", manifest)
            self.assertEqual(manifest["formula_params"]["n"], 9)
            self.assertEqual(manifest["formula_params"]["m"], 3)
            self.assertIn("market_data_asof", manifest)
            self.assertIn("pool_counts", manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
