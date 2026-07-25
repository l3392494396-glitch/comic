import json
import unittest

from checkin import (
    CheckinError,
    ComicClient,
    Config,
    ConfigError,
    MonthlyAction,
    NotificationError,
    TaskProgress,
    parse_monthly_action,
    parse_task_progress,
    send_pushplus,
)


class FakeResponse:
    def __init__(self, body, url="https://18comic.ink/", status=200):
        self.status_code = status
        self.url = url
        self.text = body
        self.headers = {}
        self.cookies = {}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.curl_options = {}

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


class ConfigTests(unittest.TestCase):
    def test_reads_cookie_only_configuration(self):
        config = Config.from_env(
            {"JM_USERNAME": "alice", "JM_COOKIE": "AVS=session-value"}
        )
        self.assertEqual(config.cookie, "AVS=session-value")

    def test_accepts_bare_avs_value_for_compatibility(self):
        config = Config.from_env(
            {"JM_USERNAME": "alice", "JM_COOKIE": "session-value"}
        )
        self.assertEqual(config.cookie, "AVS=session-value")

    def test_keeps_only_avs_from_full_cookie_header(self):
        config = Config.from_env(
            {
                "JM_USERNAME": "alice",
                "JM_COOKIE": "theme=light; AVS=session-value; remember=secret",
            }
        )
        self.assertEqual(config.cookie, "AVS=session-value")

    def test_rejects_missing_cookie(self):
        with self.assertRaises(ConfigError):
            Config.from_env({"JM_USERNAME": "alice"})


class ParserTests(unittest.TestCase):
    TASK_HTML = """
    <div class="tasks-row">
      <div class="tasks-row-title">每日登入</div>
      <div class="totoal-count">1 / 1</div>
    </div>
    """

    def test_parses_task_rows(self):
        tasks = parse_task_progress(self.TASK_HTML)
        self.assertEqual(tasks["每日登入"], TaskProgress(1, 1))

    def test_parses_monthly_checkin_link(self):
        action = parse_monthly_action(
            '<a href="/user/alice/bonus/open"><span>開啟本月簽到</span></a>',
            "https://18comic.ink/user/alice/bonus",
        )
        self.assertEqual(
            action,
            MonthlyAction(
                "GET",
                "https://18comic.ink/user/alice/bonus/open",
                {},
            ),
        )

    def test_parses_monthly_checkin_form(self):
        action = parse_monthly_action(
            """
            <form method="post" action="/ajax/bonus">
              <input type="hidden" name="token" value="csrf-value">
              <button name="open" value="1">开启本月签到</button>
            </form>
            """,
            "https://18comic.ink/user/alice/bonus",
        )
        self.assertEqual(action.method, "POST")
        self.assertEqual(action.data, {"token": "csrf-value", "open": "1"})

    def test_parses_monthly_checkin_onclick(self):
        action = parse_monthly_action(
            """<button onclick="$.post('/ajax/checkin')">開啟本月簽到</button>""",
            "https://18comic.ink/user/alice/bonus",
        )
        self.assertEqual(action.method, "POST")
        self.assertEqual(action.url, "https://18comic.ink/ajax/checkin")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(username="alice", cookie="AVS=session-value")

    def test_calls_personal_daily_sign_endpoint(self):
        session = FakeSession(
            [
                FakeResponse(
                    json.dumps({"msg": "签到完成"}),
                    url="https://18comic.ink/ajax/user_daily_sign",
                )
            ]
        )
        result = ComicClient(self.config, session=session).sign_daily()
        self.assertTrue(result.signed_now)
        self.assertEqual(session.requests[0][0], "POST")

    def test_runs_monthly_checkin_from_exact_button(self):
        page_url = "https://18comic.ink/user/alice/bonus"
        session = FakeSession(
            [
                FakeResponse(
                    '<a href="/user/alice/bonus/open">開啟本月簽到</a>',
                    url=page_url,
                ),
                FakeResponse("<button>今日已簽到</button>", url=page_url),
            ]
        )
        result = ComicClient(self.config, session=session).monthly_checkin()
        self.assertEqual(result.message, "今日已经签到")
        self.assertEqual(
            session.requests[1][1],
            "https://18comic.ink/user/alice/bonus/open",
        )

    def test_rejects_cross_site_monthly_action(self):
        page_url = "https://18comic.ink/user/alice/bonus"
        session = FakeSession(
            [
                FakeResponse(
                    '<a href="https://evil.example/collect">開啟本月簽到</a>',
                    url=page_url,
                )
            ]
        )
        with self.assertRaisesRegex(CheckinError, "以外"):
            ComicClient(self.config, session=session).monthly_checkin()


class PushPlusTests(unittest.TestCase):
    def test_posts_markdown_notification(self):
        session = FakeSession(
            [FakeResponse(json.dumps({"code": 200, "data": "message-id"}))]
        )
        message_id = send_pushplus(
            "push-token",
            "签到成功",
            "任务已完成",
            session=session,
        )
        self.assertEqual(message_id, "message-id")
        payload = session.requests[0][2]["json"]
        self.assertEqual(payload["template"], "markdown")

    def test_rejects_pushplus_error_response(self):
        session = FakeSession(
            [FakeResponse(json.dumps({"code": 500, "msg": "bad token"}))]
        )
        with self.assertRaisesRegex(NotificationError, "bad token"):
            send_pushplus("push-token", "标题", "正文", session=session)


if __name__ == "__main__":
    unittest.main()
