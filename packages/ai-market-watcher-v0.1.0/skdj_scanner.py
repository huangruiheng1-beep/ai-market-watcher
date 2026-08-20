#!/usr/bin/env python3
"""
SKDJ 超跌观察池扫描器  -  SKDJ Oversold Observation Pool Scanner
================================================================
对应《数字资产市场观察助手》PPT 第 6、19 页：SKDJ 慢速随机指标只用来把
"可能已经很热或很冷"的股票单独列出，给人工继续看图；不输出买入、卖出或反转结论。

核心纪律（照抄 PPT）：
  - 20 以下是超卖观察区，80 以上是超买观察区。
  - 低位 K 上穿 D 只代表"早期观察"；高位 K 下穿 D 只代表"动能转弱观察"。
  - SKDJ 必须叠加背离、价格关键位和多周期方向，不能直接等同于买卖点。

数据层：复用 nd100_resonance_scanner 的下载/缓存层（cache/nd100/{tk}_1d.parquet），
        不新建第二套数据层、不读取/打印/复制 API Key。

公式状态：candidate_skdj_9_3_v1 —— PPT 未给出精确公式/参数，本 profile 为常用慢速
        SKDJ 候选，未经与原系统逐根核对，manifest 标注 formula_status=candidate_unverified。
        不得宣称已精确复刻 PPT 原系统。

用法:
    python skdj_scanner.py --source synthetic --output-tag phase1-smoke
    python skdj_scanner.py --tickers AAPL,APP,META --cache-only --output-tag cache-smoke
    python skdj_scanner.py --nd100-input /path/nd100_filtered.csv --cache-only
依赖: pandas numpy
"""

import sys
import json
import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from nd100_resonance_scanner import (  # noqa: E402
    CACHE_DIR, OUTPUT_DIR, last_data_asof, load_batch_or_download,
)

NY_TZ = ZoneInfo("America/New_York")

# ============================================================
# 公式 profile
# ============================================================
# PPT 未提供精确计算式 / N、M 参数 / EMA 初始化方式 / 使用 K 还是 D 判定进入 20/80。
# 这里先固定一套常用慢速 SKDJ 候选公式，所有参数写进 manifest，不隐藏在代码里。
FORMULA_PROFILES = {
    "candidate_skdj_9_3_v1": {
        "n": 9,                 # LOWV/HIGHV 回看窗口
        "m": 3,                 # RSV/K 的 EMA 平滑周期
        "oversold": 20,         # 超卖观察线（PPT 文字）
        "overbought": 80,       # 超买观察线（PPT 文字）
        "rsv_smoothing": "EMA",
        "k_smoothing": "EMA",
        "d_smoothing": "SMA",   # D = MA(K, M)
        "d_window": 3,           # D 的简单移动平均窗口（= M）
        "ema_adjust": False,    # ewm(span=m, adjust=False)：首值=自身，递推
        "boundary": "<= 20 / >= 80",
        "formula_status": "candidate_unverified",
        "note": "常用慢速 SKDJ 候选公式；PPT 未给出精确公式/参数，未经逐根核对。",
    },
}
DEFAULT_PROFILE = "candidate_skdj_9_3_v1"

TIMEFRAME = "1d"           # Phase 1 只算日线
MIN_BARS = "max(60, n + 2*m + 10)"   # 最低数据长度说明（实际按 profile 的 n/m 计算）
PERIOD_REAL = "2y"         # 真实下载周期（与共振扫描器日线一致）

# PPT 第 19 页三类展示
SCENARIO_ORDER = ["下跌超跌", "上升回调", "顶部超买", "待人工分类", "普通区"]
SCENARIO_COLOR = {
    "下跌超跌": "#c0392b",
    "上升回调": "#2471a3",
    "顶部超买": "#8e44ad",
    "待人工分类": "#7f8c8d",
    "普通区": "#bdc3c7",
}


