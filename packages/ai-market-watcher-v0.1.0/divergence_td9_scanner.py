#!/usr/bin/env python3
"""
背离 + TD9 扫描器  ·  Divergence & TD9 Scanner
=============================================
复刻《数字资产市场观察助手》PPT 第 10 页「背离 / TD9」已落地模块：

  · RSI 背离（底/顶）：价格新低/新高 + RSI 未同步
  · MACD 背离（底/顶）：价格新低/新高 + DIF 线未同步
  · 价量背离（底/顶）：价格新低/新高 + 成交量萎缩
  · TD Sequential：连续收盘价与 4 根前比较，计数到 9 = 进入疲劳区

核心纪律（照抄 PPT）：
  · 背离只负责「提醒」，不负责「买卖」——输出一律标注「等待价格确认」
  · 失效位优先于预测：每个信号都给出确认位 / 失效位
  · 不预测涨跌，只输出「哪些标的进入了动能疲劳 / 反转观察区」

数据源：yfinance（ND100 成分股），复用 nd100_resonance_scanner 的下载/缓存层。
演示模式：--source synthetic 生成覆盖全部场景的真实 OHLCV，无需联网。

用法:
    python divergence_td9_scanner.py                  # 全量扫描（yfinance）
    python divergence_td9_scanner.py --tickers AAPL,NVDA
    python divergence_td9_scanner.py --source synthetic   # 演示（覆盖全场景）
    python divergence_td9_scanner.py --no-cache
依赖: pip install yfinance pandas numpy
"""

import sys
import time
import argparse
import csv
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np

# 复用兄弟模块
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from nd100_resonance_scanner import (  # noqa: E402
    fetch_nd100_tickers, get_company_names,
    load_batch_or_download, calc_alignment,
    EMA_FAST, EMA_SLOW, TIMEFRAMES, CACHE_DIR, OUTPUT_DIR,
)
# 复用工具2的指标函数（DRY）
from five_rankings_daily import (  # noqa: E402
    calc_ema, calc_macd, calc_rsi, find_swings,
    _close, _high, _low, _vol,
)


def load_or_download(ticker, interval, period, use_cache=True, cache_only=False):
    """兼容旧的单标的调用，实际复用新版 ND100 批量/缓存数据层。"""
    if cache_only:
        cache_file = CACHE_DIR / f"{ticker}_{interval}.parquet"
        try:
            return pd.read_parquet(cache_file)
        except (FileNotFoundError, OSError, ValueError):
            return None
    return load_batch_or_download([ticker], interval, period, use_cache).get(ticker)

# ============================================================
# TD9 计数（TD Sequential Setup）
# ============================================================
def calc_td9(close):
    """Tom DeMark TD Sequential Setup。
    Buy Setup : 连续 K 线收盘价 < 其前第 4 根收盘价  → 计数 1..9
    Sell Setup: 连续 K 线收盘价 > 其前第 4 根收盘价  → 计数 1..9
    中途不满足即清零（必须连续）。

    返回 dict:
      counts      : 每根 K 线的当前计数（完成到 9 后归 0 继续）
      current     : {'count':int,'dir':'buy'/'sell'} 或 None（进行中）
      completed   : {'idx':int,'dir':str} 或 None（最近一次完成到 9 的位置/方向）
      setup_low   : Buy Setup 期间最低收盘价（失效参考）
      setup_high  : Sell Setup 期间最高收盘价（失效参考）
    """
    c = np.asarray(close, dtype=float)
    n = len(c)
    counts = [0] * n
    current = 0
    cur_dir = None
    completed = None
    setup_lo = setup_hi = None

    for i in range(4, n):
        buy = c[i] < c[i - 4]
        sell = c[i] > c[i - 4]
        if buy and not sell:
            current = current + 1 if cur_dir == "buy" else 1
            cur_dir = "buy"
            setup_lo = c[i] if setup_lo is None else min(setup_lo, c[i])
        elif sell and not buy:
            current = current + 1 if cur_dir == "sell" else 1
            cur_dir = "sell"
            setup_hi = c[i] if setup_hi is None else max(setup_hi, c[i])
        else:
            current = 0
            cur_dir = None
            setup_lo = setup_hi = None
        counts[i] = current
        if current == 9:
            completed = {"idx": i, "dir": cur_dir,
                         "low": setup_lo, "high": setup_hi}
            setup_lo = setup_hi = None  # 完成后重新累计下一段

    cur = {"count": current, "dir": cur_dir} if 0 < current < 9 else None
    return {
        "counts": counts,
        "current": cur,
        "completed": completed,
        "setup_low": setup_lo,
        "setup_high": setup_hi,
    }


