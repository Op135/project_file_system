import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from src import components


class _FakeDownloadResponse:
    def __init__(self, content: bytes = b"svn-content") -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeDownloadClient:
    def __init__(self, response: _FakeDownloadResponse, requested_urls: list[str]) -> None:
        self._response = response
        self._requested_urls = requested_urls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def get(self, url: str) -> _FakeDownloadResponse:
        self._requested_urls.append(url)
        return self._response


class SvnDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_uses_direct_connection_and_extended_timeout(self):
        client_options: dict[str, object] = {}
        requested_urls: list[str] = []
        response = _FakeDownloadResponse()

        def build_client(**kwargs):
            client_options.update(kwargs)
            return _FakeDownloadClient(response, requested_urls)

        with patch.object(components.httpx, "AsyncClient", side_effect=build_client):
            file_name, content = await components._fetch_svn_file_http_async(
                "https://svn.example.test/Product/project/file.hex",
                "user",
                "password",
            )

        self.assertEqual(file_name, "file.hex")
        self.assertEqual(content, b"svn-content")
        self.assertEqual(requested_urls, ["https://svn.example.test/Product/project/file.hex"])
        self.assertIs(client_options["trust_env"], False)
        self.assertEqual(client_options["timeout"], 60.0)

    async def test_failed_download_is_not_marked_as_downloaded(self):
        for component_class in (components.InteractiveButton, components.OverviewTableGroup):
            fake_component: Any = SimpleNamespace(
                trigger_download_svn_async=AsyncMock(return_value=False),
            )
            run_javascript = AsyncMock(return_value=None)

            with patch.object(components.ui, "run_javascript", run_javascript):
                await component_class.check_and_download_svn(fake_component, "https://svn/file.hex", "file.hex")

            run_javascript.assert_awaited_once_with('sessionStorage.getItem("downloaded_file.hex")')

    async def test_successful_download_is_marked_after_completion(self):
        for component_class in (components.InteractiveButton, components.OverviewTableGroup):
            fake_component: Any = SimpleNamespace(
                trigger_download_svn_async=AsyncMock(return_value=True),
            )
            run_javascript = AsyncMock(side_effect=[None, None])

            with patch.object(components.ui, "run_javascript", run_javascript):
                await component_class.check_and_download_svn(fake_component, "https://svn/file.hex", "file.hex")

            self.assertEqual(
                [call.args[0] for call in run_javascript.await_args_list],
                [
                    'sessionStorage.getItem("downloaded_file.hex")',
                    'sessionStorage.setItem("downloaded_file.hex", "true")',
                ],
            )


if __name__ == "__main__":
    unittest.main()
