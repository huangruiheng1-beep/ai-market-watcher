#!/usr/bin/env python3
"""
底部状态链 · 信号接入 + 价格确认  -  status_chain_ingest.py
================================================================
阶段 B：把工具 1–4 的 CSV 统一转成 SignalPack（信号接入），并用缓存日 K
做价格确认（失效/突破/量能/已走远/结构变化）。

分工（与 status_chain_tracker.py 的 ingest/update 子命令对应）：
  - ingest：只读 CSV → SignalPack 的【信号字段】（has_left_signal / tags /
    signal_disappeared）。不读 parquet、不碰 API。
  - update：只读 cache/nd100/{tk}_1d.parquet → SignalPack 的【价格确认字段】
    （structure_improved / structure_degraded / approaching_trigger /
    breakout_confirmed / moved_away / close / data_sufficient）。不读 CSV。

核心纪律：
  - 复用同一份缓存文件（cache/nd100/*_1d.parquet），只读不下载，不新建数据层。
  - 不读取/打印/复制 API Key（本模块不导入下载层）。
  - T9/背离 CSV 可能是测试子集或不同日期，不假设覆盖 100 只——部分覆盖即部分信号。
  - 缺数据记录并跳过，不把"缺数据"当成"没有信号"（fail closed：保持原状+留痕）。
  - 价格确认阈值（结构窗口/触发位/量能倍数/已走远幅度）为本工具自定候选，
    标 candidate_unverified，未经历史回测验证。

依赖: pandas（读 CSV/parquet）。读 parquet 需 pyarrow，用 ./.venv/bin/python。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Dict, List, Any

import pandas as pd

from status_chain_rules import SignalPack

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache" / "nd100"

# 五榜单 ranking → 名称（与 five_rankings_daily.py 的 RANKINGS 一致）
RANK_NAME = {
    1: "已确认优先复核",
    2: "背离观察池",
    3: "强趋势等待触发",
    4: "C级高潜力",
    5: "风险噪音",
}
# 计划文档点名的左侧观察触发榜单：背离观察池 / 强趋势等待触发
RANKING_LEFT = {2, 3}

# 价格确认候选阈值（PPT 未给精确值，本工具自定，标 candidate_unverified）
PRICE_CONFIG = {
    "min_bars": 25,                 # 价格确认最少日K根数，不足 fail closed
    "structure_window": 5,          # 结构改善/退化：最近 N 根 vs 前 N 根 low
    "trigger_window": 20,           # 触发位 = 最近 N 根 high 最大值
    "approach_ratio": 0.97,         # close >= trigger * 0.97 视为逼近触发
    "breakout_vol_ratio": 1.5,      # 突破日量能 >= 1.5x 近 N 日均量
    "moved_away_pct": 8.0,          # 较观察起点收盘上涨 >= 8% 未确认 → 已走远
    "rules_status": "candidate_unverified",
    "note": "结构窗口/触发位/量能/已走远阈值为本工具自定候选值，未经历史回测验证。",
}
MIN_BARS_PRICE = PRICE_CONFIG["min_bars"]


# ============================================================
# 小工具
# ============================================================
def _to_float(v) -> Optional[float]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("true", "1", "yes")


def _to_int(v) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def _to_evidence_date(v) -> Optional[str]:
    """把报告里的数据截至时间统一成 YYYY-MM-DD。

    现有 CSV 会写 ``2026-08-18 00:00 ET``；ET 不是 pandas 可靠识别的
    时区名，因此只取已明确写在字段中的日期部分。
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(v))
    return m.group(0) if m else None


def _reports_data_insufficient(*values) -> bool:
    """识别上游报告已明确标注的数据不足/错误，不把它当成“无信号”。"""
    for value in values:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        text = str(value).strip().lower()
        if "不足" in text or text in {"missing", "insufficient", "error", "failed"}:
            return True
    return False


# ============================================================
# 报告类型识别
# ============================================================
def detect_report_kind(path: str | Path) -> Optional[str]:
    name = Path(path).name.lower()
    if name.startswith("nd100_resonance"):
        return "resonance"
    if name.startswith("five_rankings"):
        return "rankings"
    if name.startswith("divergence_td9"):
        return "divergence"
    if name.startswith("skdj"):
        return "skdj"
    return None