# ============================================================
# 背离检测（RSI / MACD / 价量）
# ============================================================
def _compare_divergence(price, ind, pivots, lookback, kind):
    """比较最近两个同型 pivot 的价格与指标。
    bottom: 价格新低 + 指标抬高 → 底背离（潜在做多提醒）
    top   : 价格新高 + 指标降低 → 顶背离（潜在做空提醒）
    返回 list[dict]（0 或 1 项）。
    """
    n = len(price)
    recent = [i for i in pivots if i >= n - lookback]
    if len(recent) < 2:
        return []
    # 以最近一个 pivot 为终点 p2，在其之前的 pivot 中找「最强背离」的一对：
    #   底背离 → p2 价格新低(p2<p1) 且 指标抬高(m2>m1)
    #   顶背离 → p2 价格新高(p2>p1) 且 指标降低(m2<m1)
    i2 = recent[-1]
    p2, m2 = float(price.iloc[i2]), float(ind.iloc[i2])
    best = None
    for i1 in recent[:-1]:
        p1, m1 = float(price.iloc[i1]), float(ind.iloc[i1])
        if kind == "bottom" and p2 < p1 and m2 > m1:
            strength = (p1 - p2) / p1 + (m2 - m1) / 100.0
            if best is None or strength > best[0]:
                best = (strength, i1, p1, m1)
        elif kind == "top" and p2 > p1 and m2 < m1:
            strength = (p2 - p1) / p1 + (m1 - m2) / 100.0
            if best is None or strength > best[0]:
                best = (strength, i1, p1, m1)
    if best is None:
        return []
    _, i1, p1, m1 = best
    out = [{
        "kind": kind, "p1": p1, "p2": p2,
        "m1": m1, "m2": m2,
        "price_chg": (p2 - p1) / p1 * 100.0,
        "ind_chg": m2 - m1,
    }]
    return out


def detect_rsi_macd_divergence(close, rsi, macd_line, lo_idx, hi_idx, lookback=120):
    """返回 RSI / MACD 两类背离信号列表。"""
    sigs = []
    # RSI
    for d in _compare_divergence(close, rsi, lo_idx, lookback, "bottom"):
        sigs.append(("RSI", d))
    for d in _compare_divergence(close, rsi, hi_idx, lookback, "top"):
        sigs.append(("RSI", d))
    # MACD（用 DIF 线）
    for d in _compare_divergence(close, macd_line, lo_idx, lookback, "bottom"):
        sigs.append(("MACD", d))
    for d in _compare_divergence(close, macd_line, hi_idx, lookback, "top"):
        sigs.append(("MACD", d))
    return sigs


def detect_volume_divergence(close, vol, lo_idx, hi_idx, lookback=120):
    """价量背离：价格创新低/新高，但对应成交量未同步（萎缩）。"""
    n = len(close)
    sigs = []
    recent_lo = [i for i in lo_idx if i >= n - lookback]
    if len(recent_lo) >= 2:
        i1, i2 = recent_lo[-2], recent_lo[-1]
        p1, p2 = float(close.iloc[i1]), float(close.iloc[i2])
        v1, v2 = float(vol.iloc[i1]), float(vol.iloc[i2])
        if p2 < p1 and v2 < v1:  # 价格新低 + 量萎缩 → 抛压衰竭
            sigs.append({"kind": "bottom", "p1": p1, "p2": p2,
                         "v1": v1, "v2": v2,
                         "price_chg": (p2 - p1) / p1 * 100.0,
                         "vol_chg": (v2 - v1) / v1 * 100.0})
    recent_hi = [i for i in hi_idx if i >= n - lookback]
    if len(recent_hi) >= 2:
        i1, i2 = recent_hi[-2], recent_hi[-1]
        p1, p2 = float(close.iloc[i1]), float(close.iloc[i2])
        v1, v2 = float(vol.iloc[i1]), float(vol.iloc[i2])
        if p2 > p1 and v2 < v1:  # 价格新高 + 量萎缩 → 上涨无量
            sigs.append({"kind": "top", "p1": p1, "p2": p2,
                         "v1": v1, "v2": v2,
                         "price_chg": (p2 - p1) / p1 * 100.0,
                         "vol_chg": (v2 - v1) / v1 * 100.0})
    return sigs


