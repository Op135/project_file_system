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
    def _fake_app(self):
        return SimpleNamespace(
            storage=SimpleNamespace(
                general={"project_summary": {"项目A": {"state": "研发"}}},
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


if __name__ == "__main__":
    unittest.main()
