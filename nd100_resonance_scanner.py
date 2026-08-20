#!/usr/bin/env python3
"""
ND100 多周期共振扫描器
=====================
对纳斯达克100成分股，同时计算 60分钟 / 日线 / 周线 三个周期的 EMA(20)/EMA(60) 排列方向，
三周期同向 = 共振。每日生成一张"共振清单" + HTML 可视化日报。

对应 PPT《数字资产市场观察助手》趋势与多周期模块。
不预测涨跌，只输出"查看顺序"。

用法:
    python nd100_resonance_scanner.py                    # 全量扫描并生成日报
    python nd100_resonance_scanner.py --limit 50          # 扫前 50 只
    python nd100_resonance_scanner.py --limit 50 --offset 50  # 扫后 50 只
    python nd100_resonance_scanner.py --no-cache          # 忽略缓存重新下载
    python nd100_resonance_scanner.py --tickers AAPL,MSFT # 只扫指定股票

依赖: pip install pandas requests pyarrow
"""

import os
import sys
import time
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# ============================================================
# 配置
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache" / "nd100"
OUTPUT_DIR = BASE_DIR / "output"

EMA_FAST = 20
EMA_SLOW = 60
TIMEFRAMES = [
    ("60m", "60min", "1mo"),   # interval, 中文名, 下载周期
    ("1d",  "日线", "2y"),
    ("1wk", "周线", "5y"),
]

# Twelve Data 的行情可能有延迟；缓存只用来减少重复请求，不能把旧数据
# 当成“今日最新”。有效期按美东时间计算，因为纳斯达克交易时段在美东。
NY_TZ = ZoneInfo("America/New_York")
TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
TD_INTERVAL = {"60m": "1h", "1d": "1day", "1wk": "1week"}
TD_OUTPUTSIZE = {"60m": 5000, "1d": 1000, "1wk": 500}
LOCAL_ENV_FILE = BASE_DIR / ".env"
LEGACY_TD_KEY_FILE = (
    Path.home() / "Library" / "Application Support" /
    "ND100 Research" / "credentials" / "twelve_data.env"
)
# Basic 免费层是 8 credits/分钟；批量请求按 symbol 扣 credit，不能把 50 只
# 股票塞进一个请求后立即继续发下一个请求。
TD_FREE_CREDITS_PER_MINUTE = 8
TD_BATCH_WAIT_SECONDS = 61
TD_LAST_REQUEST_AT = None
CACHE_TTL_SECONDS = {
    "60m": 5 * 60,
    "1d": 30 * 60,
    "1wk": 60 * 60,
}

# ND100 成分股（截至 2026-08，Wikipedia 抓取）
# 脚本运行时会尝试从 Wikipedia 自动刷新；抓取失败则用此清单
ND100_FALLBACK = [
    "ADBE","AMD","ABNB","ALNY","GOOGL","GOOG","AMZN","AEP","AMGN","ADI","AAPL",
    "AMAT","APP","ARM","ASML","ALAB","ADSK","ADP","AXON","BKR","BKNG","AVGO",
    "CDNS","CTAS","CSCO","CCEP","CMCSA","CEG","CPRT","CRWV","COST","CRWD","CSX",
    "DDOG","DXCM","FANG","DASH","EXC","FAST","FER","FTNT","GEHC","GILD","HONA",
    "HON","IDXX","INTC","INTU","ISRG","KDP","KLAC","KHC","LRCX","LIN","LITE",
    "MAR","MRVL","MELI","META","MCHP","MU","MSFT","MSTR","MDLZ","MPWR","MNST",
    "NBIS","NFLX","NVDA","NXPI","ORLY","ODFL","PCAR","PLTR","PANW","PAYX","PYPL",
    "PDD","PEP","QCOM","REGN","RKLB","ROP","ROST","SNDK","STX","SHOP","SPCX",
    "SBUX","SNPS","TMUS","TTWO","TER","TSLA","TXN","TRI","VRTX","WMT","WBD",
    "WDC","WDAY","XEL",
]

