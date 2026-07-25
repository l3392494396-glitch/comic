

from __future__ import annotations

import json
import ipaddress
import os
import re
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Mapping, MutableMapping
from urllib.parse import quote, urljoin, urlparse

from curl_cffi import requests as curl_requests
from curl_cffi.const import CurlOpt


DEFAULT_BASE_URL = "https://18comic.ink"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 8
KNOWN_INTERCEPT_IPS = {ipaddress.ip_address("182.43.124.7")}
CLOUDFLARE_IPV4_NETWORKS = (
    ipaddress.ip_network("104.16.0.0/13"),
    ipaddress.ip_network("172.64.0.0/13"),
)
PUSHPLUS_URL = "https://www.pushplus.plus/send"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
MONTHLY_BUTTON_TEXTS = {"开启本月签到", "開啟本月簽到"}
MONTHLY_SUCCESS_TEXTS = ("今日已签到", "今日已簽到", "签到成功", "簽到成功")


def _load_local_env(
    path: Path | None = None,
    env: MutableMapping[str, str] | None = None,
) -> Path | None:
    """Load local dotenv values without overriding existing environment values."""
    values = os.environ if env is None else env
    if path is None:
        project_dir = Path(__file__).resolve().parent
        candidates = (project_dir / ".env.local", project_dir / ".env")
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None or not path.is_file():
        return None

    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values.setdefault(name, value)
    return path


class CheckinError(RuntimeError):
    """Base class for expected, user-facing failures."""


class ConfigError(CheckinError):
    """Raised when required environment configuration is missing or unsafe."""


class VerificationError(CheckinError):
    """Raised when the daily-login task cannot be verified."""


class NotificationError(CheckinError):
    """Raised when PushPlus does not accept a notification request."""


def _build_avs_cookie(raw_value: str) -> str:
    """Keep only the AVS authentication cookie from a Cookie header."""
    if "\r" in raw_value or "\n" in raw_value:
        raise ConfigError("JM_COOKIE 不能包含换行符")

    if not raw_value.strip():
        raise ConfigError("JM_COOKIE 不能为空")
    for part in raw_value.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name == "AVS" and value.strip():
            return f"AVS={value.strip()}"
    if "=" not in raw_value:
        return f"AVS={raw_value.strip()}"
    raise ConfigError("JM_COOKIE 中缺少有效的 AVS Cookie")


