#!/usr/bin/env python3
"""Local HTTP service for generating and serving Chan reports."""
import argparse
import html
import json
import re
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from analyze_chan_points import REPORT_SYMBOLS


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS_DIR = ROOT / "reports"
DEFAULT_SYMBOL = "SH000001"
SYMBOL_MAP = {item["symbol"]: item["label"] for item in REPORT_SYMBOLS}
GENERATE_LOCK = threading.Lock()
MAX_QUERY_LEN = 40
QUERY_RE = re.compile(r"^[A-Za-z0-9.\-\u4e00-\u9fff]+$")


def parse_args():
    parser = argparse.ArgumentParser(description="本地动态生成缠论 1分钟报告服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，代码服务器可用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="报告输出和静态服务目录")
    return parser.parse_args()


def page(title, body):
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:#20242a; background:#f6f7f9; line-height:1.6; }}
    main {{ width:min(760px,100%); margin:0 auto; padding:36px 20px; }}
    section {{ background:#fff; border:1px solid #d9dee7; border-radius:8px; padding:22px; }}
    h1 {{ margin:0 0 12px; font-size:26px; }}
    p {{ margin:8px 0; }}
    a, button {{ color:#1f6f8b; }}
    form {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:16px; }}
    select, input {{ height:34px; min-width:180px; border:1px solid #d9dee7; border-radius:6px; padding:4px 10px; background:#fff; }}
    input {{ min-width:300px; }}
    button {{ height:34px; border:1px solid #1f6f8b; border-radius:6px; background:#1f6f8b; color:#fff; padding:0 14px; font-weight:700; cursor:pointer; }}
    .note {{ color:#667085; font-size:13px; }}
  </style>
</head>
<body><main><section>{body}</section></main></body>
</html>"""


def symbol_form(selected=DEFAULT_SYMBOL, query=""):
    options = "\n".join(
        f'<option value="{html.escape(item["symbol"])}"{" selected" if item["symbol"] == selected else ""}>{html.escape(item["label"])}</option>'
        for item in REPORT_SYMBOLS
    )
    return f"""<form action="/generate" method="get">
  <label>报告标的 <select name="symbol">{options}</select></label>
  <label>代码/名称 <input type="text" name="query" maxlength="{MAX_QUERY_LEN}" value="{html.escape(query)}" placeholder="输入代码或名称，如 600519.SH / 贵州茅台 / 沪深300"></label>
  <button type="submit">生成</button>
</form>"""


def validate_query(query):
    if not query:
        raise ValueError("请输入代码或名称，或选择一个快捷标的。")
    if len(query) > MAX_QUERY_LEN:
        raise ValueError(f"输入过长，最多 {MAX_QUERY_LEN} 个字符。")
    if not QUERY_RE.fullmatch(query):
        raise ValueError("输入只能包含中文、字母、数字、点号和短横线。")
    return query


def run_report(stock_query, reports_dir):
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "analyze_chan_points.py"),
        "--stock",
        stock_query,
        "--source",
        "web",
        "--out-dir",
        str(reports_dir),
        "--chart-timeframe",
        "1m",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"退出码 {proc.returncode}"
        raise RuntimeError(detail)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"无法解析生成结果：{proc.stdout[-1000:]}") from exc
    html_report = payload.get("html_report")
    if not html_report:
        raise RuntimeError("生成结果缺少 html_report")
    report_path = Path(html_report).resolve()
    try:
        relative = report_path.relative_to(reports_dir.resolve())
    except ValueError as exc:
        raise RuntimeError(f"报告不在 reports-dir 下：{report_path}") from exc
    return relative.as_posix()


class ReportHandler(SimpleHTTPRequestHandler):
    server_version = "ChanReportServer/1.0"

    def __init__(self, *args, directory=None, **kwargs):
        server = args[2] if len(args) >= 3 else None
        report_dir = getattr(server, "reports_dir", DEFAULT_REPORTS_DIR)
        super().__init__(*args, directory=str(report_dir), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_html(self, status, title, body):
        content = page(title, body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def redirect(self, location):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.redirect(f"/generate?symbol={quote(DEFAULT_SYMBOL)}")
            return
        if parsed.path == "/generate":
            self.handle_generate(parsed)
            return
        self.path = unquote(parsed.path)
        super().do_GET()

    def handle_generate(self, parsed):
        params = parse_qs(parsed.query, keep_blank_values=True)
        symbol = (params.get("symbol") or [DEFAULT_SYMBOL])[0].strip()
        query = (params.get("query") or [""])[0].strip()
        selected = symbol if symbol in SYMBOL_MAP else DEFAULT_SYMBOL
        if query:
            try:
                target = validate_query(query)
            except ValueError as exc:
                self.send_html(
                    HTTPStatus.BAD_REQUEST,
                    "输入无效",
                    f"<h1>输入无效</h1><p>{html.escape(str(exc))}</p>{symbol_form(selected, query)}",
                )
                return
        elif symbol in SYMBOL_MAP:
            target = symbol
        else:
            self.send_html(
                HTTPStatus.BAD_REQUEST,
                "输入无效",
                f"<h1>输入无效</h1><p>请选择快捷标的，或输入代码/名称。</p>{symbol_form(DEFAULT_SYMBOL, query)}",
            )
            return
        acquired = GENERATE_LOCK.acquire(blocking=False)
        if not acquired:
            self.send_html(
                HTTPStatus.CONFLICT,
                "正在生成",
                f"<h1>正在生成报告</h1><p>当前已有一个生成任务在运行，请稍后再试。</p>{symbol_form(selected, query)}",
            )
            return
        try:
            relative = run_report(target, self.server.reports_dir)
        except Exception as exc:
            self.send_html(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "生成失败",
                f"<h1>生成失败</h1><p>{html.escape(str(exc))}</p>{symbol_form(selected, query)}",
            )
            return
        finally:
            GENERATE_LOCK.release()
        self.redirect("/" + quote(relative))


def main():
    args = parse_args()
    reports_dir = Path(args.reports_dir).expanduser().resolve()
    reports_dir.mkdir(parents=True, exist_ok=True)

    class ConfiguredServer(ThreadingHTTPServer):
        pass

    server = ConfiguredServer((args.host, args.port), ReportHandler)
    server.reports_dir = reports_dir
    print(f"Serving Chan reports at http://{args.host}:{args.port}/")
    print(f"Reports directory: {reports_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