# ============================================================
# CSV 解析器（每个返回 {ticker: fields}）
# ============================================================
def parse_resonance_csv(path: str | Path) -> Dict[str, dict]:
    df = pd.read_csv(path)
    out: Dict[str, dict] = {}
    for _, r in df.iterrows():
        tk = str(r["ticker"]).strip().upper()
        out[tk] = {
            "layer": r.get("分层"),
            "daily_dir": r.get("日线_方向"),
            "weekly_dir": r.get("周线_方向"),
            "close": _to_float(r.get("日线_收盘")),
            "data_asof": _to_evidence_date(r.get("日线_数据截至")),
        }
    return out


def parse_five_rankings_csv(path: str | Path) -> Dict[str, dict]:
    df = pd.read_csv(path)
    out: Dict[str, dict] = {}
    for _, r in df.iterrows():
        tk = str(r["ticker"]).strip().upper()
        out[tk] = {
            "ranking": _to_int(r.get("ranking")),
            "reason": r.get("reason"),
            "left": _to_int(r.get("left")),
            "right": _to_int(r.get("right")),
        }
    return out


def parse_divergence_td9_csv(path: str | Path) -> Dict[str, dict]:
    df = pd.read_csv(path)
    out: Dict[str, dict] = {}
    for _, r in df.iterrows():
        tk = str(r["ticker"]).strip().upper()
        out[tk] = {
            "group": str(r.get("group")).lower() if pd.notna(r.get("group")) else None,
            "signals": r.get("signals"),
            "td": _to_float(r.get("td")),
            "close": _to_float(r.get("close")),
        }
    return out


def parse_skdj_csv(path: str | Path) -> Dict[str, dict]:
    df = pd.read_csv(path)
    out: Dict[str, dict] = {}
    for _, r in df.iterrows():
        tk = str(r["ticker"]).strip().upper()
        out[tk] = {
            "scenario": r.get("scenario"),
            "k": _to_float(r.get("k")),
            "d": _to_float(r.get("d")),
            "both_le_20": _to_bool(r.get("both_le_20")),
            "both_ge_80": _to_bool(r.get("both_ge_80")),
            "cross": r.get("cross"),
            "data_status": r.get("data_status"),
            "data_asof": _to_evidence_date(r.get("data_asof")),
        }
    return out


_PARSERS = {
    "resonance": parse_resonance_csv,
    "rankings": parse_five_rankings_csv,
    "divergence": parse_divergence_td9_csv,
    "skdj": parse_skdj_csv,
}