# ============================================================
# SKDJ 计算
# ============================================================
def calc_skdj(high, low, close, n=9, m=3, ema_adjust=False, d_window=3):
    """计算 SKDJ 候选公式 candidate_skdj_9_3_v1。

    LOWV_t  = 最近 N 根 K 线的 Low 最小值
    HIGHV_t = 最近 N 根 K 线的 High 最大值
    RAW_t   = 100 * (Close_t - LOWV_t) / (HIGHV_t - LOWV_t)
    RSV_t   = EMA(RAW, M)
    K_t     = EMA(RSV, M)
    D_t     = MA(K, M)   # 简单移动平均

    HIGHV == LOWV（一字板/极窄区间）时 RAW 必须为 NaN，不允许无穷大或虚假交叉。
    返回 DataFrame(index 同输入): raw, rsv, k, d。
    """
    high = pd.Series(high, dtype=float)
    low = pd.Series(low, dtype=float)
    close = pd.Series(close, dtype=float)
    lowv = low.rolling(n).min()
    highv = high.rolling(n).max()
    rng = highv - lowv
    # 分母为 0 时整体置 NaN：防除零、防假信号
    raw = 100.0 * (close - lowv) / rng
    raw = raw.where(rng != 0, np.nan)
    rsv = raw.ewm(span=m, adjust=ema_adjust).mean()
    k = rsv.ewm(span=m, adjust=ema_adjust).mean()
    d = k.rolling(d_window).mean()
    return pd.DataFrame({"raw": raw, "rsv": rsv, "k": k, "d": d})


def detect_cross(k, d):
    """交叉：返回与 k 同长的 Series，值 '上穿'/'下穿'/''。

    上穿 = K_t > D_t 且 K_(t-1) <= D_(t-1)
    下穿 = K_t < D_t 且 K_(t-1) >= D_(t-1)
    只标记交叉当根，不连续重复。
    """
    k = pd.Series(k, dtype=float)
    d = pd.Series(d, dtype=float)
    prev_k = k.shift(1)
    prev_d = d.shift(1)
    up = (k > d) & (prev_k <= prev_d)
    down = (k < d) & (prev_k >= prev_d)
    cross = pd.Series("", index=k.index, dtype=object)
    cross[up.fillna(False)] = "上穿"
    cross[down.fillna(False)] = "下穿"
    return cross


def _normalize_ohlcv(df):
    """缓存里 Twelve Data 用小写列名；统一成大写，只改内存副本。"""
    if df is None:
        return None
    rename = {
        lo: up for lo, up in {
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        }.items() if lo in df.columns and up not in df.columns
    }
    return df.rename(columns=rename) if rename else df


def _series(df, col):
    s = df[col]
    return s.squeeze() if isinstance(s, pd.DataFrame) else s


def _completed_daily_frame(df, now=None):
    """只保留已完成交易日，永不把当前美东自然日当作日线收盘。"""
    if df is None or len(df) == 0:
        return df
    out = df.copy()
    now = pd.Timestamp.now(tz=NY_TZ) if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize(NY_TZ)
    else:
        now = now.tz_convert(NY_TZ)
    keep = []
    for pos, value in enumerate(out.index):
        try:
            ts = pd.Timestamp(value)
            if ts.tzinfo is None:
                ts = ts.tz_localize(NY_TZ)
            else:
                ts = ts.tz_convert(NY_TZ)
            keep.append((pos, ts.date() < now.date()))
        except (TypeError, ValueError):
            keep.append((pos, False))
    positions = [pos for pos, include in keep if include]
    if not positions:
        return out.iloc[0:0]
    return out.iloc[positions].sort_index()


def _trend_to_str(trend_context):
    """把 ND100 输入的趋势上下文 dict 转成可读字符串。"""
    if not trend_context:
        return None
    parts = []
    for key in ("分层", "日线_方向", "周线_方向"):
        val = str(trend_context.get(key, "") or "").strip()
        if val:
            parts.append(f"{key}={val}")
    return "; ".join(parts) if parts else None


