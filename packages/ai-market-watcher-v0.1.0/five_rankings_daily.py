#!/usr/bin/env python3
"""
五榜单日报生成器  ·  Five-Rankings Daily Report
=============================================
完整复刻《数字资产市场观察助手》PPT 的输出形态：

  五维盘面检查 → 左/右侧分离打分 → 自动分发到 5 个榜单 → HTML 日报

五维：趋势(均线位置) / 结构(高低点抬升降低) / 动能(MACD/RSI 增强·衰减)
      / 量能(突破有无量配合) / 风险(已走远·信号冲突·数据缺失)

五榜单（对应 PPT 第 3/14/15/16 页）：
  1. 已确认优先复核   —— 右侧确认：突破 + 多周期共振 + 量能配合（今天 30 秒先看这些）
  2. 背离观察池       —— 左侧观察：底/顶背离、超卖/超买，等价格确认
  3. 强趋势等待触发   —— 多周期共振已就位，但未突破/未放量，等触发
  4. C级高潜力        —— 底部结构形成中，左+右部分信号叠加
  5. 风险与噪音       —— 已走远 / 信号冲突 / 数据不足 / 无明显信号

核心纪律（照抄 PPT）：
  - 不预测涨跌，只输出"查看顺序"
  - 左侧负责"提前观察"，右侧负责"确认触发"，两者不混在一个分数里
  - 失效位优先于预测：已走远 = 追单风险，直接进风险榜

数据源：yfinance（ND100 成分股），复用 nd100_resonance_scanner 的下载/缓存层。
用法:
    python five_rankings_daily.py                  # 全量扫描
    python five_rankings_daily.py --tickers AAPL,NVDA
    python five_rankings_daily.py --no-cache
    python five_rankings_daily.py --demo            # 不下载，用上次 CSV 生成样例报告
依赖: pip install yfinance pandas requests lxml
"""

import os
import sys
import time
import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np

# 复用兄弟模块的数据层
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from nd100_resonance_scanner import (  # noqa: E402
    fetch_nd100_tickers, get_company_names,
    load_batch_or_download, calc_alignment,
    last_data_asof, EMA_FAST, EMA_SLOW, TIMEFRAMES, CACHE_DIR, OUTPUT_DIR,
)


def load_or_download(ticker, interval, period, use_cache=True):
    """兼容旧的单标的调用，实际复用新版 ND100 批量/缓存数据层。"""
    return load_batch_or_download([ticker], interval, period, use_cache).get(ticker)


def cache_status(tickers, use_cache, cache_only=False):
    """记录本次扫描开始前各周期缓存状态，不泄露凭据。"""
    now = datetime.now().timestamp()
    ttl = {"60m": 5 * 60, "1d": 30 * 60, "1wk": 60 * 60}
    result = {}
    for interval, _, _ in TIMEFRAMES:
        valid = 0
        for tk in tickers:
            path = CACHE_DIR / f"{tk}_{interval}.parquet"
            try:
                age = now - path.stat().st_mtime
                if use_cache and (cache_only or 0 <= age <= ttl[interval]) and path.stat().st_size > 0:
                    valid += 1
            except OSError:
                pass
        if not use_cache:
            state = "禁用缓存，将请求 API"
        elif valid == len(tickers):
            state = "全部使用有效缓存"
        elif valid:
            state = f"缓存 {valid}/{len(tickers)}，其余由 API 批量补齐"
        else:
            state = "无有效缓存，将由 API 批量补齐"
        result[interval] = {"state": state, "valid_cache_count": valid}
    return result

# ============================================================
# 指标计算（纯 pandas，无外部 TA 库依赖）
# ============================================================
def _close(df):
    """统一取出 Close 为一维 Series（兼容 yfinance MultiIndex）"""
    c = df["Close"] if "Close" in df.columns else df["close"]
    return c.squeeze() if isinstance(c, pd.DataFrame) else c

def _high(df):
    h = df["High"] if "High" in df.columns else df["high"]
    return h.squeeze() if isinstance(h, pd.DataFrame) else h

def _low(df):
    l = df["Low"] if "Low" in df.columns else df["low"]
    return l.squeeze() if isinstance(l, pd.DataFrame) else l

def _vol(df):
    v = df["Volume"] if "Volume" in df.columns else df["volume"]
    return v.squeeze() if isinstance(v, pd.DataFrame) else v


def calc_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def calc_macd(close, fast=12, slow=26, signal=9):
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd = ema_f - ema_s
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return macd, sig, hist


