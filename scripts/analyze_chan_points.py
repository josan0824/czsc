#!/usr/bin/env python3
"""
缠论一二三类买卖点分析 + K线标注脚本
支持：日线/30分钟数据、K线包含处理、分型识别、内联SVG图表
"""
import argparse, csv, html, json, math, os, re, sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import warnings
warnings.filterwarnings("ignore", message=".*character detection.*")
REVERSE_GAP_THRESHOLD = 0.006
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
    preserve_reason: str = ""
    replaced_reason: str = ""
    note_reason: str = ""
    process_notes: List[str] = field(default_factory=list)

@dataclass
class Center:
    start_index: int; end_index: int; start_date: str; end_date: str
    zd: float; zg: float; gg: float; dd: float; direction: str

@dataclass
class MergedBar:
    high: float; low: float; open: float; close: float; date: str; vol: float; amount: float
    absorbed_dates: List[str] = None
    absorb_processes: List[str] = None
    raw_start: int = 0
    raw_end: int = 0

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
    p.add_argument("--chart-timeframe", default="auto", choices=["auto", "1m", "5m", "30m", "daily"],
                   help="HTML 默认展示级别；auto 按 1分钟、5分钟、30分钟、日线顺序自动降级")
    p.add_argument("--lookback", type=int, default=260)
    p.add_argument("--pivot-window", type=int, default=2)
    p.add_argument("--min-pivot-gap", type=int, default=3)
    p.add_argument("--min-swing-pct", type=float, default=0.0, help=argparse.SUPPRESS)
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
        items.append({"code":sec.get("Code",""),"name":sec.get("Name",""),"mkt":sec.get("MktNum",0),
                      "type":sec.get("SecurityTypeName",""),"quote_id":sec.get("QuoteID","")})
    return items

def is_index_query(query: str) -> bool:
    q = query.strip().upper().replace(" ", "")
    return any(x in q for x in ("上证", "沪指", "上证指数", "深证成指", "创业板指", "科创50", "指数"))

def normalize_index_code(code: str, market: str = "") -> str:
    raw = code.strip().upper().replace(" ", "")
    if raw in ("上证", "沪指", "上证指数", "000001", "SH000001", "1.000001"):
        return "SH000001"
    if raw in ("深证成指", "399001", "SZ399001", "0.399001"):
        return "SZ399001"
    if raw in ("创业板指", "399006", "SZ399006", "0.399006"):
        return "SZ399006"
    if raw in ("科创50", "000688", "SH000688", "1.000688"):
        return "SH000688"
    quote_m = re.fullmatch(r"([01])\.(\d{6})", raw)
    if quote_m:
        return f"{'SH' if quote_m.group(1) == '1' else 'SZ'}{quote_m.group(2)}"
    m = re.fullmatch(r"(SH|SZ)(\d{6})", raw)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    if re.fullmatch(r"\d{6}", raw):
        if market == "1" or raw.startswith("000"):
            return f"SH{raw}"
        return f"SZ{raw}"
    raise ValueError(f"无法解析指数代码: {code}")

def normalize_search_result_code(item: dict) -> str:
    code = str(item.get("code", "") or "").strip().upper()
    quote_id = str(item.get("quote_id", "") or "").strip().upper()
    market = str(item.get("mkt", "") or "").strip()
    if quote_id.startswith("116.") or market == "116" or re.fullmatch(r"\d{5}", code):
        raw = quote_id.split(".", 1)[1] if quote_id.startswith("116.") else code
        if re.fullmatch(r"\d{5}", raw):
            return f"{raw}.HK"
    return normalize_stock_code(code)

def is_index_code(code: str) -> bool:
    raw = str(code).strip().upper().replace(" ", "")
    return bool(re.fullmatch(r"(SH000\d{3}|SH000688|SZ399\d{3})", raw))

def code_to_secid(code: str) -> str:
    c = normalize_stock_code(code)
    raw = code.strip().upper().replace(" ", "")
    if is_index_code(raw):
        return f"{'1' if raw.startswith('SH') else '0'}.{raw[2:]}"
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
    if is_index_query(query):
        idx_code = normalize_index_code(query)
        return code_to_secid(idx_code), idx_code, query
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
    resolved_code = normalize_search_result_code(best)
    return code_to_secid(resolved_code), resolved_code.replace(".HK", ""), best.get("name","")

def lookup_stock_name(code: str, query: str = "") -> str:
    known = {
        "000001.SZ": "平安银行",
        "SH000001": "上证指数",
        "SZ399001": "深证成指",
        "SZ399006": "创业板指",
        "SH000688": "科创50",
        "399852": "中证1000",
        "000852": "中证1000",
    }
    if query and not re.fullmatch(r"[A-Za-z0-9.]+", query.strip()):
        if is_index_query(query) or re.search(r"[\u4e00-\u9fff]", query):
            return query.strip()
    normalized = normalize_stock_code(code)
    raw = str(code).strip().upper().replace(" ", "")
    for key in (raw, normalized, normalized.replace(".HK", ""), raw.replace(".", "")):
        if key in known:
            return known[key]
    try:
        search_code = normalized.replace(".HK", "") if normalized.endswith(".HK") else normalized[:6]
        results = eastsrch(search_code)
        for item in results:
            item_code = str(item.get("code", "")).upper()
            quote_id = str(item.get("quote_id", "")).upper()
            if item_code == search_code.upper() or item_code == normalized[:6] or quote_id.endswith(search_code.upper()):
                return str(item.get("name", "") or "")
    except Exception:
        pass
    return ""

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