def analyze_skdj(ticker, df_d, profile_name, trend_context=None):
    """单标的 SKDJ 分析，返回一行结果 dict。

    trend_context: 来自 ND100 输入的 {分层, 日线_方向, 周线_方向}；无则 None。
    """
    p = FORMULA_PROFILES[profile_name]
    n, m = p["n"], p["m"]
    d_window = p["d_window"]
    oversold, overbought = p["oversold"], p["overbought"]
    min_len = max(60, n + 2 * m + 10)

    res = {
        "ticker": ticker, "data_asof": None, "timeframe": TIMEFRAME,
        "formula_profile": profile_name, "n": n, "m": m,
        "k": None, "d": None, "prev_k": None, "prev_d": None,
        "k_le_20": False, "d_le_20": False, "both_le_20": False,
        "k_ge_80": False, "d_ge_80": False, "both_ge_80": False,
        "cross": "无", "trend_context": _trend_to_str(trend_context),
        "scenario": "普通区", "observation_label": "",
        "needs_human_confirmation": False, "data_status": "数据不足",
    }

    df_d = _normalize_ohlcv(df_d)
    df_d = _completed_daily_frame(df_d)
    if df_d is None or len(df_d) < min_len:
        return res
    for col in ("High", "Low", "Close"):
        if col not in df_d.columns:
            return res

    high = pd.to_numeric(_series(df_d, "High"), errors="coerce")
    low = pd.to_numeric(_series(df_d, "Low"), errors="coerce")
    close = pd.to_numeric(_series(df_d, "Close"), errors="coerce")
    recent = pd.concat({"High": high, "Low": low, "Close": close}, axis=1).tail(n)
    if len(recent) < n or not np.isfinite(recent.to_numpy(dtype=float)).all():
        return res

    skdj = calc_skdj(high, low, close, n, m, p["ema_adjust"], d_window)
    k, d = skdj["k"], skdj["d"]
    if len(k) == 0 or pd.isna(k.iloc[-1]) or pd.isna(d.iloc[-1]):
        # 有效 K/D 不够（含一字板导致 RAW 全 NaN 的情况）
        res["data_asof"] = last_data_asof(df_d)
        return res

    k_last = float(k.iloc[-1])
    d_last = float(d.iloc[-1])
    k_prev = float(k.iloc[-2]) if len(k) >= 2 and not pd.isna(k.iloc[-2]) else None
    d_prev = float(d.iloc[-2]) if len(d) >= 2 and not pd.isna(d.iloc[-2]) else None

    # 区域布尔（最后一根，边界按 PPT 文字 <= 20 / >= 80）
    k_le_20 = k_last <= oversold
    d_le_20 = d_last <= oversold
    both_le_20 = bool(k_le_20 and d_le_20)
    k_ge_80 = k_last >= overbought
    d_ge_80 = d_last >= overbought
    both_ge_80 = bool(k_ge_80 and d_ge_80)

    # 交叉（只在最后一根报告今日交叉）
    cross = "无"
    if k_prev is not None and d_prev is not None:
        if k_last > d_last and k_prev <= d_prev:
            cross = "上穿"
        elif k_last < d_last and k_prev >= d_prev:
            cross = "下穿"

    # 低位上穿 / 高位下穿（"今日或前一日至少有一条线越界"）
    prev_k_le_20 = k_prev is not None and k_prev <= oversold
    prev_d_le_20 = d_prev is not None and d_prev <= oversold
    prev_k_ge_80 = k_prev is not None and k_prev >= overbought
    prev_d_ge_80 = d_prev is not None and d_prev >= overbought
    low_cross = (cross == "上穿") and (
        k_le_20 or d_le_20 or prev_k_le_20 or prev_d_le_20)
    high_cross = (cross == "下穿") and (
        k_ge_80 or d_ge_80 or prev_k_ge_80 or prev_d_ge_80)

    # 原始观察标签（可多个，便于审计）
    labels = []
    if both_le_20:
        labels.append("超卖区")
    if both_ge_80:
        labels.append("超买区")
    if low_cross:
        labels.append("低位上穿")
    if high_cross:
        labels.append("高位下穿")

    # 趋势上下文（来自 ND100 输入）
    tc = trend_context or {}
    layer = str(tc.get("分层", "") or "").strip()
    daily = str(tc.get("日线_方向", "") or "").strip()
    weekly = str(tc.get("周线_方向", "") or "").strip()
    bearish = layer in ("空头共振", "偏空") or daily == "空" or weekly == "空"
    bullish = layer in ("多头共振", "偏多") or daily == "多" or weekly == "多"

    # 主分组（唯一）：顶部超买 > 下跌超跌/上升回调 > 待人工分类 > 普通区
    in_oversold = both_le_20 or low_cross
    in_overbought = both_ge_80 or high_cross
    scenario = "普通区"
    if in_overbought:
        scenario = "顶部超买"
    elif in_oversold:
        if bullish and not bearish:
            scenario = "上升回调"
        elif bearish and not bullish:
            scenario = "下跌超跌"
        else:
            scenario = "待人工分类"

    res.update({
        "data_asof": last_data_asof(df_d),
        "k": round(k_last, 2), "d": round(d_last, 2),
        "prev_k": round(k_prev, 2) if k_prev is not None else None,
        "prev_d": round(d_prev, 2) if d_prev is not None else None,
        "k_le_20": bool(k_le_20), "d_le_20": bool(d_le_20),
        "both_le_20": both_le_20,
        "k_ge_80": bool(k_ge_80), "d_ge_80": bool(d_ge_80),
        "both_ge_80": both_ge_80,
        "cross": cross,
        "scenario": scenario,
        "observation_label": "; ".join(labels),
        "needs_human_confirmation": scenario != "普通区",
        "data_status": "ok",
    })
    return res