# ============================================================
# 信号接入：CSV → SignalPack（仅信号字段）
# ============================================================
def build_signal_packs(reports, asof: str):
    """合并多份 CSV，按 ticker 构造 SignalPack（信号字段 + 四类详细字段；价格字段留默认）。

    返回 (packs, covered_sources)：
      packs: {ticker: SignalPack}
      covered_sources: {ticker: set(来源类型)} —— 该 ticker 出现在哪些来源 CSV
        （'resonance'/'rankings'/'divergence'/'skdj'）。子集缺席的来源不在集合里，
        供 caller 判断"信号消失"时区分"未覆盖=未知" vs "覆盖但无信号"。

    has_left_signal 触发条件（任一）：
      - SKDJ：both_le_20（超卖区）或 低位上穿（cross=='上穿' 且 k<=20）
      - 背离/TD9：group=='bull'（底背离）或 td>=9（TD9 setup）
      - 五榜单：ranking ∈ {2 背离观察池, 3 强趋势等待触发}
    """
    parsed: Dict[str, Dict[str, dict]] = {
        "resonance": {}, "rankings": {}, "divergence": {}, "skdj": {}}
    sources: Dict[str, List[str]] = {}

    for p in reports:
        kind = detect_report_kind(p)
        if kind is None:
            continue
        data = _PARSERS[kind](p)
        parsed[kind].update(data)
        for tk in data:
            sources.setdefault(tk, []).append(Path(p).name)

    all_tickers: set = set()
    for k in parsed:
        all_tickers |= set(parsed[k].keys())

    packs: Dict[str, SignalPack] = {}
    covered: Dict[str, set] = {}
    for tk in sorted(all_tickers):
        tags: List[str] = []
        close: Optional[float] = None
        cov: set = set()
        detail_parts: List[str] = []

        rs = parsed["resonance"].get(tk)
        if rs:
            cov.add("resonance")
            if rs.get("layer"):
                detail_parts.append(f"分层={rs['layer']}")
            if rs.get("close") is not None and close is None:
                close = rs["close"]

        rk = parsed["rankings"].get(tk)
        if rk:
            cov.add("rankings")
            rkn = rk.get("ranking")
            if rkn is not None:
                detail_parts.append(f"ranking={rkn}({RANK_NAME.get(rkn, '')})")
                if rkn in RANKING_LEFT:
                    tags.append(f"ranking_{RANK_NAME[rkn]}")

        sk = parsed["skdj"].get(tk)
        if sk:
            cov.add("skdj")
            if sk["both_le_20"]:
                tags.append("SKDJ_oversold")
            if str(sk.get("cross")) == "上穿" and sk.get("k") is not None and sk["k"] <= 20:
                tags.append("SKDJ_low_cross")
            k_v, d_v = sk.get("k"), sk.get("d")
            scen = sk.get("scenario")
            if k_v is not None or d_v is not None or scen:
                detail_parts.append(
                    f"SKDJ K={k_v} D={d_v} {scen or ''}".strip())

        dv = parsed["divergence"].get(tk)
        if dv:
            cov.add("divergence")
            g = dv.get("group")
            if g == "bull":
                tags.append("divergence_bull")
                detail_parts.append(f"底背离({dv.get('signals', '')})")
            elif g == "bear":
                detail_parts.append(f"顶背离({dv.get('signals', '')})")
            td = dv.get("td")
            if td is not None:
                detail_parts.append(f"TD9={int(td)}")
                if td >= 9:
                    tags.append("td9_setup")
            if dv.get("close") is not None:
                close = dv["close"]

        # transition_date 必须是真实行情截至日，不是报告生成日。
        # resonance / SKDJ 是全量日报且自带 data_asof，优先用它们定日；
        # synthetic/旧 CSV 没有该列时才兼容性回退到 CLI --asof。
        explicit_dates = {
            item.get("data_asof") for item in (rs, sk)
            if item and item.get("data_asof")
        }
        evidence_date = min(explicit_dates) if explicit_dates else asof
        data_sufficient = not _reports_data_insufficient(
            sk.get("data_status") if sk else None,
            rk.get("reason") if rk else None,
        )
        if len(explicit_dates) > 1:
            # 同一 ticker 的全量日报日期不一致时不混合推进状态。
            data_sufficient = False
            detail_parts.append(
                f"数据日期不一致={','.join(sorted(explicit_dates))}")
        if not data_sufficient:
            detail_parts.append("数据不足：记录并跳过状态判定")

        covered[tk] = cov
        packs[tk] = SignalPack(
            ticker=tk, evidence_date=evidence_date,
            has_left_signal=len(tags) > 0,
            left_signal_tags=tags,
            signal_disappeared=False,  # 由 caller 按 current_state + 覆盖情况补
            source_report=",".join(sources.get(tk, [])),
            source_detail="; ".join(detail_parts) or None,
            close=close,
            data_sufficient=data_sufficient,
            resonance_layer=(rs.get("layer") if rs else None),
            ranking=(rk.get("ranking") if rk else None),
            ranking_name=(RANK_NAME.get(rk.get("ranking")) if rk and rk.get("ranking") else None),
            skdj_scenario=(sk.get("scenario") if sk else None),
            skdj_k=(sk.get("k") if sk else None),
            skdj_d=(sk.get("d") if sk else None),
            divergence_flag=(dv is not None and dv.get("group") in ("bull", "bear", "both")),
            td9_count=(int(dv["td"]) if dv and dv.get("td") is not None else None),
        )
    return packs, covered


def tag_to_source(tag: Optional[str]) -> Optional[str]:
    """左侧信号 tag → 其来源类型（用于判断"入池信号来源今天是否覆盖"）。"""
    if not tag:
        return None
    if tag.startswith("SKDJ"):
        return "skdj"
    if tag.startswith("divergence") or tag.startswith("td9"):
        return "divergence"
    if tag.startswith("ranking"):
        return "rankings"
    return None


# ============================================================
# 价格确认：cache parquet → SignalPack 价格字段
# ============================================================
def load_cache_only(ticker: str, cache_dir: Path = CACHE_DIR):
    """只读 cache/nd100/{tk}_1d.parquet，不下载、不检查 TTL。缺文件返回 None。"""
    f = cache_dir / f"{ticker}_1d.parquet"
    if not f.exists():
        return None
    try:
        return pd.read_parquet(f)
    except Exception:
        return None


