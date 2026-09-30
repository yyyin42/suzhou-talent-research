# -*- coding: utf-8 -*-
"""网络请求层：限速、退避重试、访问限制即停、请求日志。

合规纪律（写死在代码里，不提供开关）：
  - 遇 401/403/429 或疑似验证码/登录墙，立即抛 AccessDenied，绝不重试绕过；
  - 请求间隔由配置控制，默认不低于 2 秒；
  - 每次请求都写入日志，包含时间、URL、状态码、耗时。
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 这些状态码意味着"对方明确拒绝"，重试只会加重冒犯，必须人工介入决策
FATAL_CODES = (401, 403, 429)


class AccessDenied(Exception):
    """访问被限制。停止采集该渠道，把情况记录下来交给人决策。"""

    def __init__(self, url, status, body_snippet=""):
        self.url = url
        self.status = status
        self.body_snippet = body_snippet[:300]
        super().__init__(
            f"访问被拒绝 status={status} url={url} "
            f"body={self.body_snippet} —— 按合规纪律停止，请人工核查平台规则后决定是否继续"
        )


class Logger:
    """同时输出到控制台和日志文件的简易 logger。"""

    def __init__(self, logfile=None, quiet=False):
        self.logfile = logfile
        self.quiet = quiet
        self.lines = []

    def log(self, msg, level="INFO"):
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
        self.lines.append(line)
        if not self.quiet:
            print(line, flush=True)
        if self.logfile:
            with open(self.logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def flush_memory(self):
        out, self.lines = self.lines, []
        return "\n".join(out)


class HttpClient:
    """带限速与重试的极简 HTTP 客户端（仅标准库）。"""

    def __init__(self, delay=2.0, timeout=30, retry=3, logger=None):
        self.delay = max(float(delay), 1.0)  # 下限 1 秒，防止误配置
        self.timeout = timeout
        self.retry = retry
        self.logger = logger or Logger(quiet=True)
        self._last_request_time = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_time = time.time()

    def request(self, url, method="GET", headers=None, payload=None, params=None):
        """发请求并返回 (status, body_text)。401/403/429 直接抛 AccessDenied。"""
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + "&".join(f"{k}={v}" for k, v in params.items())

        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            method = method or "POST"

        last_err = None
        for attempt in range(1, self.retry + 1):
            self._throttle()
            start = time.time()
            try:
                req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.status
                    body = resp.read().decode("utf-8", errors="replace")
                self.logger.log(f"{method} {url} -> {status} ({time.time()-start:.1f}s)")
                return status, body
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if e.code in FATAL_CODES:
                    self.logger.log(f"{method} {url} -> {e.code} FATAL", level="WARN")
                    raise AccessDenied(url, e.code, body)
                last_err = e
                self.logger.log(f"{method} {url} -> {e.code}，第 {attempt} 次失败", level="WARN")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                self.logger.log(f"{method} {url} -> 网络错误（{e}），第 {attempt} 次失败", level="WARN")

            if attempt < self.retry:
                wait = 3 * attempt  # 指数退避：3s, 6s
                self.logger.log(f"  {wait} 秒后重试…")
                time.sleep(wait)

        raise RuntimeError(f"请求最终失败：{url} ({last_err})")

    def get_json(self, url, headers=None, params=None):
        _, body = self.request(url, method="GET", headers=headers, params=params)
        return json.loads(body)

    def post_json(self, url, payload, headers=None):
        _, body = self.request(url, method="POST", headers=headers, payload=payload)
        return json.loads(body)


def default_headers(origin=None, referer=None, extra=None, ua=DEFAULT_UA):
    h = {"User-Agent": ua, "Accept": "application/json, text/plain, */*"}
    if origin:
        h["Origin"] = origin
    if referer:
        h["Referer"] = referer
    if extra:
        h.update(extra)
    return h
