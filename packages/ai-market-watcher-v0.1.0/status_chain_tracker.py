#!/usr/bin/env python3
"""
底部状态链追踪器 · 引擎 + SQLite 层  -  Status-Chain Tracker
================================================================
对应《数字资产市场观察助手》PPT 第 7 页：用 SQLite 给每只进入观察池的股票维护
一台"筑底状态机"，把工具 1–4 的零散单日信号串成一条可回放、可审计的状态链：
`无信号 → 底部观察 → 筑底进行(写定失效线) → 待触发 → 确认移交 / 失效撤销 / 已走远`。

它追踪的是"过程"，不是再造一个更复杂的单日分数。这是 7 个工具里最有系统思维
的一个，直接喂给工具 7（7 天复盘闭环）做增益验证。

核心纪律（照抄 PPT / 既有代码库约定，不得违反）：
  - 失效位优先于预测：结构破坏立即撤销候选。
  - transition_date（行情截至）与 created_at（生成时间）严格分离。
  - 输出只追踪过程，不出现任何买卖指令文案。
  - 一只股票数据不足 fail closed，不中断整批。
  - 不新建 API Key 或缓存目录、不读取/打印/复制 Key（阶段 A 纯逻辑不联网）。

阶段 A 范围：Schema + 引擎 + 规则 + 单测，纯逻辑，synthetic 信号包，不联网。
本文件不修改前 4 个工具、run_nd100_t9_workflow.py、workflow_config.json。

依赖: sqlite3（标准库）+ status_chain_rules。阶段 A 不依赖 pandas/numpy/pyarrow。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from status_chain_rules import (
    SignalPack, TransitionDecision, evaluate_transition,
    NOISE, OBSERVE, BASE, TRIGGER, CONFIRM, INVALID, EXPIRED,
    ACTIVE_STATES, TERMINAL_STATES, STATES_WITH_INVALIDATION,
    ALLOWED_TRANSITIONS, is_allowed, transition_type_for,
    rules_manifest,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "output" / "status_chain.sqlite"

# ============================================================
# 时间工具：transition_date（行情截至）与 created_at（生成时间）严格分离
# ============================================================

def now_created_at() -> str:
    """生成时间（写入 created_at），ISO8601 UTC。注意：这不是行情截至日期。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================