# ============================================================
# 单标的分析
# ============================================================
def analyze(ticker, df_d):
    res = {
        "ticker": ticker, "signals": [],
        "close": None, "chg": None, "rsi": None,
        "macd_hist": None, "dist_ema": None,
        "td_progress": None, "group": "none",
        "max_strength": 0, "empty": False,
    }
    if df_d is None or len(df_d) < EMA_SLOW + 20:
        res["empty"] = True
        return res

    # Twelve Data 缓存使用小写 OHLCV；指标函数沿用旧版大写字段名。
    # 只在内存中统一字段，不改动缓存文件。
    rename = {
        lower: upper for lower, upper in {
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        }.items() if lower in df_d.columns and upper not in df_d.columns
    }
    df_d = df_d.rename(columns=rename)

    close = _close(df_d).astype(float).dropna()
    high = _high(df_d).astype(float).reindex(close.index)
    low = _low(df_d).astype(float).reindex(close.index)
    vol = _vol(df_d).astype(float).reindex(close.index)
    if len(close) < EMA_SLOW + 15:
        res["empty"] = True
        return res

    # 指标
    ema20 = calc_ema(close, EMA_FAST)
    ema60 = calc_ema(close, EMA_SLOW)
    macd, macd_sig, macd_hist = calc_macd(close)
    rsi = calc_rsi(close)
    last_close = float(close.iloc[-1])
    last_ema20 = float(ema20.iloc[-1])
    last_rsi = float(rsi.iloc[-1])
    last_hist = float(macd_hist.iloc[-1])
    res["close"] = round(last_close, 2)
    res["rsi"] = round(last_rsi, 1)
    res["macd_hist"] = round(last_hist, 4)
    if len(close) >= 2:
        res["chg"] = round((close.iloc[-1] / close.iloc[-2] - 1) * 100, 2)
    res["dist_ema"] = round((last_close - last_ema20) / last_ema20 * 100.0, 1) if last_ema20 else 0.0

    # swing 高低点（用干净的 close 序列；find_swings 返回 (hi_idx, lo_idx)）
    hi_idx, lo_idx = find_swings(close, close, window=5)

    # 1) RSI / MACD 背离
    divs = detect_rsi_macd_divergence(close, rsi, macd, lo_idx, hi_idx)
    for ind_name, d in divs:
        if abs(d["price_chg"]) < 2.5:   # 最小价格背离幅度，过滤伪信号
            continue
        if d["kind"] == "bottom":
            side = "bull"
            label = f"{ind_name}底背离"
            strength = min(100, abs(d["price_chg"]) * 4 + abs(d["ind_chg"]) * 2.5)
            detail = (f"价格新低 {d['price_chg']:.1f}%，{ind_name} 抬高 "
                      f"{d['ind_chg']:.1f}（{d['m1']:.1f}→{d['m2']:.1f}）")
            confirm = float(high.iloc[lo_idx[-1] + 1:].max()) if lo_idx else last_close
            invalid = d["p2"]
        else:
            side = "bear"
            label = f"{ind_name}顶背离"
            strength = min(100, abs(d["price_chg"]) * 4 + abs(d["ind_chg"]) * 2.5)
            detail = (f"价格新高 {d['price_chg']:.1f}%，{ind_name} 降低 "
                      f"{abs(d['ind_chg']):.1f}（{d['m1']:.1f}→{d['m2']:.1f}）")
            confirm = float(low.iloc[hi_idx[-1] + 1:].min()) if hi_idx else last_close
            invalid = d["p2"]
        res["signals"].append({
            "type": label, "side": side, "detail": detail,
            "strength": round(strength, 0),
            "confirm": round(confirm, 2),
            "invalid": round(invalid, 2),
            "wait": "等待价格确认（突破确认位 / 跌破失效位）",
        })

    # 2) 价量背离
    vdivs = detect_volume_divergence(close, vol, lo_idx, hi_idx)
    for d in vdivs:
        if abs(d["price_chg"]) < 2.0:   # 最小价格背离幅度，过滤伪信号
            continue
        if d["kind"] == "bottom":
            side = "bull"
            label = "底量背离"
            strength = min(100, abs(d["price_chg"]) * 3 + abs(d["vol_chg"]) * 0.4)
            detail = (f"价格新低 {d['price_chg']:.1f}%，成交量萎缩 "
                      f"{abs(d['vol_chg']):.0f}%（抛压衰竭）")
            confirm = float(high.iloc[lo_idx[-1] + 1:].max()) if lo_idx else last_close
            invalid = d["p2"]
        else:
            side = "bear"
            label = "顶量背离"
            strength = min(100, abs(d["price_chg"]) * 3 + abs(d["vol_chg"]) * 0.4)
            detail = (f"价格新高 {d['price_chg']:.1f}%，成交量萎缩 "
                      f"{abs(d['vol_chg']):.0f}%（上涨无量）")
            confirm = float(low.iloc[hi_idx[-1] + 1:].min()) if hi_idx else last_close
            invalid = d["p2"]
        res["signals"].append({
            "type": label, "side": side, "detail": detail,
            "strength": round(strength, 0),
            "confirm": round(confirm, 2),
            "invalid": round(invalid, 2),
            "wait": "等待价格确认（突破确认位 / 跌破失效位）",
        })

    # 3) TD9
    td = calc_td9(close)
    res["td_progress"] = (td["current"]["count"] if td["current"]
                           else (9 if (td["completed"]
                                       and td["completed"]["idx"] >= len(close) - 15)
                                 else 0))
    # 完成的 9 计数（最近 15 根内才算「今日触发」）
    if td["completed"] and td["completed"]["idx"] >= len(close) - 15:
        d = td["completed"]["dir"]
        side = "bull" if d == "buy" else "bear"
        res["signals"].append({
            "type": f"TD9{d.capitalize()}",
            "side": side,
            "detail": f"TD Sequential 计数抵达 9，进入动能疲劳区（{d}  exhaustion）",
            "strength": 92,
            "confirm": round(last_close, 2),
            "invalid": round(td["completed"]["low"] if d == "buy"
                              else td["completed"]["high"], 2),
            "wait": "疲劳≠反转：等价格确认，跌破 Setup 极值即失效",
        })
    elif td["current"] and td["current"]["count"] >= 5:
        d = td["current"]["dir"]
        side = "bull" if d == "buy" else "bear"
        cnt = td["current"]["count"]
        res["signals"].append({
            "type": f"TD9{d.capitalize()}(进行中)",
            "side": side,
            "detail": f"TD Sequential 计数进行中 {cnt}/9（{d}），接近疲劳区",
            "strength": 50 + cnt * 4,
            "confirm": round(last_close, 2),
            "invalid": round(td["setup_low"] if d == "buy" else td["setup_high"], 2),
            "wait": "尚未抵达 9，继续观察计数是否连续完成",
        })

    # 汇总分组与强度
    if res["signals"]:
        # 进行中 TD 单独成组（仅当没有已确认的背离/完成TD信号时）
        prog = [s for s in res["signals"] if "进行中" in s["type"]]
        non_prog = [s for s in res["signals"] if "进行中" not in s["type"]]
        bull = [s for s in non_prog if s["side"] == "bull"]
        bear = [s for s in non_prog if s["side"] == "bear"]
        if non_prog:
            if bull and not bear:
                res["group"] = "bull"
            elif bear and not bull:
                res["group"] = "bear"
            elif bull and bear:
                res["group"] = "both"
            else:
                res["group"] = "bull" if bull else "bear"
        elif prog:
            res["group"] = "progress"
        elif bull and not bear:
            res["group"] = "bull"
        elif bear and not bull:
            res["group"] = "bear"
        elif bull and bear:
            res["group"] = "both"
        res["max_strength"] = max(s["strength"] for s in res["signals"])
    return res