def calc_rsi(close, period=14):
    """Wilder's RSI"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # 纯涨段（avg_loss==0 且有涨幅）RSI 应为 100，而非 fillna 误报的 50；纯平（两者皆0）保持 50
    pure_up = (avg_loss == 0) & (avg_gain > 0)
    rsi = rsi.where(~pure_up, 100.0)
    return rsi.fillna(50.0)


def find_swings(high, low, window=5):
    """基于窗口的局部极值点。返回 (swing_high_idx[], swing_low_idx[])"""
    n = len(high)
    hi_idx, lo_idx = [], []
    for i in range(window, n - window):
        seg_hi = high.iloc[i - window:i + window + 1]
        seg_lo = low.iloc[i - window:i + window + 1]
        if high.iloc[i] == seg_hi.max() and high.iloc[i] > high.iloc[i - 1] and high.iloc[i] > high.iloc[i + 1]:
            hi_idx.append(i)
        if low.iloc[i] == seg_lo.min() and low.iloc[i] < low.iloc[i - 1] and low.iloc[i] < low.iloc[i + 1]:
            lo_idx.append(i)
    return hi_idx, lo_idx


def detect_divergence(close, rsi, lo_idx, lookback=60, kind="bottom"):
    """检测底/顶背离。
    bottom: 价格新低 + RSI 未新低（抬高） → 左侧做多观察
    top:    价格新高 + RSI 未新高（降低） → 左侧做空观察
    返回 bool。
    """
    if len(lo_idx) < 2:
        return False
    end = len(close)
    recent = [i for i in lo_idx if i >= end - lookback]
    if len(recent) < 2:
        recent = lo_idx[-2:]
    if len(recent) < 2:
        return False
    i1, i2 = recent[-2], recent[-1]
    p1, p2 = close.iloc[i1], close.iloc[i2]
    r1, r2 = rsi.iloc[i1], rsi.iloc[i2]
    if kind == "bottom":
        return p2 < p1 and r2 > r1
    else:
        return p2 > p1 and r2 < r1


# ============================================================
# 五维打分 + 左右侧分离
# ============================================================
def score_stock(tk, df_d, align_60m, align_1d, align_1wk):
    """对单只股票打分。
    返回 dict：五维分、左右侧分、关键布尔、路由榜单、理由。
    df_d: 日线 DataFrame；align_*: 各周期方向 ('多'/'空'/'平'/None)
    """
    r = {
        "ticker": tk,
        "trend": None, "structure": None, "momentum": None, "volume": None, "risk": None,
        "left": 0, "right": 0,
        "direction": "-",  # 多 / 空 / 中
        "breakout_up": False, "breakout_dn": False,
        "bottom_div": False, "top_div": False,
        "rsi_oversold": False, "rsi_overbought": False,
        "too_far_up": False, "too_far_dn": False,
        "resonance": "待定",
        "rsi": None, "dist_ema": None, "macd_hist": None,
        "reason": "",
        "ranking": 5,
        "daily_chg": None,
    }

    # ---- 数据不足 ----
    if df_d is None or len(df_d) < EMA_SLOW + 20:
        r["trend"] = r["structure"] = r["momentum"] = r["volume"] = "-"
        r["risk"] = "数据不足"
        r["reason"] = "日线数据不足"
        r["ranking"] = 5
        return r

    close = _close(df_d).astype(float).dropna()
    high = _high(df_d).astype(float).reindex(close.index)
    low = _low(df_d).astype(float).reindex(close.index)
    vol = _vol(df_d).astype(float).reindex(close.index)
    if len(close) < EMA_SLOW + 15:
        r["risk"] = "数据不足"
        r["reason"] = "有效日线不足"
        r["ranking"] = 5
        return r

    # ---- 指标 ----
    ema20 = calc_ema(close, EMA_FAST)
    ema60 = calc_ema(close, EMA_SLOW)
    macd, macd_sig, macd_hist = calc_macd(close)
    rsi = calc_rsi(close)
    last_close = float(close.iloc[-1])
    last_ema20 = float(ema20.iloc[-1])
    last_ema60 = float(ema60.iloc[-1])
    last_rsi = float(rsi.iloc[-1])
    last_hist = float(macd_hist.iloc[-1])
    prev_hist = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else 0.0

    # 涨跌幅
    if len(close) >= 2:
        r["daily_chg"] = round((close.iloc[-1] / close.iloc[-2] - 1) * 100, 2)

    # 偏离 EMA20
    dist = (last_close - last_ema20) / last_ema20 * 100.0 if last_ema20 else 0.0
    r["dist_ema"] = round(dist, 1)
    r["rsi"] = round(last_rsi, 0)
    r["macd_hist"] = round(last_hist, 4)

    # ---- 多周期共振 ----
    dirs = [align_60m, align_1d, align_1wk]
    bull = sum(1 for d in dirs if d == "多")
    bear = sum(1 for d in dirs if d == "空")
    flat = sum(1 for d in dirs if d == "平")
    if flat > 0 or (bull + bear) < 3:
        r["resonance"] = "待定"
    elif bull == 3:
        r["resonance"] = "多头共振"
    elif bear == 3:
        r["resonance"] = "空头共振"
    elif bull == 2:
        r["resonance"] = "偏多"
    elif bear == 2:
        r["resonance"] = "偏空"
    else:
        r["resonance"] = "待定"

    # ================= 维度1: 趋势 =================
    if last_ema20 > last_ema60 and last_close > last_ema20:
        trend_score = 2
    elif last_ema20 > last_ema60:
        trend_score = 1
    elif last_ema20 < last_ema60 and last_close < last_ema20:
        trend_score = -2
    elif last_ema20 < last_ema60:
        trend_score = -1
    else:
        trend_score = 0
    # 共振加成
    if r["resonance"] == "多头共振":
        trend_score = max(trend_score, 1) + 1
    elif r["resonance"] == "空头共振":
        trend_score = min(trend_score, -1) - 1
    trend_score = max(-3, min(3, trend_score))
    r["trend"] = trend_score

    # ================= 维度2: 结构 =================
    hi_idx, lo_idx = find_swings(high, low, window=5)
    struct = 0
    breakout_up = breakout_dn = False
    if len(hi_idx) >= 2:
        hh = high.iloc[hi_idx[-1]] > high.iloc[hi_idx[-2]]  # 高点抬升
        lh = high.iloc[hi_idx[-1]] < high.iloc[hi_idx[-2]]  # 高点降低
    else:
        hh = lh = False
    if len(lo_idx) >= 2:
        hl = low.iloc[lo_idx[-1]] > low.iloc[lo_idx[-2]]   # 低点抬升
        ll = low.iloc[lo_idx[-1]] < low.iloc[lo_idx[-2]]   # 低点降低
    else:
        hl = ll = False
    if hh and hl:
        struct = 2
    elif lh and ll:
        struct = -2
    elif hh or hl:
        struct = 1
    elif lh or ll:
        struct = -1
    else:
        struct = 0
    # 突破：收盘价突破最近 swing 高 / 跌破最近 swing 低
    if hi_idx and last_close > high.iloc[hi_idx[-1]] * 1.005:
        breakout_up = True
    if lo_idx and last_close < low.iloc[lo_idx[-1]] * 0.995:
        breakout_dn = True
    r["structure"] = struct
    r["breakout_up"] = breakout_up
    r["breakout_dn"] = breakout_dn

    # ================= 维度3: 动能 =================
    if last_hist > 0 and last_hist > prev_hist:
        mom = 2          # 正且放大
    elif last_hist > 0 and last_hist <= prev_hist:
        mom = 1          # 正但缩小
    elif last_hist < 0 and last_hist > prev_hist:
        mom = -1         # 负但收敛（转好迹象）
    elif last_hist < 0 and last_hist <= prev_hist:
        mom = -2         # 负且放大
    else:
        mom = 0
    r["momentum"] = mom

    # 背离检测
    r["bottom_div"] = detect_divergence(close, rsi, lo_idx, kind="bottom")
    r["top_div"] = detect_divergence(close, rsi, hi_idx, kind="top")
    r["rsi_oversold"] = last_rsi < 30
    r["rsi_overbought"] = last_rsi > 70

    # ================= 维度4: 量能 =================
    vol_ma = vol.rolling(20).mean()
    last_vol = float(vol.iloc[-1]) if not vol.empty else 0.0
    last_vol_ma = float(vol_ma.iloc[-1]) if not vol_ma.isna().all() else 0.0
    vol_ratio = (last_vol / last_vol_ma) if last_vol_ma > 0 else 1.0
    if (breakout_up or breakout_dn):
        if vol_ratio >= 1.5:
            vol_score = 2          # 放量突破
        elif vol_ratio >= 1.1:
            vol_score = 1          # 温和放量
        else:
            vol_score = 0          # 突破但缩量（量价背离，扣分）
    else:
        vol_score = 1 if vol_ratio >= 1.1 else 0
    r["volume"] = vol_score
    r["_vol_ratio"] = round(vol_ratio, 2)

    # ================= 维度5: 风险 =================
    too_far_up = dist > 15.0       # 远离 EMA20 上方 → 追多风险
    too_far_dn = dist < -15.0      # 远离 EMA20 下方 → 追空风险
    r["too_far_up"] = too_far_up
    r["too_far_dn"] = too_far_dn
    risk = 0
    risk_reasons = []
    if too_far_up:
        risk += 2; risk_reasons.append(f"上方偏离EMA20 {dist:.0f}%")
    if too_far_dn:
        risk += 2; risk_reasons.append(f"下方偏离EMA20 {dist:.0f}%")
    # 多周期冲突（偏多/偏空 = 周期打架）
    if r["resonance"] in ("偏多", "偏空"):
        risk += 1; risk_reasons.append("周期打架")
    # 趋势与动能冲突
    if (trend_score >= 1 and mom <= -1) or (trend_score <= -1 and mom >= 1):
        risk += 1; risk_reasons.append("趋势与动能冲突")
    # 超买叠加顶背离
    if r["rsi_overbought"] and r["top_div"]:
        risk += 1; risk_reasons.append("超买+顶背离")
    r["risk"] = risk
    r["_risk_txt"] = "；".join(risk_reasons) if risk_reasons else "无明显风险"

    # ================= 左侧观察分（提前观察：超跌/衰竭/筑底）=================
    left = 0
    if r["bottom_div"]:
        left += 2
    if r["rsi_oversold"]:
        left += 2
    if r["top_div"]:
        left += 2          # 顶背离也是左侧（做空观察）
    if r["rsi_overbought"]:
        left += 1
    if trend_score <= -2 and too_far_dn:
        left += 1          # 深跌超卖
    if mom >= 1 and trend_score <= -1:
        left += 1          # 动能转正但趋势仍空 → 底部苗头（左侧）
    if mom <= -1 and trend_score >= 1:
        left += 1          # 动能转弱但趋势仍多 → 顶部苗头（左侧）
    r["left"] = min(left, 5)

    # ================= 右侧确认分（确认触发：突破/共振/量能）=================
    right = 0
    if breakout_up and r["resonance"] in ("多头共振", "偏多"):
        right += 2
    elif breakout_up:
        right += 1
    if breakout_dn and r["resonance"] in ("空头共振", "偏空"):
        right += 2
    elif breakout_dn:
        right += 1
    if r["resonance"] == "多头共振":
        right += 2
    elif r["resonance"] == "空头共振":
        right += 2
    elif r["resonance"] in ("偏多", "偏空"):
        right += 1
    if vol_score == 2:
        right += 1
    if mom >= 1:
        right += 1
    r["right"] = min(right, 6)

    # 方向标签
    if r["right"] >= 3 and trend_score >= 1:
        r["direction"] = "多"
    elif r["right"] >= 3 and trend_score <= -1:
        r["direction"] = "空"
    elif r["left"] >= 2 and (r["bottom_div"] or r["rsi_oversold"]):
        r["direction"] = "多(左)"
    elif r["left"] >= 2 and (r["top_div"] or r["rsi_overbought"]):
        r["direction"] = "空(左)"
    else:
        r["direction"] = "中"

    # 结构是否正在转向（用于 C 级判定）
    struct_turning = (
        (struct >= 1 and r["bottom_div"]) or              # 结构转升 + 底背离
        (struct <= -1 and r["top_div"]) or                # 结构转降 + 顶背离
        (mom >= 1 and trend_score <= -1 and not too_far_dn)  # 动能转正但趋势仍空
    )
    r["_struct_turning"] = struct_turning

    # ================= 路由到五榜单 =================
    rank, reason = _route(r)
    r["ranking"] = rank
    r["reason"] = reason
    return r


def _route(s):
    """分发到 5 个榜单。返回 (榜单号, 理由)。优先级从高到低。
    顺序遵循 PPT：右侧确认(①) > 风险过滤(已走远→⑤) > 左侧观察(②)
    > 强趋势待触发(③) > 底部形成中(④) > 噪音(⑤)。"""
    # 0. 数据不足 → 风险与噪音
    if s["risk"] == "数据不足":
        return 5, "数据不足"

    # 1. 已确认优先复核：突破 + 共振 + 量能 + 未走远 + 低风险
    if s["breakout_up"] and s["resonance"] in ("多头共振", "偏多") and s["volume"] >= 1 \
            and not s["too_far_up"] and s["risk"] <= 1:
        return 1, "突破+多头共振+量能配合"
    if s["breakout_dn"] and s["resonance"] in ("空头共振", "偏空") and s["volume"] >= 1 \
            and not s["too_far_dn"] and s["risk"] <= 1:
        return 1, "跌破+空头共振+量能配合"

    # 2. 已走远且无背离 → 风险与噪音（追单风险优先于左侧超买超卖观察）
    if s["too_far_up"] and not s["top_div"]:
        return 5, f"已走远(上方偏离{s['dist_ema']:.0f}%)，追单风险"
    if s["too_far_dn"] and not s["bottom_div"]:
        return 5, f"已走远(下方偏离{s['dist_ema']:.0f}%)，追单风险"

    # 3. 背离观察池：左侧（底/顶背离 或 超卖/超买），尚未突破
    if (s["bottom_div"] or s["rsi_oversold"]) and not s["breakout_up"]:
        tag = "底背离" if s["bottom_div"] else "RSI超卖"
        return 2, f"{tag}，等待价格确认"
    if (s["top_div"] or s["rsi_overbought"]) and not s["breakout_dn"]:
        tag = "顶背离" if s["top_div"] else "RSI超买"
        return 2, f"{tag}，等待价格确认"

    # 4. 强趋势等待触发：多周期共振 + 未突破 + 未走远
    if s["resonance"] == "多头共振" and s["trend"] >= 1 and not s["breakout_up"] and not s["too_far_up"]:
        return 3, "多头共振，等待突破触发"
    if s["resonance"] == "空头共振" and s["trend"] <= -1 and not s["breakout_dn"] and not s["too_far_dn"]:
        return 3, "空头共振，等待跌破触发"

    # 5. C级高潜力：左+右部分信号，结构转向中
    if s["_struct_turning"] and s["left"] >= 1 and s["right"] >= 1:
        return 4, "底部结构形成中"
    if s["left"] >= 2 and s["right"] >= 1:
        return 4, "左侧信号+部分确认"

    # 6. 风险与噪音
    if s["resonance"] in ("偏多", "偏空"):
        return 5, "周期打架"
    return 5, "无明显共振信号"


# ============================================================
# 合成数据源（演示用）
# 当 yfinance 限速/不可用时，生成覆盖全部 5 榜单场景的真实 OHLCV，
# 让五维打分与路由逻辑可被完整验证。数据源恢复后用 --source yfinance 切回。
# ============================================================
SYNTH_PLAN = [
    # (ticker, scenario, company_name)
    # 榜单1 已确认优先复核（多头突破）
    ("NVDA", "bull_breakout", "英伟达"),
    ("AAPL", "bull_breakout", "苹果"),
    ("MSFT", "bull_breakout", "微软"),
    ("META", "bull_breakout", "Meta"),
    ("GOOGL", "bull_breakout", "谷歌"),
    ("AMZN", "bull_breakout", "亚马逊"),
    ("AVGO", "bull_breakout", "博通"),
    ("CRM", "bull_breakout", "赛富时"),
    # 榜单1 已确认优先复核（空头跌破）
    ("INTEL", "bear_breakdown", "英特尔"),
    ("WBA", "bear_breakdown", "沃尔格林"),
    ("CSCO", "bear_breakdown", "思科"),
    # 榜单2 背离观察池（底背离/超卖）
    ("TSLA", "bottom_div", "特斯拉"),
    ("AMD", "bottom_div", "AMD"),
    ("NFLX", "bottom_div", "奈飞"),
    ("DIS", "bottom_div", "迪士尼"),
    ("PYPL", "bottom_div", "PayPal"),
    ("INTC", "bottom_div", "英特尔"),
    # 榜单2 背离观察池（顶背离/超买）
    ("SMCI", "top_div", "超微电脑"),
    ("MSTR", "top_div", "微策"),
    # 榜单3 强趋势等待触发（多头共振未突破）
    ("COST", "bull_wait", "好市多"),
    ("LLY", "bull_wait", "礼来"),
    ("NOW", "bull_wait", "ServiceNow"),
    ("PLTR", "bull_wait", "Palantir"),
    ("CRWD", "bull_wait", "CrowdStrike"),
    ("PANW", "bull_wait", "Palo Alto"),
    ("SHOP", "bull_wait", "Shopify"),
    # 榜单3 强趋势等待触发（空头共振未跌破）
    ("NKE", "bear_wait", "耐克"),
    ("F", "bear_wait", "福特"),
    # 榜单4 C级高潜力（底部结构形成中）
    ("BABA", "bottoming", "阿里巴巴"),
    ("JD", "bottoming", "京东"),
    ("PDD", "bottoming", "拼多多"),
    ("BIDU", "bottoming", "百度"),
    ("TME", "bottoming", "腾讯音乐"),
    # 榜单5 风险与噪音（已走远）
    ("MCD", "too_far", "麦当劳"),
    ("V", "too_far", "Visa"),
    ("MA", "too_far", "万事达"),
    # 榜单5 风险与噪音（无明显信号/震荡）
    ("KO", "mixed", "可口可乐"),
    ("PEP", "mixed", "百事"),
    ("WMT", "mixed", "沃尔玛"),
    ("XOM", "mixed", "埃克森美孚"),
    ("CVX", "mixed", "雪佛龙"),
    ("PFE", "mixed", "辉瑞"),
    ("MRK", "mixed", "默沙东"),
    ("T", "mixed", "AT&T"),
    ("VZ", "mixed", "威瑞森"),
]


def _gen_close(scenario, n, seed):
    """按场景生成收盘价路径（分段漂移）"""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.009, n)
    drift = np.zeros(n)
    if scenario == "bull_uptrend":
        drift[:] = 0.0018
    elif scenario == "bull_breakout":
        drift[:] = 0.0018
        drift[n - 26:n - 1] = 0.0       # 平台整理
        drift[n - 1] = 0.045            # 突破跳升
    elif scenario == "bull_wait":
        drift[:] = 0.0018
        drift[n - 9:n] = -0.0015        # 温和回落靠近 EMA
    elif scenario == "bear_downtrend":
        drift[:] = -0.0018
    elif scenario == "bear_breakdown":
        drift[:] = -0.0018
        drift[n - 26:n - 1] = 0.0       # 平台整理
        drift[n - 1] = -0.045           # 跌破跳低
    elif scenario == "bear_wait":
        drift[:] = -0.0018
        drift[n - 9:n] = 0.0015         # 温和反弹靠近 EMA
    elif scenario == "bottom_div":
        drift[:] = -0.0020              # 初段急跌
        drift[n - 55:n - 35] = 0.005    # 反弹
        drift[n - 35:n - 6] = -0.0010   # 更温和下跌（新低但 RSI 抬高）
        drift[n - 6:] = 0.003           # 末端反弹离开低点（不构成跌破）
    elif scenario == "top_div":
        drift[:] = 0.0020
        drift[n - 55:n - 35] = -0.005   # 回调
        drift[n - 35:n - 6] = 0.0010    # 更温和上涨（新高但 RSI 降低）
        drift[n - 6:] = -0.003          # 末端回落离开高点（不构成突破）
    elif scenario == "bottoming":
        drift[:] = -0.0014
        drift[n - 45:n - 20] = 0.0      # 横盘磨底
        drift[n - 20:] = 0.0020         # 温和回升
    elif scenario == "too_far":
        drift[:] = 0.0015               # 基础上升
        drift[n - 15:] = 0.016          # 末端加速，远离 EMA20
    else:  # mixed
        drift[:] = 0.0
    c = np.empty(n)
    c[0] = 100.0
    for i in range(1, n):
        c[i] = c[i - 1] * (1 + drift[i] + noise[i])
    return c, drift


def _to_ohlcv(closes, seed, vol_spike_idx=None, n_bars=None):
    """收盘价序列 → OHLCV DataFrame（与 yfinance auto_adjust 格式一致）"""
    rng = np.random.default_rng(seed + 7)
    if n_bars is not None:
        closes = closes[-n_bars:]
    n = len(closes)
    dates = pd.bdate_range(end=datetime.now().date(), periods=n)
    o = np.empty(n); h = np.empty(n); l = np.empty(n); v = np.empty(n)
    o[0] = closes[0]
    o[1:] = closes[:-1]
    for i in range(n):
        cc, op = closes[i], o[i]
        ran = cc * rng.uniform(0.004, 0.012)
        h[i] = max(op, cc) + ran
        l[i] = min(op, cc) - ran
        v[i] = 1e7 * rng.uniform(0.7, 1.3)
    if vol_spike_idx:
        for idx in vol_spike_idx:
            if 0 <= idx < n:
                v[idx] *= 2.6
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": closes, "Volume": v}, index=dates)


def gen_synthetic_ticker(scenario, seed):
    """生成一个场景的 日线/60min/周线 DataFrame"""
    n = 260
    closes_d, _ = _gen_close(scenario, n, seed)
    # 突破/跌破场景：最后一根放量
    spike = [n - 1] if scenario in ("bull_breakout", "bear_breakdown") else None
    df_d = _to_ohlcv(closes_d, seed, vol_spike_idx=spike)

    # 60min：方向与日线一致（用日线漂移生成 70 根，够算 EMA60）
    bull_scn = scenario in ("bull_uptrend", "bull_breakout", "bull_wait", "too_far", "top_div")
    bear_scn = scenario in ("bear_downtrend", "bear_breakdown", "bear_wait", "bottom_div")
    if bull_scn:
        c60, _ = _gen_close("bull_uptrend", 70, seed + 1)
    elif bear_scn:
        c60, _ = _gen_close("bear_downtrend", 70, seed + 1)
    else:
        c60, _ = _gen_close("mixed", 70, seed + 1)
    df_60 = _to_ohlcv(c60, seed + 1, n_bars=70)

    # 周线：底部/筑底场景用 mixed（避免空头共振抢占路由），其余与日线同向
    if bull_scn:
        cw, _ = _gen_close("bull_uptrend", 260, seed + 2)
    elif bear_scn:
        cw, _ = _gen_close("bear_downtrend", 260, seed + 2)
    else:
        cw, _ = _gen_close("mixed", 260, seed + 2)
    df_w = _to_ohlcv(cw, seed + 2)
    return df_d, df_60, df_w


def scan_synthetic(plan):
    """用合成数据扫描，返回 rows"""
    rows = []
    for i, (tk, scenario, nm) in enumerate(plan, 1):
        # 跨进程确定性种子：Python 内置 hash() 默认会每个进程随机化，
        # 会导致同一演示每次运行的榜单结果不一致。
        seed = sum(ord(ch) for ch in tk) * 31 + 17
        df_d, df_60, df_w = gen_synthetic_ticker(scenario, seed)
        a60, _, _, _ = calc_alignment(df_60)
        a1d, _, _, _ = calc_alignment(df_d)
        a1w, _, _, _ = calc_alignment(df_w)
        r = score_stock(tk, df_d, a60, a1d, a1w)
        r["_name"] = nm
        rows.append(r)
        print(f"  [{i:>2}/{len(plan)}] {tk:<6} [{scenario:<13}] {r['resonance']:<7} "
              f"趋{r['trend']} 结{r['structure']} 动{r['momentum']} 量{r['volume']} 风险{r['risk']} "
              f"左{r['left']} 右{r['right']} -> [{r['ranking']}] {RANK_NAME[r['ranking']]}")
    return rows


# ============================================================
# 主扫描
# ============================================================
RANKINGS = [
    (1, "已确认优先复核", "#C0392B", "右侧确认：突破+共振+量能。今天 30 秒先看这些。"),
    (2, "背离观察池",     "#8E44AD", "左侧观察：背离/超卖超买。只提醒，等价格确认。"),
    (3, "强趋势等待触发", "#1F6FB0", "多周期共振已就位，等突破/放量触发。"),
    (4, "C级高潜力",      "#B9770E", "底部结构形成中，左+右部分信号叠加。"),
    (5, "风险与噪音",     "#7F8C8D", "已走远/周期打架/数据不足/无信号。先避开。"),
]
RANK_NAME = {r[0]: r[1] for r in RANKINGS}


def scan(tickers, use_cache=True, cache_only=False):
    """批量读取三个周期后，再逐只评分，避免逐股触发 API 限速。"""
    rows = []
    total = len(tickers)
    data = {
        interval: load_batch_or_download(tickers, interval, period, use_cache, cache_only)
        for interval, _, period in TIMEFRAMES
    }
    scan.last_meta = {
        "asof_by_interval": {
            interval: max(
                (last_data_asof(df) for df in data[interval].values() if df is not None and len(df)),
                default=None,
            )
            for interval, _, _ in TIMEFRAMES
        }
    }
    for i, tk in enumerate(tickers, 1):
        try:
            df_60 = data["60m"].get(tk)
            df_d = data["1d"].get(tk)
            df_w = data["1wk"].get(tk)
            a60, _, _, _ = calc_alignment(df_60)
            a1d, _, _, _ = calc_alignment(df_d)
            a1w, _, _, _ = calc_alignment(df_w)
            r = score_stock(tk, df_d, a60, a1d, a1w)
            rows.append(r)
            print(f"  [{i:>3}/{total}] {tk:<6} {r['resonance']:<6} "
                  f"趋{r['trend']} 结{r['structure']} 动{r['momentum']} 量{r['volume']} 风险{r['risk']} "
                  f"左{r['left']} 右{r['right']} -> [{r['ranking']}] {RANK_NAME[r['ranking']]}")
        except Exception as e:
            print(f"  [{i:>3}/{total}] {tk:<6} 扫描失败: {e}")
            rows.append({
                "ticker": tk, "ranking": 5, "reason": f"扫描异常: {e}",
                "trend": "-", "structure": "-", "momentum": "-", "volume": "-", "risk": "异常",
                "left": 0, "right": 0, "direction": "-",
                "resonance": "待定", "daily_chg": None, "rsi": None, "dist_ema": None, "macd_hist": None,
            })
    return rows


# ============================================================
# HTML 日报（复刻 PPT 输出形态）
# ============================================================
def dim_badge(val, kind):
    """五维小徽章。val 可能是 int 或 '-'"""
    if val == "-" or val is None:
        return "<span class='db d0'>·</span>"
    try:
        v = int(val)
    except (ValueError, TypeError):
        return "<span class='db d0'>·</span>"
    # 趋势/结构/动能: -3..3 ；量能: 0..2
    if kind in ("trend", "structure", "momentum"):
        if v >= 2: cls = "dp"   # 强正
        elif v == 1: cls = "dl" # 弱正
        elif v == 0: cls = "d0"
        elif v >= -1: cls = "sl"
        else: cls = "sp"
        txt = f"{v:+d}" if v != 0 else "0"
    else:  # volume
        cls = "dp" if v >= 2 else ("dl" if v == 1 else "d0")
        txt = f"{v}"
    return f"<span class='db {cls}'>{txt}</span>"


def dir_tag(d):
    if d == "多" or d == "多(左)": return "<span class='tag long'>多</span>"
    if d == "空" or d == "空(左)": return "<span class='tag short'>空</span>"
    return "<span class='tag mid'>中</span>"


def gen_html(rows, names, out_path, source_label="实时数据", report_date=None):
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    today_date = report_date or today.split()[0]
    total = len(rows)
    by_rank = {r[0]: [] for r in RANKINGS}
    for r in rows:
        by_rank[r["ranking"]].append(r)

    # 漏斗
    scored = sum(1 for r in rows if r["risk"] != "数据不足" and r["risk"] != "异常")
    priority = len(by_rank[1])

    # 排序：每个榜单内按 右侧分 降序、再 左侧分 降序
    for k in by_rank:
        by_rank[k].sort(key=lambda x: (x.get("right", 0), x.get("left", 0)), reverse=True)

    # 30秒先看谁 callout
    priority_rows = by_rank[1]
    if priority_rows:
        pitems = []
        for r in priority_rows[:8]:
            nm = names.get(r["ticker"], "")
            chg = r.get("daily_chg")
            chg_cls = "up" if (chg and chg > 0) else ("down" if (chg and chg < 0) else "")
            chg_txt = f"{chg:+.2f}%" if chg is not None else "-"
            pitems.append(f"""
            <div class='pitem'>
              <div class='pti'>{dir_tag(r['direction'])} <b>{r['ticker']}</b> <span class='pnm'>{nm}</span></div>
              <div class='pch {chg_cls}'>{chg_txt}</div>
              <div class='prs'>{r['reason']}</div>
            </div>""")
        callout = f"""
        <div class='callout'>
          <div class='co-hd'><span class='co-bolt'>⚡</span> 今天 30 秒先看这些</div>
          <div class='co-sub'>右侧已确认 · 突破+共振+量能 · 共 {priority} 只</div>
          <div class='co-list'>{''.join(pitems)}</div>
        </div>"""
    else:
        callout = """
        <div class='callout empty'>
          <div class='co-hd'><span class='co-bolt'>⚡</span> 今天没有"已确认优先复核"标的</div>
          <div class='co-sub'>没有突破+共振+量能同时成立的标的。先看「背离观察池」和「强趋势等待触发」即可。</div>
        </div>"""

    # 概览卡片
    cards = []
    for rk, name, color, desc in RANKINGS:
        cnt = len(by_rank[rk])
        cards.append(f"""
        <div class='card' style='border-left-color:{color}' data-rk='{rk}'>
          <div class='cnum' style='color:{color}'>{cnt}</div>
          <div class='cname'>{name}</div>
          <div class='cdesc'>{desc}</div>
        </div>""")
    cards_html = "".join(cards)

    # 各榜单表格
    sections = []
    for rk, name, color, desc in RANKINGS:
        sub = by_rank[rk]
        icon = {1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤"}[rk]
        if not sub:
            body = "<p class='empty'>该榜单暂无标的</p>"
        else:
            trs = []
            for r in sub:
                nm = names.get(r["ticker"], "")
                chg = r.get("daily_chg")
                chg_cls = "up" if (chg and chg > 0) else ("down" if (chg and chg < 0) else "")
                chg_txt = f"{chg:+.2f}%" if chg is not None else "-"
                rsi_txt = f"{r['rsi']:.0f}" if r.get("rsi") is not None else "-"
                dist_txt = f"{r['dist_ema']:+.1f}%" if r.get("dist_ema") is not None else "-"
                res_txt = r.get("resonance", "-")
                trs.append(f"""
            <tr>
              <td class='tk'>{dir_tag(r['direction'])}<b>{r['ticker']}</b><span class='nm'>{nm}</span></td>
              <td class='chg {chg_cls}'>{chg_txt}</td>
              <td class='dims'>
                {dim_badge(r['trend'],'trend')}{dim_badge(r['structure'],'structure')}
                {dim_badge(r['momentum'],'momentum')}{dim_badge(r['volume'],'volume')}
                <span class='dimlab'>趋/结/动/量</span>
              </td>
              <td class='num'>{res_txt}</td>
              <td class='num'>L{r['left']}·R{r['right']}</td>
              <td class='num'>{rsi_txt}</td>
              <td class='num'>{dist_txt}</td>
              <td class='rs'>{r['reason']}</td>
            </tr>""")
            body = f"""
          <table class='rtab'>
            <thead><tr>
              <th>标的</th><th>日涨跌</th><th>五维</th><th>共振</th>
              <th>左·右</th><th>RSI</th><th>偏离EMA</th><th>入榜理由</th>
            </tr></thead>
            <tbody>{''.join(trs)}</tbody>
          </table>"""
        sections.append(f"""
        <section class='rsec' id='rk{rk}'>
          <h2 style='color:{color}'>{icon} {name} <span class='cnt'>({len(sub)})</span></h2>
          <div class='rdesc'>{desc}</div>
          {body}
        </section>""")
    sections_html = "".join(sections)

    src_cls = "live" if source_label == "实时数据" else ""
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>五榜单日报 · 行情日 {today_date}</title>
<style>
  :root {{
    --bg:#f6f5f1; --card:#fff; --txt:#1a1a1a; --mut:#6b6b66; --bd:#e6e6e0;
    --up:#c0392b; --dn:#1e8449;  /* 涨红跌绿（中国习惯） */
  }}
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
    background:var(--bg); color:var(--txt); line-height:1.6; padding:22px; max-width:none; margin:0 auto }}
  .hd {{ border-bottom:2px solid #2b2b2b; padding-bottom:14px; margin-bottom:18px; display:flex; justify-content:space-between; align-items:flex-end }}
  .hd h1 {{ font-size:23px; font-weight:700; letter-spacing:.5px }}
  .hd .src {{ font-size:11px; font-weight:600; color:#fff; background:#7F8C8D; padding:2px 8px; border-radius:10px; vertical-align:4px; margin-left:6px }}
  .hd .src.live {{ background:#1e8449 }}
  .hd .sub {{ color:var(--mut); font-size:13px; margin-top:4px }}
  .hd .meta {{ text-align:right; color:var(--mut); font-size:12px }}
  .hd .meta b {{ color:var(--txt); font-weight:600 }}

  /* 漏斗 */
  .funnel {{ display:flex; gap:0; margin:16px 0 22px; background:var(--card); border-radius:10px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.04) }}
  .fstep {{ flex:1; padding:14px 16px; border-right:1px solid var(--bd); text-align:center }}
  .fstep:last-child {{ border-right:none }}
  .fnum {{ font-size:26px; font-weight:700; line-height:1 }}
  .flab {{ font-size:11px; color:var(--mut); margin-top:5px }}
  .fstep.s1 .fnum {{ color:#555 }} .fstep.s2 .fnum {{ color:#8E44AD }} .fstep.s3 .fnum {{ color:#c0392b }}

  /* 30秒先看谁 */
  .callout {{ background:linear-gradient(135deg,#fff5f3,#fff); border:1px solid #f3d9d2; border-left:5px solid #c0392b;
    border-radius:12px; padding:18px 20px; margin-bottom:24px; box-shadow:0 2px 6px rgba(192,57,43,.06) }}
  .callout.empty {{ border-left-color:#7F8C8D; background:linear-gradient(135deg,#f7f7f5,#fff); border-color:#e0e0d8 }}
  .co-hd {{ font-size:18px; font-weight:700 }}
  .co-bolt {{ margin-right:6px }}
  .co-sub {{ color:var(--mut); font-size:12px; margin:4px 0 12px }}
  .co-list {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:10px }}
  .pitem {{ background:#fff; border:1px solid #f0e6e2; border-radius:8px; padding:10px 12px }}
  .pti {{ font-size:14px; font-weight:600 }}
  .pnm {{ font-weight:400; font-size:11px; color:var(--mut); margin-left:4px }}
  .pch {{ font-family:ui-monospace,monospace; font-weight:700; font-size:13px; margin-top:2px }}
  .prs {{ font-size:11px; color:var(--mut); margin-top:3px }}
  .up {{ color:var(--up) }} .down {{ color:var(--dn) }}

  /* 概览卡片 */
  .summary {{ display:grid; grid-template-columns:repeat(5,1fr); gap:11px; margin:0 0 26px }}
  .card {{ background:var(--card); border-radius:10px; padding:15px 13px; border-left:4px solid #ccc;
    box-shadow:0 1px 3px rgba(0,0,0,.04); cursor:default; transition:transform .12s }}
  .card:hover {{ transform:translateY(-2px) }}
  .cnum {{ font-size:30px; font-weight:700; line-height:1 }}
  .cname {{ font-size:13.5px; font-weight:600; margin-top:6px }}
  .cdesc {{ font-size:10.5px; color:var(--mut); margin-top:4px; line-height:1.45 }}

  /* 榜单区 */
  .rsec {{ background:var(--card); border-radius:10px; padding:18px 20px; margin-bottom:18px; box-shadow:0 1px 3px rgba(0,0,0,.04) }}
  .rsec h2 {{ font-size:17px; font-weight:700; margin-bottom:4px }}
  .cnt {{ color:var(--mut); font-weight:400; font-size:13px }}
  .rdesc {{ font-size:12px; color:var(--mut); margin-bottom:12px }}
  .empty {{ color:var(--mut); font-size:13px; padding:10px 0 }}
  table {{ width:100%; border-collapse:collapse; font-size:14px }}
  th {{ text-align:left; color:var(--mut); font-weight:500; font-size:12px; padding:10px 8px;
    border-bottom:1px solid var(--bd); text-transform:uppercase; letter-spacing:.4px; white-space:nowrap }}
  td {{ padding:11px 8px; border-bottom:1px solid #f0f0ec; vertical-align:middle }}
  tr:hover td {{ background:#fafaf7 }}
  .tk {{ font-weight:600; white-space:nowrap }}
  .tk b {{ margin-left:4px }}
  .nm {{ display:block; font-weight:400; font-size:10.5px; color:var(--mut); margin-left:30px; margin-top:0 }}
  .num {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11.5px; color:#444; white-space:nowrap }}
  .chg {{ font-family:ui-monospace,monospace; font-weight:700; white-space:nowrap }}
  .rs {{ font-size:11.5px; color:#555 }}
  .dims {{ white-space:nowrap }}
  .dimlab {{ display:none }}

  /* 五维徽章 */
  .db {{ display:inline-block; width:20px; height:20px; line-height:20px; text-align:center;
    border-radius:4px; font-size:10.5px; font-weight:700; margin-right:2px; font-family:ui-monospace,monospace }}
  .dp {{ background:#eaf3de; color:#1e6b1e }}   /* 强正 绿 */
  .dl {{ background:#f0f5e8; color:#4a7a2a }}   /* 弱正 */
  .d0 {{ background:#eeece4; color:#888780 }}   /* 中性 */
  .sl {{ background:#fbeee0; color:#9a5a12 }}   /* 弱负 */
  .sp {{ background:#fce5dc; color:#9a2a12 }}   /* 强负 红 */

  /* 方向标签 */
  .tag {{ display:inline-block; width:30px; height:18px; line-height:18px; text-align:center;
    border-radius:4px; font-size:10px; font-weight:700; margin-right:4px; vertical-align:1px }}
  .long {{ background:#fdecea; color:#c0392b }}
  .short {{ background:#e8f5ee; color:#1e8449 }}
  .mid {{ background:#eeece4; color:#888780 }}

  .ft {{ margin-top:22px; padding-top:14px; border-top:1px solid var(--bd); color:var(--mut); font-size:11px; line-height:1.75 }}
  .ft b {{ color:var(--txt) }}
  @media(max-width:760px){{
    .summary {{ grid-template-columns:repeat(2,1fr) }}
    .funnel {{ flex-direction:column }}
    .fstep {{ border-right:none; border-bottom:1px solid var(--bd) }}
  }}
</style></head>
<body>
  <div class='hd'>
    <div>
      <h1>五榜单日报 · ND100 <span class='src {src_cls}'>{source_label}</span></h1>
      <div class='sub'>五维盘面检查 · 左/右侧分离打分 · 自动分发 5 榜单 · 只输出"查看顺序"，不预测涨跌</div>
    </div>
    <div class='meta'>生成时间<b><br>{today}</b><br>标的数<b> {total}</b></div>
  </div>

  <div class='funnel'>
    <div class='fstep s1'><div class='fnum'>{total}</div><div class='flab'>扫描总数</div></div>
    <div class='fstep s2'><div class='fnum'>{scored}</div><div class='flab'>进入五维评分</div></div>
    <div class='fstep s3'><div class='fnum'>{priority}</div><div class='flab'>已确认优先复核</div></div>
  </div>

  {callout}

  <div class='summary'>{cards_html}</div>

  {sections_html}

  <div class='ft'>
    <b>五维读图</b>：趋=趋势(EMA20/60位置+多周期共振) ｜ 结=结构(高低点抬升/降低+突破) ｜
    动=动能(MACD柱体增强/衰减+RSI) ｜ 量=量能(突破有无量配合) ｜ 风险=已走远/周期打架/数据不足。<br>
    <b>左·右</b>：左侧=提前观察(超跌/衰竭/底背离)，右侧=确认触发(突破/共振/量能)。两者不混在一个分数里。<br>
    <b>使用方式</b>：先看榜单①（30秒确定今天先看谁）→ 再扫榜单②③等价格确认/触发 → 榜单④低频观察 → 榜单⑤先避开。<br>
    <b>涨跌色</b>：红涨绿跌（中国习惯）。<b>风险提示</b>：本报告只整理"查看顺序"，不构成买卖建议；
    均线共振只反映趋势现状，追入已走远的标的需配合结构、量能与风险维度复核。失效位优先于预测。<br>
    生成自 five_rankings_daily.py · 对应 PPT《数字资产市场观察助手》第 3/14/15/16 页
  </div>
</body></html>"""
    out_path.write_text(html, encoding="utf-8")


