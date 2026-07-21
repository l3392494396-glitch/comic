import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from curl_cffi import requests as curl_requests
from curl_cffi.const import CurlOpt

from checkin import (
    CheckinError,
    ComicClient,
    Config,
    ConfigError,
    DailySignResult,
    NotificationError,
    TaskProgress,
    VerificationError,
    _dns_override_for_intercepted_host,
    _find_login_form_action,
    _load_local_env,
    parse_task_progress,
    run,
    send_pushplus,
)


class LocalEnvTests(unittest.TestCase):
    def test_loads_dotenv_without_overriding_existing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.example"
            path.write_text(
                "JM_USERNAME=file-user\n"
                "JM_COOKIE='cookie-value'\n"
                "PUSHPLUS_TOKEN=push-token\n",
                encoding="utf-8",
            )
            env = {"JM_USERNAME": "existing-user"}

            loaded_path = _load_local_env(path, env)

        self.assertEqual(loaded_path, path)
        self.assertEqual(env["JM_USERNAME"], "existing-user")
        self.assertEqual(env["JM_COOKIE"], "cookie-value")
        self.assertEqual(env["PUSHPLUS_TOKEN"], "push-token")


class FakeHeaders(dict):
    def get_content_charset(self):
        return "utf-8"


class FakeResponse:
    def __init__(
        self,
        body,
        url="https://18comic.ink/login",
        status=200,
        cookies=None,
    ):
        self._body = body.encode("utf-8")
        self._url = url
        self.status = status
        self.status_code = status
        self.url = url
        self.text = body
        self.headers = FakeHeaders()
        self.cookies = cookies or {}

    def read(self):
        return self._body

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.responses.pop(0)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ConfigTests(unittest.TestCase):
    def test_reads_password_login_values(self):
        config = Config.from_env(
            {"JM_USERNAME": "alice", "JM_PASSWORD": "secret-value"}
        )
        self.assertEqual(config.username, "alice")
        self.assertEqual(config.password, "secret-value")
        self.assertIsNone(config.cookie)
        self.assertEqual(config.base_url, "https://18comic.ink")

    def test_keeps_cookie_login_as_compatibility_mode(self):
        config = Config.from_env(
            {"JM_USERNAME": "alice", "JM_COOKIE": "session-value"}
        )
        self.assertEqual(config.cookie, "AVS=session-value")
        self.assertEqual(config.base_url, "https://18comic.ink")

    def test_password_mode_ignores_old_cookie_value(self):
        config = Config.from_env(
            {
                "JM_USERNAME": "alice",
                "JM_PASSWORD": "secret-value",
                "JM_COOKIE": "AVS=old-value",
            }
        )
        self.assertEqual(config.password, "secret-value")
        self.assertIsNone(config.cookie)

    def test_rejects_missing_password_and_cookie(self):
        with self.assertRaises(ConfigError):
            Config.from_env({"JM_USERNAME": "alice"})

    def test_rejects_avs_prefix(self):
        with self.assertRaisesRegex(ConfigError, "不要包含 AVS= 前缀"):
            Config.from_env(
                {"JM_USERNAME": "alice", "JM_COOKIE": "AVS=session-value"}
            )

    def test_rejects_full_cookie_header(self):
        with self.assertRaisesRegex(ConfigError, "其他 Cookie"):
            Config.from_env(
                {
                    "JM_USERNAME": "alice",
                    "JM_COOKIE": "theme=light; AVS=session-value",
                }
            )

    def test_rejects_cookie_with_newline(self):
        with self.assertRaisesRegex(ConfigError, "换行符"):
            Config.from_env(
                {"JM_USERNAME": "alice", "JM_COOKIE": "value\r\nevil=1"}
            )

    def test_ignores_old_base_url_override(self):
        config = Config.from_env(
            {
                "JM_USERNAME": "alice",
                "JM_COOKIE": "session-value",
                "JM_BASE_URL": "https://18comic.vip",
            }
        )
        self.assertEqual(config.base_url, "https://18comic.ink")