# ============================================================
# 合成数据源（覆盖全部场景，无需联网）
# ============================================================
def _gen_path(scenario, n, seed):
    """按场景生成收盘价路径（关键锚点线性插值，形态绝对标准，无随机噪声破坏结构）。
    底/顶背离：末段创新低/高且斜率放缓 → 指标反向。
    TD9：末段单调 → 连续计数触发。
    """
    rng = np.random.default_rng(seed)
    # (位置比例, 价格) 锚点；转折点即 swing 高低点
    anchors = {
        # —— 底背离（先急跌到 L1 → 反弹 → 再缓跌创新低 L2）——
        # L2 紧贴末端 idx144（find_swings 允许的最后一个 pivot），其后仅 5 根短回收且末根回落：
        #   既不触发 TD9（同向最多 3 连），也不在 L2 之后产生新 pivot（L2 保持「最近低点」→ 背离成立）。
        "rsi_bottom_div":  [(0,100),(0.22,88),(0.45,96),(0.966,85),(0.98,88),(1.0,86)],
        "macd_bottom_div": [(0,100),(0.22,88),(0.45,96),(0.966,85),(0.98,88),(1.0,86)],
        "vol_bot_div":     [(0,100),(0.22,88),(0.45,96),(0.966,85),(0.98,88),(1.0,86)],
        # —— 顶背离（H2 紧贴末端 idx144，其后短回收末根回升 → 不触发 TD9 / 新 pivot）——
        "rsi_top_div":     [(0,100),(0.22,112),(0.45,104),(0.966,115),(0.98,112),(1.0,114)],
        "macd_top_div":    [(0,100),(0.22,112),(0.45,104),(0.966,115),(0.98,112),(1.0,114)],
        "vol_top_div":     [(0,100),(0.22,112),(0.45,104),(0.966,115),(0.98,112),(1.0,114)],
        # —— TD9 买（先涨清空买计数 → 走平 → 末 18 根持续下行，第 9 根落在 idx139）——
        "td9_buy":  [(0,100),(0.4,100),(0.85,108),(0.88,108),(1.0,82)],
        # —— TD9 卖（先跌清空卖计数 → 走平 → 末 18 根持续上行，第 9 根落在 idx139）——
        "td9_sell": [(0,100),(0.4,100),(0.85,92),(0.88,92),(1.0,118)],
        # —— TD9 进行中（先涨清空买计数 → 末 7 根缓跌，计数停在 7/9）——
        "td9_progress": [(0,100),(0.95,108),(1.0,101)],
    }
    if scenario in anchors:
        pts = anchors[scenario]
        xs = np.array([p[0] * (n - 1) for p in pts], dtype=float)
        ys = np.array([p[1] for p in pts], dtype=float)
        c = np.interp(np.arange(n), xs, ys)
        # 背离场景用轻噪（让 RSI/MACD 不退化为 0/100）；TD9/clean 场景近乎无噪（保证计数与拐点干净）
        noise = 0.0012 if scenario.startswith(("rsi_", "macd_", "vol_")) else 0.00005
        c = c * (1 + rng.normal(0, noise, n))
        return c
    # clean：低幅正弦（半周期 4 根 < 5 → 既不达 TD9 进度阈值，也不形成新低/高背离）
    t = np.arange(n)
    c = 100.0 + 1.0 * np.sin(2 * np.pi * t / 8.0) + rng.normal(0, 0.02, n)
    return c


