#!/usr/bin/env python3
"""
缠论一二三类买卖点分析 + K线标注脚本
支持：日线/30分钟数据、K线包含处理、分型识别、内联SVG图表
"""
import argparse, csv, html, json, math, os, re, sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import warnings
warnings.filterwarnings("ignore", message=".*character detection.*")
try:
    import requests
    HAS_REQUESTS = True
    _session = requests.Session()
    _session.trust_env = False
except ImportError:
    HAS_REQUESTS = False
    _session = None


@dataclass
class Bar:
    ts_code: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    vol: float
    amount: float

@dataclass
class Pivot:
    index: int
    date: str
    kind: str
    price: float
    high: float
    low: float
    high_date: str = ""
    low_date: str = ""
    valid: bool = True
    filter_reason: str = ""

@dataclass
class Center:
    start_index: int; end_index: int; start_date: str; end_date: str
    zd: float; zg: float; gg: float; dd: float; direction: str

@dataclass
class MergedBar:
    high: float; low: float; open: float; close: float; date: str; vol: float; amount: float
    absorbed_dates: List[str] = None
    absorb_processes: List[str] = None

@dataclass
class Segment:
    start_idx: int; end_idx: int; start_date: str; end_date: str
    start_price: float; end_price: float; direction: str

@dataclass
class PenStep:
    """记录笔构造的每一步决策"""
    seq: int; prev_idx: int; prev_kind: str; prev_price: float; prev_high: float; prev_low: float
    curr_idx: int; curr_kind: str; curr_price: float; curr_high: float; curr_low: float
    gap: int; swing: float
    check: str  # "成笔"/"跳过(区间重叠)"/"跳过(间隔不足)"/"跳过(涨幅不足)"/"替换"
    accepted: bool


# ───────── 参数 ─────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="缠论一二三类买卖点分析 + K线标注")
    p.add_argument("--stock", required=True, help="股票名称或代码")
    p.add_argument("--source", default="web", choices=["auto", "local", "web"],
                   help="数据来源；默认 web。auto 也按网络取数处理，local 仅用于显式调试本地 CSV")
    p.add_argument("--data-dir", default="/Users/josan/Desktop/czsc/Stock")
    p.add_argument("--out-dir", default="/Users/josan/Desktop/czsc/reports")
    p.add_argument("--chart-timeframe", default="auto", choices=["auto", "5m", "30m", "daily"],
                   help="HTML 默认展示级别；auto 按 5分钟、30分钟、日线顺序自动降级")
    p.add_argument("--lookback", type=int, default=260)
    p.add_argument("--pivot-window", type=int, default=2)
    p.add_argument("--min-pivot-gap", type=int, default=3)
    p.add_argument("--min-swing-pct", type=float, default=1.2)
    p.add_argument("--with-30min", action="store_true", default=True)
    return p.parse_args()


# ───────── 股票代码解析 ─────────

def normalize_stock_code(code: str) -> str:
    c = code.strip().upper().replace(" ", "")
    m = re.fullmatch(r"H(\d{5})", c)
    if m: return f"{m.group(1)}.HK"
    m = re.fullmatch(r"\d{6}", c)
    if m: return f"{c}.SH" if c.startswith(("6","9")) else f"{c}.SZ"
    if re.fullmatch(r"\d{5}", c): return f"{c}.HK"
    return c

def resolve_csv(stock: str, data_dir: Path):
    n = normalize_stock_code(stock)
    candidates = []
    if n.endswith((".SH",".SZ",".BJ",".HK")):
        candidates.append(data_dir / f"{n}.csv")
    m = re.fullmatch(r"\d{6}", stock.strip())
    if m: candidates.extend(data_dir.glob(f"{stock.strip()}.*.csv"))
    for c in candidates:
        if c.exists(): return c, c.stem
    low = stock.strip().lower()
    fuzzy = [p for p in data_dir.glob("*.csv") if low in p.stem.lower()]
    if len(fuzzy) == 1: return fuzzy[0], fuzzy[0].stem
    if len(fuzzy) > 1: raise SystemExit(f"匹配到多个文件：{', '.join(p.stem for p in fuzzy[:10])}")
    raise SystemExit("未在本地 Stock CSV 中找到该股票。")


# ───────── 网络获取 ─────────

