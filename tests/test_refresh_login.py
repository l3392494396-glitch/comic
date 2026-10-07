import os
import tempfile
import unittest

from refresh_login import (
    RefreshConfig,
    RefreshError,
    extract_avs_cookie,
    find_login_form_action,
    main,
    normalize_cookie,
    refresh_cookie,
    write_env_file,
)


class FakeResponse:
    def __init__(self, body, url="https://18comic.ink/login", status=200, cookies=None):
        self.status_code = status
        self.url = url
        self.text = body
        self.headers = {}
        self.cookies = {} if cookies is None else cookies


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.cookies = {}

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


LOGIN_PAGE = (
    '<form method="post" action="/user/login">'
    '<input name="username"><input type="password" name="password">'
    "</form>"
)


class ParserTests(unittest.TestCase):
    def test_finds_login_form_action(self):
        self.assertEqual(find_login_form_action(LOGIN_PAGE), "/user/login")

    def test_ignores_form_without_password(self):
        html = '<form action="/search"><input name="username"></form>'
        self.assertIsNone(find_login_form_action(html))


class CookieTests(unittest.TestCase):
    def test_extracts_avs_from_mapping(self):
        self.assertEqual(extract_avs_cookie({"AVS": "abc"}), "abc")

    def test_extracts_avs_from_header(self):
        self.assertEqual(extract_avs_cookie("theme=dark; AVS=xyz; a=b"), "xyz")

    def test_handles_missing_cookie(self):
        self.assertIsNone(extract_avs_cookie({"other": "1"}))
        self.assertIsNone(extract_avs_cookie(None))

    def test_normalizes_bare_and_header_values(self):
        self.assertEqual(normalize_cookie("abc"), "AVS=abc")
        self.assertEqual(normalize_cookie("AVS=abc"), "AVS=abc")
        self.assertIsNone(normalize_cookie("other=1"))


class RefreshTests(unittest.TestCase):
    def test_signs_in_and_returns_fresh_cookie(self):
        session = FakeSession(
            [
                FakeResponse(LOGIN_PAGE, url="https://18comic.ink/login"),
                FakeResponse(
                    "welcome",
                    url="https://18comic.ink/",
                    cookies={"AVS": "fresh-value"},
                ),
            ]
        )
        cookie = refresh_cookie(
            RefreshConfig(username="alice", password="secret"), session=session
        )
        self.assertEqual(cookie, "AVS=fresh-value")
        self.assertEqual(session.requests[1][0], "POST")
        self.assertEqual(session.requests[1][1], "https://18comic.ink/user/login")
        self.assertEqual(session.requests[1][2]["data"]["password"], "secret")

    def test_raises_when_login_form_is_missing(self):
        session = FakeSession(
            [
                FakeResponse("<html>login</html>", url="https://18comic.ink/login"),
                FakeResponse("<html>login</html>", url="https://18comic.ink/user/login"),
            ]
        )
        with self.assertRaises(RefreshError):
            refresh_cookie(
                RefreshConfig(username="alice", password="secret"), session=session
            )


class EnvFileTests(unittest.TestCase):
    def test_appends_env_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env")
            write_env_file(path, "JM_COOKIE", "AVS=abc")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(
                    handle.read().splitlines(), ["JM_COOKIE=AVS=abc"]
                )

    def test_main_records_fallback_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = os.path.join(tmp, "env")
            env = {
                "JM_USERNAME": "alice",
                "JM_COOKIE": "legacy-value",
                "GITHUB_ENV": env_file,
            }
            self.assertEqual(main(env), 0)
            with open(env_file, encoding="utf-8") as handle:
                self.assertEqual(
                    handle.read().splitlines(), ["JM_COOKIE=AVS=legacy-value"]
                )

    def test_main_fails_without_credentials(self):
        self.assertEqual(main({"JM_USERNAME": "alice"}), 1)


if __name__ == "__main__":
    unittest.main()
