"""文件性概述健康快照的关键路径验证。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, patch

from src import overview_file_health as health
from src.utils import build_overview_page_url
from tests.test_error_management_concurrency import load_isolated_db_storage


class OverviewFileHealthTests(unittest.IsolatedAsyncioTestCase):
    def test_health_link_only_requests_view_focus(self) -> None:
        url = build_overview_page_url(
            review=False, project_name="P1", focus_label="docs", focus_chip_id="c1"
        )
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["focus_label"], ["docs"])
        self.assertEqual(query["focus_chip_id"], ["c1"])
        self.assertNotIn("correction_label", query)

    async def test_reminder_claim_is_atomic_across_connections(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "health.db"
            first = load_isolated_db_storage("health_claim_first", database)
            second = load_isolated_db_storage("health_claim_second", database)
            await first.init_db()
            await second.init_db()
            try:
                with patch.object(health, "db_storage", first):
                    result = await asyncio.gather(*(
                        health.claim_file_health_reminder("张三", "2026-10-23") for _ in range(2)
                    ))
                self.assertEqual(sorted(result), [False, True])
                # 第二个连接持有旧缓存，仍须从数据库最新值判断。
                with patch.object(health, "db_storage", second):
                    self.assertFalse(await health.claim_file_health_reminder("张三", "2026-10-23"))
                self.assertTrue(await first.get_fresh_item(health.OVERVIEW_FILE_HEALTH_REMINDER_KEY))
            finally:
                await first.close_db()
                await second.close_db()

    async def test_reminder_follows_current_owner(self) -> None:
        fake_app = SimpleNamespace(storage=SimpleNamespace(general={
            "overview_role": {"P1": {"结构": {"latest_user": "最近：李四"}}},
        }))
        snapshot = {"items": [{
            "project": "P1", "role": "结构", "owner": "张三", "healthy": False,
            "label": "docs", "chip_id": "c1",
        }]}
        with patch.object(health, "app", fake_app):
            self.assertEqual(health.unhealthy_items_for_owner(snapshot, "张三"), [])
            self.assertEqual(len(health.unhealthy_items_for_owner(snapshot, "李四")), 1)

    async def test_scan_only_includes_active_files_and_persists_complete_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            existing_file = Path(folder) / "有效.pdf"
            existing_file.write_bytes(b"test")
            project_data = {
                "doc": {
                    "good": {"type": "file", "content": existing_file.name, "select_activ_dic": {"2.0": True}},
                    "missing": {"type": "image", "content": "缺失.png", "select_activ_dic": {"2.0": True}},
                    "old": {"type": "video", "content": "旧视频.mp4", "select_activ_dic": {"1.0": True}},
                    "pending": {"type": "file", "content": "待定.pdf", "select_activ_dic": {"2.0": None}},
                    "disabled": {"type": "file", "content": "已停用.pdf", "enabled": False, "select_activ_dic": {"2.0": True}},
                    "text": {"type": "text", "content": "普通文字", "select_activ_dic": {"2.0": True}},
                },
                "svn": {
                    "remote": {"type": "svn", "content": "不存在的远程文件.pdf", "select_activ_dic": {"2.0": True}},
                },
                "search": {
                    "found": {"type": "search", "content": "有效硬件文件.pdf", "select_activ_dic": {"2.0": True}},
                    "lost": {"type": "search", "content": "缺失硬件文件.pdf", "select_activ_dic": {"2.0": True}},
                },
            }
            fake_app = SimpleNamespace(storage=SimpleNamespace(general={
                "project_req_max_ver": {"P1": "2.0"},
                "over_config_data_flat": {
                    "doc": {"role": "结构", "title": "设计文件", "upload_path": folder},
                    "svn": {"role": "结构", "title": "SVN 文件"},
                    "search": {"role": "硬件", "title": "硬件检索文件", "upload_path": folder},
                },
                "overview_role": {"P1": {
                    "结构": {"latest_user": "最近：张三"},
                    "硬件": {"latest_user": "最近：李四"},
                }},
            }))
            saved = AsyncMock(return_value=True)
            with (
                patch.object(health, "app", fake_app),
                patch.object(health.db_storage, "get_item", return_value=project_data),
                patch.object(health.db_storage, "set_item", saved),
                patch.object(health, "validate_svn_url", AsyncMock(return_value=(False, "", "", "链接失效"))),
                patch.object(
                    health,
                    "validate_search_path",
                    AsyncMock(side_effect=lambda content, _config, _projects: (
                        content == "有效硬件文件.pdf", "", "", "", "" if content == "有效硬件文件.pdf" else "未找到文件"
                    )),
                ),
            ):
                snapshot = await health.scan_overview_file_health()

            items = snapshot["items"]
            assert isinstance(items, list)
            self.assertEqual(len(items), 5)
            self.assertEqual({item["owner"] for item in items}, {"张三", "李四"})
            self.assertEqual({item["content"] for item in items if item["healthy"]}, {"有效.pdf", "有效硬件文件.pdf"})
            self.assertEqual(
                {item["content"] for item in items if not item["healthy"]},
                {"缺失.png", "不存在的远程文件.pdf", "缺失硬件文件.pdf"},
            )
            self.assertEqual(saved.await_count, 1)
            assert saved.await_args is not None
            self.assertEqual(saved.await_args.args[0], health.OVERVIEW_FILE_HEALTH_KEY)
            grouped = health.summarize_file_health(items, "role")
            self.assertEqual(grouped[0]["total"], 3)
            self.assertEqual(len(grouped[0]["unhealthy"]), 2)


if __name__ == "__main__":
    unittest.main()