def input_market_date(paths: list[Path], fallback: str | None = None) -> str:
    """Read one daily market date from all ND100 inputs."""
    values = set()
    for path in paths:
        frame = pd.read_csv(path, encoding="utf-8-sig")
        if "日线_数据截至" not in frame.columns:
            raise ValueError(f"输入缺少日线_数据截至: {path}")
        values.update(
            str(value).strip()[:10].replace("-", "")
            for value in frame["日线_数据截至"].dropna()
            if str(value).strip()
        )
    if not values and fallback:
        values = {fallback}
    if len(values) != 1:
        raise ValueError(f"输入的日线行情日不唯一或缺失: {sorted(values)}")
    value = next(iter(values))
    datetime.strptime(value, "%Y%m%d")
    return value


def metadata_market_date(metadata: dict, fallback: str) -> str:
    values = {
        str(value).strip()[:10].replace("-", "")
        for value in metadata.get("asof_by_interval", {}).values()
        if value
    }
    if not values:
        return fallback
    if len(values) != 1:
        raise ValueError(f"扫描周期行情日不一致: {sorted(values)}")
    value = next(iter(values))
    datetime.strptime(value, "%Y%m%d")
    return value


# ============================================================
# ============================================================
# 入口
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="五榜单日报生成器")
    ap.add_argument("--source", choices=["yfinance", "synthetic"], default="yfinance",
                    help="数据源：yfinance(默认,实时) / synthetic(演示,覆盖全部5榜单场景)")
    ap.add_argument("--tickers", help="只扫指定股票，逗号分隔 (如 AAPL,NVDA)，仅 yfinance 源生效")
    ap.add_argument("--nd100-input", action="append",
                    help="使用已完成的 ND100 CSV 作为股票范围，可重复传入两批")
    ap.add_argument("--output-tag", help="追加到输出文件名，避免覆盖同日历史报告")
    ap.add_argument("--output-dir", help="输出目录；默认 output/")
    ap.add_argument("--no-cache", action="store_true", help="忽略本地缓存重新下载")
    ap.add_argument("--cache-only", action="store_true", help="只读取现有缓存，缺失数据不请求 API")
    args = ap.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")

    if args.source == "synthetic":
        print(f"\n=== 五榜单日报扫描 · ND100（演示数据）===")
        print(f"标的数: {len(SYNTH_PLAN)}  覆盖全部 5 榜单场景\n")
        rows = scan_synthetic(SYNTH_PLAN)
        names = {r["ticker"]: r.pop("_name", "") for r in rows}
        source_label = "演示数据"
        source_files = []
        tickers = [r["ticker"] for r in rows]
    else:
        if args.cache_only and args.no_cache:
            ap.error("--cache-only 和 --no-cache 不能同时使用")
        use_cache = not args.no_cache
        cache_meta = {}
        input_paths = [Path(p).resolve() for p in (args.nd100_input or [])]
        if args.tickers and input_paths:
            ap.error("--tickers 和 --nd100-input 只能二选一")
        if input_paths:
            missing = [p for p in input_paths if not p.exists()]
            if missing:
                ap.error("ND100 输入文件不存在: " + ", ".join(str(p) for p in missing))
            frames = [pd.read_csv(p, encoding="utf-8-sig") for p in input_paths]
            merged = pd.concat(frames, ignore_index=True)
            if "ticker" not in merged.columns:
                ap.error("ND100 输入文件缺少 ticker 列")
            tickers = list(dict.fromkeys(
                str(t).strip().upper() for t in merged["ticker"].dropna() if str(t).strip()
            ))
            if not tickers:
                ap.error("ND100 输入文件没有可用 ticker")
            source_files = [str(p) for p in input_paths]
            source_label = "真实行情（API/缓存） · 指定ND100输入 " + ", ".join(p.name for p in input_paths)
        else:
            tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else fetch_nd100_tickers()
            source_files = []
            source_label = "真实行情（API/缓存） · 当前ND100成分清单"
        print(f"\n=== 五榜单日报扫描 · ND100（真实数据）===")
        print(f"标的数: {len(tickers)}  周期: 60min/日线/周线  EMA: {EMA_FAST}/{EMA_SLOW}\n")
        cache_meta = cache_status(tickers, use_cache, args.cache_only)
        for interval, info in cache_meta.items():
            print(f"  [{interval}] {info['state']}")
        rows = scan(tickers, use_cache, args.cache_only)
        print("\n[公司名] 获取中...")
        names = get_company_names([r["ticker"] for r in rows])

    scan_meta = getattr(scan, "last_meta", {}) if args.source != "synthetic" else {}
    if source_files:
        market_date = input_market_date(input_paths)
    elif args.source == "synthetic":
        market_date = "20260818"
    else:
        market_date = metadata_market_date(scan_meta, today)

    # CSV
    tag = f"_{args.output_tag}" if args.output_tag else ""
    csv_path = output_dir / f"five_rankings_{market_date}{tag}.csv"
    df_out = pd.DataFrame(rows)
    keep = ["ticker", "ranking", "direction", "reason", "resonance",
            "trend", "structure", "momentum", "volume", "risk", "left", "right",
            "rsi", "dist_ema", "macd_hist", "daily_chg"]
    df_out[[c for c in keep if c in df_out.columns]].to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n[CSV] {csv_path}")

    # HTML
    html_path = output_dir / f"five_rankings_{market_date}{tag}.html"
    gen_html(rows, names, html_path, source_label=source_label,
             report_date=f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:]}")
    print(f"[HTML] {html_path}")

    manifest_path = output_dir / f"five_rankings_{market_date}{tag}_manifest.json"
    manifest_path.write_text(json.dumps({
        "report_date": market_date,
        "market_date": f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:]}",
        "scan_date": today,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_type": "nd100_csv" if source_files else "nd100_component_list",
        "nd100_input_files": source_files,
        "ticker_count": len(tickers),
        "tickers": tickers,
        "nd100_input_rows": {
            str(p): int(pd.read_csv(p, encoding="utf-8-sig").shape[0])
            for p in source_files
        } if source_files else {},
        "market_data_access": cache_meta if args.source != "synthetic" else {},
        "market_data_asof_by_interval": scan_meta.get("asof_by_interval", {}),
        "market_data_policy": "受保护 Twelve Data API；优先使用有效缓存，过期或缺失时请求 API",
        "historical_demo_files_used": False,
        "note": "报告文件日期使用真实行情日；scan_date/created_at 保留实际运行时间。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[MANIFEST] {manifest_path}")

    # 摘要
    print(f"\n=== 五榜单分布 ===")
    for rk, name, color, desc in RANKINGS:
        cnt = sum(1 for r in rows if r["ranking"] == rk)
        bar = "█" * min(cnt, 40)
        print(f"  {rk} {name:<8} {cnt:>3}  {bar}")
    print(f"\n查看顺序：① 已确认优先复核 → ② 背离观察池 → ③ 强趋势等待触发 → ④ C级高潜力 → ⑤ 风险与噪音")


if __name__ == "__main__":
    main()
