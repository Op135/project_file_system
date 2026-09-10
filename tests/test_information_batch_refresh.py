import ast
import asyncio
import copy
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


class InformationBatchRefreshTests(unittest.IsolatedAsyncioTestCase):
    """隔离数据库和页面初始化，验证真实审批回调的刷新行为。"""

    async def run_approval(self, *, allowed=True, execution_error=False):
        source = Path(__file__).resolve().parents[1] / "src" / "pages" / "information.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        callback = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "approve_batch_request"
        )
        request = {"status": "pending", "review_log": []}

        async def atomic_update(path, updater):
            updater(copy.deepcopy(request))

        ui = MagicMock()
        refresh = MagicMock()
        update = AsyncMock()
        execute = AsyncMock(return_value={"successes": ["P1"], "failed": [], "message": "完成"})
        if execution_error:
            execute.side_effect = RuntimeError("模拟执行失败")
        namespace = {
            "asyncio": asyncio,
            "copy": copy,
            "datetime": datetime,
            "batch_request_dialog": MagicMock(),
            "_batch_overview_review_locks": defaultdict(asyncio.Lock),
            "db_storage": SimpleNamespace(atomic_deep_update=atomic_update, ATOMIC_NO_UPDATE=object()),
            "BATCH_OVERVIEW_REQUESTS_KEY": "batch_requests",
            "can_review_batch_overview_request": lambda *args: allowed,
            "current_user": "审批人",
            "current_role": "研发经理",
            "ui": ui,
            "execute_batch_overview_request": execute,
            "update_batch_overview_request": update,
            "refresh_batch_request_sections": refresh,
            "logger": MagicMock(),
        }
        exec(compile(ast.Module(body=[callback], type_ignores=[]), str(source), "exec"), namespace)
        await namespace["approve_batch_request"]("request-1")
        refresh.assert_called_once_with()
        ui.navigate.reload.assert_not_called()
        ui.timer.assert_not_called()
        return execute, update, ui

    async def test_approval_updates_status_and_refreshes_locally(self):
        execute, update, ui = await self.run_approval()
        execute.assert_awaited_once()
        call = update.await_args
        assert call is not None
        self.assertEqual(call.args[1]["status"], "approved")
        ui.notification.return_value.dismiss.assert_called_once()

    async def test_execution_failure_also_refreshes_locally(self):
        _, update, ui = await self.run_approval(execution_error=True)
        call = update.await_args
        assert call is not None
        self.assertEqual(call.args[1]["status"], "failed")
        ui.notification.return_value.dismiss.assert_called_once()

    async def test_lost_review_permission_refreshes_without_execution(self):
        execute, update, _ = await self.run_approval(allowed=False)
        execute.assert_not_awaited()
        update.assert_not_awaited()
