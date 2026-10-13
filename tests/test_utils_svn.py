import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import utils


class _FakeResponse:
    def __init__(self, status_code: int, content_type: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": content_type} if content_type else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _FakeHttpClient:
    def __init__(self, responses: list[_FakeResponse], requested_urls: list[str]) -> None:
        self._responses = responses
        self._requested_urls = requested_urls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs):
        self._requested_urls.append(url)
        return self._responses.pop(0)


class ValidateSvnUrlTests(unittest.IsolatedAsyncioTestCase):
    def _fake_app(self, project_summary=None):
        return SimpleNamespace(
            storage=SimpleNamespace(
                general={
                    "project_summary": project_summary
                    or {"项目A": {"state": "研发", "project": "RFFM-1519"}},
                },
            )
        )

    def _config(self) -> dict[str, object]:
        return {
            "upload_path": "https://svn.example.test/svn",
            "state_path": {"研发": "Control/Controlled"},
            "search_scope_regular": r"((?:RF|IT)[A-Za-z]{2})[-_]?([0-9A-Z]{4}[A-Z]?)",
            "search_folder_according": [],
            "search_hierarchy": ["src"],
            "fallback_folder_path": "Shared/Firmware",
        }

    async def test_regular_path_remains_first_choice(self):
        requested_urls: list[str] = []
        client = _FakeHttpClient([_FakeResponse(200, "application/octet-stream")], requested_urls)

        with (
            patch.object(utils, "app", self._fake_app()),
            patch.object(utils.httpx, "AsyncClient", return_value=client),
        ):
            valid, url, file_type, _ = await utils.validate_svn_url(
                "RFAB1234-firmware.bin", self._config(), ["项目A"]
            )

        self.assertTrue(valid)
        self.assertEqual(file_type, "application/octet-stream")
        self.assertEqual(
            url,
            "https://svn.example.test/svn/Control/Controlled/RFAB-1234/src/RFAB1234-firmware.bin",
        )
        self.assertEqual(requested_urls, [url])

    async def test_fallback_path_is_checked_after_regular_path_misses(self):
        requested_urls: list[str] = []
        client = _FakeHttpClient(
            [_FakeResponse(404), _FakeResponse(200, "application/pdf")],
            requested_urls,
        )

        with (
            patch.object(utils, "app", self._fake_app()),
            patch.object(utils.httpx, "AsyncClient", return_value=client),
        ):
            valid, url, file_type, _ = await utils.validate_svn_url(
                "RFAB1234-manual.pdf", self._config(), ["项目A"]
            )

        fallback_url = (
            "https://svn.example.test/svn/Control/Controlled/Shared/Firmware/RFAB1234-manual.pdf"
        )
        self.assertTrue(valid)
        self.assertEqual(url, fallback_url)
        self.assertEqual(file_type, "application/pdf")
        self.assertEqual(
            requested_urls,
            [
                "https://svn.example.test/svn/Control/Controlled/RFAB-1234/src/RFAB1234-manual.pdf",
                fallback_url,
            ],
        )

    async def test_fallback_path_supports_filename_that_does_not_match_regular_expression(self):
        requested_urls: list[str] = []
        client = _FakeHttpClient([_FakeResponse(200, "application/octet-stream")], requested_urls)

        with (
            patch.object(utils, "app", self._fake_app()),
            patch.object(utils.httpx, "AsyncClient", return_value=client),
        ):
            valid, url, _, _ = await utils.validate_svn_url("common.bin", self._config(), ["项目A"])

        self.assertTrue(valid)
        self.assertEqual(
            url,
            "https://svn.example.test/svn/Control/Controlled/Shared/Firmware/common.bin",
        )
        self.assertEqual(requested_urls, [url])

    async def test_other_project_is_resolved_from_filename_instead_of_current_project(self):
        requested_urls: list[str] = []
        client = _FakeHttpClient([_FakeResponse(200, "application/octet-stream")], requested_urls)
        config = self._config()
        config["fallback_folder_path"] = ""

        with (
            patch.object(
                utils,
                "app",
                self._fake_app(
                    {
                        "RM3000": {"state": "研发", "project": "RM3000"},
                        "RM5000": {"state": "研发", "project": "RM5000"},
                    }
                ),
            ),
            patch.object(utils.httpx, "AsyncClient", return_value=client),
        ):
            valid, url, _, _ = await utils.validate_svn_url(
                "RM3000-firmware.bin", config, ["RM5000"]
            )

        self.assertTrue(valid)
        self.assertEqual(
            url,
            "https://svn.example.test/svn/Control/Controlled/RM3000/src/RM3000-firmware.bin",
        )
        self.assertEqual(requested_urls, [url])

    async def test_regular_project_does_not_bypass_filename_rule(self):
        config = self._config()
        config["fallback_folder_path"] = ""

        with patch.object(
            utils,
            "app",
            self._fake_app(
                {"RFFM-1519-A": {"state": "研发", "project": "RFFM-1519"}}
            ),
        ):
            valid, url, _, message = await utils.validate_svn_url(
                "unexpected-firmware.bin", config, ["RFFM-1519-A"]
            )

        self.assertFalse(valid)
        self.assertEqual(url, "")
        self.assertIn("未能生成有效", message)

    async def test_equal_length_other_project_matches_are_rejected_as_ambiguous(self):
        config = self._config()
        config["fallback_folder_path"] = ""

        with patch.object(
            utils,
            "app",
            self._fake_app(
                {
                    "RM3000": {"state": "研发", "project": "RM3000"},
                    "RM5000": {"state": "研发", "project": "RM5000"},
                }
            ),
        ):
            valid, url, _, message = await utils.validate_svn_url(
                "RM3000-RM5000-firmware.bin", config, ["RM5000"]
            )

        self.assertFalse(valid)
        self.assertEqual(url, "")
        self.assertIn("同时匹配多个", message)


if __name__ == "__main__":
    unittest.main()