# ============================================================
# 数据加载
# ============================================================
def load_cache_only(ticker, interval=TIMEFRAME):
    """只读现有缓存，缺数据返回 None，绝不请求 API。"""
    cache_file = CACHE_DIR / f"{ticker}_{interval}.parquet"
    try:
        return pd.read_parquet(cache_file)
    except (FileNotFoundError, OSError, ValueError):
        return None


# ============================================================
# 合成数据源（覆盖全部场景，无需联网）
# ============================================================
# 为了精确控制末段 K/D 落点，超卖/超买场景让 close 持续创新低/高（RAW 恒为 0/100），
# 低位上穿/高位下穿在末根反向跳动触发交叉。趋势上下文由 SYNTH_PLAN 显式注入，
# 以便稳定展示"下跌超跌 / 上升回调 / 顶部超买"三类。
SYNTH_PLAN = [
    ("AAPL", "oversold",    {"分层": "空头共振", "日线_方向": "空", "周线_方向": "空"}),
    ("NVDA", "low_cross",   {"分层": "多头共振", "日线_方向": "多", "周线_方向": "多"}),
    ("MSFT", "overbought",  {"分层": "多头共振", "日线_方向": "多", "周线_方向": "多"}),
    ("GOOGL", "high_cross", {"分层": "多头共振", "日线_方向": "多", "周线_方向": "多"}),
    ("AMZN", "normal",      None),
    ("META", "insufficient", None),
]


def _gen_synth_close(scenario, seed):
    rng = np.random.default_rng(seed)
    if scenario == "oversold":
        c = np.concatenate([
            100.0 + rng.normal(0, 0.3, 60),
            np.linspace(100, 70, 30),
        ])
    elif scenario == "low_cross":
        c = np.concatenate([
            100.0 + rng.normal(0, 0.3, 60),
            np.linspace(100, 70, 28),   # idx 60..87 递减
            [88.0],                      # idx 88 反弹 -> 触发上穿
        ])
    elif scenario == "overbought":
        c = np.concatenate([
            100.0 + rng.normal(0, 0.3, 60),
            np.linspace(100, 130, 30),
        ])
    elif scenario == "high_cross":
        c = np.concatenate([
            100.0 + rng.normal(0, 0.3, 60),
            np.linspace(100, 130, 28),
            [112.0],                     # idx 88 回落 -> 触发下穿
        ])
    elif scenario == "normal":
        t = np.arange(90)
        c = 102.0 + 2.0 * np.sin(2 * np.pi * t / 8.0) + rng.normal(0, 0.05, 90)
    elif scenario == "insufficient":
        c = 100.0 + rng.normal(0, 0.3, 10)
    else:
        c = 100.0 + rng.normal(0, 0.3, 90)
    return np.asarray(c, dtype=float)


def _to_ohlcv_synth(close, seed, scenario):
    n = len(close)
    rng = np.random.default_rng(seed + 7)
    # 合成数据也明确落在最近一个已完成交易日，避免被日线收盘保护逻辑丢掉末根。
    end = pd.Timestamp.now(tz=NY_TZ).normalize() - pd.Timedelta(days=1)
    dates = pd.bdate_range(end=end, periods=n)
    df = pd.DataFrame(index=dates)
    df["Close"] = close
    df["Open"] = close
    # 超卖/低位上穿：下影为 0，上影极小 -> close 持续创新低时 RAW 恒为 0
    # 超买/高位下穿：上影为 0，下影极小 -> close 持续创新高时 RAW 恒为 100
    if scenario in ("oversold", "low_cross"):
        df["Low"] = close
        df["High"] = close * (1 + 1e-4 + rng.uniform(0, 1e-5, n))
    elif scenario in ("overbought", "high_cross"):
        df["High"] = close
        df["Low"] = close * (1 - 1e-4 - rng.uniform(0, 1e-5, n))
    else:
        df["High"] = close * (1 + 5e-4)
        df["Low"] = close * (1 - 5e-4)
    df["Volume"] = rng.normal(1_000_000, 100_000, n).clip(400_000)
    return df


