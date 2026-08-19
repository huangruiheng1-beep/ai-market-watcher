#!/usr/bin/env python3
"""
底部状态链追踪器单元测试  ·  test_status_chain.py
================================================================
覆盖 TOOL5_底部状态链追踪器_实施计划.md 的 10 条最低测试要求：
  1. NOISE → OBSERVE：左侧信号触发进入观察，开新 episode
  2. OBSERVE → BASE：结构改善触发，且写定了 invalidation_level
  3. BASE → INVALID：价格破失效线，记录 invalidation_reason
  4. TRIGGER → CONFIRM：突破+量能，移交确认
  5. 降级：BASE → OBSERVE、TRIGGER → BASE（并覆盖 OBSERVE → NOISE）
  6. EXPIRED：远离未确认触发，且与 INVALID 分开统计
  7. 重新激活：INVALID/EXPIRED → OBSERVE 开新 episode（episode_id 变化）
  8. transition_date 与 created_at 分离，报告显示行情截至日期
  9. 回放：按 transitions 能还原某 episode 完整状态链
 10. 缺数据 ticker 不写入活跃状态、不中断整批

纯逻辑，synthetic SignalPack，:memory: SQLite，不联网、不读 parquet、不碰 API。
"""

import unittest
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import status_chain_rules as R  # noqa: E402
from status_chain_rules import (  # noqa: E402
    SignalPack, compute_invalidation_level,
    NOISE, OBSERVE, BASE, TRIGGER, CONFIRM, INVALID, EXPIRED,
    T_OPEN, T_UPGRADE, T_CONFIRM, T_DOWNGRADE, T_INVALIDATE, T_EXPIRE, T_REACTIVATE,
    TRIG_INVALIDATION, TRIG_MOVED_AWAY, TRIG_DATA_INSUFFICIENT,
)
from status_chain_tracker import (  # noqa: E402
    StatusChainDB, StatusChainEngine, gen_board_html, DISCLAIMER,
)

try:
    import status_chain_ingest as ingest  # noqa: E402
    import pandas as pd  # noqa: E402
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


def _new_engine():
    """每个测试用独立的 :memory: 库，互不污染。"""
    db = StatusChainDB(":memory:")
    return db, StatusChainEngine(db)


def _pack(ticker, date, **kw):
    """构造 SignalPack 的简写，默认 data_sufficient=True。"""
    kw.setdefault("source_report", "synthetic")
    return SignalPack(ticker, date, **kw)


class _ChainCase(unittest.TestCase):
    """每个测试用独立 :memory: 库，tearDown 自动关闭，消除 ResourceWarning。"""

    def setUp(self):
        self.db, self.eng = _new_engine()

    def tearDown(self):
        self.db.close()


class _StepMixin:
    """把常见状态链前缀封装成辅助方法，减少测试样板。"""

    @staticmethod
    def to_observe(eng, ticker, date="2026-08-10", close=100.0, tag="SKDJ_oversold"):
        return eng.step(ticker, _pack(ticker, date, has_left_signal=True,
                                      left_signal_tags=[tag], close=close))

    @staticmethod
    def to_base(eng, ticker, date="2026-08-12", structure_low=98.0, close=101.0):
        return eng.step(ticker, _pack(ticker, date, structure_improved=True,
                                      structure_low=structure_low, close=close))

    @staticmethod
    def to_trigger(eng, ticker, date="2026-08-14", close=105.0):
        return eng.step(ticker, _pack(ticker, date, approaching_trigger=True,
                                      close=close))

    @staticmethod
    def to_confirm(eng, ticker, date="2026-08-16", close=108.0):
        return eng.step(ticker, _pack(ticker, date, breakout_confirmed=True,
                                      close=close))


# ============================================================
# 1. NOISE → OBSERVE
# ============================================================
class TestNoiseToObserve(_ChainCase, _StepMixin):

    def test_left_signal_opens_observe_and_episode(self):
        db, eng = self.db, self.eng
        d = self.to_observe(eng, "AAPL")
        self.assertEqual(d.next_state, OBSERVE)
        self.assertEqual(d.transition_type, T_OPEN)
        self.assertTrue(d.open_new_episode)

        row = db.get_ticker("AAPL")
        self.assertEqual(row["current_state"], OBSERVE)
        self.assertIsNotNone(row["current_episode_id"])

        eps = db.get_ticker_episodes("AAPL")
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["start_date"], "2026-08-10")
        self.assertIsNone(eps[0]["end_date"])  # 仍活跃


# ============================================================
# 2. OBSERVE → BASE 写定失效线
# ============================================================
class TestObserveToBase(_ChainCase, _StepMixin):

    def test_structure_improvement_sets_invalidation(self):
        db, eng = self.db, self.eng
        self.to_observe(eng, "AAPL", close=100.0)
        d = self.to_base(eng, "AAPL", structure_low=98.0, close=101.0)

        self.assertEqual(d.next_state, BASE)
        self.assertEqual(d.transition_type, T_UPGRADE)
        expected_inv = compute_invalidation_level(98.0)  # 98 * 0.99 = 97.02
        self.assertAlmostEqual(d.invalidation_level, expected_inv, places=4)

        row = db.get_ticker("AAPL")
        self.assertEqual(row["current_state"], BASE)
        self.assertAlmostEqual(row["invalidation_level"], expected_inv, places=4)