def _to_ohlcv_fake(close, seed, vol_profile):
    """由收盘价构造 OHLCV（合成）。vol_profile: 'normal'/'shrink_end'。"""
    rng = np.random.default_rng(seed + 7)
    n = len(close)
    dates = pd.date_range(end=datetime.now(), periods=n, freq="B")
    df = pd.DataFrame(index=dates)
    df["Close"] = close
    # 用相邻 close 作为 open 附近，加小扰动
    op = np.empty(n)
    op[0] = close[0] * (1 - rng.normal(0, 0.003))
    for i in range(1, n):
        op[i] = close[i - 1] * (1 + rng.normal(0, 0.0015))
    df["Open"] = op
    hi = np.maximum(df["Open"], df["Close"]) * (1 + np.abs(rng.normal(0, 0.0025, n)))
    lo = np.minimum(df["Open"], df["Close"]) * (1 - np.abs(rng.normal(0, 0.0025, n)))
    df["High"] = hi
    df["Low"] = lo
    base_vol = 1_000_000.0
    vol = rng.normal(base_vol, base_vol * 0.15, n).clip(base_vol * 0.4)
    if vol_profile == "shrink_end":
        vol[-40:] *= 0.5   # 末段缩量（价量背离：抛压衰竭 / 上涨无量）
    elif vol_profile == "grow_end":
        vol[-40:] *= 1.8   # 末段放量（非价量背离场景：放量创新低/高 → 价量同步，不构成背离）
    df["Volume"] = vol
    return df


SYNTH_PLAN = [
    ("AAPL", "rsi_bottom_div"),
    ("NVDA", "rsi_top_div"),
    ("MSFT", "macd_bottom_div"),
    ("GOOGL", "macd_top_div"),
    ("AMZN", "vol_bot_div"),
    ("META", "vol_top_div"),
    ("TSLA", "td9_buy"),
    ("AMD", "td9_sell"),
    ("NFLX", "td9_progress"),
    ("INTC", "clean"),
]


def scan_synthetic():
    rows = []
    for tk, sc in SYNTH_PLAN:
        seed = sum(ord(ch) for ch in tk) * 31 + 17   # 确定性种子（跨进程一致）
        close = _gen_path(sc, 150, seed)
        # 价量背离场景 → 末段缩量；RSI/MACD 场景 → 末段放量（避免误触价量背离）；其余正常
        if sc in ("vol_bot_div", "vol_top_div"):
            vol_profile = "shrink_end"
        elif sc.startswith("rsi_") or sc.startswith("macd_"):
            vol_profile = "grow_end"
        else:
            vol_profile = "normal"
        df = _to_ohlcv_fake(close, seed, vol_profile)
        rows.append(analyze(tk, df))
    return rows


# ============================================================
# 真实扫描
# ============================================================
def scan(tickers, use_cache=True, cache_only=False):
    rows = []
    total = len(tickers)
    if cache_only:
        data = {
            tk: load_or_download(tk, "1d", "2y", use_cache, True)
            for tk in tickers
        }
    else:
        # 一次按批量数据层加载，避免逐只调用造成不必要的 61 秒等待。
        data = load_batch_or_download(tickers, "1d", "2y", use_cache)
    for idx, tk in enumerate(tickers, 1):
        try:
            df_d = data.get(tk)
            r = analyze(tk, df_d)
            print(f"  [{idx:>3}/{total}] {tk:<7} 信号 {len(r['signals'])}  "
                  f"组={r['group']}  TD={r['td_progress']}")
            rows.append(r)
        except Exception as e:
            print(f"  [{idx:>3}/{total}] {tk:<7} 跳过: {type(e).__name__} {str(e)[:60]}")
            rows.append({"ticker": tk, "signals": [], "empty": True, "group": "none",
                         "max_strength": 0, "close": None, "chg": None, "rsi": None,
                         "macd_hist": None, "dist_ema": None, "td_progress": None})
        time.sleep(0.15)
    return rows