def scan_synthetic(profile_name):
    rows = []
    for tk, sc, tc in SYNTH_PLAN:
        seed = sum(ord(ch) for ch in tk) * 31 + 17
        close = _gen_synth_close(sc, seed)
        df = _to_ohlcv_synth(close, seed, sc)
        rows.append(analyze_skdj(tk, df, profile_name, tc))
    return rows


# ============================================================
# 真实/缓存扫描
# ============================================================
def scan(tickers, profile_name, cache_only=False, use_cache=True, trend_map=None):
    trend_map = trend_map or {}
    if cache_only:
        data = {tk: load_cache_only(tk) for tk in tickers}
    else:
        data = load_batch_or_download(tickers, TIMEFRAME, PERIOD_REAL, use_cache)
    rows = []
    for tk in tickers:
        r = analyze_skdj(tk, data.get(tk), profile_name, trend_map.get(tk))
        print(f"  {tk:<7} K={r['k']!s:<6} D={r['d']!s:<6} "
              f"交叉={r['cross']:<3} 分组={r['scenario']:<6} "
              f"数据={r['data_status']}")
        rows.append(r)
    return rows


# ============================================================
# HTML 报告
# ============================================================
CSS = """
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;
         background: #f6f7f9; color: #1c2733; margin: 0; padding: 24px; }
  .wrap { max-width: 1060px; margin: 0 auto; }
  .hd { display:flex; justify-content:space-between; align-items:flex-end;
        border-bottom: 3px solid #2c3e50; padding-bottom: 12px; margin-bottom: 18px; }
  .hd h1 { font-size: 23px; margin:0; font-weight:800; }
  .hd .sub { font-size: 12.5px; color:#6b7785; margin-top:6px; }
  .hd .meta { text-align:right; font-size:12px; color:#6b7785; line-height:1.6; }
  .hd .meta b { color:#1c2733; }
  .src { font-size:11px; background:#eef2f7; color:#3b556f; padding:2px 8px;
         border-radius:10px; margin-left:8px; vertical-align:middle; }
  .unverified { font-size:11px; background:#fff4e0; color:#9a6b00; padding:2px 8px;
                border-radius:10px; margin-left:6px; vertical-align:middle; }
  .discipline { background:#fff8e6; border:1px solid #f3d98b; border-left:4px solid #e0a800;
                border-radius:8px; padding:12px 16px; margin-bottom:18px; font-size:13px; line-height:1.7; }
  .discipline b { color:#9a6b00; }
  .stats { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }
  .stat { flex:1; min-width:110px; background:#fff; border-radius:10px; padding:14px 16px;
          box-shadow:0 1px 3px rgba(0,0,0,.06); border-top:3px solid #cbd3dc; }
  .stat .n { font-size:25px; font-weight:800; }
  .stat .l { font-size:11.5px; color:#6b7785; margin-top:2px; }
  .stat.s-oversold { border-top-color:#c0392b; } .stat.s-oversold .n { color:#c0392b; }
  .stat.s-pullback { border-top-color:#2471a3; } .stat.s-pullback .n { color:#2471a3; }
  .stat.s-overbought { border-top-color:#8e44ad; } .stat.s-overbought .n { color:#8e44ad; }
  .stat.s-manual { border-top-color:#7f8c8d; } .stat.s-manual .n { color:#7f8c8d; }
  .stat.s-quiet { border-top-color:#bdc3c7; } .stat.s-quiet .n { color:#7f8c8d; }
  .stat.s-insuf { border-top-color:#bdc3c7; } .stat.s-insuf .n { color:#7f8c8d; }
  .sec { margin:22px 0 10px; font-size:16px; font-weight:700; display:flex; align-items:center; gap:8px; }
  .sec .tag { font-size:11px; font-weight:600; padding:2px 9px; border-radius:10px; color:#fff; }
  .card { background:#fff; border-radius:10px; padding:13px 16px; margin-bottom:11px;
          box-shadow:0 1px 3px rgba(0,0,0,.06); border-left:4px solid #cbd3dc; }
  .card.s-oversold { border-left-color:#c0392b; }
  .card.s-pullback { border-left-color:#2471a3; }
  .card.s-overbought { border-left-color:#8e44ad; }
  .card.s-manual { border-left-color:#7f8c8d; }
  .card.s-quiet { border-left-color:#bdc3c7; }
  .card .chd { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }
  .card .tk { font-size:17px; font-weight:800; }
  .card .lab { font-size:11.5px; color:#9a6b00; background:#fff8e6; border-radius:6px;
               padding:3px 9px; margin-left:8px; }
  .card .vals { font-size:12.5px; color:#46535f; }
  .card .vals b { color:#1c2733; }
  .card .meta { font-size:11.5px; color:#8a96a3; margin-top:4px; }
  .empty { color:#8a96a3; font-size:12.5px; padding:6px 2px; }
  .foot { margin-top:26px; font-size:11.5px; color:#8a96a3; line-height:1.7;
          border-top:1px solid #e2e7ec; padding-top:12px; }
"""


