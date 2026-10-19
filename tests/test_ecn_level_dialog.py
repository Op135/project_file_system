"""等级判定弹窗必须使用打开时的快照，而非保存时的轮询数据。"""

import ast
import copy
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from src.modules.ecn import detail
from src.modules.ecn.editing import ECNResult


class ECNLevelDialogTests(unittest.IsolatedAsyncioTestCase):
    def test_own_action_does_not_claim_reopen_notice(self):
        state = {"stage_change": False}
        self.assertFalse(
            detail.claim_stage_change_notice(state, action_in_progress=True, detail_closed=False)
        )
        self.assertFalse(state["stage_change"])

    def test_external_stage_change_is_claimed_only_once(self):
        state = {"stage_change": False}
        self.assertTrue(
            detail.claim_stage_change_notice(state, action_in_progress=False, detail_closed=False)
        )
        self.assertFalse(
            detail.claim_stage_change_notice(state, action_in_progress=False, detail_closed=False)
        )

    def test_closed_detail_does_not_emit_notice(self):
        self.assertFalse(
            detail.claim_stage_change_notice(
                {"stage_change": False}, action_in_progress=False, detail_closed=True
            )
        )

    async def test_polling_does_not_replace_level_dialog_baseline(self):
        source = Path(detail.__file__).read_text(encoding="utf-8-sig")
        handler = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.FunctionDef) and node.name == "open_level_dialog")
        local_data = {"ecn_id": "TEST", "workflow": {"ecn_level": "general", "ecn_level_decisions": []}}
        original = copy.deepcopy(local_data)
        fake_ui = MagicMock()
        save = AsyncMock(return_value=ECNResult(False, "conflict"))
        namespace: dict[str, Any] = detail.__dict__.copy()
        namespace.update(ui=fake_ui, local_data=local_data, level_timing="before_scheme_review", current_user="writer", current_role="研发", set_ecn_level=save)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "actual_level_dialog", "exec"), namespace)
        namespace["open_level_dialog"]()
        submit = next(call.kwargs["on_click"] for call in fake_ui.button.call_args_list if call.args and call.args[0] == "保存等级")
        fake_ui.radio.return_value.bind_value.call_args.args[0]["code"] = "simple"
        # 模拟协同轮询覆盖页面工作数据；已打开弹窗的审核基线不得跟着变。
        local_data["workflow"].update(ecn_level="complex", ecn_level_decisions=[{"to": "complex"}])
        await submit()
        save.assert_awaited_once()
        self.assertEqual(save.call_args.args[1], original)
        self.assertEqual(save.call_args.args[2], "simple")