# ============================================================
# HTML 报告
# ============================================================
CSS = """
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
         background: #f4f5f7; color: #1c2733; margin: 0; padding: 24px; }
  .wrap { max-width: 1080px; margin: 0 auto; }
  .hd { display:flex; justify-content:space-between; align-items:flex-end;
        border-bottom: 3px solid #2c3e50; padding-bottom: 12px; margin-bottom: 18px; }
  .hd h1 { font-size: 24px; margin:0; font-weight:800; letter-spacing:.3px; }
  .hd .sub { font-size: 12.5px; color:#6b7785; margin-top:6px; }
  .hd .meta { text-align:right; font-size:12px; color:#6b7785; line-height:1.6; }
  .hd .meta b { color:#1c2733; }
  .src { font-size:11px; background:#eef2f7; color:#3b556f; padding:2px 8px;
         border-radius:10px; margin-left:8px; vertical-align:middle; }
  .discipline { background:#fff8e6; border:1px solid #f3d98b; border-left:4px solid #e0a800;
                border-radius:8px; padding:12px 16px; margin-bottom:18px; font-size:13px; line-height:1.7; }
  .discipline b { color:#9a6b00; }
  .stats { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }
  .stat { flex:1; min-width:120px; background:#fff; border-radius:10px; padding:14px 16px;
          box-shadow:0 1px 3px rgba(0,0,0,.06); border-top:3px solid #cbd3dc; }
  .stat .n { font-size:26px; font-weight:800; }
  .stat .l { font-size:12px; color:#6b7785; margin-top:2px; }
  .stat.bull { border-top-color:#c0392b; } .stat.bull .n { color:#c0392b; }
  .stat.bear { border-top-color:#27865a; } .stat.bear .n { color:#27865a; }
  .stat.td { border-top-color:#2c6fbf; } .stat.td .n { color:#2c6fbf; }
  .stat.prog { border-top-color:#8e6bb0; } .stat.prog .n { color:#8e6bb0; }
  .sec { margin:22px 0 10px; font-size:16px; font-weight:700; display:flex; align-items:center; gap:8px; }
  .sec .tag { font-size:11px; font-weight:600; padding:2px 9px; border-radius:10px; color:#fff; }
  .sec.bull .tag { background:#c0392b; } .sec.bear .tag { background:#27865a; }
  .sec.prog .tag { background:#8e6bb0; } .sec.both .tag { background:#b9770e; }
  .card { background:#fff; border-radius:10px; padding:14px 16px; margin-bottom:12px;
          box-shadow:0 1px 3px rgba(0,0,0,.06); border-left:4px solid #cbd3dc; }
  .card.bull { border-left-color:#c0392b; }
  .card.bear { border-left-color:#27865a; }
  .card.both { border-left-color:#b9770e; }
  .card .chd { display:flex; justify-content:space-between; align-items:baseline; }
  .card .tk { font-size:17px; font-weight:800; }
  .card .nm { font-size:12px; color:#8a96a3; margin-left:6px; }
  .card .px { font-size:12.5px; color:#6b7785; text-align:right; }
  .card .px b { color:#1c2733; }
  .badges { margin:10px 0 4px; display:flex; flex-wrap:wrap; gap:8px; }
  .badge { font-size:12px; padding:5px 10px; border-radius:7px; font-weight:600; }
  .badge.bull { background:#fdecea; color:#c0392b; border:1px solid #f3c5c0; }
  .badge.bear { background:#e8f5ee; color:#27865a; border:1px solid #bfe3cd; }
  .badge .bar { display:inline-block; height:5px; border-radius:3px; vertical-align:middle; margin-left:6px; }
  .badge.bull .bar { background:#c0392b; } .badge.bear .bar { background:#27865a; }
  .detail { font-size:12.5px; color:#46535f; margin:3px 0 0 2px; }
  .levels { display:flex; gap:18px; margin-top:8px; font-size:12px; flex-wrap:wrap; }
  .levels span b { font-weight:700; }
  .lv-confirm { color:#27865a; } .lv-invalid { color:#c0392b; }
  .wait { margin-top:8px; font-size:11.5px; color:#9a6b00; background:#fff8e6;
          border-radius:6px; padding:5px 10px; display:inline-block; }
  .empty { color:#8a96a3; font-size:12.5px; padding:6px 2px; }
  .foot { margin-top:26px; font-size:11.5px; color:#8a96a3; line-height:1.7;
          border-top:1px solid #e2e7ec; padding-top:12px; }
"""