def fetch_eastmoney_trends_1m(secid: str, limit: Optional[int] = None):
    if not HAS_REQUESTS: raise RuntimeError("需要 requests 库")
    url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ndays": "5",
        "iscr": "0",
        "iscca": "0",
    }
    resp = _session.get(url, params=params, headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"}, timeout=20)
    resp.raise_for_status()
    d = resp.json().get("data")
    if not d or not d.get("trends"):
        raise RuntimeError(f"东方财富分时无数据 (secid={secid})")
    code, name, trends = d.get("code", secid), d.get("name", ""), d["trends"]
    bars = []
    for line in trends:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        try:
            dt = re.sub(r"\D", "", parts[0])[:12]
            if len(dt) < 12:
                continue
            open_ = float(parts[1])
            close = float(parts[2])
            high = float(parts[3])
            low = float(parts[4])
            volume = float(parts[5] or 0)
            amount = float(parts[6] or 0)
            if open_ <= 0:
                open_ = close
            bars.append(Bar(code, dt, open_, high, low, close, volume, amount))
        except Exception:
            continue
    bars.sort(key=lambda x: x.trade_date)
    bars = [b for b in bars if b.close > 0]
    if len(bars) < 40:
        raise RuntimeError(f"东方财富分时不足40根 (实际{len(bars)}, secid={secid})")
    return code, name, bars

def tencent_symbol_from_code(code: str) -> str:
    raw = str(code).strip().upper().replace(" ", "")
    if raw.startswith("SH"):
        return "sh" + raw[2:]
    if raw.startswith("SZ"):
        return "sz" + raw[2:]
    normalized = normalize_stock_code(raw)
    if normalized.endswith(".SH"):
        return "sh" + normalized[:6]
    if normalized.endswith(".SZ"):
        return "sz" + normalized[:6]
    if normalized.endswith(".HK"):
        return "hk" + normalized[:5]
    if re.fullmatch(r"\d{6}", raw):
        return ("sh" if raw.startswith(("6", "9", "000")) else "sz") + raw
    if re.fullmatch(r"\d{5}", raw):
        return "hk" + raw
    raise ValueError(f"无法转为腾讯代码: {code}")

def fetch_tencent_daily_kline(code: str, limit: Optional[int] = None):
    if not HAS_REQUESTS: raise SystemExit("需要 requests 库")
    symbol = tencent_symbol_from_code(code)
    count = limit or 1000
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{symbol},day,,,{count},qfq"}
    resp = _session.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    item = (((payload.get("data") or {}).get(symbol)) or {})
    rows = item.get("qfqday") or item.get("day") or []
    if not rows:
        raise SystemExit(f"腾讯日线无数据 (symbol={symbol})")
    bars = []
    for row in rows:
        try:
            dt = str(row[0]).replace("-", "")[:8]
            if not re.fullmatch(r"\d{8}", dt):
                continue
            open_, close, high, low = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            bars.append(Bar(symbol, dt, open_, high, low, close, float(row[5] or 0), 0))
        except Exception:
            continue
    bars.sort(key=lambda x: x.trade_date)
    bars = [b for b in bars if b.close > 0]
    if len(bars) < 40:
        raise SystemExit(f"腾讯日线不足40根 (实际{len(bars)}, symbol={symbol})")
    names = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指", "sh000688": "科创50"}
    return symbol, names.get(symbol, item.get("qt", {}).get(symbol, [""])[1] if item.get("qt") else ""), bars

def fetch_tencent_hk_intraday_1m(code: str, limit: Optional[int] = None):
    norm = normalize_stock_code(code)
    if not norm.endswith(".HK"):
        raise RuntimeError("腾讯分时当前仅用于港股1分钟线")
    if not HAS_REQUESTS:
        raise RuntimeError("需要 requests 库")
    raw = norm.replace(".HK", "")
    symbol = f"hk{raw}"
    url = "https://ifzq.gtimg.cn/appstock/app/day/query"
    resp = _session.get(
        url,
        params={"code": symbol},
        headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://gu.qq.com/{symbol}/gp"},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    days = (((payload.get("data") or {}).get(symbol) or {}).get("data") or [])
    if not days:
        raise RuntimeError(f"腾讯分时无数据 (symbol={symbol})")
    rows = []
    for day in reversed(days):
        date = str(day.get("date", ""))
        prev_price = None
        prev_vol = 0.0
        prev_amount = 0.0
        for line in day.get("data") or []:
            parts = str(line).split()
            if len(parts) < 4:
                continue
            try:
                minute, price = parts[0], float(parts[1])
                cum_vol, cum_amount = float(parts[2]), float(parts[3])
                vol = max(0.0, cum_vol - prev_vol)
                amount = max(0.0, cum_amount - prev_amount)
                open_ = prev_price if prev_price and prev_price > 0 else price
                high = max(open_, price)
                low = min(open_, price)
                rows.append(Bar(raw, f"{date}{minute}", open_, high, low, price, vol, amount))
                prev_price, prev_vol, prev_amount = price, cum_vol, cum_amount
            except Exception:
                continue
    rows.sort(key=lambda x: x.trade_date)
    bars = [b for b in rows if b.close > 0]
    if limit is not None:
        bars = bars[-limit:]
    if len(bars) < 40:
        raise RuntimeError(f"腾讯分时不足40根 (实际{len(bars)}, symbol={symbol})")
    return raw, "腾讯控股", bars

def tdx_market_code(code: str) -> Tuple[int, str]:
    norm = normalize_stock_code(code)
    if norm.endswith(".HK"):
        raise RuntimeError("通达信不支持港股分钟线")
    raw = norm[:6] if norm.endswith((".SH", ".SZ")) else norm
    if norm.startswith("SH"):
        raw = norm[2:]
        market = 1
    elif norm.startswith("SZ"):
        raw = norm[2:]
        market = 0
    elif norm.endswith(".SH") or raw.startswith(("5", "6", "9")):
        market = 1
    else:
        market = 0
    if not re.fullmatch(r"\d{6}", raw):
        raise RuntimeError(f"通达信无法解析代码: {code}")
    return market, raw

def fetch_pytdx_1m_kline(code: str, days: int = 30):
    try:
        from pytdx.hq import TdxHq_API
    except ImportError as exc:
        raise RuntimeError("当前环境未安装 pytdx，无法使用通达信1分钟线") from exc

    market, raw = tdx_market_code(code)
    servers = [
        ("123.125.108.14", 7709),
        ("119.147.212.81", 7709),
        ("202.108.253.130", 7709),
        ("47.103.48.45", 7709),
        ("61.152.107.141", 7709),
        ("218.75.126.9", 7709),
        ("124.160.88.183", 7709),
        ("112.95.140.74", 7709),
        ("180.153.18.170", 7709),
        ("59.173.18.69", 7709),
        ("14.17.75.71", 7709),
    ]
    cutoff = datetime.now() - timedelta(days=days)
    best_rows = []
    errors = []
    for host, port in servers:
        api = TdxHq_API(heartbeat=True, auto_retry=False, raise_exception=False)
        try:
            if not api.connect(host, port, time_out=3):
                errors.append(f"{host}: 连接失败")
                continue
            rows = []
            for start in range(0, 20000, 800):
                if is_index_code(normalize_stock_code(code)):
                    chunk = api.get_index_bars(7, market, raw, start, 800) or []
                else:
                    chunk = api.get_security_bars(7, market, raw, start, 800) or []
                if not chunk:
                    break
                rows.extend(chunk)
                first_dt = datetime.strptime(chunk[0]["datetime"], "%Y-%m-%d %H:%M")
                if first_dt <= cutoff:
                    break
            if len(rows) > len(best_rows):
                best_rows = rows
            if rows:
                break
        except Exception as exc:
            errors.append(f"{host}: {exc}")
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
    if not best_rows:
        raise RuntimeError("通达信1分钟线不可用：" + "；".join(errors[:5]))
    dedup = {}
    for row in best_rows:
        try:
            dt_obj = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M")
            if dt_obj < cutoff or dt_obj > datetime.now() + timedelta(days=1):
                continue
            dt = dt_obj.strftime("%Y%m%d%H%M")
            dedup[dt] = Bar(
                raw,
                dt,
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row.get("vol") or 0),
                float(row.get("amount") or 0),
            )
        except Exception:
            continue
    bars = sorted(dedup.values(), key=lambda x: x.trade_date)
    bars = [b for b in bars if b.close > 0]
    if len(bars) < 40:
        raise RuntimeError(f"通达信1分钟线不足40根 (实际{len(bars)}, code={raw})")
    return raw, "", bars

def sina_symbol_from_code(code: str) -> str:
    raw = str(code).strip().upper().replace(" ", "")
    if raw.startswith("SH"):
        return "sh" + raw[2:]
    if raw.startswith("SZ"):
        return "sz" + raw[2:]
    if raw.startswith("1."):
        return "sh" + raw.split(".", 1)[1]
    if raw.startswith("0."):
        return "sz" + raw.split(".", 1)[1]
    if raw.endswith(".SH"):
        return "sh" + raw[:6]
    if raw.endswith(".SZ"):
        return "sz" + raw[:6]
    if re.fullmatch(r"\d{6}", raw):
        return ("sh" if raw.startswith(("6", "9")) else "sz") + raw
    raise ValueError(f"无法转为新浪代码: {code}")

def fetch_sina_intraday_kline(code: str, period: str, limit: Optional[int] = None):
    if not HAS_REQUESTS: raise RuntimeError("需要 requests 库")
    symbol = sina_symbol_from_code(code)
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": symbol, "scale": period, "ma": "no", "datalen": str(limit or 1000000)}
    resp = _session.get(url, params=params, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}, timeout=15)
    resp.raise_for_status()
    try:
        rows = resp.json()
    except Exception:
        rows = json.loads(resp.content.decode("gbk", errors="ignore"))
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"新浪分钟K线无数据 (symbol={symbol}, period={period})")
    bars = []
    for row in rows:
        try:
            dt = re.sub(r"\D", "", str(row.get("day", "")))[:12]
            if len(dt) < 12:
                continue
            open_ = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            volume = float(row.get("volume", 0) or 0)
            bars.append(Bar(symbol, dt, open_, high, low, close, volume, 0))
        except Exception:
            continue
    bars.sort(key=lambda x: x.trade_date)
    bars = [b for b in bars if b.close > 0]
    if len(bars) < 40:
        raise RuntimeError(f"新浪分钟K线不足40根 (实际{len(bars)}, symbol={symbol}, period={period})")
    return symbol, "", bars

def fetch_mootdx_intraday_kline(code: str, period: str, limit: Optional[int] = None):
    try:
        import mootdx  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("当前环境未安装 mootdx，无法使用通达信分钟线") from exc
    raise RuntimeError("mootdx 分钟线接口尚未接入可用服务器")

def fetch_baostock_intraday_kline(code: str, period: str, limit: Optional[int] = None):
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("当前环境未安装 baostock，无法使用 baostock 分钟线") from exc

    norm = normalize_stock_code(code)
    if norm.endswith(".HK"):
        raise RuntimeError("baostock 不支持港股分钟线")
    if norm.startswith("SH"):
        bs_code = "sh." + norm[2:]
    elif norm.startswith("SZ"):
        bs_code = "sz." + norm[2:]
    elif norm.endswith(".SH"):
        bs_code = "sh." + norm[:6]
    elif norm.endswith(".SZ"):
        bs_code = "sz." + norm[:6]
    elif re.fullmatch(r"\d{6}", norm):
        bs_code = ("sh." if norm.startswith(("6", "9")) else "sz.") + norm
    else:
        raise RuntimeError(f"baostock 无法解析代码: {code}")

    login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        raise RuntimeError(f"baostock 登录失败: {getattr(login, 'error_msg', '')}")
    bars = []
    try:
        end_date = datetime.now().strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,time,code,open,high,low,close,volume,amount",
            start_date="1990-01-01",
            end_date=end_date,
            frequency=period,
            adjustflag="2",
        )
        if getattr(rs, "error_code", "0") != "0":
            raise RuntimeError(f"baostock 查询失败: {getattr(rs, 'error_msg', '')}")
        while rs.next():
            row = dict(zip(rs.fields, rs.get_row_data()))
            try:
                raw_time = re.sub(r"\D", "", row.get("time") or row.get("date") or "")
                dt = raw_time[:12] if len(raw_time) >= 12 else re.sub(r"\D", "", row.get("date", ""))
                if len(dt) < 12:
                    continue
                bars.append(Bar(
                    bs_code,
                    dt,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row.get("volume") or 0),
                    float(row.get("amount") or 0),
                ))
            except Exception:
                continue
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    bars.sort(key=lambda x: x.trade_date)
    bars = [b for b in bars if b.close > 0]
    if len(bars) < 40:
        raise RuntimeError(f"baostock 分钟K线不足40根 (实际{len(bars)}, code={bs_code}, period={period})")
    return bs_code, "", bars

def fetch_hk_daily_akshare(code: str, limit: int = 500):
    hk_code = normalize_stock_code(code).replace(".HK","")
    if not re.fullmatch(r"\d{5}", hk_code):
        raise ValueError(f"不是可识别的港股代码: {code}")
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("当前环境未安装 akshare，跳过 AKShare 港股日线源") from exc
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
        raise RuntimeError("当前环境未安装 akshare，跳过 AKShare 中证指数日线源") from exc
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

def fetch_index_daily_akshare(index_code: str, limit: Optional[int] = None):
    code = normalize_index_code(index_code)
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("当前环境未安装 akshare，跳过 AKShare 指数日线源") from exc
    symbol = code.lower()
    df = ak.stock_zh_index_daily(symbol=symbol)
    if df is None or len(df) == 0:
        raise SystemExit(f"AKShare 未返回指数日线数据 ({symbol})")
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
            bars.append(Bar(code, dt, open_, high, low, close,
                           float(row.get("volume", 0) or 0), float(row.get("amount", 0) or 0)))
        except Exception:
            continue
    bars.sort(key=lambda x: x.trade_date)
    bars = [b for b in bars if b.close > 0]
    if len(bars) < 40:
        raise SystemExit(f"AKShare 指数数据不足40根K线 (实际{len(bars)})")
    names = {"SH000001": "上证指数", "SZ399001": "深证成指", "SZ399006": "创业板指", "SH000688": "科创50"}
    return code, names.get(code, ""), bars