def _dns_override_for_intercepted_host(hostname: str) -> list[str]:
    """Recover a Cloudflare IPv4 from AAAA only when local A DNS is intercepted."""
    try:
        ipv4_values = {
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(
                hostname,
                443,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError):
        return []
    if not (ipv4_values & KNOWN_INTERCEPT_IPS):
        return []

    try:
        ipv6_values = {
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(
                hostname,
                443,
                family=socket.AF_INET6,
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError):
        return []

    recovered = set()
    for ipv6 in ipv6_values:
        if not isinstance(ipv6, ipaddress.IPv6Address):
            continue
        candidate = ipaddress.IPv4Address(int(ipv6) & 0xFFFFFFFF)
        if any(candidate in network for network in CLOUDFLARE_IPV4_NETWORKS):
            recovered.add(candidate)
    if not recovered:
        return []

    preferred = min(recovered, key=int)
    return [f"{hostname}:443:{preferred}"]


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
        base_url = DEFAULT_BASE_URL

        if not username:
            raise ConfigError("缺少环境变量 JM_USERNAME")
        if not raw_cookie:
            raise ConfigError("缺少环境变量 JM_COOKIE")
        cookie = _build_avs_cookie(raw_cookie)

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


@dataclass(frozen=True)
class DailySignResult:
    signed_now: bool
    message: str

    @property
    def status(self) -> str:
        return "签到成功" if self.signed_now else "今日已签到"


@dataclass(frozen=True)
class MonthlyAction:
    method: str
    url: str
    data: dict[str, str]


@dataclass(frozen=True)
class MonthlyCheckinResult:
    message: str


class _MonthlyButtonParser(HTMLParser):
    """Find the request represented by the exact monthly check-in button."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.form_stack: list[dict[str, object]] = []
        self.clickables: list[dict[str, object]] = []
        self.clickable_stack: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attributes = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag == "form":
            self.form_stack.append({"attrs": attributes, "fields": {}})

        if tag == "input" and self.form_stack:
            name = attributes.get("name", "")
            input_type = attributes.get("type", "text").lower()
            if name and input_type in {"hidden", "submit"}:
                fields = self.form_stack[-1]["fields"]
                assert isinstance(fields, dict)
                fields[name] = attributes.get("value", "")

        if tag in {"a", "button", "input"}:
            clickable: dict[str, object] = {
                "tag": tag,
                "attrs": attributes,
                "text": [attributes.get("value", "")] if tag == "input" else [],
                "form": self.form_stack[-1] if self.form_stack else None,
            }
            self.clickables.append(clickable)
            if tag != "input":
                self.clickable_stack.append(clickable)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        for clickable in self.clickable_stack:
            text_parts = clickable["text"]
            assert isinstance(text_parts, list)
            text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"a", "button"}:
            for index in range(len(self.clickable_stack) - 1, -1, -1):
                if self.clickable_stack[index]["tag"] == tag:
                    del self.clickable_stack[index]
                    break
        if tag == "form" and self.form_stack:
            self.form_stack.pop()


def _onclick_action(onclick: str) -> tuple[str, str] | None:
    if not onclick:
        return None
    method = (
        "POST"
        if re.search(r"(?:\$\.post|method\s*:\s*['\"]post)", onclick, re.I)
        else "GET"
    )
    patterns = (
        r"(?:\$\.(?:get|post)|fetch|window\.open)\s*\(\s*['\"]([^'\"]+)",
        r"(?:location(?:\.href)?|url)\s*[:=]\s*['\"]([^'\"]+)",
        r"['\"]((?:https://|/)[^'\"]*(?:bonus|checkin|sign)[^'\"]*)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, onclick, re.I)
        if match:
            return method, unescape(match.group(1))
    return None


def parse_monthly_action(page_html: str, page_url: str) -> MonthlyAction | None:
    """Parse the exact “开启本月签到” control without guessing an endpoint."""
    parser = _MonthlyButtonParser()
    parser.feed(page_html)

    for clickable in parser.clickables:
        text_parts = clickable["text"]
        attrs = clickable["attrs"]
        assert isinstance(text_parts, list)
        assert isinstance(attrs, dict)
        text_value = " ".join("".join(text_parts).split())
        if text_value not in MONTHLY_BUTTON_TEXTS:
            continue

        form = clickable["form"]
        if isinstance(form, dict):
            form_attrs = form["attrs"]
            fields = dict(form["fields"])
            assert isinstance(form_attrs, dict)
            name = attrs.get("name", "")
            if name:
                fields[name] = attrs.get("value", "")
            action_url = attrs.get("formaction") or form_attrs.get("action") or page_url
            method = (attrs.get("formmethod") or form_attrs.get("method") or "GET").upper()
            return MonthlyAction(method, urljoin(page_url, action_url), fields)

        for attribute in (
            "formaction",
            "data-url",
            "data-href",
            "data-action",
            "data-endpoint",
            "href",
        ):
            value = attrs.get(attribute, "").strip()
            if value and value not in {"#", "javascript:void(0)", "javascript:;"}:
                return MonthlyAction(
                    attrs.get("data-method", "GET").upper(),
                    urljoin(page_url, value),
                    {},
                )

        onclick = _onclick_action(attrs.get("onclick", ""))
        if onclick:
            method, value = onclick
            return MonthlyAction(method, urljoin(page_url, value), {})
        raise VerificationError(
            "已找到“开启本月签到”按钮，但无法识别它对应的请求；网站前端结构可能已变化"
        )
    return None


def send_pushplus(
    token: str,
    title: str,
    content: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    session=None,
) -> str:
    """Send a Markdown message with PushPlus' official JSON API."""
    clean_token = token.strip()
    if not clean_token:
        raise NotificationError("缺少环境变量 PUSHPLUS_TOKEN")

    payload = {
        "token": clean_token,
        "title": title,
        "content": content,
        "template": "markdown",
    }
    if session is None:
        session = curl_requests.Session(
            impersonate="chrome",
            trust_env=False,
        )

    try:
        response = session.request(
            "POST",
            PUSHPLUS_URL,
            json=payload,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json; charset=UTF-8",
                "Origin": "https://www.pushplus.plus",
                "Referer": "https://www.pushplus.plus/",
            },
            timeout=timeout,
            allow_redirects=False,
        )
    except curl_requests.RequestsError as exc:
        raise NotificationError(f"无法连接 PushPlus：{exc}") from exc

    status = int(response.status_code)
    if 300 <= status < 400:
        raise NotificationError(f"PushPlus 返回意外重定向 HTTP {status}")
    if status >= 400:
        raise NotificationError(f"PushPlus 返回 HTTP {status}")

    try:
        response_payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise NotificationError("PushPlus 没有返回有效的 JSON") from exc
    if not isinstance(response_payload, dict):
        raise NotificationError("PushPlus 返回的 JSON 格式不正确")

    try:
        code = int(response_payload.get("code", -1))
    except (TypeError, ValueError):
        code = -1
    if code != 200:
        message = str(response_payload.get("msg", "未知错误"))
        raw_detail = response_payload.get("data")
        detail = ""
        if raw_detail not in (None, ""):
            safe_detail = str(raw_detail).replace(clean_token, "***")[:300]
            detail = f"；详情={safe_detail}"
        raise NotificationError(
            f"PushPlus 拒绝了请求（code={code}）：{message[:300]}{detail}"
        )

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
    def __init__(self, config: Config, session=None, dns_resolver=None) -> None:
        self.config = config
        self.session = session or curl_requests.Session(
            impersonate="chrome",
            trust_env=False,
        )
        if dns_resolver is not None:
            self.dns_resolver = dns_resolver
        elif session is None:
            self.dns_resolver = _dns_override_for_intercepted_host
        else:
            # Injected sessions are used by tests and do not need local DNS access.
            self.dns_resolver = lambda hostname: []
        self.cookie = config.cookie or ""
        self.base_url = config.base_url
        self._reported_dns_repair = False

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        extra_headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        include_cookie: bool = True,
        referer_url: str | None = None,
    ) -> HttpResult:
        url = urljoin(f"{self.base_url}/", path)
        request_method = method.upper()
        if request_method not in {"GET", "POST"}:
            raise VerificationError(f"不支持的请求方式：{request_method}")
        request_data = data if request_method == "POST" else None
        request_params = data if request_method == "GET" else None
        referer = referer_url or f"{self.base_url}/"
        send_cookie = include_cookie

        for redirect_count in range(MAX_REDIRECTS + 1):
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                "Referer": referer,
            }
            if send_cookie and self.cookie:
                headers["Cookie"] = self.cookie
            if extra_headers:
                headers.update(extra_headers)

            hostname = urlparse(url).hostname or ""
            resolve_entries = self.dns_resolver(hostname) if hostname else []
            if resolve_entries:
                self.session.curl_options = {CurlOpt.RESOLVE: resolve_entries}
                if not self._reported_dns_repair:
                    print(
                        "提示：检测到异常 DNS，已使用通过 HTTPS 证书校验的"
                        " Cloudflare 地址直连（未使用代理）"
                    )
                    self._reported_dns_repair = True
            else:
                self.session.curl_options = {}

            try:
                response = self.session.request(
                    request_method,
                    url,
                    headers=headers,
                    data=request_data,
                    params=request_params,
                    timeout=self.config.timeout,
                    allow_redirects=False,
                )
            except curl_requests.RequestsError as exc:
                error_text = str(exc).lower()
                is_certificate_error = (
                    "curl: (60)" in error_text
                    or "certificate" in error_text
                    or "ssl cert" in error_text
                )
                can_probe_redirect = (
                    is_certificate_error
                    and request_method in {"GET", "HEAD"}
                    and request_data is None
                    and request_params is None
                )
                if not can_probe_redirect:
                    raise CheckinError(f"无法连接网站：{exc}") from exc

                probe_headers = dict(headers)
                probe_headers.pop("Cookie", None)
                try:
                    response = self.session.request(
                        request_method,
                        url,
                        headers=probe_headers,
                        data=None,
                        params=request_params,
                        timeout=self.config.timeout,
                        allow_redirects=False,
                        verify=False,
                    )
                except curl_requests.RequestsError as probe_exc:
                    raise CheckinError(f"无法解析网站跳转：{probe_exc}") from probe_exc

                probe_status = int(response.status_code)
                probe_location = response.headers.get("location", "").strip()
                probe_target = urljoin(url, probe_location) if probe_location else ""
                source_host = (urlparse(url).hostname or "").lower()
                target = urlparse(probe_target)
                target_host = (target.hostname or "").lower()
                if not (
                    300 <= probe_status < 400
                    and target.scheme == "https"
                    and target.netloc
                    and target_host != source_host
                ):
                    raise CheckinError(
                        "跳转中间域名的 HTTPS 证书不可信，且未提供可验证的下一跳"
                    ) from exc
                print(
                    "提示：已匿名解析证书异常的跳转中间域名，"
                    "未向该域名发送账号、密码或 Cookie"
                )
                used_unverified_probe = True
            else:
                used_unverified_probe = False

            status = int(response.status_code)
            body = response.text
            response_cookies = getattr(response, "cookies", None)
            if response_cookies is not None and not used_unverified_probe:
                try:
                    refreshed_avs = response_cookies.get("AVS")
                except (KeyError, TypeError, ValueError):
                    refreshed_avs = None
                if refreshed_avs:
                    self.cookie = f"AVS={refreshed_avs}"
                    send_cookie = True

            if 300 <= status < 400:
                location = response.headers.get("location", "").strip()
                if not location:
                    raise CheckinError(
                        f"网站返回 HTTP {status}，但没有提供重定向地址：{url}"
                    )
                if redirect_count >= MAX_REDIRECTS:
                    raise CheckinError(f"网站重定向次数超过 {MAX_REDIRECTS} 次")

                target_url = urljoin(url, location)
                parsed_target = urlparse(target_url)
                if parsed_target.scheme != "https" or not parsed_target.netloc:
                    raise CheckinError("网站试图重定向到非 HTTPS 地址，已停止请求")

                if status == 303 or (
                    status in {301, 302} and request_method not in {"GET", "HEAD"}
                ):
                    request_method = "GET"
                    request_data = None
                request_params = None
                referer = url
                url = target_url
                continue

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
                hint = (
                    "；站点可能限制了 GitHub Actions 的 IP 或识别出自动请求"
                    if status == 403
                    else ""
                )
                raise CheckinError(f"网站返回 HTTP {status}: {url}{suffix}{hint}")

            final_url = str(response.url)
            parsed_final = urlparse(final_url)
            self.base_url = f"{parsed_final.scheme}://{parsed_final.netloc}"
            return HttpResult(status=status, url=final_url, body=body)

        raise CheckinError("网站重定向失败")

    def _discover_authenticated_username(self) -> str | None:
        """Find the current account path from authenticated navigation links."""
        result = self._request("/")
        if urlparse(result.url).path.rstrip("/") == "/login":
            return None

        match = re.search(
            r'href=["\'][^"\']*/user/(?P<username>[^/"\'?#]+)'
            r'/(?:notice|daily|achievements)(?:[?\#"\']|$)',
            result.body,
            re.IGNORECASE,
        )
        return match.group("username") if match else None

    def monthly_checkin(self) -> MonthlyCheckinResult:
        """Open the monthly calendar action and verify today's sign-in."""
        username = quote(self.config.username, safe="")
        page = self._request(f"/user/{username}/bonus")
        if urlparse(page.url).path.rstrip("/") == "/login":
            raise VerificationError("登录态未生效，签到活动页被重定向到登录页")

        action = parse_monthly_action(page.body, page.url)
        if action is None:
            page_text = _plain_text(page.body)
            if any(text in page_text for text in MONTHLY_SUCCESS_TEXTS):
                return MonthlyCheckinResult("今日已经签到")
            raise VerificationError(
                "签到活动页缺少“开启本月签到”按钮，网站页面结构可能已经变化"
            )

        page_origin = urlparse(page.url)
        action_target = urlparse(action.url)
        if (
            action_target.scheme != "https"
            or action_target.netloc != page_origin.netloc
        ):
            raise VerificationError("拒绝向签到站点以外的地址发送 Cookie")

        extra_headers = None
        if action.method == "POST":
            extra_headers = {
                "Accept": "application/json, text/javascript, text/html, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            }
        result = self._request(
            action.url,
            method=action.method,
            data=action.data,
            extra_headers=extra_headers,
            referer_url=page.url,
        )
        if urlparse(result.url).path.rstrip("/") == "/login":
            raise VerificationError("登录态未生效，月度签到请求被重定向到登录页")

        response_text = _plain_text(result.body)
        for success_text in MONTHLY_SUCCESS_TEXTS:
            if success_text in response_text:
                if "已" in success_text:
                    return MonthlyCheckinResult("今日已经签到")
                return MonthlyCheckinResult("今日签到成功")

        try:
            payload = json.loads(result.body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            message = str(payload.get("msg") or payload.get("message") or "").strip()
            success_value = payload.get("success")
            status_value = payload.get("status")
            successful = success_value in (True, 1, "1", "ok", "success")
            successful = successful or status_value in (
                True,
                1,
                "1",
                "ok",
                "success",
            )
            try:
                successful = successful or int(payload.get("code", -1)) in {0, 1, 200}
            except (TypeError, ValueError):
                pass
            if successful:
                return MonthlyCheckinResult(message[:120] or "月度签到请求成功")
            if message:
                raise VerificationError(f"月度签到失败：{message[:200]}")

        raise VerificationError(
            "已提交“开启本月签到”请求，但响应中没有找到成功标记"
        )

    def sign_daily(self) -> DailySignResult:
        result = self._request(
            "/ajax/user_daily_sign",
            method="POST",
            extra_headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        final_path = urlparse(result.url).path.rstrip("/")
        if final_path == "/login":
            raise VerificationError("登录态未生效，签到请求被重定向到登录页")

        try:
            payload = json.loads(result.body)
        except json.JSONDecodeError as exc:
            raise VerificationError("每日签到接口没有返回有效的 JSON") from exc
        if not isinstance(payload, dict):
            raise VerificationError("每日签到接口返回了未知的数据格式")

        message = str(payload.get("msg", "")).strip()
        error = str(payload.get("error", "")).strip()
        if error == "finished":
            return DailySignResult(signed_now=False, message=message or "今天已经完成签到")
        if error:
            details = f"：{message}" if message else ""
            raise VerificationError(f"每日签到失败（{error}）{details}")
        if not message:
            raise VerificationError("每日签到接口未确认成功，登录态可能已失效")
        return DailySignResult(signed_now=True, message=message)

    def fetch_tasks(self, reward_type: str) -> dict[str, TaskProgress]:
        if reward_type not in {"coin", "exp"}:
            raise ValueError("reward_type must be 'coin' or 'exp'")

        username = quote(self.config.username, safe="")

        def fetch_for(username_segment: str) -> HttpResult:
            return self._request(
                f"/user/{username_segment}/achievements?type={reward_type}"
            )

        result = fetch_for(username)
        final_path = urlparse(result.url).path.rstrip("/")
        if final_path == "/login":
            raise VerificationError("登录态未生效，任务页被重定向到登录页")

        tasks = parse_task_progress(result.body)
        if not tasks:
            authenticated_username = self._discover_authenticated_username()
            if authenticated_username and authenticated_username != username:
                result = fetch_for(authenticated_username)
                final_path = urlparse(result.url).path.rstrip("/")
                if final_path == "/login":
                    raise VerificationError("登录态未生效，任务页被重定向到登录页")
                tasks = parse_task_progress(result.body)
        if not tasks:
            title_match = re.search(
                r"<title[^>]*>(?P<title>.*?)</title>",
                result.body,
                re.IGNORECASE | re.DOTALL,
            )
            title = _plain_text(title_match.group("title")) if title_match else ""
            details = f"（实际页面标题：{title}）" if title else ""
            raise VerificationError(
                f"无法解析 {reward_type} 任务页{details}，网站页面结构或账号地址可能已经变化"
            )
        return tasks


def _print_tasks(label: str, tasks: Mapping[str, TaskProgress]) -> None:
    print(f"[{label}]")
    for name, progress in tasks.items():
        marker = "✓" if progress.completed else "·"
        print(f"  {marker} {name}: {progress}")


def _fetch_tasks_for_report(
    client: ComicClient,
    label: str,
    reward_type: str,
) -> tuple[dict[str, TaskProgress], str | None]:
    try:
        tasks = client.fetch_tasks(reward_type)
    except CheckinError as exc:
        warning = f"{label}任务进度无法读取：{exc}"
        print(f"警告：{warning}", file=sys.stderr)
        return {}, warning

    _print_tasks(f"{label}任务", tasks)
    daily_login = tasks.get("每日登入")
    if daily_login is None:
        warning = f"{label}任务页缺少“每日登入”项目"
        print(f"警告：{warning}", file=sys.stderr)
        return tasks, warning
    if not daily_login.completed:
        warning = f"{label}每日登录进度尚未完成：{daily_login}"
        print(f"警告：{warning}", file=sys.stderr)
        return tasks, warning
    return tasks, None


def run(
    config: Config,
) -> tuple[
    DailySignResult,
    MonthlyCheckinResult,
    dict[str, TaskProgress],
    dict[str, TaskProgress],
    list[str],
]:
    client = ComicClient(config)
    print(f"正在使用 Cookie 为账号签到：{config.username}")

    sign_result = client.sign_daily()
    print(f"个人中心签到：{sign_result.status}；{sign_result.message}")

    monthly_result = client.monthly_checkin()
    print(f"月度签到：{monthly_result.message}")

    coin_tasks, coin_warning = _fetch_tasks_for_report(client, "金币", "coin")
    exp_tasks, exp_warning = _fetch_tasks_for_report(client, "经验", "exp")
    warnings = [warning for warning in (coin_warning, exp_warning) if warning]

    if warnings:
        print("个人中心签到已完成；任务页核验警告不影响本次签到结果。")
    else:
        print("每日登录任务已在金币和经验页面完成。")
    return sign_result, monthly_result, coin_tasks, exp_tasks, warnings


def _notification_content(
    username: str,
    sign_result: DailySignResult,
    monthly_result: MonthlyCheckinResult,
    coin_tasks: Mapping[str, TaskProgress],
    exp_tasks: Mapping[str, TaskProgress],
    warnings: list[str],
) -> str:
    lines = [
        f"账号：`{username}`",
        "",
        "## 个人中心签到",
        f"- ✅ {sign_result.status}：{sign_result.message}",
        "",
        "## 月度签到",
        f"- ✅ {monthly_result.message}",
        "",
        "## 金币任务",
    ]
    for name, progress in coin_tasks.items():
        marker = "✅" if progress.completed else "▫️"
        lines.append(f"- {marker} {name}：{progress}")
    if not coin_tasks:
        lines.append("- ⚠️ 未读取到任务进度")

    lines.extend(["", "## 经验任务"])
    for name, progress in exp_tasks.items():
        marker = "✅" if progress.completed else "▫️"
        lines.append(f"- {marker} {name}：{progress}")
    if not exp_tasks:
        lines.append("- ⚠️ 未读取到任务进度")
    if warnings:
        lines.extend(["", "## 核验警告"])
        lines.extend(f"- ⚠️ {warning}" for warning in warnings)
    return "\n".join(lines)


def _append_notification_run_marker(
    content: str,
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """Make each notification unique so PushPlus does not reject reruns."""
    values = os.environ if env is None else env
    current = now or datetime.now(CHINA_TIMEZONE)
    current = current.astimezone(CHINA_TIMEZONE)
    timestamp = current.strftime("%Y-%m-%d %H:%M:%S.%f")
    run_id = values.get("GITHUB_RUN_ID", "").strip()
    run_attempt = values.get("GITHUB_RUN_ATTEMPT", "").strip()
    action_marker = ""
    if run_id:
        action_marker = f"；Actions {run_id}"
        if run_attempt:
            action_marker += f".{run_attempt}"
    return f"{content}\n\n---\n运行标识：`{timestamp}{action_marker}`"


def main() -> int:
    _load_local_env()
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    username = os.environ.get("JM_USERNAME", "").strip() or "未配置"
    exit_code = 0
    title = "[comic] 每日签到成功"
    content = ""

    try:
        config = Config.from_env()
        sign_result, monthly_result, coin_tasks, exp_tasks, warnings = run(config)
        content = _notification_content(
            config.username,
            sign_result,
            monthly_result,
            coin_tasks,
            exp_tasks,
            warnings,
        )
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
        notification_content = _append_notification_run_marker(content)
        message_id = send_pushplus(token, title, notification_content)
        suffix = f"，消息流水号：{message_id}" if message_id else ""
        print(f"PushPlus 推送请求已提交{suffix}")
    except NotificationError as exc:
        print(f"PushPlus 推送失败：{exc}", file=sys.stderr)
        if exit_code == 0:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