def _card_html(r):
    if r.get("data_status") != "ok":
        return (f"<div class='card'><div class='chd'><span class='tk'>{r['ticker']}</span></div>"
                f"<div class='empty'>数据不足，跳过（不产生观察标签）</div></div>")
    cls = "s-" + {
        "下跌超跌": "oversold", "上升回调": "pullback", "顶部超买": "overbought",
        "待人工分类": "manual", "普通区": "quiet",
    }.get(r["scenario"], "quiet")
    lab = (f"<span class='lab'>{r['observation_label']}</span>"
           if r["observation_label"] else "")
    cross_txt = {"上穿": "K 上穿 D", "下穿": "K 下穿 D", "无": "无交叉"}.get(r["cross"], r["cross"])
    tc_txt = r["trend_context"] or "未提供"
    vals = (f"K <b>{r['k']}</b> · D <b>{r['d']}</b> · 前日 K {r['prev_k']} / D {r['prev_d']} · "
            f"交叉 <b>{cross_txt}</b>")
    meta = (f"趋势上下文：{tc_txt}　|　数据截至：{r['data_asof'] or '-'}　|　"
            f"公式 {r['formula_profile']}（N={r['n']}, M={r['m']}）")
    return f"""<div class='card {cls}'>
    <div class='chd'><span><span class='tk'>{r['ticker']}</span>{lab}</span>
      <span class='vals'>{vals}</span></div>
    <div class='meta'>{meta}</div>
  </div>"""


def gen_html(rows, profile_name, out_path, source_label="真实/缓存", report_date=None):
    p = FORMULA_PROFILES[profile_name]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(rows)
    counts = {s: sum(1 for r in rows if r["scenario"] == s) for s in SCENARIO_ORDER}
    n_insuf = sum(1 for r in rows if r["data_status"] != "ok")

    def sec(title, tag, cls, items):
        if not items:
            return ""
        cards = "".join(_card_html(r) for r in items)
        color = SCENARIO_COLOR.get(cls, "#7f8c8d")
        return (f"<div class='sec'><span class='tag' style='background:{color}'>"
                f"{tag}</span>{title} · {len(items)} 只</div>{cards}")

    order = [
        ("下跌超跌", "超跌", "下跌超跌"),
        ("上升回调", "回调", "上升回调"),
        ("顶部超买", "超买", "顶部超买"),
        ("待人工分类", "人工", "待人工分类"),
        ("普通区", "普通", "普通区"),
    ]
    sections = "".join(
        sec(t, tag, s, [r for r in rows if r["scenario"] == s and r["data_status"] == "ok"])
        for s, tag, t in order
    )
    insuf = [r for r in rows if r["data_status"] != "ok"]
    if insuf:
        sections += (f"<div class='sec'><span class='tag' style='background:#bdc3c7'>数据不足</span>"
                    f"数据不足 · {len(insuf)} 只</div>"
                    + "".join(_card_html(r) for r in insuf))

    _stat_cls = {'下跌超跌': 'oversold', '上升回调': 'pullback', '顶部超买': 'overbought',
                 '待人工分类': 'manual', '普通区': 'quiet'}
    stat_cards = "".join(f"""
      <div class='stat s-{_stat_cls[s]}'>
        <div class='n'>{counts[s]}</div><div class='l'>{s}</div></div>""" for s in SCENARIO_ORDER)
    stat_cards += f"<div class='stat s-insuf'><div class='n'>{n_insuf}</div><div class='l'>数据不足</div></div>"

    html = f"""<!DOCTYPE html>
<html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>SKDJ 超跌观察池 · 行情日 {report_date or now.split()[0]}</title>
<style>{CSS}</style></head>
<body><div class='wrap'>
  <div class='hd'>
    <div>
      <h1>SKDJ 超跌观察池<span class='src'>{source_label}</span><span class='unverified'>公式未验证</span></h1>
      <div class='sub'>SKDJ 慢速随机指标 · 只输出"可能已经很热或很冷"的观察候选，不预测反转</div>
    </div>
    <div class='meta'>生成时间<b><br>{now}</b><br>标的数<b> {total}</b></div>
  </div>

  <div class='discipline'>
    <b>核心纪律（照抄 PPT 第 6、19 页）：</b>20 以下为超卖观察区，80 以上为超买观察区；
    低位 K 上穿 D 只代表"早期观察"，高位 K 下穿 D 只代表"动能转弱观察"。
    SKDJ 必须叠加背离、价格关键位和多周期方向，<b>仅供观察，等待价格结构 / 背离 / 关键位确认，不构成任何交易指令</b>。
  </div>

  <div class='stats'>{stat_cards}</div>

  {sections}

  <div class='foot'>
    方法学：SKDJ(N={p['n']}, M={p['m']}) 基于日线；RAW=100*(C-LOWV)/(HIGHV-LOWV)，
    RSV/K 用 EMA(M) 平滑，D=MA(K, M)；边界按 PPT 文字使用 {p['boundary']}。
    分类按 PPT 第 19 页：下跌超跌 / 上升回调 / 顶部超买；无趋势上下文进"待人工分类"。
    <b>公式状态：{p['formula_status']}</b> —— PPT 未给出精确公式/参数，本 profile 为候选，
    未经与原系统逐根核对，不宣称已复刻博士原系统。本工具为 PPT 工作流的代码复刻，
    仅供观察与复盘，不构成任何投资建议。
  </div>
</div></body></html>"""
    out_path.write_text(html, encoding="utf-8")