# 分层定义
LAYERS = {
    "多头共振": "3 个周期 EMA20 全部在 EMA60 之上",
    "空头共振": "3 个周期 EMA20 全部在 EMA60 之下",
    "偏多":     "2 多 1 空，尚未完全共振",
    "偏空":     "1 多 2 空，尚未完全共振",
    "待定":     "存在均线走平或数据不足",
}


# ============================================================
# 成分股获取
# ============================================================
def fetch_nd100_tickers():
    """尝试从 Wikipedia 抓最新 ND100 清单，失败用内置清单"""
    try:
        import requests
        url = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
        tables = pd.read_html(requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20).text)
        for t in tables:
            # 保留真实列名；原代码把列名先 lower() 后又拿 lower 后的名字
            # 去索引 DataFrame，遇到 "Ticker" 会直接失败并误用旧 fallback。
            ticker_col = next(
                (c for c in t.columns if "ticker" in str(c).lower()), None
            )
            if ticker_col is not None:
                tickers = (
                    t[ticker_col]
                    .astype(str)
                    .str.strip()
                    .str.replace(".", "-", regex=False)
                    .tolist()
                )
                tickers = [t for t in tickers if t and t.lower() != "nan"]
                if len(tickers) < 80:
                    continue
                print(f"[成分股] 从 Wikipedia 抓到 {len(tickers)} 只")
                return tickers
    except Exception as e:
        print(f"[成分股] Wikipedia 抓取失败({e})，使用内置清单 {len(ND100_FALLBACK)} 只")
    return list(ND100_FALLBACK)


def get_company_names(tickers):
    """不额外调用接口；公司名不是扫描所需数据，避免浪费免费额度。"""
    return {}


