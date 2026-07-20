import json
import unittest
from urllib.parse import parse_qs

from checkin import (
    ComicClient,
    Config,
    ConfigError,
    LoginError,
    TaskProgress,
    parse_task_progress,
)


class FakeHeaders(dict):
    def get_content_charset(self):
        return "utf-8"


class FakeResponse:
    def __init__(self, body, url="https://18comic.ink/login", status=200):
        self._body = body.encode("utf-8")
        self._url = url
        self.status = status
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
        opener = FakeOpener(
            [FakeResponse(json.dumps({"status": 1, "errors": "/"}))]
        )
        client = ComicClient(self.config, opener=opener)

        client.login()

        request, timeout = opener.requests[0]
        posted = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://18comic.ink/login")
        self.assertEqual(posted["username"], ["alice"])
        self.assertEqual(posted["password"], ["secret"])
        self.assertEqual(posted["submit_login"], ["1"])
        self.assertEqual(timeout, 30.0)

    def test_login_rejects_error_response(self):
        opener = FakeOpener(
            [FakeResponse(json.dumps({"status": 2, "errors": "bad login"}))]
        )
        client = ComicClient(self.config, opener=opener)

        with self.assertRaisesRegex(LoginError, "bad login"):
            client.login()


if __name__ == "__main__":
    unittest.main()