def _card_html(r, name):
    if r.get("empty"):
        return (f"<div class='card'><div class='chd'><span class='tk'>{r['ticker']}</span></div>"
                f"<div class='empty'>数据不足，跳过</div></div>")
    side_cls = r["group"]
    badges = []
    for s in sorted(r["signals"], key=lambda x: -x["strength"]):
        cls = s["side"]
        w = max(4, int(s["strength"] / 100 * 46))
        badges.append(
            f"<div class='badge {cls}'>{s['type']}"
            f"<span class='bar' style='width:{w}px'></span></div>")
    badge_html = "".join(badges)
    detail_html = "".join(
        f"<div class='detail'>· {s['detail']}</div>" for s in r["signals"])
    level_html = "".join(
        f"<div class='levels'>"
        + "".join(
            f"<span class='lv-confirm'>突破确认 <b>{s['confirm']}</b></span>"
            f"<span class='lv-invalid'>跌破失效 <b>{s['invalid']}</b></span>"
            for s in r["signals"] if s.get("confirm"))
        + "</div>")
    wait_html = ""
    if r["signals"]:
        waits = set(s["wait"] for s in r["signals"])
        wait_html = "".join(f"<span class='wait'>⏳ {w}</span>" for w in waits)
    px = (f"<div class='px'>收盘 <b>{r['close']}</b>  "
          f"{('+' if (r['chg'] or 0) >= 0 else '')}{r['chg']}%<br>"
          f"RSI {r['rsi']} · MACD柱 {r['macd_hist']} · 偏离EMA{r['dist_ema']}%"
          f"{(' · TD'+str(r['td_progress'])+'/9') if r['td_progress'] else ''}</div>")
    return f"""<div class='card {side_cls}'>
    <div class='chd'><span><span class='tk'>{r['ticker']}</span><span class='nm'>{name}</span></span>{px}</div>
    <div class='badges'>{badge_html}</div>
    {detail_html}
    {level_html}
    {wait_html}
  </div>"""


def gen_html(rows, names, source_label, out_path):
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(rows)
    n_bull = sum(1 for r in rows if r["group"] in ("bull", "both"))
    n_bear = sum(1 for r in rows if r["group"] in ("bear", "both"))
    n_prog = sum(1 for r in rows if r["group"] == "progress")
    n_td9 = sum(1 for r in rows for s in r.get("signals", [])
                if s["type"] in ("TD9Buy", "TD9Sell"))

    # 排序：有信号的按强度降序
    def sort_key(r):
        return (0 if r["signals"] else 1, -r.get("max_strength", 0))
    rows_sorted = sorted(rows, key=sort_key)

    bull_rows = [r for r in rows_sorted if r["group"] in ("bull", "both")]
    bear_rows = [r for r in rows_sorted if r["group"] in ("bear", "both")]
    # 去重（both 同时进两组）
    bear_only = [r for r in bear_rows if r not in bull_rows]
    prog_rows = [r for r in rows_sorted if r["group"] == "progress"]
    quiet_rows = [r for r in rows_sorted if not r["signals"]]

    def sec(title, tag, cls, items):
        if not items:
            return ""
        cards = "".join(_card_html(r, names.get(r["ticker"], "")) for r in items)
        return (f"<div class='sec {cls}'><span class='tag'>{tag}</span>{title}"
                f" · {len(items)} 只</div>{cards}")

    sections = ""
    sections += sec("偏多提醒 · 左侧观察（底背离 / TD9买）", "BULL", "bull", bull_rows)
    sections += sec("偏空提醒 · 左侧观察（顶背离 / TD9卖）", "BEAR", "bear", bear_only)
    sections += sec("TD9 进行中（未达 9，继续观察）", "PROGRESS", "prog", prog_rows)
    if quiet_rows:
        q = "".join(_card_html(r, names.get(r["ticker"], "")) for r in quiet_rows[:8])
        sections += (f"<div class='sec'><span class='tag' style='background:#9aa7b3'>QUIET</span>"
                     f"今日安静 · 无明显背离/TD9 · {len(quiet_rows)} 只</div>{q}")

    html = f"""<!DOCTYPE html>
<html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>背离 + TD9 扫描器 · ND100</title>
<style>{CSS}</style></head>
<body><div class='wrap'>
  <div class='hd'>
    <div>
      <h1>背离 + TD9 扫描器 <span class='src'>{source_label}</span></h1>
      <div class='sub'>RSI/MACD 双线背离 · 价量背离 · TD Sequential · 只输出「动能疲劳 / 反转观察区」，不预测涨跌</div>
    </div>
    <div class='meta'>生成时间<b><br>{today}</b><br>扫描 <b>{total}</b> 只 · ND100</div>
  </div>

  <div class='discipline'>
    <b>⚠ 核心纪律（照抄 PPT 第 10 页）：</b>背离与 TD9 只负责「提醒动能疲劳」，<b>不负责买卖</b>。
    每一个信号都标注了 <b>突破确认位</b> 与 <b>跌破失效位</b>——价格没确认前，它只是观察项，不是交易指令。
    TD9 计数到 9 只是「进入疲劳区」，不代表马上反转，历史上经常「背了又背」。
  </div>

  <div class='stats'>
    <div class='stat bull'><div class='n'>{n_bull}</div><div class='l'>偏多提醒（底背离/TD9买）</div></div>
    <div class='stat bear'><div class='n'>{n_bear}</div><div class='l'>偏空提醒（顶背离/TD9卖）</div></div>
    <div class='stat td'><div class='n'>{n_td9}</div><div class='l'>TD9 已抵达 9</div></div>
    <div class='stat prog'><div class='n'>{n_prog}</div><div class='l'>TD9 进行中</div></div>
  </div>

  {sections}

  <div class='foot'>
    方法学：RSI(14) / MACD(12,26,9) / 成交量 基于日线；背离取最近 120 根内最后两个 swing 高低点比较；
    TD Sequential 用收盘价与前第 4 根比较连续计数。本工具为《数字资产市场观察助手》PPT 工作流的代码复刻，
    <b>仅供观察与复盘，不构成任何投资建议</b>。数据源：{source_label}。
  </div>
</div></body></html>"""
    out_path.write_text(html, encoding="utf-8")
    return html