def truncate_to_asof(df, asof: str):
    """截断 df 到 index < asof（排除 asof 当天可能未完成的日线 + 防未来穿越）。

    返回 (df_cut, evidence_date)：evidence_date = 截断后实际最后数据日（≠ asof），
    避免日期错标。df_cut 为空时 evidence_date=None。
    """
    if df is None or len(df) == 0:
        return df, None
    asof_ts = pd.Timestamp(asof)
    df_cut = df[df.index < asof_ts]
    if len(df_cut) == 0:
        return df_cut, None
    ev = pd.Timestamp(df_cut.index[-1]).strftime("%Y-%m-%d")
    return df_cut, ev


def _series(df: pd.DataFrame, col: str):
    """优先用小写列（open/high/low/close/volume），兼容大写 Close。"""
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    up = col.capitalize()
    if up in df.columns:
        return pd.to_numeric(df[up], errors="coerce")
    return None


def compute_price_confirmation(df, invalidation_level: Optional[float],
                               observe_start_price: Optional[float]) -> dict:
    """从已完成日 K 计算 SignalPack 的价格确认字段。

    返回 dict（可直接 ** 展开成 SignalPack 关键字参数）：
      data_sufficient, close, structure_improved, structure_low,
      structure_degraded, approaching_trigger, breakout_confirmed, moved_away
    """
    if df is None or len(df) < MIN_BARS_PRICE:
        return {"data_sufficient": False, "close": None}

    low = _series(df, "low")
    high = _series(df, "high")
    close_s = _series(df, "close")
    vol = _series(df, "volume")
    if close_s is None or close_s.dropna().empty:
        return {"data_sufficient": False, "close": None}

    last_close = float(close_s.iloc[-1])
    res: Dict[str, Any] = {
        "data_sufficient": True,
        "close": last_close,
        "structure_improved": False,
        "structure_low": None,
        "structure_degraded": False,
        "approaching_trigger": False,
        "breakout_confirmed": False,
        "moved_away": False,
    }

    win = PRICE_CONFIG["structure_window"]
    # 结构改善/退化：用【不含当天】的历史 low（当天刚收盘，结构判断用之前）
    # recent = 当天之前 win 根；prev = 再前 win 根
    if low is not None and len(low) >= 2 * win + 1:
        recent = low.iloc[-win - 1:-1].min()
        prev = low.iloc[-2 * win - 1:-win - 1].min()
        if pd.notna(recent) and pd.notna(prev):
            if recent > prev:
                res["structure_improved"] = True
                res["structure_low"] = float(prev)
            elif recent < prev:
                res["structure_degraded"] = True

    # 触发位 = 【不含当天】的前 trigger_window 根 high 最大值
    # （合法 OHLC 中 close <= 当天 high，若 trigger 含当天则 close>trigger 永不成立）
    tw = PRICE_CONFIG["trigger_window"]
    trigger = None
    if high is not None and len(high) >= tw + 1:
        trigger = float(high.iloc[-tw - 1:-1].max())
        if trigger and last_close >= trigger * PRICE_CONFIG["approach_ratio"]:
            res["approaching_trigger"] = True

    # 突破 + 量能：均量也用【不含当天】的前 tw 根；当天 close 突破前高 + 当天量能放大
    if trigger and vol is not None and len(vol) >= tw + 1:
        mean_vol = float(vol.iloc[-tw - 1:-1].mean())
        last_vol = float(vol.iloc[-1])
        if mean_vol > 0 and last_close > trigger and last_vol >= PRICE_CONFIG["breakout_vol_ratio"] * mean_vol:
            res["breakout_confirmed"] = True

    # 已走远：较观察起点收盘上涨 >= moved_away_pct
    if observe_start_price and observe_start_price > 0:
        if last_close >= observe_start_price * (1 + PRICE_CONFIG["moved_away_pct"] / 100.0):
            res["moved_away"] = True

    return res


def get_observe_start_price(db, ticker: str, episode_id: Optional[int]) -> Optional[float]:
    """从 transitions 取该 episode 首条（OBSERVE 入池）的 price 作为观察起点。"""
    if episode_id is None:
        return None
    row = db.conn.execute(
        "SELECT price FROM transitions WHERE episode_id=? AND ticker=? "
        "ORDER BY transition_id ASC LIMIT 1",
        (episode_id, ticker),
    ).fetchone()
    if row and row["price"] is not None:
        return float(row["price"])
    return None


def price_manifest() -> dict:
    """导出价格确认阈值片段，供 manifest。"""
    return dict(PRICE_CONFIG)
