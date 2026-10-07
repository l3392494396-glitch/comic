"""Refresh the 18comic ``AVS`` login cookie before the daily check-in.

The check-in workflow previously relied on a session cookie that had to be
copied by hand.  Those cookies expire after a few days, so every scheduled run
failed once the stored value went stale.  This helper signs in with
``JM_USERNAME`` / ``JM_PASSWORD`` and exports a fresh ``JM_COOKIE`` for the
following step (through ``$GITHUB_ENV`` on GitHub Actions).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from html import unescape
from typing import Mapping, MutableMapping
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as curl_requests


DEFAULT_BASE_URL = "https://18comic.ink"
DEFAULT_TIMEOUT_SECONDS = 30.0
LOGIN_PATHS = ("/login", "/user/login")
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}
AVS_PATTERN = re.compile(r"(?:^|;\s*)AVS=([^;]+)")


class RefreshError(RuntimeError):
    """Raised when a fresh login state cannot be obtained."""


@dataclass(frozen=True)
class RefreshConfig:
    username: str
    password: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RefreshConfig":
        values = os.environ if env is None else env
        username = values.get("JM_USERNAME", "").strip()
        password = values.get("JM_PASSWORD", "").strip()
        if not username:
            raise RefreshError("缺少环境变量 JM_USERNAME")
        if not password:
            raise RefreshError("缺少环境变量 JM_PASSWORD")

        base_url = values.get("JM_BASE_URL", "").strip() or DEFAULT_BASE_URL
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RefreshError("JM_BASE_URL 必须是有效的 HTTPS 地址")

        raw_timeout = values.get("JM_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise RefreshError("JM_TIMEOUT 必须是数字") from exc
        if not 1 <= timeout <= 120:
            raise RefreshError("JM_TIMEOUT 必须在 1 到 120 秒之间")

        return cls(
            username=username,
            password=password,
            base_url=base_url,
            timeout=timeout,
        )


def find_login_form_action(page_html: str) -> str | None:
    """Return the action of a form that contains username and password inputs."""
    for match in re.finditer(
        r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>",
        page_html,
        re.IGNORECASE | re.DOTALL,
    ):
        body = match.group("body")
        if not re.search(
            r'<input\b[^>]*\bname=["\']username["\']', body, re.IGNORECASE
        ):
            continue
        if not re.search(
            r'<input\b[^>]*\bname=["\']password["\']', body, re.IGNORECASE
        ):
            continue
        action = re.search(
            r'\baction=["\'](?P<action>[^"\']*)["\']',
            match.group("attrs"),
            re.IGNORECASE,
        )
        return unescape(action.group("action")).strip() if action else ""
    return None


def extract_avs_cookie(cookies) -> str | None:
    """Pull the AVS value out of a cookie jar, mapping or raw header string."""
    if cookies is None:
        return None
    if isinstance(cookies, str):
        match = AVS_PATTERN.search(cookies)
        return match.group(1).strip() if match else None

    getter = getattr(cookies, "get", None)
    if callable(getter):
        try:
            value = getter("AVS")
        except TypeError:
            value = None
        if value:
            return str(value).strip()
    try:
        items = cookies.items()
    except (AttributeError, TypeError):
        return None
    for name, value in items:
        if str(name) == "AVS" and value:
            return str(value).strip()
    return None


def _response_cookies(response, session) -> object:
    cookies = getattr(response, "cookies", None)
    try:
        if cookies is not None and len(cookies) > 0:
            return cookies
    except TypeError:
        return cookies
    return getattr(session, "cookies", None)


def _new_session():
    return curl_requests.Session(impersonate="chrome", trust_env=False)


def refresh_cookie(config: RefreshConfig, session=None) -> str:
    """Sign in and return a ready-to-use ``AVS=...`` cookie header value."""
    session = session or _new_session()
    last_error = ""

    for path in LOGIN_PATHS:
        page_url = urljoin(f"{config.base_url}/", path.lstrip("/"))
        try:
            page = session.request(
                "GET",
                page_url,
                headers=dict(BROWSER_HEADERS),
                timeout=config.timeout,
                allow_redirects=True,
            )
        except curl_requests.RequestsError as exc:
            last_error = f"无法打开登录页 {page_url}：{exc}"
            continue

        status = int(getattr(page, "status_code", 0))
        if status >= 400:
            last_error = f"登录页返回 HTTP {status}：{page_url}"
            continue

        action = find_login_form_action(page.text)
        if action is None:
            last_error = f"登录页没有账号密码表单：{page_url}"
            continue

        post_url = urljoin(str(page.url), action or path)
        parsed = urlparse(post_url)
        if parsed.scheme != "https" or not parsed.netloc:
            last_error = f"登录表单提交地址无效：{post_url}"
            continue

        try:
            response = session.request(
                "POST",
                post_url,
                data={
                    "username": config.username,
                    "password": config.password,
                    "id_remember": "on",
                    "login_remember": "on",
                    "submit_login": "",
                },
                headers={
                    **BROWSER_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": f"{parsed.scheme}://{parsed.netloc}",
                    "Referer": str(page.url),
                },
                timeout=config.timeout,
                allow_redirects=True,
            )
        except curl_requests.RequestsError as exc:
            last_error = f"无法提交登录表单：{exc}"
            continue

        avs = extract_avs_cookie(_response_cookies(response, session))
        if avs:
            return f"AVS={avs}"
        last_error = "登录后没有返回 AVS 登录态，请检查用户名或密码"

    raise RefreshError(last_error or "登录失败")


def normalize_cookie(raw_value: str) -> str | None:
    """Accept a bare AVS value or a full cookie header and return AVS=..."""
    value = raw_value.strip()
    if not value:
        return None
    for part in value.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if separator and name == "AVS" and cookie_value.strip():
            return f"AVS={cookie_value.strip()}"
    if "=" not in value:
        return f"AVS={value}"
    return None


def write_env_file(path: str, name: str, value: str) -> None:
    """Append ``name=value`` to the file referenced by ``$GITHUB_ENV``."""
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        print(f"{name}={value}", file=handle)


def mask(value: str) -> str:
    clean = value.strip()
    if len(clean) <= 2:
        return "*" * len(clean)
    return f"{clean[:2]}***"


def main(env: MutableMapping[str, str] | None = None) -> int:
    values = os.environ if env is None else env
    username = values.get("JM_USERNAME", "").strip()
    password = values.get("JM_PASSWORD", "").strip()
    fallback = values.get("JM_COOKIE", "").strip()

    try:
        if username and password:
            config = RefreshConfig.from_env(values)
            cookie = refresh_cookie(config)
            print(f"已通过账号密码刷新登录态：{mask(username)}")
        elif fallback:
            cookie = normalize_cookie(fallback)
            if not cookie:
                raise RefreshError("JM_COOKIE 中缺少有效的 AVS Cookie")
            print("未配置 JM_PASSWORD，使用既有 JM_COOKIE 作为兜底。")
        else:
            raise RefreshError("缺少 JM_PASSWORD（或兼容用的 JM_COOKIE）")
    except RefreshError as exc:
        print(f"登录态刷新失败：{exc}", file=sys.stderr)
        return 1

    env_file = values.get("GITHUB_ENV", "").strip()
    if env_file:
        write_env_file(env_file, "JM_COOKIE", cookie)
        print("已将刷新的登录态写入后续步骤环境变量。")
    else:
        print(f"JM_COOKIE={cookie}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