# ============================================================
# 3. BASE → INVALID 记录原因
# ============================================================
class TestBaseToInvalid(_ChainCase, _StepMixin):

    def test_close_breach_invalidates_with_reason(self):
        db, eng = self.db, self.eng
        self.to_observe(eng, "AAPL", close=100.0)
        self.to_base(eng, "AAPL", structure_low=98.0, close=101.0)
        inv = db.get_ticker("AAPL")["invalidation_level"]  # 97.02

        # 收盘 95 < 97.02 → 破位
        d = eng.step("AAPL", _pack("AAPL", "2026-08-14", close=95.0))
        self.assertEqual(d.next_state, INVALID)
        self.assertEqual(d.transition_type, T_INVALIDATE)
        self.assertEqual(d.trigger_signal, TRIG_INVALIDATION)
        self.assertIsNotNone(d.invalidation_reason)
        self.assertIn("95", d.invalidation_reason)
        self.assertIn(str(inv), d.invalidation_reason)

        # transition 留痕
        row = db.conn.execute(
            "SELECT * FROM transitions WHERE ticker='AAPL' AND to_state=?",
            (INVALID,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["invalidation_reason"], d.invalidation_reason)

        # episode 关闭
        ep = db.get_episode(db.get_ticker("AAPL")["current_episode_id"])
        self.assertEqual(ep["end_reason"], "invalidated")
        self.assertEqual(ep["outcome"], "invalidated")


# ============================================================
# 4. TRIGGER → CONFIRM
# ============================================================
class TestTriggerToConfirm(_ChainCase, _StepMixin):

    def test_breakout_confirms_and_closes_episode(self):
        db, eng = self.db, self.eng
        self.to_observe(eng, "AAPL")
        self.to_base(eng, "AAPL")
        self.to_trigger(eng, "AAPL")
        d = self.to_confirm(eng, "AAPL")

        self.assertEqual(d.next_state, CONFIRM)
        self.assertEqual(d.transition_type, T_CONFIRM)

        row = db.get_ticker("AAPL")
        self.assertEqual(row["current_state"], CONFIRM)
        self.assertEqual(row["needs_human_review"], 1)

        ep = db.get_episode(row["current_episode_id"])
        self.assertEqual(ep["end_reason"], "confirmed")
        self.assertEqual(ep["outcome"], "confirmed_transfer")


# ============================================================
# 5. 降级：BASE → OBSERVE、TRIGGER → BASE、OBSERVE → NOISE
# ============================================================
class TestDowngrades(_ChainCase, _StepMixin):

    def test_base_to_observe_on_structure_degrade(self):
        db, eng = self.db, self.eng
        self.to_observe(eng, "AAPL")
        self.to_base(eng, "AAPL")
        d = eng.step("AAPL", _pack("AAPL", "2026-08-14", structure_degraded=True,
                                   close=100.0))
        self.assertEqual(d.next_state, OBSERVE)
        self.assertEqual(d.transition_type, T_DOWNGRADE)
        # 降回 OBSERVE 失效线清空
        self.assertIsNone(d.invalidation_level)
        self.assertIsNone(db.get_ticker("AAPL")["invalidation_level"])

    def test_trigger_to_base_on_structure_degrade(self):
        db, eng = self.db, self.eng
        self.to_observe(eng, "AAPL")
        self.to_base(eng, "AAPL", structure_low=98.0)
        self.to_trigger(eng, "AAPL")
        inv_before = db.get_ticker("AAPL")["invalidation_level"]
        d = eng.step("AAPL", _pack("AAPL", "2026-08-15", structure_degraded=True,
                                   close=102.0))
        self.assertEqual(d.next_state, BASE)
        self.assertEqual(d.transition_type, T_DOWNGRADE)
        # 退回 BASE 仍带失效线
        self.assertAlmostEqual(d.invalidation_level, inv_before, places=4)

    def test_observe_to_noise_on_signal_disappear(self):
        db, eng = self.db, self.eng
        self.to_observe(eng, "AAPL")
        d = eng.step("AAPL", _pack("AAPL", "2026-08-12", signal_disappeared=True,
                                   close=100.0))
        self.assertEqual(d.next_state, NOISE)
        self.assertEqual(d.transition_type, T_DOWNGRADE)
        self.assertEqual(db.get_ticker("AAPL")["current_state"], NOISE)


# ============================================================
# 6. EXPIRED 与 INVALID 分开统计
# ============================================================
class TestExpiredSeparateFromInvalid(_ChainCase, _StepMixin):

    def test_expired_vs_invalid_separately_counted(self):
        db, eng = self.db, self.eng
        # AAA: 走到 INVALID
        self.to_observe(eng, "AAA", close=100.0)
        self.to_base(eng, "AAA", structure_low=98.0, close=101.0)
        eng.step("AAA", _pack("AAA", "2026-08-14", close=95.0))  # 破位 -> INVALID

        # BBB: OBSERVE 后远离未确认 -> EXPIRED
        self.to_observe(eng, "BBB", close=100.0)
        eng.step("BBB", _pack("BBB", "2026-08-14", moved_away=True, close=109.0))

        self.assertEqual(db.get_ticker("AAA")["current_state"], INVALID)
        self.assertEqual(db.get_ticker("BBB")["current_state"], EXPIRED)

        # trigger_signal 区分
        sig_a = db.conn.execute(
            "SELECT trigger_signal FROM transitions WHERE ticker='AAA' AND to_state=?",
            (INVALID,)).fetchone()["trigger_signal"]
        sig_b = db.conn.execute(
            "SELECT trigger_signal FROM transitions WHERE ticker='BBB' AND to_state=?",
            (EXPIRED,)).fetchone()["trigger_signal"]
        self.assertEqual(sig_a, TRIG_INVALIDATION)
        self.assertEqual(sig_b, TRIG_MOVED_AWAY)

        # stats 分开统计：invalidated / expired 各 1 条，confirmed 0 条
        st = eng.stats()
        self.assertEqual(st["episodes_closed_by_outcome"]["invalidated"]["n"], 1)
        self.assertEqual(st["episodes_closed_by_outcome"]["expired"]["n"], 1)
        self.assertEqual(st["total_closed"], 2)
        self.assertEqual(st["confirmed_share"], 0.0)
        self.assertGreater(st["invalidated_share"], 0.0)
        self.assertGreater(st["expired_share"], 0.0)


# ============================================================
# 7. 重新激活：INVALID/EXPIRED → OBSERVE 开新 episode
# ============================================================
class TestReactivate(_ChainCase, _StepMixin):

    def test_invalid_then_reactivate_opens_new_episode(self):
        db, eng = self.db, self.eng
        self.to_observe(eng, "AAPL", close=100.0)
        self.to_base(eng, "AAPL", structure_low=98.0, close=101.0)
        eng.step("AAPL", _pack("AAPL", "2026-08-14", close=95.0))  # -> INVALID
        old_ep = db.get_ticker("AAPL")["current_episode_id"]

        d = eng.step("AAPL", _pack("AAPL", "2026-09-01",
                                   has_left_signal=True,
                                   left_signal_tags=["divergence_rsi"],
                                   close=92.0))
        self.assertEqual(d.next_state, OBSERVE)
        self.assertEqual(d.transition_type, T_REACTIVATE)
        self.assertTrue(d.open_new_episode)

        new_ep = db.get_ticker("AAPL")["current_episode_id"]
        self.assertIsNotNone(new_ep)
        self.assertNotEqual(new_ep, old_ep)

        # 旧 episode 已关闭，新 episode 活跃
        self.assertEqual(db.get_episode(old_ep)["end_reason"], "invalidated")
        self.assertIsNone(db.get_episode(new_ep)["end_date"])

    def test_expired_then_reactivate_opens_new_episode(self):
        db, eng = self.db, self.eng
        self.to_observe(eng, "AAPL", close=100.0)
        eng.step("AAPL", _pack("AAPL", "2026-08-14", moved_away=True, close=109.0))
        old_ep = db.get_ticker("AAPL")["current_episode_id"]

        d = eng.step("AAPL", _pack("AAPL", "2026-09-01",
                                   has_left_signal=True,
                                   left_signal_tags=["td9"], close=105.0))
        self.assertEqual(d.next_state, OBSERVE)
        self.assertEqual(d.transition_type, T_REACTIVATE)
        self.assertNotEqual(db.get_ticker("AAPL")["current_episode_id"], old_ep)


# ============================================================
# 8. transition_date 与 created_at 分离
# ============================================================
class TestDateSeparation(_ChainCase, _StepMixin):

    def test_transition_date_neq_created_at(self):
        db, eng = self.db, self.eng
        gen_time = "2026-08-19T00:00:00Z"   # 生成时间
        asof = "2026-08-10"                  # 行情截至
        eng.step("AAPL", _pack("AAPL", asof, has_left_signal=True,
                               left_signal_tags=["SKDJ_oversold"], close=100.0),
                 created_at=gen_time)

        t = db.conn.execute(
            "SELECT transition_date, evidence_date, created_at FROM transitions "
            "WHERE ticker='AAPL' ORDER BY transition_id").fetchone()
        self.assertEqual(t["transition_date"], asof)      # 行情截至
        self.assertEqual(t["evidence_date"], asof)
        self.assertEqual(t["created_at"], gen_time)       # 生成时间
        self.assertNotEqual(t["transition_date"], t["created_at"])

        # tickers: last_seen(行情截至) != updated_at(生成)
        row = db.get_ticker("AAPL")
        self.assertEqual(row["last_seen"], asof)
        self.assertEqual(row["updated_at"], gen_time)


# ============================================================
# 9. 回放：按 transitions 还原 episode 完整状态链
# ============================================================
class TestReplay(_ChainCase, _StepMixin):

    def test_replay_reconstructs_full_chain(self):
        db, eng = self.db, self.eng
        self.to_observe(eng, "AAPL", date="2026-08-10", close=100.0)
        self.to_base(eng, "AAPL", date="2026-08-12", structure_low=98.0, close=101.0)
        self.to_trigger(eng, "AAPL", date="2026-08-14", close=105.0)
        self.to_confirm(eng, "AAPL", date="2026-08-16", close=108.0)

        replay = eng.replay("AAPL")
        self.assertEqual(len(replay), 1)  # CONFIRM 关闭 episode，单条链
        ep = replay[0]
        self.assertEqual(ep["end_reason"], "confirmed")
        self.assertEqual(ep["outcome"], "confirmed_transfer")

        # 按 transition_date 还原状态序列
        seq = [t["to_state"] for t in ep["transitions"]]
        self.assertEqual(seq, [OBSERVE, BASE, TRIGGER, CONFIRM])

        # 日期严格递增（回放顺序 = 行情截至顺序）
        dates = [t["transition_date"] for t in ep["transitions"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(dates, ["2026-08-10", "2026-08-12",
                                 "2026-08-14", "2026-08-16"])

        # 每节点带证据
        for t in ep["transitions"]:
            self.assertIsNotNone(t["evidence_date"])
            self.assertIsNotNone(t["trigger_signal"])


# ============================================================
# 10. 缺数据 fail closed，不写入活跃状态、不中断整批
# ============================================================
class TestDataInsufficientFailClosed(_ChainCase, _StepMixin):

    def test_insufficient_data_stays_noise_and_does_not_break_batch(self):
        db, eng = self.db, self.eng
        # AAA 数据不足
        d_a = eng.step("AAA", _pack("AAA", "2026-08-10", data_sufficient=False,
                                    close=100.0))
        self.assertEqual(d_a.next_state, NOISE)
        self.assertEqual(d_a.trigger_signal, TRIG_DATA_INSUFFICIENT)
        self.assertEqual(db.get_ticker("AAA")["current_state"], NOISE)
        # 不开 episode、不写活跃快照状态
        self.assertEqual(len(db.get_ticker_episodes("AAA")), 0)

        # BBB 正常，不受 AAA 影响
        d_b = self.to_observe(eng, "BBB", close=50.0)
        self.assertEqual(d_b.next_state, OBSERVE)
        self.assertEqual(db.get_ticker("BBB")["current_state"], OBSERVE)
        self.assertEqual(len(db.get_ticker_episodes("BBB")), 1)

        # 整批两只都被处理
        self.assertEqual(
            db.conn.execute("SELECT COUNT(*) FROM tickers").fetchone()[0], 2)

    def test_insufficient_data_on_active_state_does_not_advance(self):
        # 已在 BASE 的股票若某日数据不足，应保持 BASE 不推进、不误判破位、不丢失效线
        db, eng = self.db, self.eng
        self.to_observe(eng, "AAPL", close=100.0)
        self.to_base(eng, "AAPL", structure_low=98.0, close=101.0)
        inv = db.get_ticker("AAPL")["invalidation_level"]

        d = eng.step("AAPL", _pack("AAPL", "2026-08-14", data_sufficient=False,
                                   close=None))
        self.assertEqual(d.next_state, BASE)            # 保持原状
        self.assertEqual(d.trigger_signal, TRIG_DATA_INSUFFICIENT)
        self.assertAlmostEqual(d.invalidation_level, inv, places=4)
        self.assertEqual(db.get_ticker("AAPL")["current_state"], BASE)


# ============================================================
# 阶段 B：信号接入 + 价格确认（ingest）
# ============================================================
@unittest.skipUnless(HAS_PANDAS, "pandas required for ingest tests")
class TestIngest(_ChainCase, _StepMixin):
    """CSV→SignalPack 字段映射、部分覆盖、价格确认、缺数据 fail closed。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="status_chain_ingest_")

    def tearDown(self):
        super().tearDown()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_csv(self, name: str, content: str) -> str:
        p = Path(self.tmp) / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_build_signal_packs_field_mapping(self):
        res = self._write_csv("nd100_resonance_20260819.csv",
            "ticker,分层,日线_方向,周线_方向,日线_收盘\n"
            "AAPL,偏多,多,空,100.0\nNVDA,多头共振,多,多,200.0\n")
        rk = self._write_csv("five_rankings_20260819_daily.csv",
            "ticker,ranking,reason,left,right\n"
            "AAPL,2,背离观察池,1,0\nNVDA,3,强趋势待触发,0,1\nMSFT,5,风险噪音,0,0\n")
        sk = self._write_csv("skdj_20260819_daily.csv",
            "ticker,k,d,both_le_20,both_ge_80,cross,scenario,data_status\n"
            "AAPL,12.0,15.0,True,False,无,下跌超跌,ok\n"
            "MSFT,15.0,18.0,False,False,上穿,上升回调,ok\n")
        dv = self._write_csv("divergence_td9_20260819.csv",
            "ticker,group,signals,td,close\n"
            "NVDA,bull,RSI底背离(30),1,200.0\nAMD,bear,RSI顶背离(100),9,50.0\n")
        packs, covered = ingest.build_signal_packs([res, rk, sk, dv], "2026-08-19")

        # AAPL: SKDJ超卖 + ranking背离观察池
        self.assertIn("SKDJ_oversold", packs["AAPL"].left_signal_tags)
        self.assertIn("ranking_背离观察池", packs["AAPL"].left_signal_tags)
        self.assertTrue(packs["AAPL"].has_left_signal)
        self.assertAlmostEqual(packs["AAPL"].close, 100.0)
        # NVDA: 底背离 + ranking强趋势待触发
        self.assertIn("divergence_bull", packs["NVDA"].left_signal_tags)
        self.assertIn("ranking_强趋势等待触发", packs["NVDA"].left_signal_tags)
        # MSFT: 低位上穿（cross=上穿 且 k=15<=20），ranking=5 不算左侧
        self.assertIn("SKDJ_low_cross", packs["MSFT"].left_signal_tags)
        self.assertNotIn("ranking_风险噪音", packs["MSFT"].left_signal_tags)
        self.assertIsNone(packs["MSFT"].close)  # 无 resonance/T9 close
        # AMD: td=9>=9 → td9_setup；group=bear 不算底背离
        self.assertIn("td9_setup", packs["AMD"].left_signal_tags)
        self.assertNotIn("divergence_bull", packs["AMD"].left_signal_tags)
        self.assertAlmostEqual(packs["AMD"].close, 50.0)
        # 详细字段（修复5：落库依据）
        self.assertEqual(packs["AAPL"].resonance_layer, "偏多")
        self.assertEqual(packs["AAPL"].ranking, 2)
        self.assertEqual(packs["AAPL"].ranking_name, "背离观察池")
        self.assertEqual(packs["AAPL"].skdj_scenario, "下跌超跌")
        self.assertAlmostEqual(packs["AAPL"].skdj_k, 12.0)
        self.assertTrue(packs["AAPL"].divergence_flag is False)  # AAPL 不在 divergence
        self.assertIsNotNone(packs["AAPL"].source_detail)
        self.assertEqual(packs["AMD"].td9_count, 9)
        self.assertTrue(packs["AMD"].divergence_flag)
        # covered_sources：AAPL 在 resonance/rankings/skdj，不在 divergence
        self.assertEqual(covered["AAPL"], {"resonance", "rankings", "skdj"})
        self.assertIn("divergence", covered["AMD"])

    def test_partial_t9_coverage_does_not_error(self):
        """T9/背离 CSV 是测试子集（只覆盖部分 ticker），不得报错。"""
        rk = self._write_csv("five_rankings_20260819_daily.csv",
            "ticker,ranking,reason,left,right\n"
            "AAPL,2,背离观察池,1,0\nNVDA,3,强趋势待触发,0,1\n")
        dv = self._write_csv("divergence_td9_20260818.csv",
            "ticker,group,signals,td,close\n"
            "AAPL,bull,RSI底背离(30),1,100.0\n")  # 只有 AAPL
        packs, covered = ingest.build_signal_packs([rk, dv], "2026-08-19")
        self.assertIn("AAPL", packs)
        self.assertIn("NVDA", packs)  # NVDA 不在 T9 但在 rankings，仍入池
        self.assertTrue(packs["NVDA"].has_left_signal)  # ranking=3 触发
        # covered 反映子集：AAPL 在 divergence+rankings，NVDA 只在 rankings
        self.assertEqual(covered["AAPL"], {"rankings", "divergence"})
        self.assertEqual(covered["NVDA"], {"rankings"})

    def test_load_cache_only_missing_returns_none(self):
        self.assertIsNone(ingest.load_cache_only("NOTICKER",
                        cache_dir=Path("/nonexistent/dir")))

    def test_compute_price_confirmation_structure_approach_moved_away(self):
        n = 30
        # 新窗口不含当天：recent=iloc[-6:-1](索引24-28), prev=iloc[-11:-6](索引19-23)
        # 要 recent.min(96) > prev.min(93) → structure_improved, structure_low=93
        lows = ([95.0] * 19 + [93.0, 94.0, 93.0, 94.0, 93.0]
                + [96.0, 97.0, 95.0, 96.0, 95.0] + [96.0])  # 19+5+5+1=30
        df = pd.DataFrame({
            "open": 100.0, "high": 100.0, "low": lows,
            "close": 109.0, "volume": 10000,
        }, index=pd.date_range("2026-07-01", periods=n))
        df.loc[df.index[9], "high"] = 110.0  # 前N根(iloc[-21:-1]=索引9-28) high 最大 110
        pf = ingest.compute_price_confirmation(df, invalidation_level=97.02,
                                               observe_start_price=100.0)
        self.assertTrue(pf["data_sufficient"])
        self.assertAlmostEqual(pf["close"], 109.0)
        self.assertTrue(pf["structure_improved"])
        self.assertAlmostEqual(pf["structure_low"], 93.0)
        self.assertTrue(pf["approaching_trigger"])  # 109 >= 110*0.97
        self.assertFalse(pf["breakout_confirmed"])  # 109 < 110
        self.assertTrue(pf["moved_away"])           # 109 >= 100*1.08

    def test_compute_price_confirmation_breakout_legal_ohlc(self):
        """合法 OHLC 突破：trigger 用前N根(不含当天)high，当天 close 突破前高。"""
        n = 30
        df = pd.DataFrame({
            "open": 100.0, "high": 110.0, "low": 99.0,
            "close": 100.0, "volume": 10000,
        }, index=pd.date_range("2026-07-01", periods=n))
        # 当天（索引29）：close=111 > trigger(前N根max=110)；当天 high=112 >= close=111 合法
        df.loc[df.index[-1], "high"] = 112.0
        df.loc[df.index[-1], "close"] = 111.0
        df.loc[df.index[-1], "volume"] = 16000  # >= 1.5 * 前N根均量 10000
        pf = ingest.compute_price_confirmation(df, invalidation_level=90.0,
                                               observe_start_price=100.0)
        self.assertTrue(pf["data_sufficient"])
        self.assertTrue(pf["breakout_confirmed"])   # 111 > 110 + 量能放大
        self.assertFalse(pf["structure_improved"])  # low 平坦
        self.assertFalse(pf["structure_degraded"])
        # 合法性自检：当天 close <= 当天 high
        self.assertLessEqual(df["close"].iloc[-1], df["high"].iloc[-1])

    def test_compute_price_confirmation_insufficient_bars(self):
        df = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0,
                           "close": 100.0, "volume": 10000},
                          index=pd.date_range("2026-07-01", periods=10))
        pf = ingest.compute_price_confirmation(df, None, None)
        self.assertFalse(pf["data_sufficient"])
        self.assertIsNone(pf["close"])

    def test_update_missing_data_holds_state_and_logs(self):
        """update 时 parquet 缺 → fail closed 保持原状 + 留痕，不中断。"""
        self.to_observe(self.eng, "FAKE", close=100.0)
        self.to_base(self.eng, "FAKE", structure_low=98.0, close=101.0)
        inv = self.db.get_ticker("FAKE")["invalidation_level"]

        # 模拟 update：parquet 缺
        df = ingest.load_cache_only("FAKE", cache_dir=Path("/nonexistent"))
        self.assertIsNone(df)
        pf = ingest.compute_price_confirmation(df, inv, None)
        self.assertFalse(pf["data_sufficient"])

        pack = SignalPack("FAKE", "2026-08-19", data_sufficient=False,
                          source_report="cache/nd100/FAKE_1d.parquet")
        d = self.eng.step("FAKE", pack)
        self.assertEqual(d.next_state, BASE)            # 保持原状
        self.assertEqual(d.trigger_signal, TRIG_DATA_INSUFFICIENT)
        self.assertEqual(self.db.get_ticker("FAKE")["current_state"], BASE)

    def test_update_asof_truncation_no_future_leak(self):
        """asof 截断：排除 asof 当天（未完成日线）+ 不用 asof 之后的未来数据。"""
        n = 30
        df = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 95.0,
                           "close": 100.0, "volume": 10000},
                          index=pd.date_range("2026-07-21", periods=n))
        # date_range 2026-07-21 + 30 → 最后 2026-08-19
        # asof=2026-08-19：截断 <8-19，最后 8-18（排除 8-19 当天未完成）
        df_cut, ev = ingest.truncate_to_asof(df, "2026-08-19")
        self.assertTrue((df_cut.index < pd.Timestamp("2026-08-19")).all())
        self.assertEqual(ev, "2026-08-18")  # 实际最后数据日，非 asof
        # 回填 asof=2026-08-10：只用 <8-10 的数据，防未来穿越
        df_cut2, ev2 = ingest.truncate_to_asof(df, "2026-08-10")
        self.assertTrue((df_cut2.index < pd.Timestamp("2026-08-10")).all())
        self.assertEqual(ev2, "2026-08-09")
        # asof 当天数据存在也被排除（未完成日线保守排除）
        self.assertNotIn(pd.Timestamp("2026-08-19"), df_cut.index)

    def test_subset_absence_no_downgrade(self):
        """子集缺席不降级：入池来源今天未覆盖 → 视为未知，不判信号消失。"""
        from status_chain_tracker import _entry_signal_source
        # 第1天：AAPL 因底背离(divergence)入池 OBSERVE
        dv1 = self._write_csv("divergence_td9_20260818.csv",
            "ticker,group,signals,td,close\nAAPL,bull,RSI底背离(30),1,100.0\n")
        packs1, _ = ingest.build_signal_packs([dv1], "2026-08-18")
        self.eng.step("AAPL", packs1["AAPL"], created_at="2026-08-18T00:00:00Z")
        self.assertEqual(self.db.get_ticker("AAPL")["current_state"], OBSERVE)

        # 第2天：rankings+skdj 全量覆盖 AAPL（无左侧），divergence 子集不含 AAPL
        rk2 = self._write_csv("five_rankings_20260819.csv",
            "ticker,ranking,reason,left,right\nAAPL,5,风险噪音,0,0\n")
        sk2 = self._write_csv("skdj_20260819.csv",
            "ticker,k,d,both_le_20,both_ge_80,cross,scenario,data_status\n"
            "AAPL,50,55,False,False,无,普通区,ok\n")
        dv2 = self._write_csv("divergence_td9_20260819_sub.csv",
            "ticker,group,signals,td,close\nNVDA,bull,RSI底背离(30),1,200.0\n")
        packs2, cov2 = ingest.build_signal_packs([rk2, sk2, dv2], "2026-08-19")

        snap = self.eng._current_snapshot("AAPL")
        self.assertFalse(packs2["AAPL"].has_left_signal)
        entry_src = _entry_signal_source(self.db, "AAPL", snap["current_episode_id"])
        self.assertEqual(entry_src, "divergence")          # 入池来源
        self.assertNotIn("divergence", cov2.get("AAPL", set()))  # 今天 divergence 未覆盖 AAPL
        # 模拟 _cmd_ingest 判断：入池来源今天未覆盖 → 不判消失
        if entry_src and entry_src in cov2.get("AAPL", set()):
            packs2["AAPL"].signal_disappeared = True
        self.assertFalse(packs2["AAPL"].signal_disappeared)
        d = self.eng.step("AAPL", packs2["AAPL"], created_at="2026-08-19T00:00:00Z")
        self.assertEqual(d.next_state, OBSERVE)            # 不降级
        self.assertEqual(self.db.get_ticker("AAPL")["current_state"], OBSERVE)

    def test_observe_to_noise_closes_episode_unique_open(self):
        """OBSERVE→NOISE 关闭旧 episode，保证每 ticker 至多一条 open episode。"""
        self.to_observe(self.eng, "AAPL", close=100.0)
        ep1 = self.db.get_ticker("AAPL")["current_episode_id"]
        d = self.eng.step("AAPL", SignalPack(
            "AAPL", "2026-08-12", signal_disappeared=True, close=100.0,
            source_report="t"))
        self.assertEqual(d.next_state, NOISE)
        # 旧 episode 已关闭
        ep1_row = self.db.get_episode(ep1)
        self.assertIsNotNone(ep1_row["end_date"])
        self.assertEqual(ep1_row["end_reason"], "signal_disappeared")
        self.assertEqual(ep1_row["outcome"], "signal_disappeared")
        # 重新入池 → 新 episode
        self.to_observe(self.eng, "AAPL", date="2026-08-15", close=101.0)
        ep2 = self.db.get_ticker("AAPL")["current_episode_id"]
        self.assertNotEqual(ep2, ep1)
        # 至多一条 open episode
        n_open = self.db.conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE ticker=? AND end_date IS NULL",
            ("AAPL",)).fetchone()[0]
        self.assertEqual(n_open, 1)

    def test_snapshot_detail_fields_persisted(self):
        """四类报告详细字段写入 daily_snapshots，source_detail 写入 transitions。"""
        res = self._write_csv("nd100_resonance_20260819.csv",
            "ticker,分层,日线_方向,周线_方向,日线_收盘\nAAPL,多头共振,多,多,100.0\n")
        rk = self._write_csv("five_rankings_20260819.csv",
            "ticker,ranking,reason,left,right\nAAPL,2,背离观察池,1,0\n")
        sk = self._write_csv("skdj_20260819.csv",
            "ticker,k,d,both_le_20,both_ge_80,cross,scenario,data_status\n"
            "AAPL,12.0,15.0,True,False,无,下跌超卖,ok\n")
        dv = self._write_csv("divergence_td9_20260819.csv",
            "ticker,group,signals,td,close\nAAPL,bull,RSI底背离(30),9,100.0\n")
        packs, _ = ingest.build_signal_packs([res, rk, sk, dv], "2026-08-19")
        self.eng.step("AAPL", packs["AAPL"], created_at="2026-08-19T00:00:00Z")

        snap = self.db.conn.execute(
            "SELECT * FROM daily_snapshots WHERE ticker='AAPL'").fetchone()
        self.assertIsNotNone(snap)
        self.assertEqual(snap["resonance_layer"], "多头共振")
        self.assertEqual(snap["ranking_list"], "背离观察池")
        self.assertEqual(snap["skdj_scenario"], "下跌超卖")
        self.assertAlmostEqual(snap["skdj_k"], 12.0)
        self.assertAlmostEqual(snap["skdj_d"], 15.0)
        self.assertEqual(snap["divergence_flag"], 1)
        self.assertEqual(snap["td9_count"], 9)

        tr = self.db.conn.execute(
            "SELECT source_detail FROM transitions WHERE ticker='AAPL' "
            "ORDER BY transition_id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(tr["source_detail"])
        self.assertIn("底背离", tr["source_detail"])

    def test_ingest_update_use_one_market_date_without_time_reversal(self):
        """ingest与update共用真实数据日，状态链不倒退、快照不冲突。"""
        from status_chain_tracker import _cmd_ingest, _cmd_update

        res = self._write_csv("nd100_resonance_20260819.csv",
            "ticker,分层,日线_方向,周线_方向,日线_收盘,日线_数据截至\n"
            "AAPL,偏多,多,空,100.0,2026-08-18 00:00 ET\n")
        rk = self._write_csv("five_rankings_20260819.csv",
            "ticker,ranking,reason,left,right\nAAPL,2,背离观察池,1,0\n")
        db_path = str(Path(self.tmp) / "date_chain.sqlite")

        self.assertEqual(_cmd_ingest(SimpleNamespace(
            db=db_path, reports=f"{res},{rk}", asof="2026-08-19")), 0)

        n = 30
        lows = ([95.0] * 19 + [93.0, 94.0, 93.0, 94.0, 93.0]
                + [96.0, 97.0, 96.0, 97.0, 96.0] + [96.0])
        frame = pd.DataFrame({
            "open": 100.0, "high": 110.0, "low": lows,
            "close": 101.0, "volume": 10000,
        }, index=pd.date_range(end="2026-08-18", periods=n))
        with mock.patch.object(ingest, "load_cache_only", return_value=frame):
            self.assertEqual(_cmd_update(SimpleNamespace(
                db=db_path, asof="2026-08-19", cache_only=True)), 0)

        check = StatusChainDB(db_path)
        try:
            dates = [r[0] for r in check.conn.execute(
                "SELECT DISTINCT transition_date FROM transitions")]
            self.assertEqual(dates, ["2026-08-18"])
            ticker = check.get_ticker("AAPL")
            self.assertEqual(ticker["last_seen"], "2026-08-18")
            self.assertEqual(ticker["current_state"], BASE)
            snapshots = check.conn.execute(
                "SELECT snapshot_date, state, ranking_list FROM daily_snapshots "
                "WHERE ticker='AAPL'").fetchall()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["snapshot_date"], "2026-08-18")
            self.assertEqual(snapshots[0]["state"], BASE)
            self.assertEqual(snapshots[0]["ranking_list"], "背离观察池")
        finally:
            check.close()

    def test_multi_source_entry_partial_absence_stays_observe(self):
        """多来源同时入池时，任一入池来源未覆盖都不能判消失。"""
        from status_chain_tracker import _entry_signal_sources

        rk1 = self._write_csv("five_rankings_20260818.csv",
            "ticker,ranking,reason,left,right\nAAPL,2,背离观察池,1,0\n")
        dv1 = self._write_csv("divergence_td9_20260818.csv",
            "ticker,group,signals,td,close\nAAPL,bull,RSI底背离(30),1,100\n")
        packs1, _ = ingest.build_signal_packs([rk1, dv1], "2026-08-18")
        self.eng.step("AAPL", packs1["AAPL"])
        episode_id = self.db.get_ticker("AAPL")["current_episode_id"]
        self.assertEqual(
            _entry_signal_sources(self.db, "AAPL", episode_id),
            {"rankings", "divergence"})

        rk2 = self._write_csv("five_rankings_20260819.csv",
            "ticker,ranking,reason,left,right\nAAPL,5,风险噪音,0,0\n")
        dv2 = self._write_csv("divergence_td9_20260819_subset.csv",
            "ticker,group,signals,td,close\nNVDA,bull,RSI底背离(30),1,200\n")
        packs2, covered2 = ingest.build_signal_packs([rk2, dv2], "2026-08-19")
        entry_sources = _entry_signal_sources(self.db, "AAPL", episode_id)
        self.assertFalse(entry_sources.issubset(covered2["AAPL"]))
        if entry_sources and entry_sources.issubset(covered2["AAPL"]):
            packs2["AAPL"].signal_disappeared = True
        decision = self.eng.step("AAPL", packs2["AAPL"])
        self.assertEqual(decision.next_state, OBSERVE)
        self.assertEqual(self.db.get_ticker("AAPL")["current_state"], OBSERVE)

    def test_ingest_explicit_insufficient_is_logged_not_no_signal(self):
        """上游明确标“数据不足”时 fail closed，留痕而不当成无信号。"""
        rk = self._write_csv("five_rankings_20260819.csv",
            "ticker,ranking,reason,left,right\nSPCX,5,日线数据不足,0,0\n")
        sk = self._write_csv("skdj_20260819.csv",
            "ticker,data_asof,k,d,both_le_20,both_ge_80,cross,scenario,data_status\n"
            "SPCX,, , ,False,False,无,普通区,数据不足\n")
        packs, _ = ingest.build_signal_packs([rk, sk], "2026-08-19")
        pack = packs["SPCX"]
        self.assertFalse(pack.data_sufficient)
        decision = self.eng.step("SPCX", pack)
        self.assertEqual(decision.next_state, NOISE)
        self.assertEqual(decision.trigger_signal, TRIG_DATA_INSUFFICIENT)
        trace = self.db.conn.execute(
            "SELECT trigger_signal FROM transitions WHERE ticker='SPCX'").fetchone()
        self.assertEqual(trace["trigger_signal"], TRIG_DATA_INSUFFICIENT)


# ============================================================
# 阶段 C：报告（总览板 HTML / 链回放 / 复盘统计）
# ============================================================
class TestReport(_ChainCase, _StepMixin):

    def test_report_html_disclaimer_sort_and_archive(self):
        # AAPL→CONFIRM, MSFT→BASE, NVDA→OBSERVE, AMD→INVALID(归档)
        self.to_observe(self.eng, "AAPL", close=100.0)
        self.to_base(self.eng, "AAPL", structure_low=98.0, close=101.0)
        self.to_trigger(self.eng, "AAPL", close=105.0)
        self.to_confirm(self.eng, "AAPL", close=108.0)
        self.to_observe(self.eng, "MSFT", close=200.0)
        self.to_base(self.eng, "MSFT", structure_low=195.0, close=201.0)
        self.to_observe(self.eng, "NVDA", close=300.0)
        self.to_observe(self.eng, "AMD", close=50.0)
        self.to_base(self.eng, "AMD", structure_low=48.0, close=51.0)
        self.eng.step("AMD", SignalPack("AMD", "2026-08-14", close=45.0,
                                        source_report="t"))  # 破位 → INVALID
        html = gen_board_html(self.db, asof="2026-08-19")
        # 固定免责声明
        self.assertIn(DISCLAIMER, html)
        # 活跃排序：CONFIRM(AAPL) → BASE(MSFT) → OBSERVE(NVDA)
        self.assertLess(html.index("AAPL"), html.index("MSFT"))
        self.assertLess(html.index("MSFT"), html.index("NVDA"))
        # 归档：INVALID(AMD) 在归档标题之后
        self.assertGreater(html.index("AMD"), html.index("归档"))
        # 链回放折叠 + 复盘统计区
        self.assertIn("链回放", html)
        self.assertIn("复盘统计", html)

    def test_replay_returns_full_chain(self):
        self.to_observe(self.eng, "AAPL", close=100.0)
        self.to_base(self.eng, "AAPL", structure_low=98.0, close=101.0)
        self.to_trigger(self.eng, "AAPL", close=105.0)
        self.to_confirm(self.eng, "AAPL", close=108.0)
        chain = self.eng.replay("AAPL")
        self.assertEqual(len(chain), 1)
        seq = [t["to_state"] for t in chain[0]["transitions"]]
        self.assertEqual(seq, [OBSERVE, BASE, TRIGGER, CONFIRM])

    def test_stats_counts_outcomes(self):
        self.to_observe(self.eng, "AAPL", close=100.0)
        self.to_base(self.eng, "AAPL", structure_low=98.0, close=101.0)
        self.to_trigger(self.eng, "AAPL", close=105.0)
        self.to_confirm(self.eng, "AAPL", close=108.0)  # confirmed_transfer
        st = self.eng.stats()
        self.assertEqual(st["episodes_closed_by_outcome"]["confirmed_transfer"]["n"], 1)
        self.assertEqual(st["total_closed"], 1)
        self.assertAlmostEqual(st["confirmed_share"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
