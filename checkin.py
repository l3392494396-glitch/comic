#!/usr/bin/env python3
"""Verify 18comic daily-login achievements with an existing login cookie.

The cookie is read only from an environment variable so it can be supplied by
GitHub Actions encrypted secrets. This script never submits an account password
and intentionally does not automate advertisements, comments, uploads, or likes.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
from dataclasses import dataclass
from html import unescape
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import HTTPSHandler, Request, build_opener

from curl_cffi import requests as curl_requests


DEFAULT_BASE_URL = "https://18comic.ink"
DEFAULT_TIMEOUT_SECONDS = 30.0
PUSHPLUS_URL = "https://www.pushplus.plus/send"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0 Safari/537.36"
)


class CheckinError(RuntimeError):
    """Base class for expected, user-facing failures."""


class ConfigError(CheckinError):
    """Raised when required environment configuration is missing or unsafe."""


class VerificationError(CheckinError):
    """Raised when the daily-login task cannot be verified."""


class NotificationError(CheckinError):
    """Raised when PushPlus does not accept a notification request."""


def _normalize_avs_cookie(raw_cookie: str) -> str:
    """Keep only the authentication cookie and discard tracking/remember data."""
    if "\r" in raw_cookie or "\n" in raw_cookie:
        raise ConfigError("JM_COOKIE 不能包含换行符")

    for part in raw_cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name == "AVS" and value:
            return f"AVS={value.strip()}"
    raise ConfigError("JM_COOKIE 中缺少有效的 AVS Cookie")


@dataclass(frozen=True)
class Config:
    username: str
    cookie: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        values = os.environ if env is None else env
        username = values.get("JM_USERNAME", "").strip()
        raw_cookie = values.get("JM_COOKIE", "").strip()
        base_url = values.get("JM_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")

        if not username:
            raise ConfigError("缺少环境变量 JM_USERNAME")
        if not raw_cookie:
            raise ConfigError("缺少环境变量 JM_COOKIE")
        cookie = _normalize_avs_cookie(raw_cookie)

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
            cookie=cookie,
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


def send_pushplus(
    token: str,
    title: str,
    content: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener=None,
) -> str:
    """Submit a Markdown notification to PushPlus and return its message ID."""
    if not token.strip():
        raise NotificationError("缺少环境变量 PUSHPLUS_TOKEN")

    payload = json.dumps(
        {
            "token": token.strip(),
            "title": title,
            "content": content,
            "template": "markdown",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        PUSHPLUS_URL,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    if opener is None:
        opener = build_opener(HTTPSHandler(context=ssl.create_default_context()))

    try:
        with opener.open(request, timeout=timeout) as response:
            raw_body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            body = raw_body.decode(charset, errors="replace")
    except HTTPError as exc:
        raise NotificationError(f"PushPlus 返回 HTTP {exc.code}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise NotificationError(f"无法连接 PushPlus：{reason}") from exc
    except TimeoutError as exc:
        raise NotificationError(f"访问 PushPlus 超时（{timeout:g} 秒）") from exc

    try:
        response_payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NotificationError("PushPlus 没有返回有效的 JSON") from exc

    try:
        code = int(response_payload.get("code", -1))
    except (AttributeError, TypeError, ValueError):
        code = -1
    if code != 200:
        message = str(response_payload.get("msg", "未知错误"))
        raise NotificationError(f"PushPlus 拒绝了请求：{message[:300]}")

    return str(response_payload.get("data", ""))


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
    def __init__(self, config: Config, session=None) -> None:
        self.config = config
        self.session = session or curl_requests.Session(impersonate="chrome")

    def _request(self, path: str) -> HttpResult:
        url = urljoin(f"{self.config.base_url}/", path.lstrip("/"))
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Referer": f"{self.config.base_url}/",
            "Cookie": self.config.cookie,
        }

        try:
            response = self.session.request(
                "GET",
                url,
                headers=headers,
                timeout=self.config.timeout,
                allow_redirects=True,
            )
        except curl_requests.RequestsError as exc:
            raise CheckinError(f"无法连接网站：{exc}") from exc

        status = int(response.status_code)
        body = response.text
        if status >= 400:
            details = []
            server = response.headers.get("server", "").strip()
            challenge = response.headers.get("cf-mitigated", "").strip()
            snippet = _plain_text(body)[:160]
            if server:
                details.append(f"server={server}")
            if challenge:
                details.append(f"cf-mitigated={challenge}")
            if snippet:
                details.append(f"响应={snippet}")
            suffix = f"（{'；'.join(details)}）" if details else ""
            hint = "；站点可能限制了 GitHub Actions 的 IP 或识别出自动请求" if status == 403 else ""
            raise CheckinError(f"网站返回 HTTP {status}: {url}{suffix}{hint}")

        return HttpResult(status=status, url=str(response.url), body=body)

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


def run(config: Config) -> tuple[dict[str, TaskProgress], dict[str, TaskProgress]]:
    client = ComicClient(config)
    print(f"正在使用 Cookie 验证账号：{config.username}")

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
    return coin_tasks, exp_tasks


def _notification_content(
    username: str,
    coin_tasks: Mapping[str, TaskProgress],
    exp_tasks: Mapping[str, TaskProgress],
) -> str:
    lines = [f"账号：`{username}`", "", "## 金币任务"]
    for name, progress in coin_tasks.items():
        marker = "✅" if progress.completed else "▫️"
        lines.append(f"- {marker} {name}：{progress}")

    lines.extend(["", "## 经验任务"])
    for name, progress in exp_tasks.items():
        marker = "✅" if progress.completed else "▫️"
        lines.append(f"- {marker} {name}：{progress}")
    return "\n".join(lines)


def main() -> int:
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    username = os.environ.get("JM_USERNAME", "").strip() or "未配置"
    exit_code = 0
    title = "[comic] 每日签到成功"
    content = ""

    try:
        config = Config.from_env()
        coin_tasks, exp_tasks = run(config)
        content = _notification_content(config.username, coin_tasks, exp_tasks)
    except ConfigError as exc:
        message = f"配置错误：{exc}"
        print(message, file=sys.stderr)
        title = "[comic] 每日签到失败"
        content = f"账号：`{username}`\n\n{message}"
        exit_code = 2
    except CheckinError as exc:
        message = f"签到失败：{exc}"
        print(message, file=sys.stderr)
        title = "[comic] 每日签到失败"
        content = f"账号：`{username}`\n\n{message}"
        exit_code = 1
    except Exception as exc:  # Defensive boundary for useful Actions logs.
        message = f"签到发生未预期错误：{type(exc).__name__}: {exc}"
        print(message, file=sys.stderr)
        title = "[comic] 每日签到失败"
        content = f"账号：`{username}`\n\n{message}"
        exit_code = 1

    try:
        message_id = send_pushplus(token, title, content)
        suffix = f"，消息流水号：{message_id}" if message_id else ""
        print(f"PushPlus 推送请求已提交{suffix}")
    except NotificationError as exc:
        print(f"PushPlus 推送失败：{exc}", file=sys.stderr)
        if exit_code == 0:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