# Schema（5 张表，照抄实施计划 v0.1）
# ============================================================
SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS tickers (
      ticker TEXT PRIMARY KEY,
      universe TEXT,
      company_name TEXT,
      first_seen TEXT,
      last_seen TEXT,
      current_state TEXT,
      current_state_since TEXT,
      current_episode_id INTEGER,
      invalidation_level REAL,
      invalidation_note TEXT,
      needs_human_review INTEGER,
      updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS episodes (
      episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
      ticker TEXT,
      start_date TEXT,
      end_date TEXT,
      end_reason TEXT,
      start_signal TEXT,
      outcome TEXT,
      created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transitions (
      transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
      episode_id INTEGER,
      ticker TEXT,
      from_state TEXT,
      to_state TEXT,
      transition_type TEXT,
      transition_date TEXT,
      evidence_date TEXT,
      trigger_signal TEXT,
      source_report TEXT,
      source_detail TEXT,
      invalidation_level REAL,
      invalidation_reason TEXT,
      price REAL,
      notes TEXT,
      created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_snapshots (
      snapshot_date TEXT,
      ticker TEXT,
      state TEXT,
      episode_id INTEGER,
      resonance_layer TEXT,
      ranking_list TEXT,
      skdj_scenario TEXT,
      skdj_k REAL, skdj_d REAL,
      divergence_flag INTEGER,
      td9_count INTEGER,
      close REAL,
      invalidation_level REAL,
      PRIMARY KEY (snapshot_date, ticker)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_runs (
      run_id TEXT PRIMARY KEY,
      run_date TEXT,
      source_reports TEXT,
      tickers_processed INTEGER,
      transitions_written INTEGER,
      notes TEXT,
      created_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_transitions_episode ON transitions(episode_id)",
    "CREATE INDEX IF NOT EXISTS idx_transitions_ticker_date ON transitions(ticker, transition_date)",
    "CREATE INDEX IF NOT EXISTS idx_episodes_ticker ON episodes(ticker)",
]


# ============================================================
# StatusChainDB：SQLite 读写封装
# ============================================================
class StatusChainDB:
    """状态链 SQLite 库。支持文件库与 ':memory:'（单测用）。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        # check_same_thread=False 便于测试与未来多线程 ingest 复用同一连接
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        for stmt in SCHEMA_SQL:
            self.conn.execute(stmt)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- 事务辅助 ----------
    def commit(self) -> None:
        self.conn.commit()

    # ---------- tickers ----------
    def get_ticker(self, ticker: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tickers WHERE ticker = ?", (ticker,)
        ).fetchone()

    def upsert_ticker(self, ticker: str, *, universe: str = "nasdaq100",
                      company_name: Optional[str] = None,
                      evidence_date: str, current_state: str,
                      current_state_since: str,
                      current_episode_id: Optional[int],
                      invalidation_level: Optional[float],
                      invalidation_note: Optional[str],
                      needs_human_review: int = 0,
                      updated_at: str) -> None:
        existing = self.get_ticker(ticker)
        first_seen = existing["first_seen"] if existing else evidence_date
        self.conn.execute(
            """
            INSERT INTO tickers (ticker, universe, company_name, first_seen, last_seen,
                                  current_state, current_state_since, current_episode_id,
                                  invalidation_level, invalidation_note, needs_human_review,
                                  updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
              universe=excluded.universe,
              company_name=COALESCE(excluded.company_name, tickers.company_name),
              last_seen=excluded.last_seen,
              current_state=excluded.current_state,
              current_state_since=excluded.current_state_since,
              current_episode_id=excluded.current_episode_id,
              invalidation_level=excluded.invalidation_level,
              invalidation_note=excluded.invalidation_note,
              needs_human_review=excluded.needs_human_review,
              updated_at=excluded.updated_at
            """,
            (ticker, universe, company_name, first_seen, evidence_date,
             current_state, current_state_since, current_episode_id,
             invalidation_level, invalidation_note, needs_human_review, updated_at),
        )
        self.commit()

    # ---------- episodes ----------
    def open_episode(self, ticker: str, start_date: str, start_signal: str,
                     created_at: str) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO episodes (ticker, start_date, end_date, end_reason,
                                   start_signal, outcome, created_at)
            VALUES (?, ?, NULL, NULL, ?, 'open', ?)
            """,
            (ticker, start_date, start_signal, created_at),
        )
        self.commit()
        return int(cur.lastrowid)

    def close_episode(self, episode_id: int, end_date: str,
                      end_reason: str, outcome: str, created_at: str) -> None:
        self.conn.execute(
            """
            UPDATE episodes SET end_date = ?, end_reason = ?, outcome = ?
            WHERE episode_id = ? AND end_date IS NULL
            """,
            (end_date, end_reason, outcome, episode_id),
        )
        self.commit()

    def get_episode(self, episode_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone()

    # ---------- transitions ----------
    def insert_transition(self, *, episode_id: Optional[int], ticker: str,
                          from_state: str, to_state: str,
                          transition_type: str, transition_date: str,
                          evidence_date: str, trigger_signal: str,
                          source_report: Optional[str],
                          source_detail: Optional[str],
                          invalidation_level: Optional[float],
                          invalidation_reason: Optional[str],
                          price: Optional[float], notes: Optional[str],
                          created_at: str) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO transitions (episode_id, ticker, from_state, to_state,
                transition_type, transition_date, evidence_date, trigger_signal,
                source_report, source_detail, invalidation_level, invalidation_reason,
                price, notes, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (episode_id, ticker, from_state, to_state, transition_type,
             transition_date, evidence_date, trigger_signal, source_report,
             source_detail, invalidation_level, invalidation_reason, price,
             notes, created_at),
        )
        self.commit()
        return int(cur.lastrowid)

    def get_episode_chain(self, episode_id: int) -> List[sqlite3.Row]:
        """按 transition_date（行情截至）排序，还原某 episode 的完整状态链。"""
        return self.conn.execute(
            """
            SELECT * FROM transitions
            WHERE episode_id = ?
            ORDER BY transition_date ASC, transition_id ASC
            """,
            (episode_id,),
        ).fetchall()

    def get_ticker_episodes(self, ticker: str) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM episodes WHERE ticker = ? ORDER BY episode_id ASC",
            (ticker,),
        ).fetchall()

    # ---------- daily_snapshots ----------
    def upsert_snapshot(self, *, snapshot_date: str, ticker: str, state: str,
                        episode_id: Optional[int], close: Optional[float],
                        invalidation_level: Optional[float],
                        resonance_layer: Optional[str] = None,
                        ranking_list: Optional[str] = None,
                        skdj_scenario: Optional[str] = None,
                        skdj_k: Optional[float] = None,
                        skdj_d: Optional[float] = None,
                        divergence_flag: Optional[int] = None,
                        td9_count: Optional[int] = None) -> None:
        self.conn.execute(
            """
            INSERT INTO daily_snapshots (snapshot_date, ticker, state, episode_id,
                resonance_layer, ranking_list, skdj_scenario, skdj_k, skdj_d,
                divergence_flag, td9_count, close, invalidation_level)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(snapshot_date, ticker) DO UPDATE SET
              state=excluded.state, episode_id=excluded.episode_id,
              close=excluded.close, invalidation_level=excluded.invalidation_level,
              resonance_layer=COALESCE(excluded.resonance_layer, daily_snapshots.resonance_layer),
              ranking_list=COALESCE(excluded.ranking_list, daily_snapshots.ranking_list),
              skdj_scenario=COALESCE(excluded.skdj_scenario, daily_snapshots.skdj_scenario),
              skdj_k=COALESCE(excluded.skdj_k, daily_snapshots.skdj_k),
              skdj_d=COALESCE(excluded.skdj_d, daily_snapshots.skdj_d),
              divergence_flag=COALESCE(excluded.divergence_flag, daily_snapshots.divergence_flag),
              td9_count=COALESCE(excluded.td9_count, daily_snapshots.td9_count)
            """,
            (snapshot_date, ticker, state, episode_id, resonance_layer,
             ranking_list, skdj_scenario, skdj_k, skdj_d, divergence_flag,
             td9_count, close, invalidation_level),
        )
        self.commit()

    # ---------- ingest_runs ----------
    def log_ingest_run(self, *, run_id: str, run_date: str,
                       source_reports: List[str], tickers_processed: int,
                       transitions_written: int, notes: Optional[str],
                       created_at: str) -> None:
        import json
        self.conn.execute(
            """
            INSERT INTO ingest_runs (run_id, run_date, source_reports,
                tickers_processed, transitions_written, notes, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (run_id, run_date, json.dumps(source_reports, ensure_ascii=False),
             tickers_processed, transitions_written, notes, created_at),
        )
        self.commit()


# ============================================================
# 状态机驱动：把引擎决策落到 DB
# ============================================================
class StatusChainEngine:
    """驱动 evaluate_transition → 写 transitions / 更新 tickers / 管理 episode。

    封装"当前状态 + 当前失效线 + 当前 episode_id"的读取与回写，使调用方只需
    传入 (ticker, pack)。单测与未来 ingest 共用同一入口。
    """

    def __init__(self, db: StatusChainDB, universe: str = "nasdaq100"):
        self.db = db
        self.universe = universe

    def _current_snapshot(self, ticker: str) -> Dict[str, Any]:
        row = self.db.get_ticker(ticker)
        if row is None:
            return {
                "current_state": NOISE,
                "current_state_since": None,
                "current_episode_id": None,
                "invalidation_level": None,
            }
        return {
            "current_state": row["current_state"] or NOISE,
            "current_state_since": row["current_state_since"],
            "current_episode_id": row["current_episode_id"],
            "invalidation_level": row["invalidation_level"],
        }

    def step(self, ticker: str, pack: SignalPack,
             created_at: Optional[str] = None) -> Optional[TransitionDecision]:
        """推进一只 ticker 一天的状态机。

        参数:
          ticker:    标的代码（大写）
          pack:      当日信号包（evidence_date = 行情截至日期）
          created_at: 生成时间；None 则取当前 UTC。

        返回:
          TransitionDecision（transition_type=None 表示无转移/停留，但 tickers 的
          last_seen 与 daily_snapshot 仍会更新）；若因数据不足 fail closed 落 NOISE
          且原本就是 NOISE，返回的 decision.transition_type 仍为 None。
        """
        created_at = created_at or now_created_at()
        evidence_date = pack.evidence_date  # 行情截至日期（≠ created_at）
        snap = self._current_snapshot(ticker)
        current_state = snap["current_state"]
        current_inv = snap["invalidation_level"]
        current_ep = snap["current_episode_id"]

        decision = evaluate_transition(current_state, pack, current_inv)

        # ---- episode 管理 ----
        episode_id = current_ep
        # 关闭旧 episode（CONFIRM/INVALID/EXPIRED/信号消失回NOISE）
        if decision.close_episode and episode_id is not None:
            self.db.close_episode(
                episode_id=episode_id, end_date=evidence_date,
                end_reason=decision.episode_end_reason or "closed",
                outcome=_end_reason_to_outcome(decision.episode_end_reason),
                created_at=created_at,
            )
        # 开新 episode（NOISE/终态 → OBSERVE；CONFIRM 降级）
        if decision.open_new_episode:
            # 保存本次入池的全部正信号，而不只是第一个 trigger。
            # 后续判断“信号消失”时，必须确认这些来源今日都有覆盖。
            start_signal = ",".join(dict.fromkeys(pack.left_signal_tags))
            episode_id = self.db.open_episode(
                ticker=ticker, start_date=evidence_date,
                start_signal=(start_signal or decision.trigger_signal
                              or TRIG_LEFT_SIGNAL_LOOKUP),
                created_at=created_at,
            )

        # ---- 写 transition（仅当真发生状态变化或有必要留痕时）----
        # 留痕策略：真实转移(transition_type not None)必写；数据不足落 NOISE 也写一条留痕。
        wrote_transition = False
        if decision.transition_type is not None:
            from_state = current_state
            to_state = decision.next_state
            # 合法性校验（不合法则记为 manual 留痕并保持原状，避免脏数据）
            if not is_allowed(from_state, to_state):
                # 兜底：理论上 evaluate_transition 不会产出非法转移
                self.db.insert_transition(
                    episode_id=episode_id, ticker=ticker,
                    from_state=from_state, to_state=to_state,
                    transition_type=transition_type_for(from_state, to_state),
                    transition_date=evidence_date, evidence_date=evidence_date,
                    trigger_signal=decision.trigger_signal or "manual",
                    source_report=decision.source_report,
                    source_detail=pack.source_detail,
                    invalidation_level=decision.invalidation_level,
                    invalidation_reason=decision.invalidation_reason,
                    price=decision.price, notes=(decision.notes or "") + " [illegal-blocked]",
                    created_at=created_at,
                )
                # 非法转移不改变状态
                to_state = from_state
                decision.invalidation_level = current_inv
            else:
                self.db.insert_transition(
                    episode_id=episode_id, ticker=ticker,
                    from_state=from_state, to_state=to_state,
                    transition_type=decision.transition_type,
                    transition_date=evidence_date, evidence_date=evidence_date,
                    trigger_signal=decision.trigger_signal or "manual",
                    source_report=decision.source_report,
                    source_detail=pack.source_detail,
                    invalidation_level=decision.invalidation_level,
                    invalidation_reason=decision.invalidation_reason,
                    price=decision.price, notes=decision.notes,
                    created_at=created_at,
                )
                wrote_transition = True
        elif decision.trigger_signal == "data_insufficient":
            # 数据不足 fail closed：写一条留痕 transition（from==to=NOISE），便于审计
            self.db.insert_transition(
                episode_id=episode_id, ticker=ticker,
                from_state=current_state, to_state=decision.next_state,
                transition_type=None,
                transition_date=evidence_date, evidence_date=evidence_date,
                trigger_signal=decision.trigger_signal,
                source_report=decision.source_report, source_detail=None,
                invalidation_level=None, invalidation_reason=None,
                price=decision.price, notes=decision.notes,
                created_at=created_at,
            )
            wrote_transition = True

        # ---- 更新 tickers 快照 ----
        new_state = decision.next_state
        new_state_since = snap["current_state_since"]
        if wrote_transition and new_state != current_state:
            new_state_since = evidence_date
        elif new_state_since is None:
            new_state_since = evidence_date

        needs_review = 1 if new_state == CONFIRM else 0
        inv_note = decision.invalidation_reason
        self.db.upsert_ticker(
            ticker=ticker, universe=self.universe, company_name=None,
            evidence_date=evidence_date, current_state=new_state,
            current_state_since=new_state_since,
            current_episode_id=episode_id,
            invalidation_level=decision.invalidation_level,
            invalidation_note=inv_note, needs_human_review=needs_review,
            updated_at=created_at,
        )

        # ---- 写每日快照（含四类报告详细字段，供阶段C证据展示）----
        self.db.upsert_snapshot(
            snapshot_date=evidence_date, ticker=ticker, state=new_state,
            episode_id=episode_id, close=decision.price,
            invalidation_level=decision.invalidation_level,
            resonance_layer=pack.resonance_layer,
            ranking_list=pack.ranking_name,
            skdj_scenario=pack.skdj_scenario,
            skdj_k=pack.skdj_k, skdj_d=pack.skdj_d,
            divergence_flag=(1 if pack.divergence_flag else None),
            td9_count=pack.td9_count,
        )

        return decision

    def replay(self, ticker: str) -> List[Dict[str, Any]]:
        """回放某 ticker 的全部状态链：按 episode、按 transition_date 还原。"""
        episodes = self.db.get_ticker_episodes(ticker)
        out: List[Dict[str, Any]] = []
        for ep in episodes:
            chain = self.db.get_episode_chain(ep["episode_id"])
            out.append({
                "episode_id": ep["episode_id"],
                "start_date": ep["start_date"],
                "end_date": ep["end_date"],
                "end_reason": ep["end_reason"],
                "outcome": ep["outcome"],
                "start_signal": ep["start_signal"],
                "transitions": [dict(r) for r in chain],
            })
        return out

    def stats(self) -> Dict[str, Any]:
        """复盘统计（喂工具7）：到达 CONFIRM 的链数、平均链天数、失效/已走远占比。"""
        rows = self.db.conn.execute(
            """
            SELECT outcome, COUNT(*) AS n,
                   AVG(julianday(COALESCE(end_date, end_date)) -
                       julianday(start_date)) AS avg_days
            FROM episodes WHERE end_date IS NOT NULL
            GROUP BY outcome
            """
        ).fetchall()
        by_outcome = {r["outcome"]: {"n": r["n"], "avg_days": r["avg_days"]} for r in rows}
        total_closed = sum(v["n"] for v in by_outcome.values())
        return {
            "episodes_closed_by_outcome": by_outcome,
            "total_closed": total_closed,
            "invalidated_share": (
                by_outcome.get("invalidated", {}).get("n", 0) / total_closed
                if total_closed else 0.0),
            "expired_share": (
                by_outcome.get("expired", {}).get("n", 0) / total_closed
                if total_closed else 0.0),
            "confirmed_share": (
                by_outcome.get("confirmed_transfer", {}).get("n", 0) / total_closed
                if total_closed else 0.0),
        }


# 用于 open_episode 的 start_signal 占位（避免循环导入字面量）
TRIG_LEFT_SIGNAL_LOOKUP = "left_signal"


def _episode_outcome(next_state: str) -> str:
    """根据转移目标态推断 episode 的 outcome（供工具7）。"""
    if next_state == CONFIRM:
        return "confirmed_transfer"
    if next_state == INVALID:
        return "invalidated"
    if next_state == EXPIRED:
        return "expired"
    return "open"


def _end_reason_to_outcome(end_reason: Optional[str]) -> str:
    """根据 episode_end_reason 映射 outcome（覆盖信号消失等关闭原因）。"""
    mapping = {
        "confirmed": "confirmed_transfer",
        "invalidated": "invalidated",
        "expired": "expired",
        "signal_disappeared": "signal_disappeared",
    }
    return mapping.get(end_reason or "", "open")


# ============================================================
# CLI 骨架（阶段 A 仅提供 schema/init/selftest；ingest/update/report 留阶段 B/C）
# ============================================================
def _cmd_init(args) -> int:
    db = StatusChainDB(args.db)
    print(f"[ok] schema initialized at {db.db_path}")
    db.close()
    return 0


def _cmd_selftest(args) -> int:
    """跑一段 synthetic 状态链，验证引擎端到端可用（不联网）。"""
    db = StatusChainDB(":memory:")
    eng = StatusChainEngine(db)
    tk = "SYNTH"
    # NOISE -> OBSERVE -> BASE -> TRIGGER -> CONFIRM
    steps = [
        SignalPack(tk, "2026-08-10", has_left_signal=True,
                   left_signal_tags=["SKDJ_oversold"], close=100.0,
                   source_report="synthetic"),
        SignalPack(tk, "2026-08-12", structure_improved=True,
                   structure_low=98.0, close=101.0, source_report="synthetic"),
        SignalPack(tk, "2026-08-14", approaching_trigger=True, close=105.0,
                   source_report="synthetic"),
        SignalPack(tk, "2026-08-16", breakout_confirmed=True, close=108.0,
                   source_report="synthetic"),
    ]
    for p in steps:
        d = eng.step(tk, p, created_at="2026-08-19T00:00:00Z")
        print(f"  {p.evidence_date}: {d.next_state} ({d.transition_type})")
    chain = eng.replay(tk)
    print(f"[ok] synthetic chain: {len(chain)} episode(s), "
          f"{sum(len(e['transitions']) for e in chain)} transition(s)")
    print(f"[ok] stats: {eng.stats()}")
    db.close()
    return 0


def _entry_signal_sources(db, ticker: str,
                          episode_id: Optional[int]) -> set[str]:
    """查该 episode 入池时全部正信号的来源类型。

    episodes.start_signal 保存逗号分隔的全部入池 tag。旧库只保存
    单个 tag 也可兼容。只有全部入池来源今日都覆盖且全部无信号，
    才能判定消失；任一子集来源缺席都是未知。
    """
    if episode_id is None:
        return set()
    row = db.conn.execute(
        "SELECT start_signal FROM episodes WHERE episode_id=? AND ticker=?",
        (episode_id, ticker),
    ).fetchone()
    if not row:
        return set()
    import status_chain_ingest as ingest
    tags = [tag.strip() for tag in (row["start_signal"] or "").split(",")
            if tag.strip()]
    return {src for src in (ingest.tag_to_source(tag) for tag in tags) if src}


def _entry_signal_source(db, ticker: str,
                         episode_id: Optional[int]) -> Optional[str]:
    """向后兼容旧调用；新逻辑使用 _entry_signal_sources。"""
    sources = sorted(_entry_signal_sources(db, ticker, episode_id))
    return sources[0] if sources else None


def _cmd_ingest(args) -> int:
    """读工具 1–4 CSV → 信号接入状态链（不读 parquet、不联网）。"""
    import status_chain_ingest as ingest
    db = StatusChainDB(args.db)
    eng = StatusChainEngine(db)
    reports = [p.strip() for p in args.reports.split(",") if p.strip()]
    if not reports:
        print("[error] --reports 不能为空")
        db.close()
        return 2
    packs, covered = ingest.build_signal_packs(reports, args.asof)
    created = now_created_at()
    processed = written = left_count = 0
    for tk in sorted(packs):
        pack = packs[tk]
        # 信号消失判定：当前 OBSERVE 且当日无左侧信号。
        # 但必须确认"入池信号来源今天覆盖了该 ticker"——子集缺席（T9/背离未覆盖）
        # 表示"未知/未覆盖"，不能当成"信号为假"。只有入池来源今天覆盖且无信号才判消失。
        snap = eng._current_snapshot(tk)
        if snap["current_state"] == OBSERVE and not pack.has_left_signal:
            entry_sources = _entry_signal_sources(
                db, tk, snap["current_episode_id"])
            if entry_sources and entry_sources.issubset(covered.get(tk, set())):
                pack.signal_disappeared = True
        d = eng.step(tk, pack, created_at=created)
        processed += 1
        if pack.has_left_signal:
            left_count += 1
        if d.transition_type is not None:
            written += 1
    db.log_ingest_run(
        run_id=f"ingest-{args.asof}-{created}", run_date=args.asof,
        source_reports=reports, tickers_processed=processed,
        transitions_written=written, notes=f"left_signal={left_count}",
        created_at=created,
    )
    print(f"[ok] ingest asof={args.asof}: processed={processed} "
          f"left_signal={left_count} transitions={written}")
    db.close()
    return 0


def _cmd_update(args) -> int:
    """读缓存日 K → 价格确认推进状态链（--cache-only：只读缓存不下载）。

    只处理活跃态 ticker（OBSERVE/BASE/TRIGGER/CONFIRM）。缺数据 fail closed：
    保持原状 + 留痕 data_insufficient，不中断整批，不把缺数据当成没信号。
    """
    import status_chain_ingest as ingest
    db = StatusChainDB(args.db)
    eng = StatusChainEngine(db)
    active = tuple(sorted(ACTIVE_STATES))
    rows = db.conn.execute(
        f"SELECT ticker, current_state, current_episode_id, invalidation_level, last_seen "
        f"FROM tickers WHERE current_state IN ({','.join('?' * len(active))})",
        active,
    ).fetchall()
    created = now_created_at()
    processed = written = 0
    missing: List[str] = []
    for row in rows:
        tk = row["ticker"]
        df = ingest.load_cache_only(tk)
        # 截断到 asof 之前（排除 asof 当天未完成日线 + 防未来穿越）
        evidence_date = None
        if df is not None:
            df, evidence_date = ingest.truncate_to_asof(df, args.asof)
        if df is None or len(df) < ingest.MIN_BARS_PRICE:
            # 缺数据 fail closed：保持原状 + 留痕
            pack = SignalPack(tk, row["last_seen"] or args.asof,
                              data_sufficient=False,
                              source_report=f"cache/nd100/{tk}_1d.parquet")
            eng.step(tk, pack, created_at=created)
            missing.append(tk)
            processed += 1
            continue
        # evidence_date = 截断后实际最后数据日（不用命令行 asof，避免日期错标）
        obs_start = ingest.get_observe_start_price(
            db, tk, row["current_episode_id"])
        pf = ingest.compute_price_confirmation(
            df, row["invalidation_level"], obs_start)
        pack = SignalPack(tk, evidence_date,
                          source_report=f"cache/nd100/{tk}_1d.parquet", **pf)
        d = eng.step(tk, pack, created_at=created)
        processed += 1
        if d.transition_type is not None:
            written += 1
    print(f"[ok] update asof={args.asof}: active={len(rows)} "
          f"processed={processed} transitions={written} "
          f"missing_data={len(missing)}")
    if missing:
        print(f"     缺数据(已留痕保持原状): {', '.join(missing)}")
    db.close()
    return 0


# ============================================================
# 阶段 C：报告（总览板 HTML / 链回放 / 复盘统计）
# ============================================================
STATE_COLOR = {
    CONFIRM: "#C0392B", TRIGGER: "#E67E22", BASE: "#1F6FB0",
    OBSERVE: "#8E44AD", INVALID: "#7F8C8D", EXPIRED: "#95A5A6",
}
_ACTIVE_ORDER = {CONFIRM: 4, TRIGGER: 3, BASE: 2, OBSERVE: 1}

DISCLAIMER = ("本工具只追踪状态过程，不预测涨跌，不构成任何交易指令。"
              "失效位优先于预测。")


def _last_transition(db, ticker):
    return db.conn.execute(
        "SELECT * FROM transitions WHERE ticker=? "
        "ORDER BY transition_id DESC LIMIT 1", (ticker,)).fetchone()


def _episode_chain_html(db, ticker):
    eps = db.get_ticker_episodes(ticker)
    if not eps:
        return "<p class='muted'>无链</p>"
    parts = []
    for ep in eps:
        chain = db.get_episode_chain(ep["episode_id"])
        parts.append(
            f"<div class='ep'><b>Episode #{ep['episode_id']}</b> "
            f"{ep['start_date']}→{ep['end_date'] or '进行中'} "
            f"({ep['end_reason'] or 'open'}/{ep['outcome']})</div>")
        parts.append(
            "<table class='chain'><tr><th>日期</th><th>转移</th>"
            "<th>触发信号</th><th>证据</th><th>收盘</th><th>失效线</th></tr>")
        for t in chain:
            parts.append(
                f"<tr><td>{t['transition_date']}</td>"
                f"<td>{t['from_state']}→{t['to_state']}</td>"
                f"<td>{t['trigger_signal'] or ''}</td>"
                f"<td class='detail'>{t['source_detail'] or t['source_report'] or ''}</td>"
                f"<td>{t['price'] if t['price'] is not None else ''}</td>"
                f"<td>{t['invalidation_level'] if t['invalidation_level'] is not None else ''}</td></tr>")
        parts.append("</table>")
    return "".join(parts)


def _ticker_rows(db, rows):
    state_label = {
        "OBSERVE": "观察中",
        "BASE": "底部形成",
        "TRIGGER": "条件触发",
        "CONFIRM": "已确认",
        "INVALID": "已失效",
        "EXPIRED": "已过期",
        "NOISE": "噪音",
    }
    out = []
    for r in rows:
        lt = _last_transition(db, r["ticker"])
        st = r["current_state"]
        col = STATE_COLOR.get(st, "#333")
        inv = r["invalidation_level"]
        inv_s = f"{inv:.4f}" if inv is not None else "—"
        out.append(
            f"<tr><td><span class='badge' style='background:{col}'>{state_label.get(st, st)}"
            f" <small>({st})</small></span>"
            f" {r['ticker']}</td>"
            f"<td>{r['current_state_since'] or ''}</td><td>{inv_s}</td>"
            f"<td>{(lt['trigger_signal'] if lt else '') or ''}</td>"
            f"<td class='detail'>{(lt['source_report'] if lt else '') or ''}</td></tr>"
            f"<tr><td colspan='5'><details><summary>链回放 {r['ticker']}</summary>"
            f"{_episode_chain_html(db, r['ticker'])}</details></td></tr>")
    return "".join(out)


def gen_board_html(db, asof: Optional[str] = None) -> str:
    """生成总览板 HTML：活跃池按 CONFIRM/TRIGGER→BASE→OBSERVE 排序，归档单独列，含链回放。"""
    rows = db.conn.execute("SELECT * FROM tickers").fetchall()
    active = [r for r in rows if r["current_state"] in ACTIVE_STATES]
    archived = [r for r in rows if r["current_state"] in TERMINAL_STATES]
    active.sort(key=lambda r: (-_ACTIVE_ORDER.get(r["current_state"], 0), r["ticker"]))
    archived.sort(key=lambda r: r["ticker"])

    eng = StatusChainEngine(db)
    st = eng.stats()
    bo = st["episodes_closed_by_outcome"]

    def _n(key):
        return bo.get(key, {}).get("n", 0)
    stats_html = (
        f"确认移交 {_n('confirmed_transfer')} / 失效 {_n('invalidated')} / "
        f"已走远 {_n('expired')} / 信号消失 {_n('signal_disappeared')} | "
        f"确认占比 {st['confirmed_share']:.1%} / 失效占比 {st['invalidated_share']:.1%} "
        f"/ 已走远占比 {st['expired_share']:.1%}")

    active_rows = _ticker_rows(db, active)
    archived_rows = _ticker_rows(db, archived)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>底部状态链总览板</title>
<style>
body{{font-family:-apple-system,"PingFang SC",sans-serif;margin:20px;color:#222;background:#fafafa}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:24px;border-left:4px solid #1F6FB0;padding-left:8px}}
table{{border-collapse:collapse;width:100%;background:#fff;margin:8px 0;font-size:14px}}
th,td{{border:1px solid #e0e0e0;padding:8px 10px;text-align:left;vertical-align:top}}
th{{background:#f0f4f8}} .badge{{color:#fff;padding:2px 8px;border-radius:3px;font-size:12px;margin-right:6px}}
.detail{{color:#555;font-size:12px;max-width:320px;word-break:break-all}}
.disclaimer{{background:#fff3cd;border:1px solid #ffe08a;padding:10px;border-radius:4px;margin:12px 0;font-size:13px;color:#856404;font-weight:bold}}
.stats{{background:#e8f4f8;padding:8px 12px;border-radius:4px;margin:12px 0;font-size:13px}}
.muted{{color:#999}} .chain{{font-size:12px;margin:4px 0}} .ep{{margin:6px 0;font-size:12px;color:#1F6FB0}}
summary{{cursor:pointer;color:#1F6FB0;font-size:13px}}
</style></head><body>
<h1>底部状态链总览板</h1>
<div class="asof">数据截至: {asof or "最新"}</div>
<div class="disclaimer">{DISCLAIMER}</div>
<div class="stats">复盘统计: {stats_html}</div>
<h2>活跃观察池（{len(active)}，按 CONFIRM/TRIGGER → BASE → OBSERVE 排序）</h2>
<table><tr><th>标的·状态</th><th>自何日起</th><th>失效线</th><th>上次触发信号</th><th>来源报告</th></tr>
{active_rows}</table>
<h2>归档（{len(archived)}，失效/已走远）</h2>
<table><tr><th>标的·状态</th><th>自何日起</th><th>失效线</th><th>上次触发信号</th><th>来源报告</th></tr>
{archived_rows}</table>
</body></html>"""


def _cmd_report(args) -> int:
    db = StatusChainDB(args.db)
    html = gen_board_html(db, getattr(args, "asof", None))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"[ok] report -> {out_path}")
    db.close()
    return 0


def _cmd_replay(args) -> int:
    db = StatusChainDB(args.db)
    eng = StatusChainEngine(db)
    chain = eng.replay(args.ticker)
    if not chain:
        print(f"[info] {args.ticker} 无状态链")
        db.close()
        return 0
    for ep in chain:
        print(f"=== Episode #{ep['episode_id']} {ep['start_date']}→"
              f"{ep['end_date'] or '进行中'} ({ep['end_reason'] or 'open'}/"
              f"{ep['outcome']}) 启动信号={ep['start_signal']} ===")
        for t in ep["transitions"]:
            print(f"  {t['transition_date']} {t['from_state']}→{t['to_state']} "
                  f"[{t['transition_type']}] {t['trigger_signal'] or ''} "
                  f"close={t['price']} inv={t['invalidation_level']}")
            if t["source_detail"]:
                print(f"      证据: {t['source_detail']}")
    db.close()
    return 0


def _cmd_stats(args) -> int:
    db = StatusChainDB(args.db)
    eng = StatusChainEngine(db)
    st = eng.stats()
    print("=== 底部状态链复盘统计 ===")
    for outcome, info in st["episodes_closed_by_outcome"].items():
        avg = info["avg_days"]
        avg_s = f"(平均 {avg:.1f} 天)" if avg is not None else ""
        print(f"  {outcome}: {info['n']} 条 {avg_s}")
    print(f"  总关闭: {st['total_closed']}")
    print(f"  确认占比 {st['confirmed_share']:.1%} / "
          f"失效占比 {st['invalidated_share']:.1%} / "
          f"已走远占比 {st['expired_share']:.1%}")
    db.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="底部状态链追踪器（引擎+SQLite+信号接入+价格确认+报告）")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH),
                        help="SQLite 库路径（默认 output/status_chain.sqlite）")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="初始化 schema")
    p_init.set_defaults(func=_cmd_init)

    p_st = sub.add_parser("selftest", help="synthetic 端到端自检（不联网）")
    p_st.set_defaults(func=_cmd_selftest)

    p_ing = sub.add_parser("ingest", help="读工具1-4 CSV → 信号接入状态链")
    p_ing.add_argument("--reports", required=True,
                       help="CSV 路径，逗号分隔（nd100_resonance/five_rankings/divergence_td9/skdj）")
    p_ing.add_argument("--asof", required=True, help="行情截至日期 YYYY-MM-DD")
    p_ing.set_defaults(func=_cmd_ingest)

    p_upd = sub.add_parser("update", help="读缓存日K → 价格确认推进状态链")
    p_upd.add_argument("--asof", required=True, help="行情截至日期 YYYY-MM-DD")
    p_upd.add_argument("--cache-only", action="store_true",
                       help="只读缓存不下载（load_cache_only 本就只读缓存）")
    p_upd.set_defaults(func=_cmd_update)

    p_rep = sub.add_parser("report", help="生成总览板 HTML（含链回放）")
    p_rep.add_argument("--out", default=str(DEFAULT_DB_PATH.parent / "status_chain_board.html"),
                       help="输出 HTML 路径")
    p_rep.add_argument("--asof", default=None, help="标题显示的日期（可选）")
    p_rep.set_defaults(func=_cmd_report)

    p_rpl = sub.add_parser("replay", help="单 ticker 链回放（终端）")
    p_rpl.add_argument("--ticker", required=True, help="标的代码")
    p_rpl.set_defaults(func=_cmd_replay)

    p_stat = sub.add_parser("stats", help="复盘统计（终端）")
    p_stat.set_defaults(func=_cmd_stats)

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