# ============================================================
# 数据下载（含限速重试 + 本地缓存）
# ============================================================
def _td_values_to_df(payload):
    """把 Twelve Data 单只股票响应转换成现有 EMA 计算所需的 DataFrame。"""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    values = payload.get("values") or []
    if not values:
        return None
    df = pd.DataFrame(values)
    if "datetime" not in df or "close" not in df:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df["Close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["datetime", "Close"]).set_index("datetime")
    return df.sort_index()


def load_twelve_data_key():
    """按安全顺序读取 Key，不打印、不写入报告。

    1. 环境变量 TWELVE_DATA_API_KEY；
    2. MARKET_WATCHER_CREDENTIALS_FILE 指定的项目外文件；
    3. 本地 .env（已被 .gitignore 排除）；
    4. 旧版 macOS 受保护配置路径（兼容现有用户）。
    """
    key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if key:
        return key

    explicit_file = os.getenv("MARKET_WATCHER_CREDENTIALS_FILE", "").strip()
    candidates = []
    if explicit_file:
        candidates.append(Path(explicit_file).expanduser())
    candidates.extend([LOCAL_ENV_FILE, LEGACY_TD_KEY_FILE])

    for path in candidates:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("TWELVE_DATA_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
        except (FileNotFoundError, OSError):
            continue
    return ""


def wait_for_twelve_data_window():
    """全局限制批量请求间隔，跨 60m/日线/周线也遵守免费额度。"""
    global TD_LAST_REQUEST_AT
    now = time.monotonic()
    if TD_LAST_REQUEST_AT is not None:
        remaining = TD_BATCH_WAIT_SECONDS - (now - TD_LAST_REQUEST_AT)
        if remaining > 0:
            print(f"    [Twelve Data] 等待 {remaining:.0f}s，避免跨周期触发限速")
            time.sleep(remaining)
    TD_LAST_REQUEST_AT = time.monotonic()


def download_batch_with_retry(tickers, interval, period, max_retry=4):
    """每个周期一次批量请求；Twelve Data 最多支持 120 个 symbol。"""
    api_key = load_twelve_data_key()
    if not api_key:
        raise RuntimeError(
            "未找到 Twelve Data API Key。请参考 .env.example 在本地配置，"
            "不要把 .env 或 API Key 提交到 Git。"
        )
    if len(tickers) > 120:
        raise ValueError("Twelve Data 单次最多 120 只股票，请减少本次扫描数量")

    params = {
        "symbol": ",".join(tickers),
        "interval": TD_INTERVAL[interval],
        "outputsize": TD_OUTPUTSIZE[interval],
        "timezone": "America/New_York",
        "adjust": "splits",
        "apikey": api_key,
    }
    for attempt in range(max_retry):
        try:
            wait_for_twelve_data_window()
            response = requests.get(TWELVE_DATA_URL, params=params, timeout=30)
            response.raise_for_status()
            body = response.json()
            if body.get("status") == "error":
                raise RuntimeError(body.get("message", "Twelve Data 返回错误"))
            # 批量响应按 ticker 为 key；同时兼容单只 ticker 的响应格式。
            result = {}
            if "values" in body:
                result[tickers[0]] = _td_values_to_df(body)
            else:
                for tk in tickers:
                    payload = body.get(tk) or body.get(tk.upper())
                    result[tk] = _td_values_to_df(payload)
            return result
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "rate" in err or "too many" in err:
                # 免费层的窗口可能刚刚开始，短等几秒不够；让整个分钟窗口
                # 重新开始，避免 4 次短重试后仍然失败。
                wait = TD_BATCH_WAIT_SECONDS if "429" in err else 2 ** attempt + 1
                print(f"    [{interval}] Twelve Data 限速，{wait}s 后重试 ({attempt+1}/{max_retry})")
                time.sleep(wait)
            else:
                raise RuntimeError(f"Twelve Data {interval} 批量请求失败: {e}") from e
    raise RuntimeError(f"Twelve Data {interval} 批量请求重试失败")


def load_batch_or_download(tickers, interval, period, use_cache=True):
    """读取仍在有效期内的缓存，其余股票在一次批量请求中更新。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(NY_TZ)
    result = {}
    missing = []
    ttl = CACHE_TTL_SECONDS.get(interval, 15 * 60)
    for tk in tickers:
        cache_file = CACHE_DIR / f"{tk}_{interval}.parquet"
        try:
            age = now.timestamp() - cache_file.stat().st_mtime
            if use_cache and 0 <= age <= ttl:
                df = pd.read_parquet(cache_file)
                if df is not None and len(df) > 0:
                    result[tk] = df
                    continue
        except Exception:
            pass
        missing.append(tk)

    if missing:
        # 每批最多 8 只，匹配 Basic 免费层每分钟 8 credits 的限制。
        chunks = [
            missing[i:i + TD_FREE_CREDITS_PER_MINUTE]
            for i in range(0, len(missing), TD_FREE_CREDITS_PER_MINUTE)
        ]
        for chunk_no, chunk in enumerate(chunks, 1):
            print(f"    [{interval}] 批次 {chunk_no}/{len(chunks)}：{len(chunk)} 只")
            fresh = download_batch_with_retry(chunk, interval, period)
            for tk, df in fresh.items():
                result[tk] = df
                if df is not None and len(df) > 0:
                    try:
                        df.to_parquet(CACHE_DIR / f"{tk}_{interval}.parquet")
                    except Exception:
                        pass
            if chunk_no < len(chunks):
                print(f"    [{interval}] 下一批会自动等待免费额度窗口")
    return result


# ============================================================
# EMA 与排列判断
# ============================================================
def calc_alignment(df):
    """计算 EMA20/60，返回 (方向, ema20, ema60, close)
    方向: '多' / '空' / '平' / None(数据不足)
    """
    if df is None or len(df) < EMA_SLOW + 5:
        return None, None, None, None
    close = df["Close"].squeeze() if isinstance(df["Close"], pd.DataFrame) else df["Close"]
    close = pd.to_numeric(close, errors="coerce").dropna()
    if len(close) < EMA_SLOW + 5:
        return None, None, None, None
    ema_f = close.ewm(span=EMA_FAST, adjust=False).mean()
    ema_s = close.ewm(span=EMA_SLOW, adjust=False).mean()
    last_close = float(close.iloc[-1])
    last_f = float(ema_f.iloc[-1])
    last_s = float(ema_s.iloc[-1])
    if last_f > last_s:
        direction = "多"
    elif last_f < last_s:
        direction = "空"
    else:
        direction = "平"
    return direction, last_f, last_s, last_close


def last_data_asof(df):
    """返回行情最后一根 K 线的美东时间，方便核对数据是否新鲜。"""
    if df is None or len(df) == 0:
        return None
    try:
        ts = pd.Timestamp(df.index[-1])
        if ts.tzinfo is None:
            ts = ts.tz_localize(NY_TZ)
        else:
            ts = ts.tz_convert(NY_TZ)
        return ts.strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        return None


def classify(bull_count, bear_count, flat_count):
    """三周期方向 → 分层标签"""
    if flat_count > 0 or (bull_count + bear_count) < 3:
        return "待定"
    if bull_count == 3:
        return "多头共振"
    if bear_count == 3:
        return "空头共振"
    if bull_count == 2:
        return "偏多"
    if bear_count == 2:
        return "偏空"
    return "待定"


# ============================================================
# 主扫描
# ============================================================
def scan(tickers, use_cache=True):
    """扫描所有股票，返回 DataFrame"""
    rows = []
    total = len(tickers)
    data = {
        interval: load_batch_or_download(tickers, interval, period, use_cache)
        for interval, _, period in TIMEFRAMES
    }
    for i, tk in enumerate(tickers, 1):
        result = {"ticker": tk}
        bull = bear = flat = 0
        ok = True
        for interval, cn, period in TIMEFRAMES:
            df = data[interval].get(tk)
            direction, ema_f, ema_s, close = calc_alignment(df)
            result[f"{cn}_数据截至"] = last_data_asof(df) or "-"
            result[f"{cn}_方向"] = direction or "-"
            result[f"{cn}_EMA20"] = round(ema_f, 2) if ema_f else None
            result[f"{cn}_EMA60"] = round(ema_s, 2) if ema_s else None
            result[f"{cn}_收盘"] = round(close, 2) if close is not None else None
            if direction == "多":
                bull += 1
            elif direction == "空":
                bear += 1
            elif direction == "平":
                flat += 1
            else:
                ok = False
        result["多头数"] = bull
        result["空头数"] = bear
        result["分层"] = classify(bull, bear, flat) if ok else "待定"
        # 日线涨跌幅（参考）
        try:
            df_d = data["1d"].get(tk)
            if df_d is not None and len(df_d) >= 2:
                c = df_d["Close"].squeeze() if isinstance(df_d["Close"], pd.DataFrame) else df_d["Close"]
                c = pd.to_numeric(c, errors="coerce").dropna()
                if len(c) >= 2:
                    result["日线涨跌%"] = round((c.iloc[-1] / c.iloc[-2] - 1) * 100, 2)
        except Exception:
            result["日线涨跌%"] = None
        rows.append(result)
        status = " ".join(f"{cn}:{result[f'{cn}_方向']}" for _, cn, _ in TIMEFRAMES)
        print(f"  [{i:>3}/{total}] {tk:<6} {status}  -> {result['分层']}")
        time.sleep(0.4)  # 礼貌限速
    return pd.DataFrame(rows)


# ============================================================
# HTML 日报生成
# ============================================================
LAYER_ORDER = ["多头共振", "空头共振", "偏多", "偏空", "待定"]
LAYER_COLOR = {
    "多头共振": "#1D9E75",
    "空头共振": "#D85A30",
    "偏多":     "#639922",
    "偏空":     "#BA7517",
    "待定":     "#888780",
}


def gen_html(df, names, out_path, report_date=None):
    """生成交互式 HTML 日报"""
    generated_at = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M ET")
    report_date = report_date or generated_at.split()[0]
    total = len(df)
    summary = {l: int((df["分层"] == l).sum()) for l in LAYER_ORDER}

    # 按分层 + 多头数排序
    df_sorted = df.sort_values(["分层", "多头数", "空头数"], ascending=[True, False, True])

    def layer_table(layer_name):
        sub = df_sorted[df_sorted["分层"] == layer_name].copy()
        if sub.empty:
            return f"<p class='empty'>该层暂无标的</p>"
        color = LAYER_COLOR[layer_name]
        rows_html = []
        for _, r in sub.iterrows():
            name = names.get(r["ticker"], "")
            chg = r.get("日线涨跌%")
            chg_cls = "up" if (chg and chg > 0) else ("down" if (chg and chg < 0) else "")
            chg_txt = f"{chg:+.2f}%" if chg is not None else "-"
            def dir_badge(d):
                if d == "多": return f"<span class='badge b'>多</span>"
                if d == "空": return f"<span class='badge s'>空</span>"
                if d == "平": return f"<span class='badge f'>平</span>"
                return "<span class='badge'>-</span>"
            rows_html.append(f"""
            <tr>
              <td class='tk'>{r['ticker']}<span class='nm'>{name}</span></td>
              <td class='chg {chg_cls}'>{chg_txt}</td>
              <td>{dir_badge(r['60min_方向'])}</td>
              <td>{dir_badge(r['日线_方向'])}</td>
              <td>{dir_badge(r['周线_方向'])}</td>
              <td class='num'>{r['60min_收盘'] if r['60min_收盘'] is not None else '-'}</td>
              <td class='num'>{r['日线_收盘'] if r['日线_收盘'] is not None else '-'}</td>
            </tr>""")
        return f"""
        <table class='ltab'>
          <thead><tr>
            <th>标的</th><th>日涨跌</th>
            <th>60min</th><th>日线</th><th>周线</th>
            <th>60min价</th><th>日线价</th>
          </tr></thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>"""

    cards = "".join(f"""
      <div class='card' style='border-left-color:{LAYER_COLOR[l]}'>
        <div class='card-num' style='color:{LAYER_COLOR[l]}'>{summary[l]}</div>
        <div class='card-name'>{l}</div>
        <div class='card-desc'>{LAYERS[l]}</div>
      </div>""" for l in LAYER_ORDER)

    sections = "".join(f"""
      <section class='lsec'>
        <h2 style='color:{LAYER_COLOR[l]}'>{l} <span class='cnt'>({summary[l]})</span></h2>
        {layer_table(l)}
      </section>""" for l in LAYER_ORDER)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ND100 多周期共振扫描 · 行情日 {report_date}</title>
<style>
  :root {{--bg:#f7f7f4;--card:#fff;--txt:#1a1a1a;--mut:#666;--bd:#e5e5e0;
    --up:#c0392b;--dn:#1e8449;}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
    background:var(--bg);color:var(--txt);line-height:1.6;padding:24px;max-width:1100px;margin:0 auto}}
  .hd{{border-bottom:2px solid #333;padding-bottom:14px;margin-bottom:20px}}
  .hd h1{{font-size:22px;font-weight:600}}
  .hd .sub{{color:var(--mut);font-size:13px;margin-top:4px}}
  .hd .meta{{float:right;text-align:right;color:var(--mut);font-size:12px}}
  .hd .meta b{{color:var(--txt)}}
  .summary{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:18px 0 28px}}
  .card{{background:var(--card);border-radius:10px;padding:16px 14px;border-left:4px solid #ccc;
    box-shadow:0 1px 3px rgba(0,0,0,.04)}}
  .card-num{{font-size:30px;font-weight:700;line-height:1}}
  .card-name{{font-size:14px;font-weight:600;margin-top:6px}}
  .card-desc{{font-size:11px;color:var(--mut);margin-top:4px}}
  .lsec{{background:var(--card);border-radius:10px;padding:18px 20px;margin-bottom:20px;
    box-shadow:0 1px 3px rgba(0,0,0,.04)}}
  .lsec h2{{font-size:16px;font-weight:600;margin-bottom:12px}}
  .cnt{{color:var(--mut);font-weight:400;font-size:13px}}
  .empty{{color:var(--mut);font-size:13px;padding:8px 0}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{text-align:left;color:var(--mut);font-weight:500;font-size:11px;
    padding:8px 10px;border-bottom:1px solid var(--bd);text-transform:uppercase;letter-spacing:.5px}}
  td{{padding:9px 10px;border-bottom:1px solid #f0f0ec;vertical-align:middle}}
  tr:hover td{{background:#fafaf7}}
  .tk{{font-weight:600;font-size:13px}}
  .nm{{display:block;font-weight:400;font-size:11px;color:var(--mut);margin-top:1px}}
  .num{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#444}}
  .chg{{font-family:ui-monospace,monospace;font-weight:600}}
  .up{{color:var(--up)}} .down{{color:var(--dn)}}
  .badge{{display:inline-block;width:26px;height:22px;line-height:22px;text-align:center;
    border-radius:5px;font-size:12px;font-weight:600}}
  .b{{background:#eaf3de;color:#27500a}}
  .s{{background:#fcebfa;color:#791f1f;background:#fcebeb}}
  .f{{background:#f1efe8;color:#444}}
  .ft{{margin-top:24px;padding-top:14px;border-top:1px solid var(--bd);
    color:var(--mut);font-size:11px;line-height:1.7}}
  @media(max-width:680px){{.summary{{grid-template-columns:repeat(2,1fr)}}}}
</style></head>
<body>
  <div class='hd'>
    <div class='meta'>行情日期<b><br>{report_date}</b><br>扫描时间<b><br>{generated_at}</b><br>标的数<b> {total}</b></div>
    <h1>ND100 多周期共振扫描</h1>
    <div class='sub'>60min / 日线 / 周线 · EMA(20/60) 排列方向 · 三周期同向 = 共振</div>
  </div>
  <div class='summary'>{cards}</div>
  {sections}
  <div class='ft'>
    <b>读图说明</b>：<span class='badge b'>多</span> EMA20&gt;EMA60（多头排列）；
    <span class='badge s'>空</span> EMA20&lt;EMA60（空头排列）；
    <span class='badge f'>平</span> 均线走平。<br>
    <b>使用方式</b>：本报告只输出"查看顺序"，不构成买卖建议。多头共振优先复核做多逻辑，
    空头共振优先复核做空/规避逻辑，偏多/偏空等待周期收敛。<br>
    <b>风险提示</b>：均线共振只反映趋势现状，不预测拐点；追入已走远的共振需配合结构、量能与风险维度复核。<br>
    生成自 nd100_resonance_scanner.py · 对应 PPT《数字资产市场观察助手》趋势与多周期模块
  </div>
</body></html>"""
    out_path.write_text(html, encoding="utf-8")


# ============================================================
# 入口
# ============================================================
def load_completed_tickers(paths):
    """读取已有扫描结果，返回去重后的已完成 ticker 集合和来源信息。"""
    completed = []
    rows_by_file = {}
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"已完成输入文件不存在: {path}")
        frame = pd.read_csv(path, encoding="utf-8-sig")
        if "ticker" not in frame.columns:
            raise ValueError(f"已完成输入文件缺少 ticker 列: {path}")
        values = [
            str(t).strip().upper()
            for t in frame["ticker"].dropna()
            if str(t).strip()
        ]
        rows_by_file[str(path)] = len(values)
        completed.extend(values)
    return list(dict.fromkeys(completed)), rows_by_file


def frame_market_date(frame: pd.DataFrame) -> str:
    """Derive one complete daily market date from the scan result."""
    values = {
        str(value).strip()[:10].replace("-", "")
        for value in frame.get("日线_数据截至", pd.Series(dtype=str)).dropna()
        if str(value).strip()
    }
    if len(values) != 1 or not next(iter(values), "").isdigit():
        raise ValueError(f"扫描结果的日线行情日不唯一或缺失: {sorted(values)}")
    value = next(iter(values))
    datetime.strptime(value, "%Y%m%d")
    return value


def main():
    ap = argparse.ArgumentParser(description="ND100 多周期共振扫描器")
    ap.add_argument("--tickers", help="只扫指定股票，逗号分隔 (如 AAPL,MSFT)")
    ap.add_argument("--limit", type=int, help="最多扫描多少只（如 50 或 80）")
    ap.add_argument("--offset", type=int, default=0, help="从成分股第几只开始，配合 --limit 分组扫描")
    ap.add_argument("--completed-input", action="append",
                    help="已有扫描结果 CSV；自动排除其中 ticker 后扫描剩余集合，可重复传入")
    ap.add_argument("--plan-only", action="store_true",
                    help="只计算并打印本次待扫描 ticker，不请求 API、不生成报告")
    ap.add_argument("--output-tag", help="追加到输出文件名，避免分批扫描互相覆盖")
    ap.add_argument("--no-cache", action="store_true", help="忽略本地缓存重新下载")
    args = ap.parse_args()

    use_cache = not args.no_cache
    if args.completed_input and args.offset:
        ap.error("--completed-input 与 --offset 不能同时使用；已完成差集会自动形成剩余集合")
    if args.completed_input and args.tickers:
        ap.error("--completed-input 与 --tickers 不能同时使用；请让程序读取当前 ND100 清单")
    if args.limit is not None and args.limit <= 0:
        ap.error("--limit 必须大于 0")

    universe_tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else fetch_nd100_tickers()
    completed_tickers = []
    completed_files = {}
    excluded_from_current = []
    if args.completed_input:
        completed_tickers, completed_files = load_completed_tickers(args.completed_input)
        completed_set = set(completed_tickers)
        tickers = [tk for tk in universe_tickers if tk not in completed_set]
        excluded_from_current = [tk for tk in completed_tickers if tk in set(universe_tickers)]
        print(f"[续跑检查] 当前 universe: {len(universe_tickers)} 只")
        print(f"[续跑检查] 已完成输入: {len(completed_tickers)} 只，排除当前 universe 中 {len(excluded_from_current)} 只")
        print(f"[续跑检查] 待扫描集合: {len(tickers)} 只")
    else:
        tickers = list(universe_tickers)
    if args.offset < 0:
        ap.error("--offset 不能小于 0")
    if args.completed_input and args.limit is not None:
        tickers = tickers[:args.limit]
    elif args.limit is not None:
        tickers = tickers[args.offset:args.offset + args.limit]
    elif args.offset:
        tickers = tickers[args.offset:]
    if not tickers:
        ap.error("没有可扫描的股票")
    print(f"\n=== ND100 多周期共振扫描 ===")
    print(f"标的数: {len(tickers)}  周期: 60min/日线/周线  EMA: {EMA_FAST}/{EMA_SLOW}\n")
    if args.completed_input:
        print("[本次实际输入] " + ",".join(tickers))
    if args.plan_only:
        print("[计划检查] 仅规划，未请求 API，未生成报告。")
        return

    df = scan(tickers, use_cache)

    # 输出 CSV
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    market_date = frame_market_date(df)
    today = datetime.now(NY_TZ).strftime("%Y%m%d")
    tag = f"_{args.output_tag}" if args.output_tag else ""
    csv_path = OUTPUT_DIR / f"nd100_resonance_{market_date}{tag}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n[CSV] {csv_path}")

    # 公司名
    print("[公司名] 获取中...")
    names = get_company_names(tickers)

    # HTML 日报
    html_path = OUTPUT_DIR / f"nd100_resonance_{market_date}{tag}.html"
    gen_html(df, names, html_path, report_date=f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:]}")
    print(f"[HTML] {html_path}")

    manifest_path = OUTPUT_DIR / f"nd100_resonance_{market_date}{tag}_manifest.json"
    manifest_path.write_text(json.dumps({
        "report_date": market_date,
        "market_date": f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:]}",
        "scan_date": today,
        "created_at": datetime.now(NY_TZ).isoformat(timespec="seconds"),
        "universe_type": "nasdaq100",
        "universe_count_before_exclusion": len(universe_tickers),
        "completed_input_files": completed_files,
        "completed_ticker_count": len(completed_tickers),
        "excluded_completed_tickers": excluded_from_current,
        "scan_ticker_count": len(tickers),
        "scan_tickers": tickers,
        "scan_limit": args.limit,
        "scan_mode": "remaining_after_completed_input" if args.completed_input else "normal_or_offset",
        "data_access_policy": "优先使用有效缓存；缺失或过期时请求受保护 Twelve Data API",
        "historical_demo_files_used": False,
        "market_data_asof_by_interval": {
            cn: (df[f"{cn}_数据截至"].dropna().astype(str).max() if f"{cn}_数据截至" in df.columns else None)
            for _, cn, _ in TIMEFRAMES
        },
        "note": "报告文件日期使用真实行情日；scan_date/created_at 保留实际运行时间。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[MANIFEST] {manifest_path}")

    # 摘要
    print(f"\n=== 扫描完成 ===")
    for l in LAYER_ORDER:
        cnt = int((df["分层"] == l).sum())
        bar = "█" * cnt + "░" * (len(tickers) - cnt)
        print(f"  {l:<6} {cnt:>3}  {bar}")
    print(f"\n查看顺序建议：多头共振 → 偏多 → 待定 → 偏空 → 空头共振")


if __name__ == "__main__":
    main()
