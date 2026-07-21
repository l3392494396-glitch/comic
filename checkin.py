

from __future__ import annotations

import json
import os
import re
import ssl
import sys
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Mapping, MutableMapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import HTTPSHandler, Request, build_opener

from curl_cffi import requests as curl_requests


DEFAULT_BASE_URL = "https://jmcomic-zzz.one"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 8
PUSHPLUS_URL = "https://www.pushplus.plus/send"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0 Safari/537.36"
)


def _load_local_env(
    path: Path | None = None,
    env: MutableMapping[str, str] | None = None,
) -> Path | None:
    """Load local dotenv values without overriding existing environment values."""
    values = os.environ if env is None else env
    if path is None:
        project_dir = Path(__file__).resolve().parent
        candidates = (project_dir / ".env", project_dir / ".env.example")
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
    """Build the Cookie header from a bare AVS value."""
    if "\r" in raw_value or "\n" in raw_value:
        raise ConfigError("JM_COOKIE 不能包含换行符")

    value = raw_value.strip()
    if not value:
        raise ConfigError("JM_COOKIE 不能为空")
    if value.startswith("AVS="):
        raise ConfigError("JM_COOKIE 只填写 AVS 的值，不要包含 AVS= 前缀")
    if ";" in value:
        raise ConfigError("JM_COOKIE 只填写 AVS 的值，不要包含其他 Cookie")
    return f"AVS={value}"


@dataclass(frozen=True)
class Config:
    username: str
    password: str | None = field(default=None, repr=False)
    cookie: str | None = field(default=None, repr=False)
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        values = os.environ if env is None else env
        username = values.get("JM_USERNAME", "").strip()
        password = values.get("JM_PASSWORD", "").strip()
        raw_cookie = values.get("JM_COOKIE", "").strip()
        base_url = values.get("JM_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")

        if not username:
            raise ConfigError("缺少环境变量 JM_USERNAME")
        if not password and not raw_cookie:
            raise ConfigError("缺少环境变量 JM_PASSWORD（或兼容用的 JM_COOKIE）")
        cookie = _build_avs_cookie(raw_cookie) if raw_cookie and not password else None

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
            password=password or None,
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


def _find_login_form_action(page_html: str) -> str | None:
    """Return the action of a form that contains username and password inputs."""
    for match in re.finditer(
        r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>",
        page_html,
        re.IGNORECASE | re.DOTALL,
    ):
        body = match.group("body")
        has_username = re.search(
            r'<input\b[^>]*\bname=["\']username["\']',
            body,
            re.IGNORECASE,
        )
        has_password = re.search(
            r'<input\b[^>]*\bname=["\']password["\']',
            body,
            re.IGNORECASE,
        )
        if not (has_username and has_password):
            continue

        action = re.search(
            r'\baction=["\'](?P<action>[^"\']*)["\']',
            match.group("attrs"),
            re.IGNORECASE,
        )
        return unescape(action.group("action")).strip() if action else ""
    return None


class ComicClient:
    def __init__(self, config: Config, session=None) -> None:
        self.config = config
        self.session = session or curl_requests.Session(impersonate="chrome")
        self.cookie = config.cookie or ""
        self.base_url = config.base_url

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        extra_headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        include_cookie: bool = True,
    ) -> HttpResult:
        url = urljoin(f"{self.base_url}/", path)
        request_method = method.upper()
        request_data = data
        referer = f"{self.base_url}/"
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

            try:
                response = self.session.request(
                    request_method,
                    url,
                    headers=headers,
                    data=request_data,
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

    def login_with_password(self) -> None:
        if not self.config.password:
            return

        login_page = self._request("/login", include_cookie=False)
        action = _find_login_form_action(login_page.body)
        if action is None:
            raise VerificationError(
                "登录页没有出现账号密码表单，可能遇到 Cloudflare 验证或站点临时页面"
            )

        login_url = urljoin(login_page.url, action or "/login")
        parsed_login_url = urlparse(login_url)
        if parsed_login_url.scheme != "https" or not parsed_login_url.netloc:
            raise VerificationError("登录表单提交地址不是有效的 HTTPS 地址")

        self._request(
            login_url,
            method="POST",
            data={
                "username": self.config.username,
                "password": self.config.password,
                "id_remember": "on",
                "login_remember": "on",
                "submit_login": "",
            },
            include_cookie=False,
            extra_headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"{parsed_login_url.scheme}://{parsed_login_url.netloc}",
            },
        )
        if not self.cookie:
            raise VerificationError("账号密码登录失败，请检查用户名、密码或网站验证")

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
    dict[str, TaskProgress],
    dict[str, TaskProgress],
    list[str],
]:
    client = ComicClient(config)
    if config.password:
        print(f"正在使用账号密码登录：{config.username}")
        client.login_with_password()
        print("账号密码登录成功，已获取新的登录态")
    else:
        print(f"正在使用 Cookie 为账号签到：{config.username}")

    sign_result = client.sign_daily()
    print(f"个人中心签到：{sign_result.status}；{sign_result.message}")

    coin_tasks, coin_warning = _fetch_tasks_for_report(client, "金币", "coin")
    exp_tasks, exp_warning = _fetch_tasks_for_report(client, "经验", "exp")
    warnings = [warning for warning in (coin_warning, exp_warning) if warning]

    if warnings:
        print("个人中心签到已完成；任务页核验警告不影响本次签到结果。")
    else:
        print("每日登录任务已在金币和经验页面完成。")
    return sign_result, coin_tasks, exp_tasks, warnings


def _notification_content(
    username: str,
    sign_result: DailySignResult,
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


def main() -> int:
    _load_local_env()
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    username = os.environ.get("JM_USERNAME", "").strip() or "未配置"
    exit_code = 0
    title = "[comic] 每日签到成功"
    content = ""

    try:
        config = Config.from_env()
        sign_result, coin_tasks, exp_tasks, warnings = run(config)
        content = _notification_content(
            config.username,
            sign_result,
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
