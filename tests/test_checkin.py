import json
import unittest

from checkin import (
    CheckinError,
    ComicClient,
    Config,
    ConfigError,
    LoginError,
    NotificationError,
    TaskProgress,
    parse_task_progress,
    send_pushplus,
)


class FakeHeaders(dict):
    def get_content_charset(self):
        return "utf-8"


class FakeResponse:
    def __init__(self, body, url="https://18comic.ink/login", status=200):
        self._body = body.encode("utf-8")
        self._url = url
        self.status = status
        self.status_code = status
        self.url = url
        self.text = body
        self.headers = FakeHeaders()

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
        return self.responses.pop(0)


class ConfigTests(unittest.TestCase):
    def test_reads_required_values(self):
        config = Config.from_env(
            {"JM_USERNAME": "alice", "JM_PASSWORD": "secret"}
        )
        self.assertEqual(config.username, "alice")
        self.assertEqual(config.base_url, "https://18comic.ink")

    def test_rejects_missing_password(self):
        with self.assertRaises(ConfigError):
            Config.from_env({"JM_USERNAME": "alice"})

    def test_accepts_cookie_without_password(self):
        config = Config.from_env(
            {"JM_USERNAME": "alice", "JM_COOKIE": "AVS=session-value"}
        )
        self.assertEqual(config.password, "")
        self.assertEqual(config.cookie, "AVS=session-value")

    def test_rejects_cookie_with_newline(self):
        with self.assertRaisesRegex(ConfigError, "换行符"):
            Config.from_env(
                {"JM_USERNAME": "alice", "JM_COOKIE": "AVS=value\r\nevil=1"}
            )

    def test_rejects_non_https_base_url(self):
        with self.assertRaises(ConfigError):
            Config.from_env(
                {
                    "JM_USERNAME": "alice",
                    "JM_PASSWORD": "secret",
                    "JM_BASE_URL": "http://example.com",
                }
            )


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


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(username="alice", password="secret")

    def test_login_posts_expected_form(self):
        session = FakeSession(
            [FakeResponse(json.dumps({"status": 1, "errors": "/"}))]
        )
        client = ComicClient(self.config, session=session)

        client.login()

        method, url, kwargs = session.requests[0]
        posted = kwargs["data"]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://18comic.ink/login")
        self.assertEqual(posted["username"], "alice")
        self.assertEqual(posted["password"], "secret")
        self.assertEqual(posted["submit_login"], "1")
        self.assertEqual(kwargs["timeout"], 30.0)

    def test_login_rejects_error_response(self):
        session = FakeSession(
            [FakeResponse(json.dumps({"status": 2, "errors": "bad login"}))]
        )
        client = ComicClient(self.config, session=session)

        with self.assertRaisesRegex(LoginError, "bad login"):
            client.login()

    def test_reports_403_diagnostics(self):
        response = FakeResponse("Restricted Access!", status=403)
        response.headers["server"] = "cloudflare"
        session = FakeSession([response])
        client = ComicClient(self.config, session=session)

        with self.assertRaisesRegex(CheckinError, "GitHub Actions"):
            client.login()

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