class ParserTests(unittest.TestCase):
    HTML = """
    <div class="tasks-row">
      <div class="tasks-row-title">每日表示喜歡作品</div>
      <div class="tasks-row-progress-block">
        <div class="totoal-count">4 / 4</div>
      </div>
    </div>
    <div class="tasks-row">
      <div class="tasks-row-title">每日登入</div>
      <div class="tasks-row-progress-block">
        <div class="totoal-count">1 / 1</div>
      </div>
    </div>
    """

    def test_parses_task_rows(self):
        tasks = parse_task_progress(self.HTML)
        self.assertEqual(tasks["每日表示喜歡作品"], TaskProgress(4, 4))
        self.assertTrue(tasks["每日登入"].completed)

    def test_finds_login_form_action(self):
        html = """
        <form action="/login" method="post">
          <input name="username">
          <input type="password" name="password">
        </form>
        """
        self.assertEqual(_find_login_form_action(html), "/login")


class DnsRepairTests(unittest.TestCase):
    def test_recovers_cloudflare_ipv4_only_for_known_intercept(self):
        def fake_getaddrinfo(hostname, port, *, family, type):
            self.assertEqual(hostname, "mirror.example")
            self.assertEqual(port, 443)
            self.assertEqual(type, socket.SOCK_STREAM)
            if family == socket.AF_INET:
                return [(socket.AF_INET, type, 6, "", ("182.43.124.7", port))]
            return [
                (
                    socket.AF_INET6,
                    type,
                    6,
                    "",
                    ("2606:4700:3035::6815:5494", port, 0, 0),
                )
            ]

        with patch("checkin.socket.getaddrinfo", side_effect=fake_getaddrinfo):
            result = _dns_override_for_intercepted_host("mirror.example")

        self.assertEqual(result, ["mirror.example:443:104.21.84.148"])

    def test_does_not_override_normal_dns(self):
        with patch(
            "checkin.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.21.1.2", 443))
            ],
        ):
            result = _dns_override_for_intercepted_host("normal.example")

        self.assertEqual(result, [])


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(username="alice", cookie="AVS=session-value")

    def test_reports_403_diagnostics(self):
        response = FakeResponse("Restricted Access!", status=403)
        response.headers["server"] = "cloudflare"
        session = FakeSession([response])
        client = ComicClient(self.config, session=session)

        with self.assertRaisesRegex(CheckinError, "GitHub Actions"):
            client.fetch_tasks("coin")

    def test_applies_validated_dns_override_to_curl(self):
        session = FakeSession(
            [FakeResponse(ParserTests.HTML, url="https://mirror.example/tasks")]
        )
        client = ComicClient(
            self.config,
            session=session,
            dns_resolver=lambda hostname: [
                f"{hostname}:443:104.21.84.148"
            ],
        )

        client._request("https://mirror.example/tasks")

        self.assertEqual(
            session.curl_options[CurlOpt.RESOLVE],
            ["mirror.example:443:104.21.84.148"],
        )

    def test_follows_https_cross_domain_redirect_with_cookie(self):
        redirect = FakeResponse(
            "",
            url="https://18comic.ink/user/alice/achievements?type=coin",
            status=301,
        )
        redirect.headers["location"] = (
            "https://jmcomic-zzz.one/user/alice/achievements?type=coin"
        )
        session = FakeSession(
            [
                redirect,
                FakeResponse(
                    ParserTests.HTML,
                    url="https://jmcomic-zzz.one/user/alice/achievements?type=coin",
                ),
            ]
        )
        client = ComicClient(self.config, session=session)

        tasks = client.fetch_tasks("coin")

        self.assertTrue(tasks["每日登入"].completed)
        self.assertEqual(len(session.requests), 2)
        self.assertFalse(session.requests[0][2]["allow_redirects"])
        self.assertEqual(
            session.requests[1][2]["headers"]["Cookie"],
            "AVS=session-value",
        )

    def test_rejects_non_https_redirect(self):
        redirect = FakeResponse(
            "",
            url="https://18comic.ink/user/alice/achievements?type=coin",
            status=302,
        )
        redirect.headers["location"] = "http://mirror.example/achievements"
        client = ComicClient(self.config, session=FakeSession([redirect]))

        with self.assertRaisesRegex(CheckinError, "非 HTTPS"):
            client.fetch_tasks("coin")

    def test_uses_anonymous_probe_for_self_signed_redirect_hop(self):
        certificate_error = curl_requests.RequestsError(
            "Failed to perform, curl: (60) SSL certificate problem"
        )
        redirect = FakeResponse(
            "",
            url="https://selfsigned.example/user/alice/achievements?type=coin",
            status=302,
        )
        redirect.headers["location"] = (
            "https://trusted.example/user/alice/achievements?type=coin"
        )
        session = FakeSession(
            [
                certificate_error,
                redirect,
                FakeResponse(
                    ParserTests.HTML,
                    url="https://trusted.example/user/alice/achievements?type=coin",
                ),
            ]
        )
        config = Config(
            username="alice",
            cookie="AVS=session-value",
            base_url="https://selfsigned.example",
        )
        client = ComicClient(config, session=session)

        tasks = client.fetch_tasks("coin")

        self.assertTrue(tasks["每日登入"].completed)
        self.assertNotIn("Cookie", session.requests[1][2]["headers"])
        self.assertFalse(session.requests[1][2]["verify"])
        self.assertEqual(
            session.requests[2][2]["headers"]["Cookie"],
            "AVS=session-value",
        )

    def test_logs_in_with_password_after_server_redirect(self):
        redirect = FakeResponse("", url="https://18comic.ink/login", status=301)
        redirect.headers["location"] = "https://jmcomic-zzz.one/login"
        login_form = FakeResponse(
            """
            <form action="/login" method="post">
              <input name="username">
              <input type="password" name="password">
            </form>
            """,
            url="https://jmcomic-zzz.one/login",
        )
        logged_in = FakeResponse(
            "<html><title>会员中心</title></html>",
            url="https://jmcomic-zzz.one/login",
            cookies={"AVS": "new-session-value"},
        )
        session = FakeSession([redirect, login_form, logged_in])
        config = Config(username="alice", password="secret-value")
        client = ComicClient(config, session=session)

        client.login_with_password()

        self.assertEqual(client.cookie, "AVS=new-session-value")
        self.assertEqual(session.requests[2][0], "POST")
        self.assertEqual(session.requests[2][1], "https://jmcomic-zzz.one/login")
        self.assertEqual(session.requests[2][2]["data"]["username"], "alice")
        self.assertEqual(
            session.requests[2][2]["data"]["password"], "secret-value"
        )
        self.assertEqual(session.requests[2][2]["data"]["login_remember"], "on")

    def test_sends_cookie_when_fetching_tasks(self):
        session = FakeSession(
            [
                FakeResponse(
                    ParserTests.HTML,
                    url="https://18comic.ink/user/alice/achievements?type=coin",
                )
            ]
        )
        config = Config(username="alice", cookie="AVS=session-value")
        client = ComicClient(config, session=session)

        tasks = client.fetch_tasks("coin")

        _, _, kwargs = session.requests[0]
        self.assertEqual(kwargs["headers"]["Cookie"], "AVS=session-value")
        self.assertTrue(tasks["每日登入"].completed)

    def test_posts_daily_sign_request(self):
        session = FakeSession(
            [
                FakeResponse(
                    json.dumps({"msg": "签到成功，获得奖励"}),
                    url="https://18comic.ink/ajax/user_daily_sign",
                )
            ]
        )
        client = ComicClient(self.config, session=session)

        result = client.sign_daily()

        method, url, kwargs = session.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://18comic.ink/ajax/user_daily_sign")
        self.assertEqual(kwargs["headers"]["Cookie"], "AVS=session-value")
        self.assertEqual(kwargs["headers"]["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(result, DailySignResult(True, "签到成功，获得奖励"))

    def test_treats_finished_daily_sign_as_success(self):
        session = FakeSession(
            [
                FakeResponse(
                    json.dumps({"msg": "", "error": "finished"}),
                    url="https://18comic.ink/ajax/user_daily_sign",
                )
            ]
        )
        client = ComicClient(self.config, session=session)

        result = client.sign_daily()

        self.assertFalse(result.signed_now)
        self.assertEqual(result.status, "今日已签到")

    def test_uses_refreshed_avs_cookie_after_sign(self):
        session = FakeSession(
            [
                FakeResponse(
                    json.dumps({"msg": "签到成功"}),
                    url="https://18comic.ink/ajax/user_daily_sign",
                    cookies={"AVS": "refreshed-value"},
                ),
                FakeResponse(
                    ParserTests.HTML,
                    url="https://18comic.ink/user/alice/achievements?type=coin",
                ),
            ]
        )
        client = ComicClient(self.config, session=session)

        client.sign_daily()
        client.fetch_tasks("coin")

        _, _, kwargs = session.requests[1]
        self.assertEqual(kwargs["headers"]["Cookie"], "AVS=refreshed-value")

    def test_discovers_authenticated_username_when_configured_name_is_wrong(self):
        session = FakeSession(
            [
                FakeResponse(
                    "<html><title>Not found</title></html>",
                    url="https://18comic.ink/user/wrong/achievements?type=coin",
                ),
                FakeResponse(
                    '<a href="/user/alice/notice">通知</a>',
                    url="https://18comic.ink/",
                ),
                FakeResponse(
                    ParserTests.HTML,
                    url="https://18comic.ink/user/alice/achievements?type=coin",
                ),
            ]
        )
        config = Config(username="wrong", cookie="AVS=session-value")
        client = ComicClient(config, session=session)

        tasks = client.fetch_tasks("coin")

        self.assertTrue(tasks["每日登入"].completed)
        self.assertEqual(
            session.requests[2][1],
            "https://18comic.ink/user/alice/achievements?type=coin",
        )


class RunTests(unittest.TestCase):
    def test_task_page_parse_failure_does_not_fail_successful_sign(self):
        class StubClient:
            def sign_daily(self):
                return DailySignResult(True, "获得签到奖励")

            def fetch_tasks(self, reward_type):
                raise VerificationError(
                    f"无法解析 {reward_type} 任务页，网站页面结构可能已经变化"
                )

        config = Config(username="alice", cookie="AVS=session-value")
        with patch("checkin.ComicClient", return_value=StubClient()):
            sign_result, coin_tasks, exp_tasks, warnings = run(config)

        self.assertTrue(sign_result.signed_now)
        self.assertEqual(coin_tasks, {})
        self.assertEqual(exp_tasks, {})
        self.assertEqual(len(warnings), 2)


class PushPlusTests(unittest.TestCase):
    def test_posts_markdown_notification(self):
        opener = FakeOpener(
            [FakeResponse(json.dumps({"code": 200, "data": "message-id"}))]
        )

        message_id = send_pushplus(
            "push-token",
            "签到成功",
            "任务已完成",
            opener=opener,
        )

        request, timeout = opener.requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://www.pushplus.plus/send")
        self.assertEqual(payload["token"], "push-token")
        self.assertEqual(payload["template"], "markdown")
        self.assertEqual(message_id, "message-id")
        self.assertEqual(timeout, 30.0)

    def test_rejects_pushplus_error_response(self):
        opener = FakeOpener(
            [FakeResponse(json.dumps({"code": 500, "msg": "bad token"}))]
        )

        with self.assertRaisesRegex(NotificationError, "bad token"):
            send_pushplus("push-token", "标题", "正文", opener=opener)


if __name__ == "__main__":
    unittest.main()