# ============================================================
# 入口
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="背离 + TD9 扫描器")
    ap.add_argument("--tickers", help="只扫指定股票，逗号分隔 (如 AAPL,NVDA)")
    ap.add_argument("--nd100-input", help="读取 ND100 扫描 CSV 中的 ticker")
    ap.add_argument("--output-tag", help="追加到测试输出文件名，避免覆盖历史报告")
    ap.add_argument("--output-dir", help="输出目录；默认使用项目 output/")
    ap.add_argument("--layers", help="读取 ND100 CSV 后只保留指定分层，逗号分隔")
    ap.add_argument("--cache-only", action="store_true",
                    help="只读现有缓存，不请求 API；适合小范围代码测试")
    ap.add_argument("--no-cache", action="store_true", help="忽略本地缓存重新下载")
    ap.add_argument("--source", default="yfinance", choices=["yfinance", "synthetic"],
                    help="数据源：yfinance(默认) 或 synthetic(演示)")
    args = ap.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")

    if args.source == "synthetic":
        print("\n=== 背离 + TD9 扫描器（synthetic 演示）===")
        rows = scan_synthetic()
        names = {r["ticker"]: r["ticker"] for r in rows}
        source_label = "演示数据 · synthetic"
    else:
        use_cache = not args.no_cache
        if args.tickers and args.nd100_input:
            ap.error("--tickers 和 --nd100-input 只能二选一")
        if args.nd100_input:
            with open(args.nd100_input, newline="", encoding="utf-8-sig") as f:
                nd_rows = list(csv.DictReader(f))
            if args.layers:
                layers = {x.strip() for x in args.layers.split(",") if x.strip()}
                nd_rows = [row for row in nd_rows if row.get("分层", "") in layers]
            tickers = [row["ticker"].strip().upper() for row in nd_rows
                       if row.get("ticker", "").strip()]
            if not tickers:
                ap.error("--nd100-input 中没有可用 ticker")
        else:
            tickers = ([t.strip().upper() for t in args.tickers.split(",")]
                       if args.tickers else fetch_nd100_tickers())
        print(f"\n=== 背离 + TD9 扫描器 · ND100 ===\n标的数: {len(tickers)}\n")
        rows = scan(tickers, use_cache, args.cache_only)
        names = get_company_names([r["ticker"] for r in rows])
        source_label = "yfinance 实盘"

    if args.source == "synthetic":
        report_date = "20260818"
    elif args.nd100_input:
        with open(args.nd100_input, newline="", encoding="utf-8-sig") as f:
            input_rows = list(csv.DictReader(f))
        dates = {
            str(row.get("日线_数据截至", "")).strip()[:10].replace("-", "")
            for row in input_rows if str(row.get("日线_数据截至", "")).strip()
        }
        if len(dates) != 1:
            raise SystemExit(f"输入的日线行情日不唯一或缺失: {sorted(dates)}")
        report_date = dates.pop()
    else:
        report_date = today

    # CSV（信号明细）
    tag = f"_{args.output_tag}" if args.output_tag else ""
    csv_path = output_dir / f"divergence_td9_{report_date}{tag}.csv"
    flat = []
    for r in rows:
        if r.get("empty"):
            flat.append({"ticker": r["ticker"], "group": "数据不足", "signals": "",
                         "max_strength": "", "close": "", "rsi": "", "td": ""})
            continue
        sigs = "; ".join(f"{s['type']}({s['strength']:.0f})" for s in r["signals"])
        flat.append({
            "ticker": r["ticker"], "group": r["group"], "signals": sigs,
            "max_strength": r["max_strength"], "close": r["close"],
            "rsi": r["rsi"], "td": r["td_progress"],
        })
    pd.DataFrame(flat).to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n[CSV] {csv_path}")

    # HTML
    html_path = output_dir / f"divergence_td9_{report_date}{tag}.html"
    gen_html(rows, names, source_label, html_path)
    print(f"[HTML] {html_path}")

    # 摘要
    print("\n=== 信号分布 ===")
    print(f"  偏多提醒 : {sum(1 for r in rows if r['group'] in ('bull','both'))}")
    print(f"  偏空提醒 : {sum(1 for r in rows if r['group'] in ('bear','both'))}")
    print(f"  TD9进行中: {sum(1 for r in rows if r['group']=='progress')}")
    print(f"  今日安静 : {sum(1 for r in rows if not r['signals'])}")
    print("\n查看顺序：偏多提醒 → 偏空提醒 → TD9进行中 → 安静标的（先看进入疲劳区的）")


if __name__ == "__main__":
    main()
