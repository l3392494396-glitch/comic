#!/usr/bin/env python3
"""Log in to 18comic and verify the daily-login achievements.

Credentials are read only from environment variables so they can be supplied by
GitHub Actions encrypted secrets.  This script intentionally does not automate
advertisement clicks, comments, replies, uploads, or public likes.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
from dataclasses import dataclass
from html import unescape
from http.cookiejar import CookieJar
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener


DEFAULT_BASE_URL = "https://18comic.ink"
DEFAULT_TIMEOUT_SECONDS = 30.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0 Safari/537.36"
)


class CheckinError(RuntimeError):
    """Base class for expected, user-facing failures."""


class ConfigError(CheckinError):
    """Raised when required environment configuration is missing or unsafe."""


class LoginError(CheckinError):
    """Raised when the website rejects or cannot process a login."""


class VerificationError(CheckinError):
    """Raised when the daily-login task cannot be verified."""


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        values = os.environ if env is None else env
        username = values.get("JM_USERNAME", "").strip()
        password = values.get("JM_PASSWORD", "")
        base_url = values.get("JM_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")

        if not username:
            raise ConfigError("缺少环境变量 JM_USERNAME")
        if not password:
            raise ConfigError("缺少环境变量 JM_PASSWORD")

        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ConfigError("JM_BASE_URL 必须是有效的 HTTPS 地址")

        raw_timeout = values.get("JM_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ConfigError("JM_TIMEOUT 必须是数字") from exc
        if not 1 <= timeout <= 120:
            raise ConfigError("JM_TIMEOUT 必须在 1 到 120 秒之间")

        return cls(
            username=username,
            password=password,
            base_url=base_url,
            timeout=timeout,
        )


@dataclass(frozen=True)
class HttpResult:
    status: int
    url: str
    body: str


@dataclass(frozen=True)
class TaskProgress:
    current: int
    total: int

    @property
    def completed(self) -> bool:
        return self.current >= self.total

    def __str__(self) -> str:
        return f"{self.current}/{self.total}"


def _plain_text(fragment: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(unescape(without_tags).split())


def parse_task_progress(page_html: str) -> dict[str, TaskProgress]:
    """Extract achievement names and progress values from the profile page."""
    title_pattern = re.compile(
        r'<div[^>]+class=["\'][^"\']*tasks-row-title[^"\']*["\'][^>]*>'
        r"(?P<title>.*?)</div>",
        re.IGNORECASE | re.DOTALL,
    )
    progress_pattern = re.compile(
        r'<div[^>]+class=["\'][^"\']*totoal-count[^"\']*["\'][^>]*>'
        r"(?P<progress>.*?)</div>",
        re.IGNORECASE | re.DOTALL,
    )

    title_matches = list(title_pattern.finditer(page_html))
    tasks: dict[str, TaskProgress] = {}

    for index, title_match in enumerate(title_matches):
        chunk_end = (
            title_matches[index + 1].start()
            if index + 1 < len(title_matches)
            else len(page_html)
        )
        chunk = page_html[title_match.end() : chunk_end]
        progress_match = progress_pattern.search(chunk)
        if progress_match is None:
            continue

        title = _plain_text(title_match.group("title"))
        progress_text = _plain_text(progress_match.group("progress"))
        numbers = re.search(r"(\d+)\s*/\s*(\d+)", progress_text)
        if not title or numbers is None:
            continue

        tasks[title] = TaskProgress(
            current=int(numbers.group(1)),
            total=int(numbers.group(2)),
        )

    return tasks


class ComicClient:
    def __init__(self, config: Config, opener=None) -> None:
        self.config = config
        self.cookies = CookieJar()
        if opener is None:
            ssl_context = ssl.create_default_context()
            opener = build_opener(
                HTTPCookieProcessor(self.cookies),
                HTTPSHandler(context=ssl_context),
            )
        self.opener = opener

    def _request(
        self,
        path: str,
        *,
        form: Mapping[str, str] | None = None,
        ajax: bool = False,
    ) -> HttpResult:
        url = urljoin(f"{self.config.base_url}/", path.lstrip("/"))
        data = None if form is None else urlencode(form).encode("utf-8")
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01"
            if ajax
            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Referer": f"{self.config.base_url}/login",
            "User-Agent": USER_AGENT,
        }
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            headers["Origin"] = self.config.base_url
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"

        request = Request(
            url,
            data=data,
            headers=headers,
            method="POST" if form is not None else "GET",
        )

        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                raw_body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                body = raw_body.decode(charset, errors="replace")
                return HttpResult(
                    status=getattr(response, "status", 200),
                    url=response.geturl(),
                    body=body,
                )
        except HTTPError as exc:
            raise CheckinError(f"网站返回 HTTP {exc.code}: {url}") from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise CheckinError(f"无法连接网站: {reason}") from exc
        except TimeoutError as exc:
            raise CheckinError(f"访问网站超时（{self.config.timeout:g} 秒）") from exc

    def login(self) -> None:
        result = self._request(
            "/login",
            form={
                "username": self.config.username,
                "password": self.config.password,
                "id_remember": "on",
                "login_remember": "on",
                "submit_login": "1",
            },
            ajax=True,
        )

        try:
            payload = json.loads(result.body)
        except json.JSONDecodeError as exc:
            raise LoginError(
                "登录接口没有返回 JSON；站点可能正在维护或触发了风控"
            ) from exc

        try:
            status = int(payload.get("status", -1))
        except (TypeError, ValueError):
            status = -1

        if status != 1:
            message = str(payload.get("errors") or payload.get("msg") or "未知错误")
            if status == 5:
                message = f"账户被冻结，需要在网页中人工处理：{message}"
            raise LoginError(f"登录失败：{message[:300]}")

    def fetch_tasks(self, reward_type: str) -> dict[str, TaskProgress]:
        if reward_type not in {"coin", "exp"}:
            raise ValueError("reward_type must be 'coin' or 'exp'")

        username = quote(self.config.username, safe="")
        result = self._request(
            f"/user/{username}/achievements?type={reward_type}"
        )
        final_path = urlparse(result.url).path.rstrip("/")
        if final_path == "/login":
            raise VerificationError("登录态未生效，任务页被重定向到登录页")

        tasks = parse_task_progress(result.body)
        if not tasks:
            raise VerificationError(
                f"无法解析 {reward_type} 任务页，网站页面结构可能已经变化"
            )
        return tasks


def _print_tasks(label: str, tasks: Mapping[str, TaskProgress]) -> None:
    print(f"[{label}]")
    for name, progress in tasks.items():
        marker = "✓" if progress.completed else "·"
        print(f"  {marker} {name}: {progress}")


def run(config: Config) -> None:
    client = ComicClient(config)
    print(f"正在登录账号：{config.username}")
    client.login()
    print("登录成功")

    coin_tasks = client.fetch_tasks("coin")
    exp_tasks = client.fetch_tasks("exp")
    _print_tasks("金币任务", coin_tasks)
    _print_tasks("经验任务", exp_tasks)

    for label, tasks in (("金币", coin_tasks), ("经验", exp_tasks)):
        daily_login = tasks.get("每日登入")
        if daily_login is None:
            raise VerificationError(f"{label}任务页缺少“每日登入”项目")
        if not daily_login.completed:
            raise VerificationError(
                f"{label}每日登录尚未完成：{daily_login}"
            )

    print("每日登录任务已在金币和经验页面完成。")


def main() -> int:
    try:
        run(Config.from_env())
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except CheckinError as exc:
        print(f"签到失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # Defensive boundary for useful Actions logs.
        print(f"签到发生未预期错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