# ============================================================
# 入口
# ============================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="SKDJ 超跌观察池扫描器")
    ap.add_argument("--tickers", help="只扫指定股票，逗号分隔 (如 AAPL,APP,META)")
    ap.add_argument("--nd100-input", action="append",
                    help="读取 ND100 扫描 CSV，取 ticker 与趋势上下文；可重复传入多批")
    ap.add_argument("--output-dir", help="输出目录；默认 output/")
    ap.add_argument("--output-tag", help="追加到输出文件名，避免覆盖历史报告")
    ap.add_argument("--cache-only", action="store_true",
                    help="只读现有缓存，缺数据返回数据不足，禁止请求 API")
    ap.add_argument("--no-cache", action="store_true", help="忽略本地缓存重新下载")
    ap.add_argument("--source", default="real", choices=["real", "synthetic"],
                    help="数据源：real(默认，复用缓存/API) 或 synthetic(演示)")
    ap.add_argument("--formula-profile", default=DEFAULT_PROFILE,
                    choices=list(FORMULA_PROFILES), help="公式 profile")
    args = ap.parse_args(argv)

    if args.tickers and args.nd100_input:
        ap.error("--tickers 和 --nd100-input 只能二选一")
    if args.cache_only and args.no_cache:
        ap.error("--cache-only 与 --no-cache 不能同时使用")
    if args.source == "synthetic" and (args.cache_only or args.no_cache):
        ap.error("--source synthetic 不应与 --cache-only / --no-cache 同时使用")

    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    scan_date = datetime.now().strftime("%Y%m%d")
    tag = f"_{args.output_tag}" if args.output_tag else ""
    profile = args.formula_profile
    p = FORMULA_PROFILES[profile]

    manifest = {
        "report_date": scan_date,
        "scan_date": scan_date,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "timeframe": TIMEFRAME,
        "formula_profile": profile,
        "formula_params": {
            "n": p["n"], "m": p["m"], "oversold": p["oversold"],
            "overbought": p["overbought"], "d_smoothing": p["d_smoothing"],
            "d_window": p["d_window"], "ema_adjust": p["ema_adjust"],
            "boundary": p["boundary"],
        },
        "formula_status": p["formula_status"],
        "note": p["note"],
        "min_bars_rule": MIN_BARS,
    }

    if args.source == "synthetic":
        print("\n=== SKDJ 超跌观察池（synthetic 演示）===")
        rows = scan_synthetic(profile)
        manifest.update({
            "source": "synthetic",
            "input_ticker_count": len(rows),
            "input_tickers": [r["ticker"] for r in rows],
            "data_source": "合成 OHLCV，覆盖超卖/低位上穿/超买/高位下穿/普通区/数据不足",
            "cache_or_api": "无（合成）",
            "trend_context_note": "synthetic 的趋势上下文由 SYNTH_PLAN 注入，仅用于演示三类展示",
            "missing_data_tickers": [r["ticker"] for r in rows if r["data_status"] != "ok"],
        })
        manifest["market_data_asof"] = "2026-08-18"
        manifest["market_date"] = "2026-08-18"
    else:
        trend_map = {}
        tickers = []
        if args.nd100_input:
            for input_file in args.nd100_input:
                ndf = pd.read_csv(input_file, encoding="utf-8-sig")
                if "ticker" not in ndf.columns:
                    ap.error(f"--nd100-input 缺少 ticker 列: {input_file}")
                for _, row in ndf.iterrows():
                    tk = str(row["ticker"]).strip().upper()
                    if not tk or tk in trend_map:
                        continue
                    tickers.append(tk)
                    trend_map[tk] = {
                        "分层": row.get("分层", ""),
                        "日线_方向": row.get("日线_方向", ""),
                        "周线_方向": row.get("周线_方向", ""),
                    }
        elif args.tickers:
            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        else:
            ap.error("请用 --tickers 或 --nd100-input 指定标的（本阶段不默认全量 ND100）")
        # 大写、去空、去重，保留首次出现顺序
        seen, uniq = set(), []
        for t in tickers:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        tickers = uniq
        if not tickers:
            ap.error("没有可用 ticker")

        use_cache = not args.no_cache
        print(f"\n=== SKDJ 超跌观察池 · ND100 ===\n标的数: {len(tickers)} "
              f"公式: {profile}({p['formula_status']})\n")
        rows = scan(tickers, profile, cache_only=args.cache_only,
                    use_cache=use_cache, trend_map=trend_map)
        cache_label = "仅缓存（不请求 API）" if args.cache_only else "缓存或受保护 API"
        manifest.update({
            "source": "real",
            "input_file": args.nd100_input or "(--tickers)",
            "input_ticker_count": len(tickers),
            "input_tickers": tickers,
            "data_source": cache_label,
            "cache_or_api": cache_label,
            "missing_data_tickers": [r["ticker"] for r in rows if r["data_status"] != "ok"],
        })

    asofs = [r["data_asof"] for r in rows if r["data_asof"]]
    manifest["market_data_asof"] = max(asofs) if asofs else None
    manifest["market_date"] = manifest["market_data_asof"][:10] if manifest["market_data_asof"] else None
    output_date = manifest["market_date"].replace("-", "") if manifest["market_date"] else scan_date
    if args.source == "synthetic":
        manifest["market_data_asof"] = "2026-08-18"
        manifest["market_date"] = "2026-08-18"
        output_date = "20260818"
    manifest["report_date"] = output_date
    manifest["scan_date"] = scan_date
    manifest["date_semantics"] = {
        "report_date": "文件/行情归档日",
        "market_date": "本次输入所覆盖的最新完整美股交易日",
    }
    manifest["pool_counts"] = {s: sum(1 for r in rows if r["scenario"] == s) for s in SCENARIO_ORDER}
    manifest["pool_counts"]["数据不足"] = sum(1 for r in rows if r["data_status"] != "ok")

    # CSV
    csv_path = output_dir / f"skdj_{output_date}{tag}.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n[CSV] {csv_path}")
    # HTML
    html_path = output_dir / f"skdj_{output_date}{tag}.html"
    source_label = "演示数据 · synthetic" if args.source == "synthetic" else "真实/缓存"
    gen_html(rows, profile, html_path, source_label=source_label,
             report_date=f"{output_date[:4]}-{output_date[4:6]}-{output_date[6:]}")
    print(f"[HTML] {html_path}")
    # manifest
    manifest_path = output_dir / f"skdj_{output_date}{tag}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"[MANIFEST] {manifest_path}")

    print("\n=== SKDJ 观察池分布 ===")
    for s in SCENARIO_ORDER:
        print(f"  {s:<6} {manifest['pool_counts'][s]}")
    print(f"  {'数据不足':<6} {manifest['pool_counts']['数据不足']}")
    print("\n查看顺序：下跌超跌 → 上升回调 → 顶部超买 → 待人工分类（先看进入超跌区的）")


if __name__ == "__main__":
    main()
