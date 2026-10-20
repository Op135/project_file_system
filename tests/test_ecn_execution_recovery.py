"""使用真实按钮处理函数和内存替身复现执行中断，不操作运行数据库。"""

import ast
import asyncio
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from src.modules.ecn import execution_panel
from src.ecn_management_config import ECNState, ECN_EXECUTION_STAGE_READY_TO_CLOSE


class ECNExecutionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.record: dict[str, Any] = {
            "ecn_id": "AUDIT_CANCEL",
            "workflow": {"current_state": ECNState.ECN_EXECUTING},
            "execution_info": {"stage": execution_panel.ECN_EXECUTION_STAGE_ASSISTANT},
            "change_items": [],
        }
        self.active: set[str] = set()
        self.no_update = object()
        self.executor = AsyncMock(return_value={})
        self.render = Mock()
        self.notify = Mock()
        self.read = Mock(side_effect=lambda path: copy.deepcopy(self.record))

        async def atomic(path, update):
            return update(self.record) is not self.no_update

        # 提取当前实际嵌套处理函数，保留其控制流；仅替换UI、权限和存储边界。
        source = Path(execution_panel.__file__).read_text(encoding="utf-8-sig")
        handler = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_overview_execution"
        )
        namespace: dict[str, Any] = execution_panel.__dict__.copy()
        namespace.update(
            ui=SimpleNamespace(context=SimpleNamespace(client=object())),
            app=SimpleNamespace(state=SimpleNamespace(user_service=None)),
            local_data=self.record, current_user="audit", current_role="audit",
            get_active_ecn_actor_role=lambda *args, **kwargs: "audit",
            can_execute_ecn_assistant_stage=lambda *args, **kwargs: True,
            can_view_ecn=lambda *args, **kwargs: True,
            is_ecn_assistant_execution_ready=lambda *args: True,
            atomic_ecn_deep_update=atomic,
            ACTIVE_ECN_OVERVIEW_EXECUTIONS=self.active,
            notify_execution_safely=self.notify,
            sync_execution_local_data=Mock(), render_execution_tab=self.render,
            db_storage=SimpleNamespace(ATOMIC_NO_UPDATE=self.no_update, get_deep_item=self.read),
            execute_ecn_overview_schemes=self.executor,
        )
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "actual_execution_handler", "exec"), namespace)
        self.run_execution = namespace["run_overview_execution"]
        self.namespace = namespace

    async def test_revoked_view_blocks_start_even_when_assistant_permission_remains(self):
        self.namespace["can_view_ecn"] = lambda *args, **kwargs: False
        before = copy.deepcopy(self.record)
        await self.run_execution()
        self.assertEqual(self.record, before)
        self.assertFalse(self.active)
        self.executor.assert_not_awaited()

    async def test_revoked_view_blocks_material_confirmation_before_any_mutation(self):
        source = Path(execution_panel.__file__).read_text(encoding="utf-8-sig")
        handler = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.FunctionDef) and node.name == "update_confirmation")
        blocked = {"reason": ""}
        self.namespace.update(can_view_ecn=lambda *args, **kwargs: False, blocked=blocked)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "actual_material_handler", "exec"), self.namespace)
        before = copy.deepcopy(self.record)
        result = self.namespace["update_confirmation"](self.record)
        self.assertIs(result, self.no_update)
        self.assertIn("ECN查看权限", blocked["reason"])
        self.assertEqual(self.record, before)

    async def test_cancel_releases_active_marker_and_expired_execution_can_resume(self):
        self.executor.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_execution()
        self.assertFalse(self.active)
        self.assertEqual(self.record["execution_info"]["stage"], execution_panel.ECN_EXECUTION_STAGE_OVERVIEW_RUNNING)
        # 保留数据库租约，避免取消时尚未结束的落盘与重试重叠。
        self.executor.reset_mock()
        await self.run_execution()
        self.executor.assert_not_awaited()
        self.record["execution_info"]["overview_started_epoch"] = 0
        self.executor.side_effect = None
        await self.run_execution()
        self.executor.assert_awaited_once()
        self.assertFalse(self.active)
        self.assertEqual(self.record["workflow"]["current_state"], ECNState.ECN_EXECUTING)
        self.assertEqual(self.record["execution_info"]["stage"], ECN_EXECUTION_STAGE_READY_TO_CLOSE)

    async def test_initial_render_failure_also_releases_active_marker(self):
        self.render.side_effect = RuntimeError("render failed")
        with self.assertRaisesRegex(RuntimeError, "render failed"):
            await self.run_execution()
        self.assertFalse(self.active)
        self.executor.assert_not_awaited()

    async def test_missing_record_early_return_releases_active_marker(self):
        self.read.side_effect = None
        self.read.return_value = None
        await self.run_execution()
        self.assertFalse(self.active)
        self.executor.assert_not_awaited()

    async def test_successful_execution_releases_marker(self):
        await self.run_execution()
        self.assertFalse(self.active)
        self.assertEqual(self.record["workflow"]["current_state"], ECNState.ECN_EXECUTING)
        self.assertEqual(self.record["execution_info"]["stage"], ECN_EXECUTION_STAGE_READY_TO_CLOSE)