def eastsrch(query: str) -> List[dict]:
    if not HAS_REQUESTS: raise SystemExit("需要 requests 库")
    url = "https://searchadapter.eastmoney.com/api/suggest/get"
    params = {"input": query, "type": 14, "token": "D43BF722C8E33BDC906FB84D85E326E8"}
    resp = _session.get(url, params=params, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    items = []
    data = (resp.json().get("QuotationCodeTable",{}) or {}).get("Data") or []
    for sec in data:
        items.append({"code":sec.get("Code",""),"name":sec.get("Name",""),"mkt":sec.get("MktNum",0)})
    return items

def code_to_secid(code: str) -> str:
    c = normalize_stock_code(code)
    raw = code.strip().upper().replace(" ", "")
    if "中证1000" in code or raw == "399852":
        return "0.399852"
    if c.endswith(".HK"): return f"116.{c.replace('.HK','')}"
    m = re.match(r"(\d{6})\.(SH|SZ|BJ)", c)
    if m: return f"{'1' if m.group(2)=='SH' else '0'}.{m.group(1)}"
    if re.fullmatch(r"\d{6}", c): return f"{'1' if c.startswith(('6','9')) else '0'}.{c}"
    if re.fullmatch(r"\d{5}", c): return f"116.{c}"
    raise ValueError(f"无法解析股票代码: {code}")

def resolve_web_secid(stock_query: str) -> Tuple[str, str, str]:
    query = stock_query.strip()
    raw = query.upper().replace(" ", "")
    if "中证1000" in query or raw == "399852":
        return "0.399852", "399852", "中证1000"
    normalized = normalize_stock_code(query)
    try:
        secid = code_to_secid(normalized)
        return secid, normalized.replace(".HK", ""), ""
    except Exception:
        pass
    search_query = normalized.replace(".HK","") if normalized.endswith(".HK") else query
    results = eastsrch(search_query)
    if not results:
        raise SystemExit(f"搜索不到「{query}」")
    best = results[0]
    return code_to_secid(best["code"]), best["code"], best.get("name","")

def fetch_kline(secid: str, klt: str = "101", limit: Optional[int] = None):
    if not HAS_REQUESTS: raise SystemExit("需要 requests 库")
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    lmt = str(limit if limit is not None else 1000000)
    params = {"secid":secid,"fields1":"f1,f2,f3,f4,f5,f6",
              "fields2":"f51,f52,f53,f54,f55,f56,f57",
              "klt":klt,"fqt":"1","end":"20500101","lmt":lmt}
    resp = _session.get(url, params=params, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    d = resp.json().get("data")
    if not d or not d.get("klines"): raise SystemExit(f"无K线数据 (secid={secid}, klt={klt})")
    code, name, klines = d.get("code",secid), d.get("name",""), d["klines"]
    bars = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7: continue
        try:
            dt = re.sub(r"\D", "", parts[0])
            bars.append(Bar(code, dt, float(parts[1]), float(parts[3]), float(parts[4]),
                           float(parts[2]), float(parts[5]), float(parts[6])))
        except: continue
    bars.sort(key=lambda x: x.trade_date)
    bars = [b for b in bars if b.close > 0]
    if len(bars) < 40: raise SystemExit(f"数据不足40根K线 (实际{len(bars)})")
    return code, name, bars

def fetch_hk_daily_akshare(code: str, limit: int = 500):
    hk_code = normalize_stock_code(code).replace(".HK","")
    if not re.fullmatch(r"\d{5}", hk_code):
        raise ValueError(f"不是可识别的港股代码: {code}")
    try:
        import akshare as ak
    except ImportError as exc:
        raise SystemExit("东方财富历史行情接口不可用，且未安装 akshare，无法获取港股日线数据。") from exc
    df = ak.stock_hk_daily(symbol=hk_code)
    if df is None or len(df) == 0:
        raise SystemExit(f"akshare 未返回港股日线数据 ({hk_code})")
    if limit and len(df) > limit:
        df = df.tail(limit)
    bars = []
    for row in df.to_dict("records"):
        try:
            dt = str(row.get("date","")).replace("-","")[:8]
            if not re.fullmatch(r"\d{8}", dt):
                continue
            bars.append(Bar(hk_code, dt, float(row["open"]), float(row["high"]), float(row["low"]),
                           float(row["close"]), float(row.get("volume",0) or 0), float(row.get("amount",0) or 0)))
        except Exception:
            continue
    bars.sort(key=lambda x: x.trade_date)
    bars = [b for b in bars if b.close > 0]
    if len(bars) < 40:
        raise SystemExit(f"akshare 港股数据不足40根K线 (实际{len(bars)})")
    name = "腾讯控股" if hk_code == "00700" else ""
    return hk_code, name, bars

def csindex_symbol_from_query(query: str) -> Optional[str]:
    raw = query.strip().upper().replace(" ", "")
    normalized = normalize_stock_code(raw)
    digits = raw
    m = re.match(r"(\d{6})\.(SH|SZ|BJ)", normalized)
    if m:
        digits = m.group(1)
    if "中证1000" in query or digits in ("399852", "000852"):
        return "000852"
    return None

def fetch_csindex_daily_akshare(query: str, limit: int = 500):
    symbol = csindex_symbol_from_query(query)
    if not symbol:
        raise ValueError(f"不是可识别的中证指数代码: {query}")
    try:
        import akshare as ak
    except ImportError as exc:
        raise SystemExit("东方财富历史行情接口不可用，且未安装 akshare，无法获取中证指数日线数据。") from exc
    start_date = "20180101"
    end_date = datetime.now().strftime("%Y%m%d")
    df = ak.stock_zh_index_hist_csindex(symbol=symbol, start_date=start_date, end_date=end_date)
    if df is None or len(df) == 0:
        raise SystemExit(f"akshare 未返回中证指数日线数据 ({symbol})")
    if limit and len(df) > limit:
        df = df.tail(limit)
    bars = []
    name = ""
    for row in df.to_dict("records"):
        try:
            dt = str(row.get("日期", "")).replace("-", "")[:8]
            if not re.fullmatch(r"\d{8}", dt):
                continue
            open_ = float(row["开盘"])
            high = float(row["最高"])
            low = float(row["最低"])
            close = float(row["收盘"])
            if any(math.isnan(x) for x in (open_, high, low, close)):
                continue
            code = str(row.get("指数代码") or symbol)
            name = str(row.get("指数中文简称") or row.get("指数中文全称") or name)
            bars.append(Bar(code, dt, open_, high, low, close,
                           float(row.get("成交量", 0) or 0), float(row.get("成交金额", 0) or 0)))
        except Exception:
            continue
    bars.sort(key=lambda x: x.trade_date)
    bars = [b for b in bars if b.close > 0]
    if len(bars) < 40:
        raise SystemExit(f"akshare 中证指数数据不足40根K线 (实际{len(bars)})")
    return symbol, name or "中证1000", bars

def fetch_a_daily_akshare(code: str, limit: Optional[int] = None):
    normalized = normalize_stock_code(code)
    m = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", normalized)
    if not m:
        raise ValueError(f"不是可识别的 A 股代码: {code}")
    try:
        import akshare as ak
    except ImportError as exc:
        raise SystemExit("主网络行情接口不可用，且未安装 akshare，无法获取 A 股日线数据。") from exc
    market_code = f"{m.group(2).lower()}{m.group(1)}"
    df = ak.stock_zh_a_daily(symbol=market_code, start_date="19900101", end_date="20500101", adjust="qfq")
    if df is None or len(df) == 0:
        raise SystemExit(f"akshare 未返回 A 股日线数据 ({market_code})")
    if limit and len(df) > limit:
        df = df.tail(limit)
    bars = []
    for row in df.to_dict("records"):
        try:
            dt = str(row.get("date", "")).replace("-", "")[:8]
            if not re.fullmatch(r"\d{8}", dt):
                continue
            open_ = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            if any(math.isnan(x) for x in (open_, high, low, close)):
                continue
            bars.append(Bar(normalized, dt, open_, high, low, close,
                           float(row.get("volume", 0) or 0), float(row.get("amount", 0) or 0)))
        except Exception:
            continue
    bars.sort(key=lambda x: x.trade_date)
    bars = [b for b in bars if b.close > 0]
    if len(bars) < 40:
        raise SystemExit(f"akshare A 股数据不足40根K线 (实际{len(bars)})")
    return normalized, "", bars

def fetch_a_daily_tdx(code: str, limit: Optional[int] = None):
    try:
        import mootdx  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("通达信历史行情接口不可用：当前环境未安装 mootdx/pytdx") from exc
    raise RuntimeError("通达信历史行情接口尚未接入")

def fetch_a_daily_ths(code: str, limit: Optional[int] = None):
    try:
        import akshare as ak  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("同花顺历史行情接口不可用：当前环境未安装 akshare") from exc
    raise RuntimeError("同花顺个股历史 K 线接口尚未接入")

def fetch_stock_from_web(stock_query: str, klt: str = "101", limit: int = 500):
    query = stock_query.strip()
    normalized = normalize_stock_code(query)
    if klt != "101":
        secid = code_to_secid(normalized)
        code, name, bars = fetch_kline(secid, klt, limit)
        for b in bars: b.ts_code = f"{code} ({name})" if name else code
        return code, name, bars, "东方财富"

    try:
        code_to_secid(normalized)
        resolved_code = normalized
        resolved_name = ""
    except Exception:
        if csindex_symbol_from_query(query):
            resolved_code = query
            resolved_name = ""
        else:
            search_query = normalized.replace(".HK","") if normalized.endswith(".HK") else query
            results = eastsrch(search_query)
            if not results: raise SystemExit(f"搜索不到「{query}」")
            best = results[0]
            resolved_code = normalize_stock_code(best["code"])
            resolved_name = best.get("name","")

    attempts = []
    if normalize_stock_code(resolved_code).endswith((".SH", ".SZ", ".BJ")):
        attempts.extend([
            ("AKShare-新浪财经", lambda: fetch_a_daily_akshare(resolved_code, limit)),
            ("东方财富", lambda: fetch_kline(code_to_secid(resolved_code), klt, limit)),
            ("通达信", lambda: fetch_a_daily_tdx(resolved_code, limit)),
            ("同花顺", lambda: fetch_a_daily_ths(resolved_code, limit)),
        ])
    elif normalize_stock_code(resolved_code).endswith(".HK"):
        attempts.extend([
            ("AKShare-港股", lambda: fetch_hk_daily_akshare(resolved_code, limit)),
            ("东方财富", lambda: fetch_kline(code_to_secid(resolved_code), klt, limit)),
        ])
    elif csindex_symbol_from_query(query):
        attempts.extend([
            ("AKShare-中证指数", lambda: fetch_csindex_daily_akshare(query, limit)),
            ("东方财富", lambda: fetch_kline(code_to_secid(resolved_code), klt, limit)),
        ])
    else:
        attempts.append(("东方财富", lambda: fetch_kline(code_to_secid(resolved_code), klt, limit)))

    errors = []
    for provider, fetcher in attempts:
        try:
            code, name, bars = fetcher()
            name = name or resolved_name
            for b in bars: b.ts_code = f"{code} ({name})" if name else code
            return code, name, bars, provider
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
    raise SystemExit("所有网络行情源均不可用：" + "；".join(errors))

def fetch_stock_from_web_legacy(stock_query: str, klt: str = "101", limit: int = 500):
    """保留旧逻辑用于对照；主流程不再调用。"""
    query = stock_query.strip()
    normalized = normalize_stock_code(query)
    try:
        secid = code_to_secid(normalized)
        return fetch_kline(secid, klt, limit)
    except Exception as direct_err:
        if normalized.endswith((".SH", ".SZ", ".BJ")) and klt == "101":
            try:
                return fetch_a_daily_akshare(normalized, limit)
            except Exception:
                pass
        if normalized.endswith(".HK") and klt == "101":
            try:
                return fetch_hk_daily_akshare(normalized, limit)
            except Exception:
                pass
        if klt == "101":
            try:
                return fetch_csindex_daily_akshare(query, limit)
            except Exception:
                pass
    search_query = normalized.replace(".HK","") if normalized.endswith(".HK") else query
    results = eastsrch(search_query)
    if not results: raise SystemExit(f"搜索不到「{query}」")
    best = results[0]
    secid = code_to_secid(best["code"])
    try:
        code, name, bars = fetch_kline(secid, klt, limit)
    except Exception:
        best_code = normalize_stock_code(best["code"])
        if best_code.endswith((".SH", ".SZ", ".BJ")) and klt == "101":
            code, name, bars = fetch_a_daily_akshare(best_code, limit)
            name = name or best.get("name","")
        elif best_code.endswith(".HK") and klt == "101":
            code, name, bars = fetch_hk_daily_akshare(best["code"], limit)
            name = name or best.get("name","")
        else:
            raise
    for b in bars: b.ts_code = f"{code} ({name})"
    return best["code"], name, bars


# ───────── 数据读取 ─────────

def tof(v, d=0.0):
    try: return float(v) if v not in (None,"") else d
    except: return d

def read_bars(path: Path) -> List[Bar]:
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(Bar(r.get("ts_code",path.stem), r.get("trade_date",""),
                           tof(r.get("open")), tof(r.get("high")), tof(r.get("low")),
                           tof(r.get("close")), tof(r.get("vol")), tof(r.get("amount"))))
    rows = [r for r in rows if r.trade_date and r.close > 0]
    rows.sort(key=lambda x: x.trade_date)
    if len(rows) < 40: raise SystemExit("行情少于40根K线")
    return rows


# ───────── K线包含处理 ─────────

def merge_containing_bars(bars: List[Bar]) -> List[MergedBar]:
    if not bars: return []
    p = [MergedBar(bars[0].high,bars[0].low,bars[0].open,bars[0].close,bars[0].trade_date,bars[0].vol,bars[0].amount,[],[])]
    i = 1
    while i < len(bars):
        mb = MergedBar(bars[i].high,bars[i].low,bars[i].open,bars[i].close,bars[i].trade_date,bars[i].vol,bars[i].amount,[],[])
        last = p[-1]
        if (mb.high <= last.high and mb.low >= last.low) or (last.high <= mb.high and last.low >= mb.low):
            up = len(p) < 2 or last.high >= p[-2].high
            absorbed = list(last.absorbed_dates or []) + [last.date]
            processes = list(last.absorb_processes or [])
            process = "向上处理：取高高（最高取高、最低取高）" if up else "向下处理：取低低（最高取低、最低取低）"
            processes.append(f"{process}（处理{fmt_date(last.date)}）")
            if up: p[-1] = MergedBar(max(last.high,mb.high),max(last.low,mb.low),last.open,mb.close,mb.date,last.vol+mb.vol,last.amount+mb.amount,absorbed,processes)
            else: p[-1] = MergedBar(min(last.high,mb.high),min(last.low,mb.low),last.open,mb.close,mb.date,last.vol+mb.vol,last.amount+mb.amount,absorbed,processes)
        else: p.append(mb)
        i += 1
    return p


# ───────── 分型识别 ─────────

def find_fractal_candidates(merged: List[MergedBar]) -> List[Pivot]:
    candidates = []
    for i in range(1, len(merged)-1):
        a,b,c = merged[i-1],merged[i],merged[i+1]
        if b.high > a.high and b.high > c.high and b.low > a.low and b.low > c.low:
            # high = b.high（中间K线最高，即整个分型最高点）
            # low = min(a.low, c.low)（左右K线最低点可能低于中间K线最低点）
            low = min(a.low, c.low)
            low_date = a.date if a.low <= c.low else c.date
            candidates.append(Pivot(i,b.date,"top",b.high,b.high,low,b.date,low_date))
        if b.low < a.low and b.low < c.low and b.high < a.high and b.high < c.high:
            # high = max(a.high, c.high)（左右K线最高点可能高于中间K线最高点）
            # low = b.low（中间K线最低，即整个分型最低点）
            high = max(a.high, c.high)
            high_date = a.date if a.high >= c.high else c.date
            candidates.append(Pivot(i,b.date,"bottom",b.low,high,b.low,high_date,b.date))
    candidates.sort(key=lambda p: (p.index, 0 if p.kind=="bottom" else 1))
    return candidates

def find_fractals(merged: List[MergedBar]) -> List[Pivot]:
    records = filter_fractals_by_occupied_bars(find_fractal_candidates(merged))
    return [p for p in records if p.valid]

def filter_fractals_by_occupied_bars(candidates: List[Pivot]) -> List[Pivot]:
    """确定分型序列。

    - 每个分型占用3根K线，两个分型之间至少隔1根独立K线。
    - 同类连续分型只保留更极端者：顶取更高，底取更低。
    - 底后顶：顶分型的底不能低于/等于前底分型的顶。
    - 顶后底：底分型的顶不能高于/等于前顶分型的底。
    """
    pivots = []
    for p in candidates:
        p.valid = True
        p.filter_reason = ""
        if not pivots:
            pivots.append(p)
            continue
        last = pivots[-1]
        if p.kind == last.kind:
            better_top = p.kind == "top" and p.price > last.price
            better_bottom = p.kind == "bottom" and p.price < last.price
            if better_top or better_bottom:
                last.valid = False
                last.filter_reason = (
                    f"同类分型，后续顶分型更高，保留{fmt_date(p.date)}"
                    if p.kind == "top"
                    else f"同类分型，后续底分型更低，保留{fmt_date(p.date)}"
                )
                pivots[-1] = p
            else:
                p.valid = False
                p.filter_reason = (
                    f"同类分型，顶分型未高于已保留顶分型{fmt_date(last.date)}"
                    if p.kind == "top"
                    else f"同类分型，底分型未低于已保留底分型{fmt_date(last.date)}"
                )
            continue

        last_end = last.index + 1
        curr_start = p.index - 1
        if curr_start < last_end + 2:
            p.valid = False
            p.filter_reason = f"分型占用区间与前一有效分型{fmt_date(last.date)}重叠，或中间不足1根独立K线"
            continue

        if last.kind == "bottom" and p.kind == "top":
            if p.low > last.high:
                pivots.append(p)
            else:
                p.valid = False
                p.filter_reason = f"顶分型最低{p.low:.2f}未高于前一底分型最高{last.high:.2f}，区间重叠"
            continue
        if last.kind == "top" and p.kind == "bottom":
            if p.high < last.low:
                pivots.append(p)
            else:
                p.valid = False
                p.filter_reason = f"底分型最高{p.high:.2f}未低于前一顶分型最低{last.low:.2f}，区间重叠"
            continue
    return candidates


# ───────── 笔构造 ─────────

def build_pens(fractals: List[Pivot], min_gap=2, min_swing_pct=0.8, return_details=False):
    """构造笔，规则：
    - 顶底交替，连续同向取极值
    - 反向分型须间隔 min_gap 根K线
    - 满足最小波动百分比 min_swing_pct
    - 价格区间检查：底分型整体high < 顶分型整体low（向上笔）；顶分型整体low > 底分型整体high（向下笔）
    - return_details=True 时额外返回笔构造步骤明细
    """
    pens = []
    details = []
    seq = 0
    for p in fractals:
        if not pens:
            pens.append(p)
            continue
        last = pens[-1]
        if p.kind == last.kind:
            # 同向取极值：顶取更高，底取更低
            replaced = False
            if (p.kind=="top" and p.price>last.price) or (p.kind=="bottom" and p.price<last.price):
                # 同向替换时检查与前一个反向分型的区间重叠
                if len(pens) >= 2:
                    prev = pens[-2]
                    ok = False
                    if p.kind == "top":
                        ok = prev.high < p.low
                        if not ok and return_details:
                            details.append(PenStep(seq, prev.index, prev.kind, prev.price, prev.high, prev.low,
                                                  p.index, p.kind, p.price, p.high, p.low,
                                                  p.index - prev.index, abs((p.price-prev.price)/prev.price*100) if prev.price else 0,
                                                  f"跳过(区间重叠): 底high({prev.high:.1f}) >= 顶low({p.low:.1f})", False))
                            seq += 1
                    else:
                        ok = prev.low > p.high
                        if not ok and return_details:
                            details.append(PenStep(seq, prev.index, prev.kind, prev.price, prev.high, prev.low,
                                                  p.index, p.kind, p.price, p.high, p.low,
                                                  p.index - prev.index, abs((p.price-prev.price)/prev.price*100) if prev.price else 0,
                                                  f"跳过(区间重叠): 顶low({prev.low:.1f}) <= 底high({p.high:.1f})", False))
                            seq += 1
                    if ok:
                        if return_details:
                            details.append(PenStep(seq, prev.index, prev.kind, prev.price, prev.high, prev.low,
                                                  p.index, p.kind, p.price, p.high, p.low,
                                                  p.index - prev.index, abs((p.price-prev.price)/prev.price*100) if prev.price else 0,
                                                  f"替换: {last.kind}({last.price:.1f})→{p.kind}({p.price:.1f})", True))
                            seq += 1
                        pens[-1] = p
                        replaced = True
                else:
                    pens[-1] = p
                    replaced = True
            if not replaced and return_details and p.kind != last.kind:
                pass  # better extreme but no prev to check
            continue
        gap = p.index - last.index
        move = abs((p.price - last.price) / last.price * 100) if last.price else 0
        if gap < min_gap:
            if return_details:
                details.append(PenStep(seq, last.index, last.kind, last.price, last.high, last.low,
                                      p.index, p.kind, p.price, p.high, p.low,
                                      gap, move, f"跳过(间隔不足): gap={gap}<{min_gap}", False))
                seq += 1
            continue
        if move < min_swing_pct:
            if return_details:
                details.append(PenStep(seq, last.index, last.kind, last.price, last.high, last.low,
                                      p.index, p.kind, p.price, p.high, p.low,
                                      gap, move, f"跳过(涨幅不足): {move:.2f}%<{min_swing_pct}%", False))
                seq += 1
            continue
        # 价格区间检查
        overlap = False
        if p.kind == "top":  # last=底, p=顶, 向上笔
            overlap = last.high >= p.low
            check_str = f"底整体high({last.high:.1f}) < 顶整体low({p.low:.1f})?"
        else:  # p.kind=="bottom", last=顶, p=底, 向下笔
            overlap = last.low <= p.high
            check_str = f"顶整体low({last.low:.1f}) > 底整体high({p.high:.1f})?"
        if not overlap:
            if return_details:
                details.append(PenStep(seq, last.index, last.kind, last.price, last.high, last.low,
                                      p.index, p.kind, p.price, p.high, p.low,
                                      gap, move, f"成笔: {check_str} 通过", True))
                seq += 1
            pens.append(p)
        else:
            if return_details:
                details.append(PenStep(seq, last.index, last.kind, last.price, last.high, last.low,
                                      p.index, p.kind, p.price, p.high, p.low,
                                      gap, move, f"跳过(区间重叠): {check_str} false", False))
                seq += 1
    if return_details:
        return pens, details
    return pens


# ───────── 线段检测 ─────────

def find_segments(pens: List[Pivot], merged: List[MergedBar]) -> List[Segment]:
    segs = []
    if len(pens) < 3: return segs
    for i in range(0, len(pens)-2, 2):
        if i+2 >= len(pens): break
        p1,p2,p3 = pens[i],pens[i+1],pens[i+2]
        if p1.kind=="bottom" and p2.kind=="top" and p3.kind=="bottom" and p3.price>p1.price:
            segs.append(Segment(p1.index,p3.index,p1.date,p3.date,p1.price,p3.price,"up"))
        elif p1.kind=="top" and p2.kind=="bottom" and p3.kind=="top" and p3.price<p1.price:
            segs.append(Segment(p1.index,p3.index,p1.date,p3.date,p1.price,p3.price,"down"))
    if not segs: return segs
    ms = [segs[0]]
    for s in segs[1:]:
        l = ms[-1]
        if s.start_idx <= l.end_idx and s.direction == l.direction:
            ms[-1] = Segment(l.start_idx,max(l.end_idx,s.end_idx),l.start_date,s.end_date,l.start_price,s.end_price,l.direction)
        else: ms.append(s)
    return ms


# ───────── 中枢 ─────────

def swing_bounds(a: Pivot, b: Pivot): return max(a.price,b.price), min(a.price,b.price)

def find_centers(pens: List[Pivot]) -> List[Center]:
    centers = []
    if len(pens) < 4: return centers
    for i in range(len(pens)-3):
        sw = [(pens[i+j],pens[i+j+1]) for j in range(3)]
        hs,ls = [],[]
        for a,b in sw: h,l=swing_bounds(a,b); hs.append(h); ls.append(l)
        zd,zg = max(ls), min(hs)
        if zd <= zg:
            centers.append(Center(pens[i].index,pens[i+3].index,pens[i].date,pens[i+3].date,zd,zg,max(hs),min(ls),
                                  "up" if pens[i+3].price>pens[i].price else "down"))
    mg = []
    for c in centers:
        if mg and c.start_index <= mg[-1].end_index and max(c.zd,mg[-1].zd) <= min(c.zg,mg[-1].zg):
            p=mg[-1]; mg[-1]=Center(p.start_index,max(p.end_index,c.end_index),p.start_date,c.end_date,max(p.zd,c.zd),min(p.zg,c.zg),max(p.gg,c.gg),min(p.dd,c.dd),c.direction)
        else: mg.append(c)
    return mg


# ───────── 指标计算 ─────────

def sma(vals, n):
    out,s=[],0.0
    for i,v in enumerate(vals):
        s+=v
        if i>=n: s-=vals[i-n]
        out.append(s/n if i>=n-1 else None)
    return out

def ema(vals, n):
    a=2/(n+1); out=[vals[0]]
    for v in vals[1:]: out.append(a*v+(1-a)*out[-1])
    return out

def macd(vals):
    e12,e26=ema(vals,12),ema(vals,26)
    dif=[a-b for a,b in zip(e12,e26)]; dea=ema(dif,9); hist=[(d-e)*2 for d,e in zip(dif,dea)]
    return dif,dea,hist

def fmt_date(v):
    if re.fullmatch(r"\d{8}",v): return f"{v[:4]}-{v[4:6]}-{v[6:]}"
    return v

def area_between(hist,start,end,sign):
    s,e=max(0,start),min(len(hist)-1,end)
    if s>e: return 0.0
    return sum(abs(x) for x in hist[s:e+1] if (sign=="negative" and x<0) or (sign=="positive" and x>0) or sign not in ("negative","positive"))

def last_kind(pivots,kind,n=3): return [p for p in pivots if p.kind==kind][-n:]

def signal(score,label,conf,ev,ct,lv):
    return {"label":label,"score":score,"confidence":conf,"evidence":ev,"counter_evidence":ct,"levels":lv}


# ───────── 买卖点诊断 ─────────

def diagnose(bars, pens, centers, lookback):
    closes=[b.close for b in bars]
    ma5,ma10,ma20,ma60=sma(closes,5),sma(closes,10),sma(closes,20),sma(closes,60)
    dif,dea,hist=macd(closes)
    lat,ci=bars[-1],len(bars)-1
    rs=max(0,len(bars)-lookback)
    rp=[p for p in pens if p.index>=rs]
    rc=[c for c in centers if c.end_index>=rs]
    lc=rc[-1] if rc else (centers[-1] if centers else None)
    lma={"ma5":ma5[-1],"ma10":ma10[-1],"ma20":ma20[-1],"ma60":ma60[-1]}
    ma_state="unknown"
    if ma5[-1] is not None and ma20[-1] is not None:
        ma_state="女上位" if ma5[-1]>ma20[-1] else "男上位" if ma5[-1]<ma20[-1] else "均线粘合"
    bp,tp=last_kind(rp,"bottom",4),last_kind(rp,"top",4)
    lb,pb=bp[-1] if bp else None,bp[-2] if len(bp)>=2 else None
    lt,pt=tp[-1] if tp else None,tp[-2] if len(tp)>=2 else None
    signals=[]
    if lc:
        nl=lb and ci-lb.index<=20; nh=lt and ci-lt.index<=20
        bc=lat.close<lc.zd or (lb and lb.price<lc.zd)
        ac=lat.close>lc.zg or (lt and lt.price>lc.zg)
        dv,de=False,[]
        if lb and pb:
            pa=area_between(hist,max(0,pb.index-20),pb.index,"negative")
            la2=area_between(hist,max(0,lb.index-20),lb.index,"negative")
            if lb.price<pb.price and la2<pa*0.85: dv=True; de.append(f"底部价格创新低（{lb.price:.2f}<{pb.price:.2f}），近段MACD绿柱收缩（{la2:.4f}<{pa:.4f}）。")
            else: de.append(f"未形成标准底背驰：价格{lb.price:.2f}/{pb.price:.2f}，绿柱{la2:.4f}/{pa:.4f}。")
        sc,ev,ct=0,[],[]
        if bc: sc+=2; ev.append(f"位于中枢下方或跌破ZD={lc.zd:.2f}")
        else: ct.append(f"未处于中枢下方，中枢[{lc.zd:.2f},{lc.zg:.2f}]")
        if dv: sc+=3; ev.extend(de)
        else: ct.extend(de[:1])
        if nl: sc+=1; ev.append(f"底分型距当前{ci-lb.index}根K线")
        else: ct.append("底分型距离较远")
        signals.append(signal(sc,"第一类买点候选" if sc>=3 else "未确认第一类买点",
                             "高" if sc>=5 else "中" if sc>=3 else "低",ev,ct,{"ZD":lc.zd,"ZG":lc.zg}))
        uv,ue=False,[]
        if lt and pt:
            pa=area_between(hist,max(0,pt.index-20),pt.index,"positive")
            la2=area_between(hist,max(0,lt.index-20),lt.index,"positive")
            if lt.price>pt.price and la2<pa*0.85: uv=True; ue.append(f"顶价创新高（{lt.price:.2f}>{pt.price:.2f}），MACD红柱收缩（{la2:.4f}<{pa:.4f}）。")
            else: ue.append(f"未形成标准顶背驰：价格{lt.price:.2f}/{pt.price:.2f}，红柱{la2:.4f}/{pa:.4f}。")
        sc,ev,ct=0,[],[]
        if ac: sc+=2; ev.append(f"位于中枢上方或突破ZG={lc.zg:.2f}")
        else: ct.append(f"未处于中枢上方")
        if uv: sc+=3; ev.extend(ue)
        else: ct.extend(ue[:1])
        if nh: sc+=1; ev.append(f"顶分型距当前{ci-lt.index}根K线")
        else: ct.append("顶分型距离较远")
        signals.append(signal(sc,"第一类卖点候选" if sc>=3 else "未确认第一类卖点",
                             "高" if sc>=5 else "中" if sc>=3 else "低",ev,ct,{"ZD":lc.zd,"ZG":lc.zg}))
        signals.extend(diagnose_2nd_3rd(bars,rp,lc,hist))
    ranked=sorted(signals,key=lambda x:x["score"],reverse=True)
    cur=ranked[0] if ranked else signal(0,"无法判断","低",[],["缺少中枢结构"],{})
    if cur["score"]<3: cur={**cur,"label":"当前无确认的一二三类买卖点","confidence":"低"}
    return {"latest":{"ts_code":lat.ts_code,"trade_date":lat.trade_date,"date":fmt_date(lat.trade_date),
            "open":lat.open,"high":lat.high,"low":lat.low,"close":lat.close},
            "ma_state":ma_state,"ma":lma,"macd":{"dif":dif[-1],"dea":dea[-1],"hist":hist[-1]},
            "latest_center":lc.__dict__ if lc else None,
            "centers":[c.__dict__ for c in rc[-6:]],
            "pivots":[p.__dict__ for p in rp[-20:]],
            "signals":ranked,"current":cur}

def diagnose_2nd_3rd(bars,pens,center,hist):
    out=[]; ci=len(bars)-1
    after=[p for p in pens if p.index>center.end_index]
    ta=[p for p in after if p.kind=="top"]; ba=[p for p in after if p.kind=="bottom"]
    ev,ct,sc=[],[],0; ult=next((p for p in after if p.kind=="top" and p.price>center.zg),None)
    if ult:
        fb=next((p for p in ba if p.index>ult.index),None); sc+=2
        ev.append(f"向上离开中枢：{fmt_date(ult.date)} 高点{ult.price:.2f}>ZG{center.zg:.2f}")
        if fb:
            if fb.price>=center.zg: sc+=4; ev.append(f"回试低点{fb.price:.2f}未跌破ZG，符合三类买点")
            else: ct.append(f"回试低点{fb.price:.2f}已跌回ZG下方")
        else: ct.append("未出现回试底分型")
    else: ct.append("未识别离开中枢的顶分型")
    out.append(signal(sc,"第三类买点候选" if sc>=4 else "未确认第三类买点",
                     "高" if sc>=6 else "中" if sc>=4 else "低",ev,ct,{"ZG":center.zg,"ZD":center.zd}))
    ev,ct,sc=[],[],0; ulb=next((p for p in after if p.kind=="bottom" and p.price<center.zd),None)
    if ulb:
        fr=next((p for p in ta if p.index>ulb.index),None); sc+=2
        ev.append(f"向下离开中枢：{fmt_date(ulb.date)} 低点{ulb.price:.2f}<ZD{center.zd:.2f}")
        if fr:
            if fr.price<=center.zd: sc+=4; ev.append(f"回抽高点{fr.price:.2f}未升破ZD，符合三类卖点")
            else: ct.append(f"回抽高点{fr.price:.2f}已回到ZD上方")
        else: ct.append("未出现回抽顶分型")
    else: ct.append("未识别离开中枢的底分型")
    out.append(signal(sc,"第三类卖点候选" if sc>=4 else "未确认第三类卖点",
                     "高" if sc>=6 else "中" if sc>=4 else "低",ev,ct,{"ZG":center.zg,"ZD":center.zd}))
    ev,ct,sc=[],[],0
    if len(ba)>=2 and len(ta)>=1:
        fl,pl=ba[0],ba[1]
        if pl.price>fl.price*0.98: sc+=3; ev.append(f"一买后回调低点{pl.price:.2f}未跌破前低{fl.price:.2f}")
        else: ct.append(f"回调低点{pl.price:.2f}跌破前低{fl.price:.2f}")
    else: ct.append("未形成底-顶-底结构")
    out.append(signal(sc,"第二类买点候选" if sc>=3 else "未确认第二类买点","中" if sc>=4 else "低",ev,ct,{}))
    ev,ct,sc=[],[],0
    if len(ta)>=2 and len(ba)>=1:
        fh,rh=ta[0],ta[1]
        if rh.price<fh.price*1.02: sc+=3; ev.append(f"一卖后反弹高点{rh.price:.2f}未突破前高{fh.price:.2f}")
        else: ct.append(f"反弹高点{rh.price:.2f}突破前高{fh.price:.2f}")
    else: ct.append("未形成顶-底-顶结构")
    out.append(signal(sc,"第二类卖点候选" if sc>=3 else "未确认第二类卖点","中" if sc>=4 else "低",ev,ct,{}))
    return out


# ───────── 完整缠论分析管道 ─────────

def chan_analysis(bars, merged, args):
    raw=find_fractals(merged)
    pens,details=build_pens(raw,args.min_pivot_gap,args.min_swing_pct,return_details=True)
    segs=find_segments(pens,merged)
    centers=find_centers(pens)
    diag=diagnose(bars,pens,centers,args.lookback)
    fractal_records=filter_fractals_by_occupied_bars(find_fractal_candidates(merged))
    return raw,pens,centers,segs,diag,details,fractal_records


# ───────── 内联SVG K线图（viewBox 缩放/拖拽） ─────────

def _make_svg_chart(stock_code, bars, pens, raw_fractals, centers, segments, timeframe_label="日线", chart_id="main"):
    """用 SVG viewBox 实现可缩放/拖拽的 K 线分型标注图。

    当前 HTML 报告聚焦 K 线包含处理和原始分型验证，所以图中只绘制：
    - K 线
    - 顶分型标记：蓝色 ▼
    - 底分型标记：橙色 ▲
    """
    W, H = 960, 520  # viewBox 初始宽高
    LM, TM, BM, RM = 55, 8, 28, 15  # 边距
    BW = 8  # 每根K线宽度（px）
    cid = re.sub(r"[^A-Za-z0-9_-]+", "_", chart_id)
    pb = bars
    # 限制最大渲染条数（保留足够的数据用于缩放/拖拽）
    MAX_RENDER_BARS = 800
    if len(pb) > MAX_RENDER_BARS:
        pb = pb[-MAX_RENDER_BARS:]
    if len(pb) < 2:
        return ""

    ah = max(b.high for b in pb)
    al = min(b.low for b in pb)
    pr = ah - al if ah > al else 1
    yh = ah + pr * 0.08
    yl = al - pr * 0.08
    yr = yh - yl

    def yp(p):
        return TM + (H - TM - BM) - (p - yl) / yr * (H - TM - BM)

    total_width = LM + len(pb) * BW + RM  # SVG 内容总宽

    # 日期 → X 坐标映射（用于分型的日期定位）
    dt_map = {}
    for i, b in enumerate(pb):
        dt_map[b.trade_date] = LM + i * BW + BW / 2
    # 获取显示范围内的最早和最晚日期
    first_date = pb[0].trade_date[:8]
    last_date = pb[-1].trade_date[:8]
    def xd(d):
        if d in dt_map:
            return dt_map[d]
        # 模糊匹配（仅比较 YYYYMMDD 部分）
        d8 = d[:8]
        for k, v in dt_map.items():
            if k[:8] == d8:
                return v
        return LM

    # 过滤：只保留在显示日期范围内的原始分型
    visible_raw = [p for p in raw_fractals if first_date <= p.date[:8] <= last_date]

    # 内置 K 线数据为 JSON（用于 JS 交互）
    bar_data = []
    for i, b in enumerate(pb):
        dt = b.trade_date
        if len(dt) >= 12:
            ds = f"{dt[:4]}\u5e74{int(dt[4:6])}\u6708{int(dt[6:8])}\u65e5 {dt[8:10]}:{dt[10:12]}"
        elif len(dt) == 8:
            ds = f"{dt[:4]}\u5e74{int(dt[4:6])}\u6708{int(dt[6:])}\u65e5"
        else:
            ds = dt
        bar_data.append({"i": i, "dt": dt, "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.vol, "x": round(LM + i * BW + BW / 2, 1)})
    bar_json = json.dumps(bar_data, ensure_ascii=False)

    total_bars = len(pb)
    def_offset = max(0, total_bars - 80)

    # ── 构建 SVG ──
    svg = f'''<svg id="chan-chart-svg-{cid}" class="chan-chart-svg" xmlns="http://www.w3.org/2000/svg"
  viewBox="{LM + def_offset * BW} 0 {W} {H}"
  preserveAspectRatio="none"
  style="width:100%;height:100%;display:block;background:#fafafa;border-radius:6px;font-family:sans-serif;font-size:11px;cursor:grab;touch-action:none;">
  <g id="chart-g">'''

    # 背景
    svg += f'<rect x="{LM}" y="{TM}" width="{total_width - LM - RM}" height="{H - TM - BM}" fill="#fafafa" stroke="#e0e0e0" stroke-width="1"/>'

    # 网格
    for i in range(9):
        yy = TM + (H - TM - BM) * i / 8
        price = yh - yr * i / 8
        svg += f'<line x1="{LM}" y1="{yy:.1f}" x2="{total_width - RM}" y2="{yy:.1f}" stroke="#e8eaed" stroke-width="1"/>'
        svg += f'<text x="{LM - 5}" y="{yy:.1f}" text-anchor="end" fill="#667085" font-size="10" dominant-baseline="middle">{price:.1f}</text>'

    # K 线
    for i, b in enumerate(pb):
        x = LM + i * BW
        cx = x + BW / 2
        up = b.close >= b.open
        clr = "#b42318" if up else "#175cd3"
        bt = yp(max(b.open, b.close))
        bb = yp(min(b.open, b.close))
        svg += f'<line x1="{cx:.1f}" y1="{yp(b.high):.1f}" x2="{cx:.1f}" y2="{yp(b.low):.1f}" stroke="{clr}" stroke-width="1"/>'
        if bb - bt < 1:
            svg += f'<line x1="{x:.1f}" y1="{bt:.1f}" x2="{x + BW - 1:.1f}" y2="{bt:.1f}" stroke="{clr}" stroke-width="1.5"/>'
        else:
            svg += f'<rect x="{x:.1f}" y="{bt:.1f}" width="{BW - 1}" height="{max(1, bb - bt):.1f}" fill="{clr}" rx="0"/>'

    # 所有原始分型标注（参照参考文档：顶分型蓝色▼、底分型橙色▲）
    for p in visible_raw:
        x = xd(p.date)
        if x < LM:
            continue
        if p.kind == "top":
            svg += f'<polygon points="{x:.1f},{yp(p.price):.1f} {x - 4:.1f},{yp(p.price) - 8:.1f} {x + 4:.1f},{yp(p.price) - 8:.1f}" fill="#1f6f8b"/>'
            svg += f'<text x="{x:.1f}" y="{yp(p.price) - 10:.1f}" text-anchor="middle" fill="#444" font-size="9" dominant-baseline="bottom">{p.price:.1f}</text>'
        else:
            svg += f'<polygon points="{x:.1f},{yp(p.price):.1f} {x - 4:.1f},{yp(p.price) + 8:.1f} {x + 4:.1f},{yp(p.price) + 8:.1f}" fill="#f79009"/>'
            svg += f'<text x="{x:.1f}" y="{yp(p.price) + 18:.1f}" text-anchor="middle" fill="#444" font-size="9" dominant-baseline="top">{p.price:.1f}</text>'

    svg += f'<line id="chart-selected-line-{cid}" x1="{LM}" y1="{TM}" x2="{LM}" y2="{H - BM}" stroke="#1f6f8b" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.95" style="display:none;pointer-events:none;"/>'
    svg += '</g></svg>'

    # ── 交互 HTML（viewBox 缩放/拖拽） ──
    interactive_html = f'''
<div class="chan-chart-shell">
  <div class="chart-toolbar">
    <button id="zoom-in-btn-{cid}" title="\u653e\u5927" type="button">+</button>
    <span id="zoom-label-{cid}">1\u00d7</span>
    <button id="zoom-out-btn-{cid}" title="\u7f29\u5c0f" type="button">-</button>
    <button id="reset-zoom-btn-{cid}" title="\u91cd\u7f6e\u89c6\u56fe" type="button">\u91cd\u7f6e</button>
    <span class="chart-help">\u6eda\u8f6e\u7f29\u653e \u00b7 \u62d6\u62fd\u5e73\u79fb \u00b7 \u60ac\u505c\u67e5\u770b\u8be6\u60c5</span>
  </div>
  <div class="chart-wrap" id="chc-{cid}">
    {svg}
    <div id="chtooltip-{cid}" class="chtooltip"></div>
  </div>
</div>
<script>
(function() {{
try {{
var chartData = {{
  bars: {bar_json},
  totalBars: {total_bars}
}};

var svg = document.getElementById('chan-chart-svg-{cid}');
var wrap = document.getElementById('chc-{cid}');
var tip = document.getElementById('chtooltip-{cid}');
var selectedLine = document.getElementById('chart-selected-line-{cid}');

var LM = {LM}, BW = {BW}, TM = {TM}, BM = {BM}, RM = {RM};
var CH = {H};
var totalWidth = LM + chartData.totalBars * BW + RM;
var defOffset = Math.max(0, chartData.totalBars - 80);
var originX = LM + defOffset * BW;
var originY = 0;
var viewW = {W};
var viewH = CH;
var minVisibleBars = Math.min(chartData.totalBars, 24);
var minViewW = Math.max(minVisibleBars * BW, 180);
var maxViewW = Math.max(totalWidth, minViewW);
var dateToBar = {{}};

function normalizeDate(value) {{
  return String(value || '').replace(/[^0-9]/g, '').slice(0, 8);
}}

for (var di = 0; di < chartData.bars.length; di++) {{
  dateToBar[normalizeDate(chartData.bars[di].dt)] = di;
}}

function chartRect() {{
  var rect = wrap.getBoundingClientRect();
  if (!rect.width || !rect.height) {{
    rect = {{ width: {W}, height: {H}, left: 0, top: 0 }};
  }}
  return rect;
}}

function viewHeightForWidth(width) {{
  var rect = chartRect();
  var h = width * rect.height / rect.width;
  return Math.max(120, Math.min(CH, h));
}}

function resetViewWindow() {{
  var rect = chartRect();
  viewH = CH;
  viewW = Math.max(minViewW, Math.min(maxViewW, Math.min(totalWidth, CH * rect.width / rect.height)));
  viewH = viewHeightForWidth(viewW);
  originX = Math.max(0, totalWidth - viewW);
  originY = Math.max(0, (CH - viewH) / 2);
  updateViewBox();
}}

function updateViewBox() {{
  viewW = Math.max(minViewW, Math.min(maxViewW, viewW));
  viewH = viewHeightForWidth(viewW);
  originX = Math.max(0, Math.min(Math.max(0, totalWidth - viewW), originX));
  originY = Math.max(0, Math.min(Math.max(0, CH - viewH), originY));
  svg.setAttribute('viewBox', originX.toFixed(1) + ' ' + originY.toFixed(1) + ' ' + viewW.toFixed(1) + ' ' + viewH.toFixed(1));
  updateZoomLabel();
}}

function updateZoomLabel() {{
  var zl = document.getElementById('zoom-label-{cid}');
  if (!zl) return;
  var cw = wrap.clientWidth || wrap.getBoundingClientRect().width || {W};
  if (cw < 10) cw = {W};
  var visibleBars = Math.max(1, Math.round(viewW / BW));
  zl.textContent = visibleBars + '\u6839';
}}

function initSize() {{
  resetViewWindow();
}}

// 窗口/容器尺寸变化时，仅更新倍率标签（不改变缩放状态）
function resize() {{
  updateZoomLabel();
}}

function zoomAt(factor, mouseXRatio, mouseYRatio) {{
  if (mouseXRatio === undefined) mouseXRatio = 0.5;
  if (mouseYRatio === undefined) mouseYRatio = 0.5;
  var oldW = viewW;
  var oldH = viewH;
  viewW = Math.max(minViewW, Math.min(maxViewW, viewW * factor));
  viewH = viewHeightForWidth(viewW);
  originX += mouseXRatio * (oldW - viewW);
  originY += mouseYRatio * (oldH - viewH);
  updateViewBox();
}}

function svgCoordX(clientX) {{
  var rect = svg.getBoundingClientRect();
  if (!rect.width) return 0;
  return (clientX - rect.left) / rect.width * viewW + originX;
}}

function getBarAt(clientX) {{
  var rect = svg.getBoundingClientRect();
  if (!rect.width) return -1;
  // 鼠标在SVG元素内的像素位置
  var mx = clientX - rect.left;
  // 将每根K线的SVG X坐标映射到当前屏幕像素，找最近距离
  var best = -1, bestDist = 1e9;
  for (var i = 0; i < chartData.totalBars; i++) {{
    var barPx = (chartData.bars[i].x - originX) / viewW * rect.width;
    var d = Math.abs(mx - barPx);
    if (d < bestDist) {{ bestDist = d; best = i; }}
  }}
  // 超过1.5根K线宽度则忽略
  var oneBarPx = BW / viewW * rect.width;
  if (bestDist > oneBarPx * 1.5) return -1;
  return best;
}}

function showTip(idx, cx, cy) {{
  if (idx < 0 || idx >= chartData.totalBars) {{ tip.style.display = 'none'; return; }}
  var b = chartData.bars[idx];
  var ds = b.dt.substring(0,4) + '\u5e74' + parseInt(b.dt.substring(4,6)) + '\u6708' + parseInt(b.dt.substring(6,8)) + '\u65e5';
  tip.innerHTML = '<div><b>' + ds + '</b></div>' +
    '<div>\u5f00\u76d8: ' + b.o.toFixed(2) + ' &nbsp;|&nbsp; \u6700\u9ad8: ' + b.h.toFixed(2) + '</div>' +
    '<div>\u6536\u76d8: ' + b.c.toFixed(2) + ' &nbsp;|&nbsp; \u6700\u4f4e: ' + b.l.toFixed(2) + '</div>';
  tip.style.display = 'block';
  var r = wrap.getBoundingClientRect();
  var lx = cx - r.left + 12;
  var ly = cy - r.top - 10;
  if (lx + 200 > r.width) lx = cx - r.left - 210;
  if (lx < 4) lx = 4;
  if (ly < 4) ly = 4;
  if (ly + 80 > r.height) ly = r.height - 85;
  tip.style.left = lx + 'px';
  tip.style.top = ly + 'px';
}}

function showSelectedLine(idx) {{
  if (!selectedLine || idx < 0 || idx >= chartData.totalBars) return;
  var x = chartData.bars[idx].x;
  selectedLine.setAttribute('x1', x.toFixed(1));
  selectedLine.setAttribute('x2', x.toFixed(1));
  selectedLine.style.display = 'block';
}}

function focusChartDate(value, row) {{
  var date = normalizeDate(value);
  var idx = dateToBar[date];
  if (idx === undefined) return;
  var x = chartData.bars[idx].x;
  originX = x - viewW * 0.5;
  updateViewBox();
  showSelectedLine(idx);
  var panel = wrap.closest('.timeframe-panel') || document;
  panel.querySelectorAll('tr.chart-linked-row.selected-row').forEach(function(r) {{
    r.classList.remove('selected-row');
  }});
  if (row) row.classList.add('selected-row');
  var rect = svg.getBoundingClientRect();
  var cx = rect.left + (x - originX) / viewW * rect.width;
  var cy = rect.top + Math.min(rect.height - 20, Math.max(24, rect.height * 0.24));
  showTip(idx, cx, cy);
  wrap.scrollIntoView({{ block: 'nearest', inline: 'nearest', behavior: 'smooth' }});
}}

var isPanning = false, panStartX = 0, panStartY = 0, panStartOrgX = 0, panStartOrgY = 0;
wrap.addEventListener('mousedown', function(e) {{
  if (e.button !== 0) return;
  isPanning = true;
  panStartX = e.clientX;
  panStartY = e.clientY;
  panStartOrgX = originX;
  panStartOrgY = originY;
  svg.style.cursor = 'grabbing';
}});

window.addEventListener('mousemove', function(e) {{
  if (isPanning) {{
    var rect = svg.getBoundingClientRect();
    if (rect.width && rect.height) {{
      var dx = (panStartX - e.clientX) / rect.width * viewW;
      var dy = (panStartY - e.clientY) / rect.height * viewH;
      originX = Math.max(0, Math.min(Math.max(0, totalWidth - viewW), panStartOrgX + dx));
      originY = Math.max(0, Math.min(Math.max(0, CH - viewH), panStartOrgY + dy));
      svg.setAttribute('viewBox', originX.toFixed(1) + ' ' + originY.toFixed(1) + ' ' + viewW.toFixed(1) + ' ' + viewH.toFixed(1));
    }}
    return;
  }}
  var idx = getBarAt(e.clientX);
  var r = svg.getBoundingClientRect();
  var my = r.height ? ((e.clientY - r.top) / r.height * viewH + originY) : -1;
  if (idx >= 0 && my >= TM && my <= CH - BM) {{
    showTip(idx, e.clientX, e.clientY);
  }} else {{
    tip.style.display = 'none';
  }}
}});

window.addEventListener('mouseup', function() {{
  if (isPanning) {{ isPanning = false; svg.style.cursor = 'grab'; }}
}});

wrap.addEventListener('mouseleave', function() {{ tip.style.display = 'none'; }});

document.addEventListener('click', function(e) {{
  var row = e.target.closest('tr[data-chart-date]');
  if (!row || !wrap.closest('.timeframe-panel.active')) return;
  focusChartDate(row.getAttribute('data-chart-date'), row);
}});

wrap.addEventListener('wheel', function(e) {{
  e.preventDefault();
  var rect = svg.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  var mx = e.clientX - rect.left;
  var my = e.clientY - rect.top;
  var ratio = Math.max(0, Math.min(1, mx / rect.width));
  var yRatio = Math.max(0, Math.min(1, my / rect.height));
  var factor = e.deltaY < 0 ? 0.70 : 1.43;
  zoomAt(factor, ratio, yRatio);
}}, {{ passive: false }});

var touchState = null;
wrap.addEventListener('touchstart', function(e) {{
  if (e.touches.length === 1) {{
    touchState = {{ mode: 'pan', startX: e.touches[0].clientX, startY: e.touches[0].clientY, startOrgX: originX, startOrgY: originY }};
  }} else if (e.touches.length === 2) {{
    var tr = wrap.getBoundingClientRect();
    touchState = {{
      mode: 'zoom', dist: Math.sqrt(Math.pow(e.touches[0].clientX - e.touches[1].clientX, 2) + Math.pow(e.touches[0].clientY - e.touches[1].clientY, 2)),
      startOrgX: originX, startOrgY: originY, startVW: viewW, startVH: viewH,
      centerXRatio: Math.max(0, Math.min(1, ((e.touches[0].clientX + e.touches[1].clientX) / 2 - tr.left) / (tr.width || 1))),
      centerYRatio: Math.max(0, Math.min(1, ((e.touches[0].clientY + e.touches[1].clientY) / 2 - tr.top) / (tr.height || 1)))
    }};
  }}
}}, {{ passive: true }});

wrap.addEventListener('touchmove', function(e) {{
  if (!touchState) return;
  e.preventDefault();
  if (touchState.mode === 'pan' && e.touches.length === 1) {{
    var rect = svg.getBoundingClientRect();
    if (rect.width && rect.height) {{
      var dx = (touchState.startX - e.touches[0].clientX) / rect.width * viewW;
      var dy = (touchState.startY - e.touches[0].clientY) / rect.height * viewH;
      originX = Math.max(0, Math.min(Math.max(0, totalWidth - viewW), touchState.startOrgX + dx));
      originY = Math.max(0, Math.min(Math.max(0, CH - viewH), touchState.startOrgY + dy));
      svg.setAttribute('viewBox', originX.toFixed(1) + ' ' + originY.toFixed(1) + ' ' + viewW.toFixed(1) + ' ' + viewH.toFixed(1));
    }}
  }} else if (touchState.mode === 'zoom' && e.touches.length === 2) {{
    var dx = e.touches[0].clientX - e.touches[1].clientX;
    var dy = e.touches[0].clientY - e.touches[1].clientY;
    var nd = Math.sqrt(dx * dx + dy * dy);
    var factor = touchState.dist / nd;
    var oldW = touchState.startVW;
    var oldH = touchState.startVH;
    viewW = Math.max(minViewW, Math.min(maxViewW, touchState.startVW * factor));
    viewH = viewHeightForWidth(viewW);
    originX = touchState.startOrgX + touchState.centerXRatio * (oldW - viewW);
    originY = touchState.startOrgY + touchState.centerYRatio * (oldH - viewH);
    updateViewBox();
  }}
}}, {{ passive: false }});

wrap.addEventListener('touchend', function() {{ touchState = null; }});

document.getElementById('zoom-in-btn-{cid}').addEventListener('click', function() {{ zoomAt(0.50, 0.5, 0.5); }});
document.getElementById('zoom-out-btn-{cid}').addEventListener('click', function() {{ zoomAt(2.00, 0.5, 0.5); }});
document.getElementById('reset-zoom-btn-{cid}').addEventListener('click', function() {{
  tip.style.display = 'none';
  resetViewWindow();
}});

wrap.addEventListener('dblclick', function() {{
  tip.style.display = 'none';
  resetViewWindow();
}});

initSize();
window.addEventListener('resize', function() {{ updateZoomLabel(); }});
try {{
  var ro = new ResizeObserver(function() {{ updateZoomLabel(); }});
  ro.observe(wrap);
}} catch(e) {{}}

}} catch(e) {{ /* \u5b89\u5168\u515c\u5e95 */ }}
}})();
</script>'''
    return interactive_html


# ───────── HTML报告 ─────────

def safe(v,d=2):
    if v is None or (isinstance(v,float) and math.isnan(v)): return "-"
    return f"{v:.{d}f}"

def jsan(v):
    if isinstance(v,float) and (math.isnan(v) or math.isinf(v)): return None
    if isinstance(v,dict): return {k:jsan(v) for k,v in v.items()}
    if isinstance(v,list): return [jsan(x) for x in v]
    return v


def build_report_panel(stock_code, frame):
    label = frame.get("label", "-")
    key = frame.get("key", "main")
    bars = frame.get("bars") or []
    merged_bars = frame.get("merged") or []
    raw_fractals = frame.get("raw") or []
    fractal_records = frame.get("fractal_records") or raw_fractals

    def price_with_date(value, date):
        return f"{safe(value)}（{fmt_date(date)}）" if date else safe(value)

    f_lines = []
    for i,pv in enumerate(fractal_records,1):
        valid = getattr(pv, "valid", True)
        row_classes = ["chart-linked-row"]
        if not valid:
            row_classes.append("filtered-fractal-row")
        status = "有效" if valid else "已过滤"
        reason = html.escape(getattr(pv, "filter_reason", "") or "")
        f_lines.append(
            f"<tr class=\"{' '.join(row_classes)}\" data-chart-date=\"{pv.date}\"><td>{i}</td><td>{fmt_date(pv.date)}</td><td>{'顶分型' if pv.kind=='top' else '底分型'}</td>"
            f"<td>{price_with_date(pv.price, pv.date)}</td>"
            f"<td>{price_with_date(pv.high, getattr(pv,'high_date','') or pv.date)}</td>"
            f"<td>{price_with_date(pv.low, getattr(pv,'low_date','') or pv.date)}</td>"
            f"<td>{status}</td><td class=\"reason-cell\">{reason}</td></tr>"
        )
    frows = "".join(f_lines)

    mb_lines = []
    for i,b in enumerate(merged_bars,1):
        absorbed_dates = b.absorbed_dates or []
        row_classes = ["chart-linked-row"]
        if absorbed_dates:
            row_classes.append("absorbed-row")
        row_class = f' class="{" ".join(row_classes)}" data-chart-date="{b.date}"'
        absorbed_items = "".join(f"<li>{fmt_date(d)}</li>" for d in absorbed_dates)
        absorbed_text = f"<ul class=\"cell-list\">{absorbed_items}</ul>" if absorbed_items else ""
        process_items = "".join(f"<li>{html.escape(x)}</li>" for x in (b.absorb_processes or []))
        process_text = f"<ul class=\"cell-list\">{process_items}</ul>" if process_items else ""
        mb_lines.append(
            f"<tr{row_class}><td>{i}</td><td>{fmt_date(b.date)}</td>"
            f"<td>{safe(b.open)}</td><td>{safe(b.high)}</td><td>{safe(b.low)}</td><td>{safe(b.close)}</td>"
            f"<td>{absorbed_text}</td><td class=\"process-cell\">{process_text}</td></tr>"
        )
    mbrows = "".join(mb_lines)

    svg = _make_svg_chart(stock_code, bars, [], raw_fractals, [], [], label, key) if len(bars) > 1 else ""
    chart_section = f'''
<section class="chart-section">
<h2>{html.escape(label)} K 线分型图</h2>
<div class="legend">
<span><span class="dot" style="background:#1f6f8b"></span>顶分型(▼)</span>
<span><span class="dot" style="background:#f79009"></span>底分型(▲)</span>
</div>
{svg if svg else '<p class="note">当前级别没有足够K线生成图表。</p>'}
</section>'''

    return f'''
<details>
<summary>包含处理后K线列表（{len(merged_bars)}根）</summary>
<div class="table-wrap merge-table-wrap">
<table>
<thead><tr><th>#</th><th>日期</th><th>开盘</th><th>最高</th><th>最低</th><th>收盘</th><th>被处理日期</th><th>处理过程</th></tr></thead>
<tbody>{mbrows if mbrows else '<tr><td colspan="8">无包含处理数据</td></tr>'}</tbody>
</table></div>
</details>
{chart_section}
<details open>
<summary>原始分型列表（候选{len(fractal_records)}个，有效{len(raw_fractals)}个）</summary>
<div class="table-wrap fractal-table-wrap">
<table>
<thead><tr><th>#</th><th>日期</th><th>类型</th><th>分型价格</th><th>最高</th><th>最低</th><th>状态</th><th>过滤原因</th></tr></thead>
<tbody>{frows if frows else '<tr><td colspan="8">未识别到足够分型</td></tr>'}</tbody>
</table></div>
</details>'''


def make_html(stock_code, source_label, timeframes, default_key="daily"):
    gen = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    available_frames = [f for f in timeframes if f.get("available")]
    default_frame = next((f for f in timeframes if f.get("key") == default_key and f.get("available")), None) or (available_frames[0] if available_frames else None)
    default_key = default_frame.get("key", "") if default_frame else ""
    latest_date = default_frame.get("latest_date", "-") if default_frame else "-"
    source_desc = default_frame.get("source", source_label) if default_frame else source_label
    failed = [f for f in timeframes if not f.get("available")]
    warn_text = "；".join(f"{f.get('label')}: {f.get('error')}" for f in failed if f.get("error"))
    warn_html = f'<div class="tf-warning">部分级别不可用：{html.escape(warn_text)}</div>' if warn_text else ""

    panels = []
    buttons = []
    for frame in timeframes:
        key = frame.get("key")
        active = key == default_key
        available = frame.get("available")
        label = frame.get("label", key)
        if available:
            panels.append(f'<div class="timeframe-panel{" active" if active else ""}" data-timeframe-panel="{html.escape(key)}">{build_report_panel(stock_code, frame)}</div>')
            buttons.append(
                f'<button type="button" class="tf-button{" active" if active else ""}" data-timeframe="{html.escape(key)}" '
                f'data-meta="{html.escape(frame.get("meta", ""))}">{html.escape(label)}</button>'
            )
        else:
            buttons.append(
                f'<button type="button" class="tf-button" disabled title="{html.escape(frame.get("error", "不可用"))}">{html.escape(label)}</button>'
            )
    panels_html = "\n".join(panels) or '<section><p class="note">没有可展示的K线级别。</p></section>'
    buttons_html = "\n".join(buttons)

    return f'''<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(stock_code)} 缠论分析报告</title>
<style>
:root {{ color-scheme: light; --ink:#20242a; --muted:#667085; --line:#d9dee7; --bg:#f6f7f9; --panel:#fff; --accent:#1f6f8b; }}
* {{ box-sizing:border-box; }}
html {{ -webkit-text-size-adjust:100%; text-size-adjust:100%; }}
body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:var(--bg); line-height:1.58; overflow-x:hidden; }}
header {{ background:#fff; border-bottom:1px solid var(--line); padding:28px clamp(18px,4vw,48px); }}
main {{ width:min(1200px,100%); margin:0 auto; padding:24px; }}
h1 {{ margin:0 0 8px; font-size:clamp(24px,4vw,38px); }}
h2 {{ margin:0 0 14px; font-size:20px; }}
.sub {{ color:var(--muted); }}
.tf-switch {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:14px; }}
.tf-button {{ border:1px solid #d9dee7; border-radius:6px; background:#fff; color:var(--ink); padding:7px 14px; cursor:pointer; font-size:14px; }}
.tf-button.active {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
.tf-button:disabled {{ cursor:not-allowed; opacity:.45; }}
.tf-warning {{ margin-top:10px; color:#912018; font-size:13px; }}
.timeframe-panel {{ display:none; }}
.timeframe-panel.active {{ display:block; }}
section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:20px; margin-bottom:18px; min-width:0; }}
details {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:0; margin-bottom:18px; min-width:0; overflow:hidden; }}
summary {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:16px 20px; cursor:pointer; font-size:20px; font-weight:700; list-style:none; }}
summary::-webkit-details-marker {{ display:none; }}
summary::after {{ content:"收起"; color:var(--muted); font-size:13px; font-weight:600; }}
details:not([open]) summary::after {{ content:"展开"; }}
details .table-wrap {{ padding:0 20px 18px; }}
.table-wrap {{ overflow-x:auto; -webkit-overflow-scrolling:touch; max-width:100%; }}
.merge-table-wrap {{ max-height:546px; overflow:auto; }}
.fractal-table-wrap {{ max-height:366px; overflow:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; white-space:nowrap; }}
th {{ background:#f2f4f7; font-weight:700; position:sticky; top:0; }}
tr.chart-linked-row {{ cursor:pointer; }}
tr.absorbed-row td {{ background:#fff1f0; color:#912018; }}
tr.filtered-fractal-row td {{ background:#fff1f0; color:#912018; }}
tr.selected-row td {{ background:#e8f6fb !important; color:var(--ink); }}
tr.selected-row td:first-child {{ box-shadow:inset 3px 0 0 var(--accent); }}
td.process-cell {{ min-width:260px; white-space:normal; overflow-wrap:anywhere; }}
td.reason-cell {{ min-width:280px; white-space:normal; overflow-wrap:anywhere; }}
td .cell-list {{ list-style:none; margin:0; padding:0; min-width:0; white-space:normal; }}
td .cell-list li {{ margin:0 0 3px; padding:0; line-height:1.45; }}
td .cell-list li:last-child {{ margin-bottom:0; }}
td ul {{ min-width:280px; white-space:normal; overflow-wrap:anywhere; }}
ul {{ margin:0; padding-left:18px; }}
.note {{ color:var(--muted); font-size:13px; }}
.legend {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:8px; font-size:13px; }}
.legend span {{ display:inline-flex; align-items:center; gap:4px; }}
.legend .dot {{ display:inline-block; width:12px; height:12px; border-radius:50%; }}
.chan-chart-shell {{ position:relative; border-radius:6px; background:#fafafa; -webkit-user-select:none; user-select:none; max-width:100%; }}
.chart-toolbar {{ display:flex; gap:6px; padding:6px 0; align-items:center; flex-wrap:wrap; }}
.chart-toolbar button {{ min-width:34px; height:30px; padding:0 10px; border:1px solid #d9dee7; border-radius:4px; background:#fff; color:var(--ink); cursor:pointer; font-size:15px; line-height:1; }}
.chart-toolbar button:hover {{ background:#f8fafc; }}
.zoom-label {{ color:#667085; font-size:13px; min-width:56px; text-align:center; }}
.chart-help {{ color:#98a2b3; font-size:12px; margin-left:auto; }}
.chart-wrap {{ position:relative; overflow:hidden; border-radius:4px; border:1px solid #e0e0e0; height:clamp(360px,56vh,620px); max-height:70vh; background:#fafafa; touch-action:none; }}
.chan-chart-svg text {{ vector-effect:non-scaling-stroke; paint-order:stroke; stroke:#fafafa; stroke-width:2px; stroke-linejoin:round; }}
.chan-chart-svg line,.chan-chart-svg rect,.chan-chart-svg polygon {{ vector-effect:non-scaling-stroke; }}
.chtooltip {{ display:none; position:absolute; pointer-events:none; background:rgba(32,36,42,0.92); color:#fff; padding:8px 12px; border-radius:6px; font-size:13px; line-height:1.6; z-index:100; white-space:nowrap; font-family:sans-serif; max-width:min(280px,calc(100% - 16px)); }}
@media (max-width: 720px) {{
  header {{ padding:22px 16px; }}
  main {{ padding:14px; }}
  section {{ padding:14px; }}
  summary {{ padding:14px; font-size:18px; }}
  details .table-wrap {{ padding:0 14px 14px; }}
  h1 {{ font-size:24px; }}
  h2 {{ font-size:18px; }}
  .chart-help {{ flex-basis:100%; margin-left:0; }}
  .chart-wrap {{ height:420px; max-height:none; }}
}}
</style>
</head>
<body>
<header><h1>{html.escape(stock_code)} K线包含处理与分型测试</h1>
<div class="sub" id="timeframe-meta">数据日期：{html.escape(fmt_date(latest_date))} ｜ {html.escape(gen)} ｜ {html.escape(source_desc)}</div>
<div class="tf-switch" aria-label="K线级别切换">{buttons_html}</div>
{warn_html}</header>
<main>
{panels_html}
<section>
<h2>判读口径</h2>
<p>报告当前只展示包含处理后的 K 线顶/底分型结果，便于单独校验基础分型稳定性。</p>
<p class="note">本报告只用于技术分析研究，不构成投资建议。</p>
</section>
</main>
<script>
(function() {{
  var meta = document.getElementById('timeframe-meta');
  document.querySelectorAll('.tf-button[data-timeframe]').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var key = btn.getAttribute('data-timeframe');
      document.querySelectorAll('.tf-button[data-timeframe]').forEach(function(b) {{ b.classList.toggle('active', b === btn); }});
      document.querySelectorAll('.timeframe-panel').forEach(function(panel) {{
        panel.classList.toggle('active', panel.getAttribute('data-timeframe-panel') === key);
      }});
      if (meta && btn.dataset.meta) meta.textContent = btn.dataset.meta;
      document.querySelectorAll('tr.selected-row').forEach(function(r) {{ r.classList.remove('selected-row'); }});
    }});
  }});
}})();
</script>
</body>
</html>'''


# ───────── 主流程 ─────────

def load_data(args):
    data_dir=Path(args.data_dir)
    if args.source in ("web", "auto"):
        code,name,bars,provider=fetch_stock_from_web(args.stock,"101",None)
        stock_code=f"{code} ({name})" if name else code
        return stock_code,f"{provider} ({code})",bars
    try:
        csv_path,stock_code=resolve_csv(args.stock,data_dir)
        bars=read_bars(csv_path)
        if args.source=="local": return stock_code,f"本地 CSV ({csv_path.name})",bars
    except SystemExit:
        if args.source=="local": raise
        code,name,bars,provider=fetch_stock_from_web(args.stock,"101",None)
        return f"{code} ({name})" if name else code,f"{provider} ({code})",bars
    return stock_code,f"本地 CSV ({csv_path.name})",bars


def analyze_timeframe_frame(key, label, stock_code, source_label, bars, args):
    merged = merge_containing_bars(bars)
    raw,pens,centers,segs,diagnosis,details,fractal_records = chan_analysis(bars, merged, args)
    latest_date = diagnosis.get("latest", {}).get("date") or (bars[-1].trade_date if bars else "")
    meta = f"数据日期：{fmt_date(latest_date)} ｜ {label} ｜ {source_label}"
    return {
        "key": key,
        "label": label,
        "available": True,
        "source": source_label,
        "bars": bars,
        "merged": merged,
        "raw": raw,
        "pens": pens,
        "centers": centers,
        "segments": segs,
        "diagnosis": diagnosis,
        "details": details,
        "fractal_records": fractal_records,
        "latest_date": latest_date,
        "meta": meta,
        "bar_count": len(bars),
        "merged_count": len(merged),
        "fractal_candidate_count": len(fractal_records),
        "valid_fractal_count": len(raw),
        "error": "",
    }


def unavailable_timeframe(key, label, error):
    return {
        "key": key,
        "label": label,
        "available": False,
        "source": "",
        "bar_count": 0,
        "merged_count": 0,
        "fractal_candidate_count": 0,
        "valid_fractal_count": 0,
        "error": str(error),
    }


def fetch_intraday_frame(args, key, label, klt):
    secid, code, name = resolve_web_secid(args.stock)
    code, fetched_name, bars = fetch_kline(secid, klt, None)
    display_name = name or fetched_name
    source = f"东方财富 ({code}, {label})"
    stock_code = f"{code} ({display_name})" if display_name else code
    return stock_code, source, bars


def build_timeframes(args, daily_stock_code, daily_source, daily_bars):
    requested = ["5m", "30m", "daily"] if args.chart_timeframe == "auto" else [args.chart_timeframe]
    order = ["5m", "30m", "daily"]
    labels = {"5m": "5分钟", "30m": "30分钟", "daily": "日线"}
    klts = {"5m": "5", "30m": "30"}
    frames_by_key = {}
    stock_code = daily_stock_code

    for key in order:
        label = labels[key]
        if key not in requested and args.chart_timeframe != "auto":
            frames_by_key[key] = unavailable_timeframe(key, label, "未请求该级别")
            continue
        try:
            if key == "daily":
                frame = analyze_timeframe_frame(key, label, daily_stock_code, daily_source, daily_bars, args)
            else:
                if args.source == "local":
                    raise SystemExit("本地 CSV 仅提供日线，分钟线需要网络数据")
                intraday_stock_code, intraday_source, intraday_bars = fetch_intraday_frame(args, key, label, klts[key])
                stock_code = intraday_stock_code or stock_code
                frame = analyze_timeframe_frame(key, label, stock_code, intraday_source, intraday_bars, args)
            frames_by_key[key] = frame
        except Exception as exc:
            frames_by_key[key] = unavailable_timeframe(key, label, exc)

    frames = [frames_by_key[k] for k in order]
    default_key = ""
    if args.chart_timeframe == "auto":
        for k in order:
            if frames_by_key[k].get("available"):
                default_key = k
                break
    elif frames_by_key.get(args.chart_timeframe, {}).get("available"):
        default_key = args.chart_timeframe
    else:
        for k in order:
            if frames_by_key[k].get("available"):
                default_key = k
                break
    return stock_code, frames, default_key


def timeframe_summary(frame):
    return {
        "key": frame.get("key"),
        "label": frame.get("label"),
        "available": frame.get("available", False),
        "source": frame.get("source", ""),
        "bar_count": frame.get("bar_count", 0),
        "merged_count": frame.get("merged_count", 0),
        "fractal_candidate_count": frame.get("fractal_candidate_count", 0),
        "valid_fractal_count": frame.get("valid_fractal_count", 0),
        "error": frame.get("error", ""),
    }


def main():
    args=parse_args()
    out_dir=Path(args.out_dir); out_dir.mkdir(parents=True,exist_ok=True)
    daily_stock_code,src_label,daily_bars=load_data(args)
    stock_code,timeframes,default_key=build_timeframes(args,daily_stock_code,src_label,daily_bars)
    default_frame=next((f for f in timeframes if f.get("key")==default_key and f.get("available")), None)
    if default_frame is None:
        raise SystemExit("没有可用的K线级别，无法生成报告")

    diagnosis=default_frame["diagnosis"]
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    sc=re.sub(r"[^A-Za-z0-9_.-]+","_",stock_code)[:60]
    jp=out_dir/f"{sc}_chan_points_{stamp}.json"
    hp=out_dir/f"{sc}_chan_points_{stamp}.html"

    payload={"stock":stock_code,"source":default_frame.get("source", src_label),
             "analysis_level":f"{default_frame.get('label')}级别（含级别切换）",
             "bar_count":default_frame.get("bar_count",0),
             "pens":len(default_frame.get("pens") or []),"centers":len(default_frame.get("centers") or []),"segments":len(default_frame.get("segments") or []),
             "diagnosis":diagnosis,"html_report":str(hp),
             "default_timeframe":default_key,
             "timeframes":[timeframe_summary(f) for f in timeframes]}
    jp.write_text(json.dumps(jsan(payload),ensure_ascii=False,indent=2),encoding="utf-8")
    hp.write_text(make_html(stock_code,src_label,timeframes,default_key),encoding="utf-8")

    print(json.dumps(jsan({"stock":stock_code,"source":default_frame.get("source", src_label),
          "latest_trade_date":diagnosis["latest"]["date"],
          "conclusion":diagnosis["current"]["label"],
          "confidence":diagnosis["current"]["confidence"],
          "score":diagnosis["current"]["score"],
          "pens":len(default_frame.get("pens") or []),"centers":len(default_frame.get("centers") or []),"segments":len(default_frame.get("segments") or []),
          "default_timeframe":default_key,
          "timeframes":[timeframe_summary(f) for f in timeframes],
          "json_report":str(jp),"html_report":str(hp)}),ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