def fetch_a_daily_akshare(code: str, limit: Optional[int] = None):
    normalized = normalize_stock_code(code)
    m = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", normalized)
    if not m:
        raise ValueError(f"不是可识别的 A 股代码: {code}")
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("当前环境未安装 akshare，跳过 AKShare A 股日线源") from exc
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
        if is_index_query(query):
            secid = code_to_secid(normalize_index_code(query))
        else:
            secid = code_to_secid(normalized)
        code, name, bars = fetch_kline(secid, klt, limit)
        for b in bars: b.ts_code = f"{code} ({name})" if name else code
        return code, name, bars, "东方财富"

    try:
        if is_index_query(query):
            resolved_code = normalize_index_code(query)
            resolved_name = ""
        else:
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
            if best.get("type") == "指数" or (best.get("quote_id") and not str(best.get("quote_id")).startswith("116.")):
                resolved_code = normalize_index_code(best.get("quote_id") or best["code"], str(best.get("mkt","")))
            else:
                resolved_code = normalize_search_result_code(best)
            resolved_name = best.get("name","")

    attempts = []
    if is_index_code(resolved_code):
        attempts.extend([
            ("AKShare-新浪财经指数", lambda: fetch_index_daily_akshare(resolved_code, limit)),
            ("东方财富", lambda: fetch_kline(code_to_secid(resolved_code), klt, limit)),
            ("腾讯日线", lambda: fetch_tencent_daily_kline(resolved_code, limit)),
        ])
    elif normalize_stock_code(resolved_code).endswith((".SH", ".SZ", ".BJ")):
        attempts.extend([
            ("AKShare-新浪财经", lambda: fetch_a_daily_akshare(resolved_code, limit)),
            ("东方财富", lambda: fetch_kline(code_to_secid(resolved_code), klt, limit)),
            ("腾讯日线", lambda: fetch_tencent_daily_kline(resolved_code, limit)),
            ("通达信", lambda: fetch_a_daily_tdx(resolved_code, limit)),
            ("同花顺", lambda: fetch_a_daily_ths(resolved_code, limit)),
        ])
    elif normalize_stock_code(resolved_code).endswith(".HK"):
        attempts.extend([
            ("AKShare-港股", lambda: fetch_hk_daily_akshare(resolved_code, limit)),
            ("东方财富", lambda: fetch_kline(code_to_secid(resolved_code), klt, limit)),
            ("腾讯日线", lambda: fetch_tencent_daily_kline(resolved_code, limit)),
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
            name = name or resolved_name or lookup_stock_name(code, query)
            for b in bars: b.ts_code = f"{code} ({name})" if name else code
            return code, name, bars, provider
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
    raise SystemExit("所有网络行情源均不可用：" + "；".join(errors))

def fetch_stock_from_web_eastmoney_first_legacy(stock_query: str, klt: str = "101", limit: int = 500):
    """保留旧的东方财富优先逻辑用于对照；主流程不再调用。"""
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
    p = [MergedBar(bars[0].high,bars[0].low,bars[0].open,bars[0].close,bars[0].trade_date,bars[0].vol,bars[0].amount,[],[],0,0)]
    i = 1
    while i < len(bars):
        mb = MergedBar(bars[i].high,bars[i].low,bars[i].open,bars[i].close,bars[i].trade_date,bars[i].vol,bars[i].amount,[],[],i,i)
        last = p[-1]
        if (mb.high <= last.high and mb.low >= last.low) or (last.high <= mb.high and last.low >= mb.low):
            up = len(p) < 2 or last.high >= p[-2].high
            absorbed = list(last.absorbed_dates or []) + [last.date]
            processes = list(last.absorb_processes or [])
            process = "向上处理：取高高（最高取高、最低取高）" if up else "向下处理：取低低（最高取低、最低取低）"
            processes.append(f"{process}（处理{fmt_date(last.date)}）")
            raw_start, raw_end = min(last.raw_start, mb.raw_start), max(last.raw_end, mb.raw_end)
            if up: p[-1] = MergedBar(max(last.high,mb.high),max(last.low,mb.low),last.open,mb.close,mb.date,last.vol+mb.vol,last.amount+mb.amount,absorbed,processes,raw_start,raw_end)
            else: p[-1] = MergedBar(min(last.high,mb.high),min(last.low,mb.low),last.open,mb.close,mb.date,last.vol+mb.vol,last.amount+mb.amount,absorbed,processes,raw_start,raw_end)
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

def has_down_gap(prev: Bar, curr: Bar, threshold: float = REVERSE_GAP_THRESHOLD) -> bool:
    return prev.low > 0 and prev.low > curr.high and (prev.low - curr.high) / prev.low > threshold

def has_up_gap(prev: Bar, curr: Bar, threshold: float = REVERSE_GAP_THRESHOLD) -> bool:
    return prev.high > 0 and curr.low > prev.high and (curr.low - prev.high) / prev.high > threshold

def raw_gap_ranges_for_merged_window(merged: List[MergedBar], start: int, end: int) -> Tuple[int, int]:
    start = max(0, min(start, len(merged) - 1))
    end = max(0, min(end, len(merged) - 1))
    if start > end:
        start, end = end, start
    return merged[start].raw_start, merged[end].raw_end

def reverse_gap_preserve_reason(p: Pivot, merged: Optional[List[MergedBar]], raw_bars: Optional[List[Bar]] = None) -> str:
    if not merged or not raw_bars or p.index + 1 >= len(merged):
        return ""
    raw_start, raw_end = raw_gap_ranges_for_merged_window(merged, p.index, min(len(merged) - 1, p.index + 2))
    for i in range(raw_start, min(raw_end, len(raw_bars) - 1)):
        curr = raw_bars[i]
        nxt = raw_bars[i + 1]
        if p.kind == "top" and has_down_gap(curr, nxt):
            pct = (curr.low - nxt.high) / curr.low * 100
            return (
                f"反向缺口保护：顶分型后原始K线{fmt_date(curr.trade_date)}→{fmt_date(nxt.trade_date)}"
                f"向下缺口{pct:.2f}%"
            )
        if p.kind == "bottom" and has_up_gap(curr, nxt):
            pct = (nxt.low - curr.high) / curr.high * 100
            return (
                f"反向缺口保护：底分型后原始K线{fmt_date(curr.trade_date)}→{fmt_date(nxt.trade_date)}"
                f"向上缺口{pct:.2f}%"
            )
    return ""

def reverse_gap_from_previous_reason(prev_pivot: Pivot, p: Pivot, merged: Optional[List[MergedBar]], raw_bars: Optional[List[Bar]] = None) -> str:
    if not merged or not raw_bars:
        return ""
    start = max(0, p.index - 1)
    end = min(len(merged) - 1, p.index + 1)
    raw_start, raw_end = raw_gap_ranges_for_merged_window(merged, start, end)
    for i in range(raw_start, min(raw_end, len(raw_bars) - 1)):
        curr = raw_bars[i]
        nxt = raw_bars[i + 1]
        if prev_pivot.kind == "top" and has_down_gap(curr, nxt):
            pct = (curr.low - nxt.high) / curr.low * 100
            return (
                f"反向缺口保护：前一顶分型{fmt_date(prev_pivot.date)}后，"
                f"原始K线{fmt_date(curr.trade_date)}→{fmt_date(nxt.trade_date)}向下缺口{pct:.2f}%"
            )
        if prev_pivot.kind == "bottom" and has_up_gap(curr, nxt):
            pct = (nxt.low - curr.high) / curr.high * 100
            return (
                f"反向缺口保护：前一底分型{fmt_date(prev_pivot.date)}后，"
                f"原始K线{fmt_date(curr.trade_date)}→{fmt_date(nxt.trade_date)}向上缺口{pct:.2f}%"
            )
    return ""

def find_fractals(merged: List[MergedBar], raw_bars: Optional[List[Bar]] = None) -> List[Pivot]:
    records = filter_fractals_by_occupied_bars(find_fractal_candidates(merged), merged, raw_bars)
    return [p for p in records if p.valid]

def has_directional_gap_between(start: Pivot, end: Pivot, merged: Optional[List[MergedBar]], raw_bars: Optional[List[Bar]] = None) -> str:
    if not merged or not raw_bars:
        return ""
    left = max(0, min(start.index, end.index))
    right = min(len(merged) - 1, max(start.index, end.index))
    raw_start, raw_end = raw_gap_ranges_for_merged_window(merged, left, right)
    for i in range(raw_start, min(raw_end, len(raw_bars) - 1)):
        curr = raw_bars[i]
        nxt = raw_bars[i + 1]
        if start.kind == "top" and end.kind == "bottom" and has_down_gap(curr, nxt):
            pct = (curr.low - nxt.high) / curr.low * 100
            return f"向下缺口: 原始K线{fmt_date(curr.trade_date)}→{fmt_date(nxt.trade_date)} {pct:.2f}%"
        if start.kind == "bottom" and end.kind == "top" and has_up_gap(curr, nxt):
            pct = (nxt.low - curr.high) / curr.high * 100
            return f"向上缺口: 原始K线{fmt_date(curr.trade_date)}→{fmt_date(nxt.trade_date)} {pct:.2f}%"
    return ""

def filter_fractals_by_occupied_bars(candidates: List[Pivot], merged: Optional[List[MergedBar]] = None, raw_bars: Optional[List[Bar]] = None) -> List[Pivot]:
    """确定分型序列。

    - 每个分型占用3根K线，两个分型之间至少隔1根独立K线。
    - 同类连续分型只保留更极端者：顶取更高，底取更低。
    - 底后顶若价格区间重叠，则当前顶分型需与上一个顶分型比较：当前更高则舍弃上一个顶分型，
      并在上一个有效底分型和上上个有效底分型中舍弃低点更高者，否则舍弃当前顶分型。
    - 顶后底若价格区间重叠，则当前底分型需与上一个底分型比较：当前更低则舍弃上一个底分型，
      并在上一个有效顶分型和上上个有效顶分型中舍弃高点更低者，否则舍弃当前底分型。
    - 若待过滤分型包含反向缺口，则保留该分型。
    """
    pivots = []

    def add_process(pivot: Pivot, note: str):
        if note:
            pivot.process_notes.append(note)

    def previous_same_kind(kind: str) -> Optional[Pivot]:
        for item in reversed(pivots[:-1]):
            if item.kind == kind:
                return item
        return None

    def previous_two_of_kind(kind: str) -> List[Pivot]:
        items = []
        for item in reversed(pivots):
            if item.kind == kind:
                items.append(item)
                if len(items) == 2:
                    break
        return items

    def more_extreme_pivot(a: Pivot, b: Pivot) -> Pivot:
        if a.kind == "bottom":
            return a if a.price < b.price else b
        return a if a.price > b.price else b

    def weaker_pivot(a: Pivot, b: Pivot) -> Pivot:
        if a.kind == "bottom":
            return a if a.low > b.low else b
        return a if a.high < b.high else b

    def remove_pivot(item: Pivot):
        if item in pivots:
            idx = pivots.index(item)
            pivots.pop(idx)
            return idx
        return None

    def mark_same_kind_merged(discarded: Pivot, kept: Pivot):
        discarded_note = (
            f"相邻顶分型{fmt_date(kept.date)}更高，按同类极值归并舍弃本顶分型"
            if discarded.kind == "top"
            else f"相邻底分型{fmt_date(kept.date)}更低，按同类极值归并舍弃本底分型"
        )
        kept_note = (
            f"与相邻顶分型{fmt_date(discarded.date)}同类归并后保留：本顶分型更高"
            if kept.kind == "top"
            else f"与相邻底分型{fmt_date(discarded.date)}同类归并后保留：本底分型更低"
        )
        discarded.valid = False
        discarded.filter_reason = ""
        discarded.preserve_reason = ""
        discarded.replaced_reason = discarded_note
        kept.note_reason = kept_note
        add_process(discarded, discarded_note)
        add_process(kept, kept_note)

    def normalize_same_kind_neighbors(anchor: Optional[Pivot]) -> Optional[Pivot]:
        """向左归并相邻同类分型，顶取高、底取低。"""
        if anchor is None:
            return None
        while anchor in pivots:
            idx = pivots.index(anchor)
            if idx <= 0:
                break
            left = pivots[idx - 1]
            if left.kind != anchor.kind:
                break
            kept = more_extreme_pivot(left, anchor)
            discarded = anchor if kept is left else left
            mark_same_kind_merged(discarded, kept)
            remove_pivot(discarded)
            anchor = kept
        return anchor

    def replace_with_overlap_extreme(p: Pivot, last: Pivot):
        opposite_kind = last.kind
        opposite_candidates = previous_two_of_kind(opposite_kind)
        opposite_to_remove = weaker_pivot(opposite_candidates[0], opposite_candidates[1]) if len(opposite_candidates) >= 2 else last
        overlap_note = (
            f"与前一有效底分型{fmt_date(last.date)}区间重叠；当前顶分型高于上一个顶分型，"
            f"先舍弃较弱底分型{fmt_date(opposite_to_remove.date)}，再按同类极值归并后暂时保留"
            if p.kind == "top"
            else f"与前一有效顶分型{fmt_date(last.date)}区间重叠；当前底分型低于上一个底分型，"
            f"先舍弃较弱顶分型{fmt_date(opposite_to_remove.date)}，再按同类极值归并后暂时保留"
        )
        remove_note = (
            f"后续顶分型{fmt_date(p.date)}触发重合替换；本底分型是两个相邻底分型中低点较高者，按较弱反向分型舍弃"
            if opposite_kind == "bottom"
            else f"后续底分型{fmt_date(p.date)}触发重合替换；本顶分型是两个相邻顶分型中高点较低者，按较弱反向分型舍弃"
        )
        opposite_to_remove.valid = False
        opposite_to_remove.filter_reason = ""
        opposite_to_remove.preserve_reason = ""
        opposite_to_remove.replaced_reason = remove_note
        add_process(opposite_to_remove, remove_note)
        p.preserve_reason = ""
        p.note_reason = overlap_note
        add_process(p, overlap_note)
        removed_idx = remove_pivot(opposite_to_remove)
        if removed_idx is not None:
            anchor = pivots[removed_idx] if removed_idx < len(pivots) else (pivots[-1] if pivots else None)
            normalize_same_kind_neighbors(anchor)
        pivots.append(p)
        normalize_same_kind_neighbors(p)

    for p in candidates:
        p.valid = True
        p.filter_reason = ""
        p.replaced_reason = ""
        p.note_reason = ""
        p.process_notes = []
        add_process(p, f"识别到{'顶分型' if p.kind == 'top' else '底分型'}形态：分型价格{p.price:.2f}，高点{p.high:.2f}，低点{p.low:.2f}")
        p.preserve_reason = reverse_gap_preserve_reason(p, merged, raw_bars)
        if not pivots:
            add_process(p, "作为首个有效分型保留")
            pivots.append(p)
            continue
        last = pivots[-1]
        if not p.preserve_reason:
            p.preserve_reason = reverse_gap_from_previous_reason(last, p, merged, raw_bars)
        if p.preserve_reason:
            add_process(p, p.preserve_reason)
        if p.kind == last.kind:
            better_top = p.kind == "top" and p.price > last.price
            better_bottom = p.kind == "bottom" and p.price < last.price
            if better_top or better_bottom:
                last_note = (
                    f"同类分型，后续顶分型更高，保留{fmt_date(p.date)}"
                    if p.kind == "top"
                    else f"同类分型，后续底分型更低，保留{fmt_date(p.date)}"
                )
                keep_note = (
                    f"同类顶分型替换{fmt_date(last.date)}后保留：本顶分型更高"
                    if p.kind == "top"
                    else f"同类底分型替换{fmt_date(last.date)}后保留：本底分型更低"
                )
                last.valid = False
                last.filter_reason = last_note
                p.note_reason = keep_note
                add_process(last, last_note)
                add_process(p, keep_note)
                p.preserve_reason = ""
                pivots[-1] = p
            else:
                filter_note = (
                    f"同类分型，顶分型未高于已保留顶分型{fmt_date(last.date)}"
                    if p.kind == "top"
                    else f"同类分型，底分型未低于已保留底分型{fmt_date(last.date)}"
                )
                p.valid = False
                p.filter_reason = filter_note
                add_process(p, filter_note)
                p.preserve_reason = ""
            continue

        def handle_overlap(reason_prefix: str) -> bool:
            previous_same = previous_same_kind(p.kind)
            if previous_same is None:
                if p.preserve_reason:
                    add_process(p, f"{reason_prefix}；因反向缺口保护保留")
                    pivots.append(p)
                else:
                    filter_note = f"{reason_prefix}，且无上一个同类分型可比较"
                    p.valid = False
                    p.filter_reason = filter_note
                    add_process(p, filter_note)
                    p.preserve_reason = ""
                return True
            better_top = p.kind == "top" and p.price > previous_same.price
            better_bottom = p.kind == "bottom" and p.price < previous_same.price
            if better_top or better_bottom:
                replace_with_overlap_extreme(p, last)
            else:
                filter_note = (
                    f"{reason_prefix}；当前顶分型未高于上一个顶分型{fmt_date(previous_same.date)}，舍弃当前顶分型"
                    if p.kind == "top"
                    else f"{reason_prefix}；当前底分型未低于上一个底分型{fmt_date(previous_same.date)}，舍弃当前底分型"
                )
                p.valid = False
                p.filter_reason = filter_note
                add_process(p, filter_note)
                p.preserve_reason = ""
            return True

        def filter_invalid_spacing(reason_prefix: str) -> bool:
            if p.preserve_reason:
                add_process(p, f"{reason_prefix}；因反向缺口保护保留")
                pivots.append(p)
            else:
                filter_note = f"{reason_prefix}；不满足分型间隔要求，舍弃当前分型"
                p.valid = False
                p.filter_reason = filter_note
                add_process(p, filter_note)
                p.preserve_reason = ""
            return True

        last_end = last.index + 1
        curr_start = p.index - 1
        if curr_start < last_end + 2:
            filter_invalid_spacing(f"分型占用区间与前一有效分型{fmt_date(last.date)}重叠，或中间不足1根独立K线")
            continue

        if last.kind == "bottom" and p.kind == "top":
            if p.low > last.high:
                add_process(p, f"顶分型最低{p.low:.2f}高于前一底分型{fmt_date(last.date)}最高{last.high:.2f}，区间不重叠，保留")
                pivots.append(p)
            else:
                handle_overlap(f"顶分型最低{p.low:.2f}未高于前一底分型{fmt_date(last.date)}最高{last.high:.2f}，区间重叠")
            continue
        if last.kind == "top" and p.kind == "bottom":
            if p.high < last.low:
                add_process(p, f"底分型最高{p.high:.2f}低于前一顶分型{fmt_date(last.date)}最低{last.low:.2f}，区间不重叠，保留")
                pivots.append(p)
            else:
                handle_overlap(f"底分型最高{p.high:.2f}未低于前一顶分型{fmt_date(last.date)}最低{last.low:.2f}，区间重叠")
            continue
    return candidates


# ───────── 笔构造 ─────────

def build_pens(fractals: List[Pivot], min_gap=2, min_swing_pct=0.0, return_details=False, merged: Optional[List[MergedBar]] = None, raw_bars: Optional[List[Bar]] = None):
    """构造笔，规则：
    - 顶底交替，连续同向取极值
    - 反向分型须间隔 min_gap 根K线
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
                if return_details and len(pens) >= 2:
                    prev = pens[-2]
                    details.append(PenStep(seq, prev.index, prev.kind, prev.price, prev.high, prev.low,
                                          p.index, p.kind, p.price, p.high, p.low,
                                          p.index - prev.index, abs((p.price-prev.price)/prev.price*100) if prev.price else 0,
                                          f"替换: 同类分型取极值，{last.kind}({last.price:.2f})→{p.kind}({p.price:.2f})", True))
                    seq += 1
                pens[-1] = p
                replaced = True
            if not replaced and return_details and len(pens) >= 2:
                prev = pens[-2]
                reason = (
                    f"跳过(同类分型): 顶分型{p.price:.2f}未高于已保留顶分型{last.price:.2f}"
                    if p.kind == "top"
                    else f"跳过(同类分型): 底分型{p.price:.2f}未低于已保留底分型{last.price:.2f}"
                )
                details.append(PenStep(seq, prev.index, prev.kind, prev.price, prev.high, prev.low,
                                      p.index, p.kind, p.price, p.high, p.low,
                                      p.index - prev.index, abs((p.price-prev.price)/prev.price*100) if prev.price else 0,
                                      reason, False))
                seq += 1
            continue
        gap = p.index - last.index
        move = abs((p.price - last.price) / last.price * 100) if last.price else 0
        gap_reason = has_directional_gap_between(last, p, merged, raw_bars)
        if gap < min_gap and not gap_reason:
            if return_details:
                details.append(PenStep(seq, last.index, last.kind, last.price, last.high, last.low,
                                      p.index, p.kind, p.price, p.high, p.low,
                                      gap, move, f"跳过(间隔不足): gap={gap}<{min_gap}", False))
                seq += 1
            continue
        if return_details:
            details.append(PenStep(seq, last.index, last.kind, last.price, last.high, last.low,
                                  p.index, p.kind, p.price, p.high, p.low,
                                  gap, move, "成笔: 顶底交替且间隔通过", True))
            seq += 1
        pens.append(p)
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
    s = str(v or "")
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 12:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}"
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return s

def fmt_cn_datetime(v):
    s = str(v or "")
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 12:
        return f"{digits[:4]}年{int(digits[4:6])}月{int(digits[6:8])}日 {digits[8:10]}:{digits[10:12]}"
    if len(digits) == 8:
        return f"{digits[:4]}年{int(digits[4:6])}月{int(digits[6:])}日"
    return s

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
    raw=find_fractals(merged, bars)
    pens,details=build_pens(raw,args.min_pivot_gap,args.min_swing_pct,return_details=True,merged=merged,raw_bars=bars)
    segs=find_segments(pens,merged)
    centers=find_centers(pens)
    diag=diagnose(bars,pens,centers,args.lookback)
    fractal_records=filter_fractals_by_occupied_bars(find_fractal_candidates(merged), merged, bars)
    return raw,pens,centers,segs,diag,details,fractal_records


# ───────── 内联SVG K线图（viewBox 缩放/拖拽） ─────────

def _make_svg_chart(stock_code, bars, pens, raw_fractals, centers, segments, timeframe_label="日线", chart_id="main", merged_bars=None):
    """用 SVG viewBox 实现可缩放/拖拽的 K 线分型标注图。

    当前 HTML 报告聚焦 K 线包含处理、原始分型和笔验证，所以图中绘制：
    - K 线
    - 顶分型标记：蓝色 ▼
    - 底分型标记：橙色 ▲
    - 笔：连接相邻有效分型端点的折线
    """
    W, H = 960, 520  # viewBox 初始宽高
    LM, TM, BM, RM = 55, 8, 28, 15  # 边距
    BW = 8  # 每根K线宽度（px）
    cid = re.sub(r"[^A-Za-z0-9_-]+", "_", chart_id)
    pb = bars
    merged_bars = merged_bars or []
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
    day_map = {}
    for i, b in enumerate(pb):
        x = LM + i * BW + BW / 2
        dt_map[b.trade_date] = x
        day_map[b.trade_date[:8]] = x
    # 获取显示范围内的最早和最晚日期
    first_date = pb[0].trade_date[:8]
    last_date = pb[-1].trade_date[:8]
    def xd(d):
        if d in dt_map:
            return dt_map[d]
        # 模糊匹配（仅比较 YYYYMMDD 部分）
        d8 = str(d)[:8]
        if d8 in day_map:
            return day_map[d8]
        return LM

    def visible_date(d):
        d8 = str(d)[:8]
        return first_date <= d8 <= last_date and d8 in day_map

    # 过滤：只保留在显示日期范围内的原始分型
    visible_raw = [(row_num, p) for row_num, p in enumerate(raw_fractals, 1) if first_date <= p.date[:8] <= last_date]

    # 内置 K 线数据为 JSON（用于 JS 交互）
    bar_data = []
    bar_order = {}
    for i, b in enumerate(pb):
        dt = b.trade_date
        bar_order[dt] = i
        ds = fmt_cn_datetime(dt)
        bar_data.append({"i": i, "dt": dt, "display_dt": ds, "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.vol, "x": round(LM + i * BW + BW / 2, 1)})
    bar_json = json.dumps(bar_data, ensure_ascii=False)
    fractal_data = []
    for row_num, p in visible_raw:
        highlight_dates = []
        for mi in range(max(0, p.index - 1), min(len(merged_bars), p.index + 2)):
            mb = merged_bars[mi]
            highlight_dates.extend(mb.absorbed_dates or [])
            highlight_dates.append(mb.date)
        highlight_dates = sorted(set(highlight_dates), key=lambda d: bar_order.get(d, 10**9))
        fractal_data.append({
            "row": row_num,
            "date": p.date,
            "kind": p.kind,
            "valid": bool(getattr(p, "valid", True)),
            "dates": highlight_dates,
        })
    fractal_json = json.dumps(fractal_data, ensure_ascii=False)
    pen_data = []

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

    # 被包含处理掉的原始 K 线：保留原 K 线显示，并叠加浅红虚线框用于校对包含关系。
    absorbed_date_set = set()
    for mb in merged_bars:
        absorbed_date_set.update(mb.absorbed_dates or [])
    for i, b in enumerate(pb):
        if b.trade_date not in absorbed_date_set:
            continue
        x = LM + i * BW - 1
        y = yp(b.high) - 1
        h = max(3, yp(b.low) - yp(b.high) + 2)
        svg += (
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{BW + 1}" height="{h:.1f}" '
            f'fill="none" stroke="#d92d20" stroke-width="1.2" stroke-dasharray="3 2" opacity="0.85" rx="1"/>'
        )

    # 笔：相邻有效分型端点连线
    visible_pen_pairs = []
    for a, b in zip(pens, pens[1:]):
        if visible_date(a.date) and visible_date(b.date):
            visible_pen_pairs.append((a, b))
    for pen_idx, (a, b) in enumerate(visible_pen_pairs):
        x1, y1 = xd(a.date), yp(a.price)
        x2, y2 = xd(b.date), yp(b.price)
        direction = "up" if b.price >= a.price else "down"
        stroke = "#12b76a" if direction == "up" else "#d92d20"
        pen_data.append({
            "i": pen_idx,
            "start": a.date,
            "end": b.date,
            "x1": round(x1, 1),
            "y1": round(y1, 1),
            "x2": round(x2, 1),
            "y2": round(y2, 1),
            "direction": direction,
        })
        svg += (
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="1.8" opacity="0.82" stroke-linecap="round"/>'
        )
    pen_json = json.dumps(pen_data, ensure_ascii=False)

    # 所有候选分型标注：有效分型用实心三角，过滤分型用虚线空心三角，便于校对过滤规则。
    for row_num, p in visible_raw:
        x = xd(p.date)
        if x < LM:
            continue
        valid = getattr(p, "valid", True)
        marker_attrs = f'class="chart-fractal-marker" data-fractal-row="{row_num}" style="cursor:pointer;"'
        if p.kind == "top":
            if valid:
                svg += f'<polygon {marker_attrs} points="{x:.1f},{yp(p.price):.1f} {x - 4:.1f},{yp(p.price) - 8:.1f} {x + 4:.1f},{yp(p.price) - 8:.1f}" fill="#1f6f8b"/>'
            else:
                svg += f'<polygon {marker_attrs} points="{x:.1f},{yp(p.price):.1f} {x - 4:.1f},{yp(p.price) - 8:.1f} {x + 4:.1f},{yp(p.price) - 8:.1f}" fill="none" stroke="#1f6f8b" stroke-width="1.3" stroke-dasharray="2 2"/>'
            svg += f'<text {marker_attrs} x="{x:.1f}" y="{yp(p.price) - 10:.1f}" text-anchor="middle" fill="{"#444" if valid else "#9aa4b2"}" font-size="9" dominant-baseline="bottom">{p.price:.2f}</text>'
        else:
            if valid:
                svg += f'<polygon {marker_attrs} points="{x:.1f},{yp(p.price):.1f} {x - 4:.1f},{yp(p.price) + 8:.1f} {x + 4:.1f},{yp(p.price) + 8:.1f}" fill="#f79009"/>'
            else:
                svg += f'<polygon {marker_attrs} points="{x:.1f},{yp(p.price):.1f} {x - 4:.1f},{yp(p.price) + 8:.1f} {x + 4:.1f},{yp(p.price) + 8:.1f}" fill="none" stroke="#f79009" stroke-width="1.3" stroke-dasharray="2 2"/>'
            svg += f'<text {marker_attrs} x="{x:.1f}" y="{yp(p.price) + 18:.1f}" text-anchor="middle" fill="{"#444" if valid else "#9aa4b2"}" font-size="9" dominant-baseline="top">{p.price:.2f}</text>'

    svg += f'<line id="chart-selected-line-{cid}" x1="{LM}" y1="{TM}" x2="{LM}" y2="{H - BM}" stroke="#1f6f8b" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.95" style="display:none;pointer-events:none;"/>'
    svg += f'<line id="chart-selected-pen-{cid}" x1="{LM}" y1="{TM}" x2="{LM}" y2="{H - BM}" stroke="#7a5af8" stroke-width="4" opacity="0.95" stroke-linecap="round" style="display:none;pointer-events:none;"/>'
    svg += f'<g id="chart-fractal-highlight-{cid}" style="pointer-events:none;"></g>'
    svg += (
        f'<g id="chart-crosshair-{cid}" style="display:none;pointer-events:none;">'
        f'<line id="crosshair-v-{cid}" x1="{LM}" y1="{TM}" x2="{LM}" y2="{H - BM}" stroke="#475467" stroke-width="1" stroke-dasharray="3 3" opacity="0.9"/>'
        f'<line id="crosshair-h-{cid}" x1="{LM}" y1="{TM}" x2="{total_width - RM}" y2="{TM}" stroke="#475467" stroke-width="1" stroke-dasharray="3 3" opacity="0.9"/>'
        f'<rect id="crosshair-price-bg-{cid}" x="{LM}" y="{TM - 9}" width="44" height="18" rx="3" fill="#344054" opacity="0.96"/>'
        f'<text id="crosshair-price-text-{cid}" x="{LM + 22}" y="{TM}" text-anchor="middle" fill="#fff" font-size="10" dominant-baseline="middle">-</text>'
        f'</g>'
    )
    svg += '</g></svg>'

    # ── 交互 HTML（viewBox 缩放/拖拽） ──
    interactive_html = f'''
<div class="chan-chart-shell">
  <div class="chart-toolbar">
    <button id="zoom-in-btn-{cid}" title="\u653e\u5927" type="button">+</button>
    <span id="zoom-label-{cid}">1\u00d7</span>
    <button id="zoom-out-btn-{cid}" title="\u7f29\u5c0f" type="button">-</button>
    <button id="reset-zoom-btn-{cid}" title="\u91cd\u7f6e\u89c6\u56fe" type="button">\u91cd\u7f6e</button>
    <button id="clear-fractal-highlight-btn-{cid}" title="\u6e05\u7406\u865a\u7ebf\u6846" type="button">\u6e05\u7406</button>
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
  pens: {pen_json},
  fractals: {fractal_json},
  totalBars: {total_bars}
}};

var svg = document.getElementById('chan-chart-svg-{cid}');
var wrap = document.getElementById('chc-{cid}');
var tip = document.getElementById('chtooltip-{cid}');
var selectedLine = document.getElementById('chart-selected-line-{cid}');
var selectedPen = document.getElementById('chart-selected-pen-{cid}');
var fractalHighlight = document.getElementById('chart-fractal-highlight-{cid}');
var crosshair = document.getElementById('chart-crosshair-{cid}');
var crosshairV = document.getElementById('crosshair-v-{cid}');
var crosshairH = document.getElementById('crosshair-h-{cid}');
var crosshairPriceBg = document.getElementById('crosshair-price-bg-{cid}');
var crosshairPriceText = document.getElementById('crosshair-price-text-{cid}');

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
var crosshairState = null;
var crosshairEnabled = false;

function digitsOnly(value) {{
  return String(value || '').replace(/[^0-9]/g, '');
}}

function normalizeKey(value) {{
  var digits = digitsOnly(value);
  return digits.length >= 12 ? digits.slice(0, 12) : digits.slice(0, 8);
}}

for (var di = 0; di < chartData.bars.length; di++) {{
  var fullKey = normalizeKey(chartData.bars[di].dt);
  var dayKey = digitsOnly(chartData.bars[di].dt).slice(0, 8);
  dateToBar[fullKey] = di;
  if (dayKey && dateToBar[dayKey] === undefined) dateToBar[dayKey] = di;
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
  updateCrosshairLabel();
  updateZoomLabel();
}}

function priceAtY(y) {{
  return {yh} - ((y - TM) / ({H} - TM - BM)) * ({yr});
}}

function yAtPrice(price) {{
  return TM + (CH - TM - BM) - (price - {yl}) / ({yr}) * (CH - TM - BM);
}}

function svgPointFromEvent(e) {{
  var rect = svg.getBoundingClientRect();
  var x = (e.clientX - rect.left) / (rect.width || 1) * viewW + originX;
  var y = (e.clientY - rect.top) / (rect.height || 1) * viewH + originY;
  return {{ x: Math.max(LM, Math.min(totalWidth - RM, x)), y: Math.max(TM, Math.min(CH - BM, y)) }};
}}

function updateCrosshairLabel() {{
  if (!crosshairState || !crosshair || crosshair.style.display === 'none') return;
  var x = crosshairState.x;
  var y = crosshairState.y;
  var price = priceAtY(y);
  var labelW = Math.max(48, Math.min(88, String(price.toFixed(2)).length * 7 + 12));
  var labelX = Math.max(originX + 4, LM);
  if (labelX + labelW > originX + viewW - 4) labelX = originX + viewW - labelW - 4;
  crosshairV.setAttribute('x1', x.toFixed(1));
  crosshairV.setAttribute('x2', x.toFixed(1));
  crosshairV.setAttribute('y1', originY.toFixed(1));
  crosshairV.setAttribute('y2', (originY + viewH).toFixed(1));
  crosshairH.setAttribute('x1', originX.toFixed(1));
  crosshairH.setAttribute('x2', (originX + viewW).toFixed(1));
  crosshairH.setAttribute('y1', y.toFixed(1));
  crosshairH.setAttribute('y2', y.toFixed(1));
  crosshairPriceBg.setAttribute('x', labelX.toFixed(1));
  crosshairPriceBg.setAttribute('y', (y - 9).toFixed(1));
  crosshairPriceBg.setAttribute('width', labelW.toFixed(1));
  crosshairPriceText.setAttribute('x', (labelX + labelW / 2).toFixed(1));
  crosshairPriceText.setAttribute('y', y.toFixed(1));
  crosshairPriceText.textContent = price.toFixed(2);
}}

function showCrosshairAtEvent(e) {{
  var p = svgPointFromEvent(e);
  crosshairState = p;
  crosshairEnabled = true;
  if (crosshair) crosshair.style.display = 'block';
  updateCrosshairLabel();
}}

function hideCrosshair() {{
  crosshairEnabled = false;
  crosshairState = null;
  if (crosshair) crosshair.style.display = 'none';
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
  var ds = b.display_dt || b.dt;
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
  if (selectedPen) selectedPen.style.display = 'none';
}}

function showSelectedPen(pen) {{
  if (!selectedPen || !pen) return;
  selectedPen.setAttribute('x1', Number(pen.x1).toFixed(1));
  selectedPen.setAttribute('y1', Number(pen.y1).toFixed(1));
  selectedPen.setAttribute('x2', Number(pen.x2).toFixed(1));
  selectedPen.setAttribute('y2', Number(pen.y2).toFixed(1));
  selectedPen.style.display = 'block';
  if (selectedLine) selectedLine.style.display = 'none';
}}

function clearFractalHighlight() {{
  if (fractalHighlight) fractalHighlight.innerHTML = '';
}}

function showFractalHighlight(fractal) {{
  if (!fractalHighlight || !fractal || !fractal.dates) return;
  var minX = Infinity, maxX = -Infinity, high = -Infinity, low = Infinity;
  fractal.dates.forEach(function(dateValue) {{
    var idx = dateToBar[normalizeKey(dateValue)];
    if (idx === undefined) return;
    var b = chartData.bars[idx];
    minX = Math.min(minX, b.x - BW / 2 - 2);
    maxX = Math.max(maxX, b.x + BW / 2 + 2);
    high = Math.max(high, b.h);
    low = Math.min(low, b.l);
  }});
  if (!isFinite(minX) || !isFinite(maxX) || !isFinite(high) || !isFinite(low)) return;
  var yTop = yAtPrice(high) - 5;
  var yBottom = yAtPrice(low) + 5;
  var stroke = fractal.kind === 'top' ? '#d92d20' : '#175cd3';
  var rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rect.setAttribute('x', minX.toFixed(1));
  rect.setAttribute('y', yTop.toFixed(1));
  rect.setAttribute('width', Math.max(4, maxX - minX).toFixed(1));
  rect.setAttribute('height', Math.max(8, yBottom - yTop).toFixed(1));
  rect.setAttribute('fill', 'none');
  rect.setAttribute('stroke', stroke);
  rect.setAttribute('stroke-width', '1.6');
  rect.setAttribute('stroke-dasharray', '5 3');
  rect.setAttribute('rx', '2');
  fractalHighlight.appendChild(rect);
}}

function markSelectedRow(row) {{
  var panel = wrap.closest('.timeframe-panel') || document;
  panel.querySelectorAll('tr.chart-linked-row.selected-row').forEach(function(r) {{
    r.classList.remove('selected-row');
  }});
  if (row) row.classList.add('selected-row');
}}

function focusChartDate(value, row) {{
  var date = normalizeKey(value);
  var idx = dateToBar[date];
  if (idx === undefined) return;
  var x = chartData.bars[idx].x;
  originX = x - viewW * 0.5;
  updateViewBox();
  showSelectedLine(idx);
  markSelectedRow(row);
  var rect = svg.getBoundingClientRect();
  var cx = rect.left + (x - originX) / viewW * rect.width;
  var cy = rect.top + Math.min(rect.height - 20, Math.max(24, rect.height * 0.24));
  showTip(idx, cx, cy);
  wrap.scrollIntoView({{ block: 'nearest', inline: 'nearest', behavior: 'smooth' }});
}}

function findNearestFractalRow(panel, key) {{
  var exact = panel.querySelector('tr[data-fractal-row][data-chart-date="' + key + '"]');
  if (exact) return exact;
  var target = Number(normalizeKey(key));
  var best = null;
  var bestDelta = Infinity;
  panel.querySelectorAll('tr[data-fractal-row][data-chart-date]').forEach(function(row) {{
    var value = Number(normalizeKey(row.getAttribute('data-chart-date')));
    if (!isFinite(value)) return;
    var delta = Math.abs(value - target);
    if (delta < bestDelta) {{
      bestDelta = delta;
      best = row;
    }}
  }});
  return best;
}}

function focusFractalTimePicker(picker) {{
  if (!picker) return false;
  var value = picker.value;
  if (!value) return false;
  var key = picker.getAttribute('data-picker-level') === 'daily'
    ? value.replace(/-/g, '')
    : value.replace('T', '').replace(/[-:]/g, '');
  var panel = wrap.closest('.timeframe-panel') || document;
  var details = picker.closest('details');
  if (details) details.open = true;
  var fractalRow = findNearestFractalRow(panel, key);
  focusChartDate(key, fractalRow);
  if (fractalRow) {{
    markSelectedRow(fractalRow);
    scrollRowWithinTable(fractalRow);
  }}
  return true;
}}

function findFractal(rowNumber) {{
  rowNumber = Number(rowNumber);
  for (var i = 0; i < chartData.fractals.length; i++) {{
    if (Number(chartData.fractals[i].row) === rowNumber) return chartData.fractals[i];
  }}
  return null;
}}

function findFractalByDate(value) {{
  var date = normalizeKey(value);
  for (var i = 0; i < chartData.fractals.length; i++) {{
    if (normalizeKey(chartData.fractals[i].date) === date) return chartData.fractals[i];
  }}
  return null;
}}

function highlightFractalReferenceByDate(value) {{
  var fractal = findFractalByDate(value);
  if (!fractal) return false;
  showFractalHighlight(fractal);
  return true;
}}

function scrollRowWithinTable(row) {{
  if (!row) return;
  var tableWrap = row.closest('.table-wrap');
  if (!tableWrap) return;
  var rowTop = row.offsetTop;
  var target = rowTop - tableWrap.clientHeight / 2 + row.clientHeight / 2;
  tableWrap.scrollTo({{ top: Math.max(0, target), behavior: 'smooth' }});
}}

function focusFractal(rowNumber, sourceElement) {{
  var fractal = findFractal(rowNumber);
  if (!fractal) return false;
  var row = (wrap.closest('.timeframe-panel') || document).querySelector('tr[data-fractal-row="' + rowNumber + '"]');
  var idx = dateToBar[normalizeKey(fractal.date)];
  if (idx !== undefined) {{
    var x = chartData.bars[idx].x;
    originX = x - viewW * 0.5;
    updateViewBox();
    showSelectedLine(idx);
    var rect = svg.getBoundingClientRect();
    var cx = rect.left + (x - originX) / viewW * rect.width;
    var cy = rect.top + Math.min(rect.height - 20, Math.max(24, rect.height * 0.24));
    showTip(idx, cx, cy);
  }}
  showFractalHighlight(fractal);
  markSelectedRow(row);
  if (row) {{
    var details = row.closest('details');
    if (details) details.open = true;
    scrollRowWithinTable(row);
  }}
  if (sourceElement) sourceElement.classList.add('selected-fractal-marker');
  return true;
}}

function focusChartPen(row) {{
  var penIndex = parseInt(row.getAttribute('data-pen-index'), 10);
  if (isNaN(penIndex)) {{
    return focusChartDate(row.getAttribute('data-chart-date'), row);
  }}
  var pen = chartData.pens[penIndex];
  if (!pen) return false;
  var startIdx = dateToBar[normalizeKey(pen.start)];
  var endIdx = dateToBar[normalizeKey(pen.end)];
  if (startIdx === undefined || endIdx === undefined) return false;
  var x1 = Number(pen.x1), x2 = Number(pen.x2);
  var span = Math.abs(x2 - x1) + BW * 16;
  if (span > viewW) {{
    viewW = Math.max(minViewW, Math.min(maxViewW, span * 1.15));
  }}
  originX = (x1 + x2) / 2 - viewW * 0.5;
  updateViewBox();
  showSelectedPen(pen);
  markSelectedRow(row);
  var rect = svg.getBoundingClientRect();
  var cx = rect.left + (x2 - originX) / viewW * rect.width;
  var cy = rect.top + (Number(pen.y2) - originY) / viewH * rect.height;
  showTip(endIdx, cx, cy);
  wrap.scrollIntoView({{ block: 'nearest', inline: 'nearest', behavior: 'smooth' }});
  return true;
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
  var r = svg.getBoundingClientRect();
  var my = r.height ? ((e.clientY - r.top) / r.height * viewH + originY) : -1;
  if (crosshairEnabled && my >= TM && my <= CH - BM) {{
    showCrosshairAtEvent(e);
  }}
  var idx = getBarAt(e.clientX);
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
  var timePicker = e.target.closest('[data-fractal-time-picker]');
  if (timePicker && wrap.closest('.timeframe-panel.active')) {{
    e.stopPropagation();
    return;
  }}
  var timePickerButton = e.target.closest('[data-fractal-time-submit]');
  if (timePickerButton && wrap.closest('.timeframe-panel.active')) {{
    e.preventDefault();
    e.stopPropagation();
    var picker = timePickerButton.closest('.summary-tools').querySelector('[data-fractal-time-picker]');
    focusFractalTimePicker(picker);
    return;
  }}
  var reasonDate = e.target.closest('[data-fractal-date]');
  if (reasonDate && wrap.closest('.timeframe-panel.active')) {{
    e.preventDefault();
    e.stopPropagation();
    var date = normalizeKey(reasonDate.getAttribute('data-fractal-date'));
    highlightFractalReferenceByDate(date);
    return;
  }}
  var marker = e.target.closest('.chart-fractal-marker');
  if (marker && wrap.closest('.timeframe-panel.active')) {{
    e.preventDefault();
    focusFractal(marker.getAttribute('data-fractal-row'), marker);
    return;
  }}
  var penRow = e.target.closest('tr[data-pen-index]');
  if (penRow && wrap.closest('.timeframe-panel.active')) {{
    focusChartPen(penRow);
    return;
  }}
  var fractalRow = e.target.closest('tr[data-fractal-row]');
  if (fractalRow && wrap.closest('.timeframe-panel.active')) {{
    focusFractal(fractalRow.getAttribute('data-fractal-row'));
    return;
  }}
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
document.getElementById('clear-fractal-highlight-btn-{cid}').addEventListener('click', function() {{
  clearFractalHighlight();
}});

wrap.addEventListener('dblclick', function(e) {{
  e.preventDefault();
  tip.style.display = 'none';
  if (crosshairEnabled) {{
    hideCrosshair();
  }} else {{
    showCrosshairAtEvent(e);
  }}
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
    pens = frame.get("pens") or []
    pen_details = frame.get("details") or []
    fractal_records = frame.get("fractal_records") or raw_fractals
    is_intraday = key in ("1m", "5m", "30m")

    def display_date(date):
        full = fmt_date(date)
        if is_intraday and " " in full:
            return full.split(" ", 1)[1]
        return full

    def price_with_date(value, date):
        return f"{safe(value)}（{display_date(date)}）" if date else safe(value)

    def link_reason_dates(text: str) -> str:
        escaped = html.escape(text)
        pattern = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
        return pattern.sub(
            lambda m: (
                f'<button type="button" class="reason-date-link" '
                f'data-fractal-date="{m.group(1).replace("-", "").replace(":", "").replace(" ", "")}">'
                f'{m.group(1)}</button>'
            ),
            escaped,
        )

    def render_notes(notes) -> str:
        clean = [str(x) for x in (notes or []) if str(x).strip()]
        if not clean:
            return ""
        items = "".join(f"<li>{link_reason_dates(x)}</li>" for x in clean)
        return f"<ol class=\"note-list\">{items}</ol>"

    f_lines = []
    picker_type = "date" if key == "daily" else "datetime-local"
    picker_level = "daily" if key == "daily" else "intraday"
    picker_step = "86400" if key == "daily" else "60"
    for i,pv in enumerate(fractal_records,1):
        valid = getattr(pv, "valid", True)
        row_classes = ["chart-linked-row"]
        if not valid:
            row_classes.append("filtered-fractal-row")
        status = "有效" if valid else "已过滤"
        process_notes = list(getattr(pv, "process_notes", []) or [])
        fallback_note = (
            getattr(pv, "filter_reason", "")
            or getattr(pv, "replaced_reason", "")
            or getattr(pv, "preserve_reason", "")
            or getattr(pv, "note_reason", "")
            or ""
        )
        if fallback_note and fallback_note not in process_notes:
            process_notes.append(fallback_note)
        reason = render_notes(process_notes)
        f_lines.append(
            f"<tr class=\"{' '.join(row_classes)}\" data-chart-date=\"{pv.date}\" data-fractal-row=\"{i}\"><td>{i}</td><td>{fmt_date(pv.date)}</td><td>{'顶分型' if pv.kind=='top' else '底分型'}</td>"
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

    effective_pen_keys = {
        (start.index, end.index)
        for start, end in zip(pens, pens[1:])
    }
    effective_pen_by_start_kind = {
        (start.index, end.kind): end
        for start, end in zip(pens, pens[1:])
    }
    pen_lines = []
    if pen_details:
        pen_records = pen_details
    else:
        pen_records = [
            PenStep(i - 1, start.index, start.kind, start.price, start.high, start.low,
                    end.index, end.kind, end.price, end.high, end.low,
                    max(0, end.index - start.index),
                    abs((end.price - start.price) / start.price * 100) if start.price else 0,
                    "成笔: 顶底交替且间隔通过", True)
            for i, (start, end) in enumerate(zip(pens, pens[1:]), 1)
        ]
    valid_pen_seq = 0
    for i, step in enumerate(pen_records, 1):
        is_effective = bool(step.accepted and (step.prev_idx, step.curr_idx) in effective_pen_keys)
        if is_effective:
            pen_index = valid_pen_seq
            valid_pen_seq += 1
        else:
            pen_index = ""
        direction = "向上笔" if step.curr_price >= step.prev_price else "向下笔"
        row_classes = ["chart-linked-row", "pen-row"]
        if not is_effective:
            row_classes.append("filtered-pen-row")
        status = "有效" if is_effective else "已过滤"
        if is_effective:
            reason = render_notes(["成笔: 顶底交替且间隔通过，保留为有效笔"])
        elif step.accepted:
            replacement = effective_pen_by_start_kind.get((step.prev_idx, step.curr_kind))
            if replacement and replacement.index != step.curr_idx:
                replacement_date = fmt_date(replacement.date)
                comparison = "更高" if step.curr_kind == "top" else "更低"
                reason = render_notes([
                    f"跳过(被同类极值替换): 后续{'顶分型' if step.curr_kind=='top' else '底分型'}"
                    f"{replacement_date} {replacement.price:.2f} {comparison}，最终保留该分型"
                ])
            else:
                reason = render_notes(["跳过(后续构造调整): 该候选笔未保留在最终笔序列"])
        else:
            reason = render_notes([step.check])
        data_pen_index = f' data-pen-index="{pen_index}"' if is_effective else ""
        prev_date = fmt_date(merged_bars[step.prev_idx].date) if 0 <= step.prev_idx < len(merged_bars) else str(step.prev_idx)
        curr_date = fmt_date(merged_bars[step.curr_idx].date) if 0 <= step.curr_idx < len(merged_bars) else str(step.curr_idx)
        pen_lines.append(
            f"<tr class=\"{' '.join(row_classes)}\" data-chart-date=\"{curr_date}\"{data_pen_index} data-pen-start-index=\"{step.prev_idx}\" data-pen-end-index=\"{step.curr_idx}\">"
            f"<td>{i}</td><td>{direction}</td>"
            f"<td>{prev_date}</td>"
            f"<td>{'顶分型' if step.prev_kind=='top' else '底分型'}</td><td>{safe(step.prev_price)}</td>"
            f"<td>{curr_date}</td>"
            f"<td>{'顶分型' if step.curr_kind=='top' else '底分型'}</td><td>{safe(step.curr_price)}</td>"
            f"<td>{max(0, step.gap)}</td><td>{safe(abs(step.curr_price - step.prev_price))}</td>"
            f"<td>{status}</td><td class=\"reason-cell\">{reason}</td></tr>"
        )
    penrows = "".join(pen_lines)

    svg = _make_svg_chart(stock_code, bars, pens, fractal_records, [], [], label, key, merged_bars) if len(bars) > 1 else ""
    chart_section = f'''
<section class="chart-section">
<h2>{html.escape(label)} K 线分型图</h2>
<div class="legend">
<span><span class="dot" style="background:#1f6f8b"></span>有效顶分型(▼)</span>
<span><span class="dot" style="background:#f79009"></span>有效底分型(▲)</span>
<span><span class="triangle-sample dashed-top"></span>过滤顶分型</span>
<span><span class="triangle-sample dashed-bottom"></span>过滤底分型</span>
<span><span class="box-sample dashed-absorbed"></span>被包含处理K线</span>
<span><span class="line-sample line-up"></span>向上笔</span>
<span><span class="line-sample line-down"></span>向下笔</span>
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
<details>
<summary><span>原始分型列表（形态{len(fractal_records)}个，有效{len(raw_fractals)}个）</span><span class="summary-tools"><label class="time-jump-label">定位时间<input type="{picker_type}" step="{picker_step}" data-picker-level="{picker_level}" data-fractal-time-picker></label><button type="button" class="time-jump-button" data-fractal-time-submit>确定</button></span></summary>
<div class="table-wrap fractal-table-wrap">
<table>
<thead><tr><th>#</th><th>日期</th><th>类型</th><th>分型价格</th><th>最高</th><th>最低</th><th>状态</th><th>备注</th></tr></thead>
<tbody>{frows if frows else '<tr><td colspan="8">未识别到足够分型</td></tr>'}</tbody>
</table></div>
</details>
{chart_section}
<details open>
<summary>笔列表（候选{len(pen_records)}笔，有效{max(0, len(pens)-1)}笔）</summary>
<div class="table-wrap pen-table-wrap">
<table>
<thead><tr><th>#</th><th>方向</th><th>起点日期</th><th>起点类型</th><th>起点价格</th><th>终点日期</th><th>终点类型</th><th>终点价格</th><th>K线间隔</th><th>价差</th><th>状态</th><th>备注</th></tr></thead>
<tbody>{penrows if penrows else '<tr><td colspan="12">未构造出足够的笔</td></tr>'}</tbody>
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
.summary-tools {{ display:flex; align-items:center; gap:10px; margin-left:auto; }}
.time-jump-label {{ display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:13px; font-weight:600; white-space:nowrap; cursor:default; }}
.time-jump-label input {{ height:32px; min-width:178px; border:1px solid var(--line); border-radius:6px; padding:4px 8px; color:var(--ink); background:#fff; font:inherit; font-size:13px; }}
.time-jump-label input[type="date"] {{ min-width:138px; }}
.time-jump-label input:focus {{ outline:2px solid rgba(31,111,139,.16); border-color:var(--accent); }}
.time-jump-button {{ height:32px; border:1px solid var(--accent); border-radius:6px; background:var(--accent); color:#fff; padding:0 12px; font-size:13px; font-weight:700; cursor:pointer; }}
.time-jump-button:hover {{ background:#175cd3; border-color:#175cd3; }}
details .table-wrap {{ padding:0 20px 18px; }}
.table-wrap {{ overflow-x:auto; -webkit-overflow-scrolling:touch; max-width:100%; }}
.merge-table-wrap {{ max-height:546px; overflow:auto; }}
.fractal-table-wrap {{ max-height:366px; overflow:auto; }}
.pen-table-wrap {{ max-height:366px; overflow:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; white-space:nowrap; }}
th {{ background:#f2f4f7; font-weight:700; position:sticky; top:0; }}
tr.chart-linked-row {{ cursor:pointer; }}
tr.absorbed-row td {{ background:#fff1f0; color:#912018; }}
tr.filtered-fractal-row td {{ background:#fff1f0; color:#912018; }}
tr.filtered-pen-row td {{ background:#fff1f0; color:#912018; }}
tr.selected-row td {{ background:#e8f6fb !important; color:var(--ink); }}
tr.selected-row td:first-child {{ box-shadow:inset 3px 0 0 var(--accent); }}
td.process-cell {{ min-width:260px; white-space:normal; overflow-wrap:anywhere; }}
td.reason-cell {{ min-width:280px; white-space:normal; overflow-wrap:anywhere; }}
.reason-date-link {{ border:0; padding:0; margin:0; background:transparent; color:var(--accent); font:inherit; text-decoration:underline; cursor:pointer; }}
.reason-date-link:hover {{ color:#175cd3; }}
.note-list {{ margin:0; padding-left:18px; min-width:300px; white-space:normal; overflow-wrap:anywhere; }}
.note-list li {{ margin:0 0 4px; line-height:1.45; }}
.note-list li:last-child {{ margin-bottom:0; }}
td .cell-list {{ list-style:none; margin:0; padding:0; min-width:0; white-space:normal; }}
td .cell-list li {{ margin:0 0 3px; padding:0; line-height:1.45; }}
td .cell-list li:last-child {{ margin-bottom:0; }}
td ul {{ min-width:280px; white-space:normal; overflow-wrap:anywhere; }}
ul {{ margin:0; padding-left:18px; }}
.note {{ color:var(--muted); font-size:13px; }}
.legend {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:8px; font-size:13px; }}
.legend span {{ display:inline-flex; align-items:center; gap:4px; }}
.legend .dot {{ display:inline-block; width:12px; height:12px; border-radius:50%; }}
.triangle-sample {{ width:0; height:0; display:inline-block; border-left:6px solid transparent; border-right:6px solid transparent; }}
.triangle-sample.dashed-top {{ border-bottom:10px dashed #1f6f8b; }}
.triangle-sample.dashed-bottom {{ border-top:10px dashed #f79009; }}
.box-sample {{ width:14px; height:14px; display:inline-block; border-radius:2px; }}
.box-sample.dashed-absorbed {{ border:1.5px dashed #d92d20; background:rgba(217,45,32,.06); }}
.line-sample {{ width:22px; height:0; display:inline-block; border-top:3px solid; border-radius:3px; }}
.line-up {{ border-color:#12b76a; }}
.line-down {{ border-color:#d92d20; }}
.chart-fractal-marker:hover {{ filter:drop-shadow(0 0 3px rgba(245,158,11,.85)); }}
.selected-fractal-marker {{ filter:drop-shadow(0 0 4px rgba(245,158,11,.95)); }}
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
  summary {{ padding:14px; font-size:18px; flex-wrap:wrap; align-items:flex-start; }}
  .summary-tools {{ width:100%; margin-left:0; }}
  .time-jump-label {{ width:100%; justify-content:space-between; }}
  .time-jump-label input {{ flex:1; min-width:0; }}
  .time-jump-button {{ width:100%; }}
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


def summarize_source_candidate(provider, label, ok, fetched_code="", bars=None, error=""):
    bar_count = len(bars or [])
    status = "可用" if ok else "不可用"
    return {
        "provider": provider,
        "label": label,
        "available": bool(ok),
        "bar_count": bar_count,
        "code": fetched_code,
        "status": f"{status}{f'，{bar_count}根' if ok else ''}",
        "selected": False,
        "error": "" if ok else str(error),
    }


def fetch_intraday_frame(args, key, label, klt):
    secid, code, name = resolve_web_secid(args.stock)
    name = name or lookup_stock_name(code, args.stock)
    attempts = [
        ("新浪分钟K线", lambda: fetch_sina_intraday_kline(code, klt, None)),
        *([("东方财富分时", lambda: fetch_eastmoney_trends_1m(secid, None))] if key == "1m" else []),
        *([("腾讯分时", lambda: fetch_tencent_hk_intraday_1m(code, None))] if key == "1m" else []),
        *([("通达信1分钟", lambda: fetch_pytdx_1m_kline(code, 30))] if key == "1m" else []),
        ("东方财富", lambda: fetch_kline(secid, klt, None)),
        ("mootdx", lambda: fetch_mootdx_intraday_kline(code, klt, None)),
        ("baostock", lambda: fetch_baostock_intraday_kline(code, klt, None)),
    ]
    candidates = []
    usable = []
    for order_idx, (provider, fetcher) in enumerate(attempts):
        try:
            fetched_code, fetched_name, bars = fetcher()
            candidates.append(summarize_source_candidate(provider, label, True, fetched_code, bars))
            usable.append({
                "order": order_idx,
                "provider": provider,
                "code": fetched_code,
                "name": fetched_name,
                "bars": bars,
            })
        except (Exception, SystemExit) as exc:
            candidates.append(summarize_source_candidate(provider, label, False, error=exc))

    if not usable:
        failures = "；".join(f"{c['provider']}: {c['error']}" for c in candidates)
        raise SystemExit(f"{label}分钟线所有来源均不可用：{failures}")

    tdx_1m = next((item for item in usable if key == "1m" and item["provider"] == "通达信1分钟"), None)
    selected = tdx_1m or sorted(usable, key=lambda x: (-len(x["bars"]), x["order"]))[0]
    display_name = name or selected.get("name") or lookup_stock_name(selected["code"], args.stock)
    source = f"{selected['provider']} ({selected['code']}, {label}, {len(selected['bars'])}根)"
    stock_code = f"{code} ({display_name})" if display_name else code
    for c in candidates:
        c["selected"] = c["provider"] == selected["provider"] and c.get("code") == selected["code"]
    return stock_code, source, selected["bars"], candidates


def build_timeframes(args, daily_stock_code, daily_source, daily_bars):
    requested = ["1m", "5m", "30m", "daily"] if args.chart_timeframe == "auto" else [args.chart_timeframe]
    order = ["1m", "5m", "30m", "daily"]
    labels = {"1m": "1分钟", "5m": "5分钟", "30m": "30分钟", "daily": "日线"}
    klts = {"1m": "1", "5m": "5", "30m": "30"}
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
                intraday_stock_code, intraday_source, intraday_bars, source_candidates = fetch_intraday_frame(args, key, label, klts[key])
                stock_code = intraday_stock_code or stock_code
                frame = analyze_timeframe_frame(key, label, stock_code, intraday_source, intraday_bars, args)
                frame["source_candidates"] = source_candidates
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
        "source_candidates": frame.get("source_candidates", []),
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
