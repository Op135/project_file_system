"""ECN跨阶段验收：真实身份、审批待办和隔离SQLite，禁止访问运行数据库或发送通知。"""

import ast
import copy
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nicegui.elements.upload_files import SmallFileUpload

from src.approval_workflow import ECN_EXECUTION_EVENT_BY_LEVEL
from src.ecn_management_config import ECNState, build_ecn_scheme_snapshot
from src.modules.ecn import actions, execution_panel, execution_review, overview_execution, repository
from src.modules.ecn import validation_reports
from src.modules.ecn import attachments
from src.modules.ecn.models import generate_initial_ecn_data
from src.modules.ecn.special_tasks import update_special_task
from tests import test_approval_workflow as fixtures
from tests.test_error_management_concurrency import load_isolated_db_storage


def ordinary_scheme() -> dict:
    return {
        "item_id": "ordinary", "author": "张三", "role": "申请岗位", "type": "text_desc",
        "scheme_category": "ordinary_document", "projects": ["P1"], "req_idxs": [1],
        "linked_docs": [], "linked_materials": [], "change_type": "其它",
        "old_content": "旧要求", "new_content": "落实新要求", "attachments": [],
    }


class ECNAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ApprovalWorkflowTests()
        self.fixture.setUp()
        self.service = self.fixture.service
        self.storage = load_isolated_db_storage("ecn_acceptance", Path(self.service.identity_store.db_path))
        await self.storage.init_db()
        permissions = [str(p["code"]) for p in self.service.list_permissions() if str(p["code"]).startswith("ecn.")]
        for position in (self.fixture.requester_position_id, self.fixture.approver_position_id, self.fixture.observer_position_id):
            self.service.set_position_permissions(position, permissions)
        for event in ("ecr_review", "ecr_review_simple", "scheme_review", "scheme_review_simple"):
            permission = "ecn.ecr.approve" if event.startswith("ecr") else "ecn.scheme.approve"
            self.publish(event, permission, [self.node("first", "李四", permission), self.node("second", "王五", permission)])
        for event in ECN_EXECUTION_EVENT_BY_LEVEL.values():
            permission = "ecn.execution.purchase.confirm"
            self.publish(event, permission, [self.node("first", "李四", permission), self.node("second", "王五", permission, stage=1)])
        self.summary = {"P1": {"state": "量产"}, "P2": {"state": "研发"}}
        self.fake_app = SimpleNamespace(
            state=SimpleNamespace(user_service=self.service),
            storage=SimpleNamespace(general={"project_summary": self.summary, "project_req_max_ver": {"P1": "1.0"}}),
        )
        self.patches = ExitStack()
        self.patches.enter_context(patch.object(attachments, "attachment_root", return_value=Path(self.service.identity_store.db_path).parent / "attachments"))
        for module in (repository, overview_execution):
            self.patches.enter_context(patch.object(module, "db_storage", self.storage))
        self.patches.enter_context(patch.object(overview_execution, "app", self.fake_app))
        # 只替换外部投递；审批、身份、覆盖、执行与文件/数据库事务均使用真实实现。
        self.patches.enter_context(patch.object(actions, "schedule_background_task", side_effect=lambda coro, *_: coro.close()))
        self.value: dict = {}

    async def asyncTearDown(self):
        self.patches.close()
        await self.storage.close_db()
        self.fixture.tearDown()

    def node(self, key, username, permission, stage=0):
        user = self.service.get_user(username)
        assert user is not None
        return {"node_key": key, "name": key, "responsible_key": key, "stage_index": stage,
                "project_scoped": False, "approval_mode": "any", "required_permission_code": permission,
                "approver": {"strategy": "users", "user_ids": [user["user_id"]]}}

    def publish(self, event, permission, nodes):
        workflow_id, _ = self.service.save_approval_workflow_draft(
            code=f"acceptance.{event}", module="ecn", event=event, name=f"验收 {event}",
            priority=1, condition={}, approver={"nodes": nodes}, required_permission_code=permission,
            approval_mode="sequential", actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")

    def accept(self, result):
        self.assertTrue(result.ok, result.message)
        assert result.record is not None
        self.value = result.record
        return result.record

    async def fresh(self):
        data = await self.storage.get_fresh_item("ecn_management_data", {})
        self.value = data[self.value["ecn_id"]]
        return self.value

    async def action(self, name, user="张三", **kwargs):
        return await actions.execute_action(
            copy.deepcopy(self.value), copy.deepcopy(self.value), name, user, "验收岗位",
            user_service=self.service, storage=self.storage, project_summary=self.summary, **kwargs,
        )

    async def create(self):
        self.value = generate_initial_ecn_data("张三", "申请岗位", {}, user_service=self.service)
        self.value["basic_info"].update(reason_desc="验收变更", requirements=[{"idx": 1, "content": "改进"}], requirement_date="2026-10-31")
        self.value["basic_info"]["reasons"]["设计改善"] = True
        self.value["target_projects"] = ["P1"]
        self.accept(await self.action("save_draft", is_new=True))
        self.assertTrue(self.value["ecn_id"])

    async def approve_phase(self):
        self.accept(await self.action("approve", "李四"))
        self.assertEqual(self.value["workflow"]["pending_roles"], ["王五"])
        self.accept(await self.action("approve", "王五"))

    async def scheming(self):
        await self.create()
        self.accept(await self.action("submit_ecr"))
        await self.approve_phase()
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.ECN_SCHEMING)

    async def save_scheme(self, scheme, original=None):
        self.accept(await actions.edit_scheme(self.value["ecn_id"], self.value, scheme, original, "张三", "申请岗位", user_service=self.service, storage=self.storage))

    async def ready(self):
        self.accept(await actions.set_participant_status(self.value["ecn_id"], self.value, "张三", "申请岗位", "confirmed", user_service=self.service, storage=self.storage))
        self.accept(await actions.set_trial_production_decision(self.value["ecn_id"], self.value, False, "张三", "申请岗位", user_service=self.service, storage=self.storage))

    async def review(self):
        await self.ready()
        self.accept(await self.action("initiate_scheme_review"))
        await self.approve_phase()

    async def special(self, key, user="张三", **kwargs):
        info = self.value["execution_info"]
        baseline = info["erp_confirmation"] if key == "__erp__" else info["ordinary_confirmations"][key]
        self.accept(await update_special_task(self.value["ecn_id"], key, copy.deepcopy(baseline), username=user, role="验收岗位", service=self.service, storage=self.storage, **kwargs))

    def handler_namespace(self, user="张三"):
        # 执行调度仍嵌在UI内，提取实际函数；只替换重绘和客户端对象，不模拟业务判断。
        namespace = execution_panel.__dict__.copy()
        namespace.update(
            ui=SimpleNamespace(context=SimpleNamespace(client=object())), app=self.fake_app,
            db_storage=self.storage, local_data=copy.deepcopy(self.value), current_user=user, current_role="验收岗位",
            atomic_ecn_deep_update=repository.atomic_ecn_deep_update,
            ACTIVE_ECN_OVERVIEW_EXECUTIONS=set(), notify_execution_safely=Mock(),
            sync_execution_local_data=Mock(), render_execution_tab=Mock(),
            execution_scheme_no=lambda key: key,
        )
        return namespace

    def extract(self, name, namespace):
        tree = ast.parse(Path(execution_panel.__file__).read_text(encoding="utf-8-sig"))
        function = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        exec(compile(ast.Module(body=[function], type_ignores=[]), "ecn_acceptance_actual_handler", "exec"), namespace)
        return namespace[name]

    async def overview(self):
        namespace = self.handler_namespace()
        await self.extract("run_overview_execution", namespace)()
        await self.fresh()
        return namespace

    async def material(self, key, user, *, confirmed=True, final=False):
        blocked = {"reason": "", "requires_final_confirmation": False}
        namespace = self.handler_namespace(user)
        namespace.update(item_id="material", confirmation_key=key, confirmed=confirmed,
                         final_confirmation_acknowledged=final, blocked=blocked, completed_route={}, operation_time="2026-10-17 12:00:00")
        handler = self.extract("update_confirmation", namespace)
        saved = await repository.atomic_ecn_deep_update(["ecn_management_data", self.value["ecn_id"]], handler)
        self.assertTrue(saved)
        await self.fresh()
        return blocked

    async def test_ordinary_full_lifecycle_transfer_reopen_and_verify(self):
        await self.scheming()
        await self.save_scheme(ordinary_scheme())
        await self.review()
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.ECN_EXECUTING)
        await self.special("ordinary", assignee="李四")
        await self.special("__erp__", confirmed=True)
        await self.overview()
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.ECN_EXECUTING)
        await self.special("ordinary", "李四", confirmed=True)
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.CLOSED)
        self.accept(await execution_review.verify_execution_result(self.value["ecn_id"], "王五", "复核", user_service=self.service, storage=self.storage))
        confirmation = copy.deepcopy(self.value["execution_info"]["ordinary_confirmations"]["ordinary"])
        self.accept(await execution_review.revoke_execution_confirmation(self.value["ecn_id"], "special", "ordinary", "", confirmation, "需重新落实", [], "王五", "复核", user_service=self.service, storage=self.storage))
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.ECN_EXECUTING)
        await self.special("ordinary", "李四", confirmed=True)
        self.accept(await execution_review.verify_execution_result(self.value["ecn_id"], "王五", "复核", user_service=self.service, storage=self.storage))
        self.assertEqual(self.value["execution_info"]["verification"]["status"], "verified")

    async def test_reject_resubmit_and_stale_votes_cannot_cross_rounds(self):
        await self.create()
        self.accept(await self.action("submit_ecr"))
        old = copy.deepcopy(self.value)
        self.accept(await self.action("reject", "李四", note="补充原因"))
        self.accept(await self.action("submit_ecr"))
        stale = await actions.execute_action(old, old, "approve", "李四", "审批", user_service=self.service, storage=self.storage)
        self.assertFalse(stale.ok)
        await self.approve_phase()
        await self.save_scheme(ordinary_scheme())
        await self.ready()
        self.accept(await self.action("initiate_scheme_review"))
        self.accept(await self.action("reject", "李四", note="完善实施细节", rejected_ids=["ordinary"]))
        denied = await actions.set_participant_status(self.value["ecn_id"], self.value, "张三", "申请", "confirmed", user_service=self.service, storage=self.storage)
        self.assertFalse(denied.ok)
        original = copy.deepcopy(self.value["change_items"][0])
        changed = copy.deepcopy(original)
        changed["new_content"] = "完善后的实施细节"
        await self.save_scheme(changed, original)
        await self.review()
        await self.special("ordinary", confirmed=True)
        await self.special("__erp__", confirmed=True)
        await self.overview()
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.CLOSED)

    async def test_material_codes_serial_execution_final_ack_and_reverification(self):
        await self.scheming()
        material = ordinary_scheme()
        material.update(item_id="material", scheme_category="material", change_type="新增", traceability_levels=["文件"],
                        material_change={"material_name": "验收器件", "quantity": 1, "unit": "pcs"})
        await self.save_scheme(material)
        await self.review()
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.MATERIAL_CODE_PENDING)
        self.accept(await actions.update_material_codes(self.value["ecn_id"], self.value["change_items"][0], {"main:material_code": "AUDIT-001"}, "张三", "助理", user_service=self.service, storage=self.storage))
        await self.special("__erp__", confirmed=True)
        await self.overview()
        tasks = self.value["execution_info"]["material_confirmations"]["material"]["traceability_tasks"]
        first, second = list(tasks)
        self.assertTrue((await self.material(second, "王五"))["reason"])
        self.assertFalse((await self.material(first, "李四"))["reason"])
        self.assertTrue((await self.material(second, "王五"))["requires_final_confirmation"])
        self.assertFalse((await self.material(second, "王五", final=True))["reason"])
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.CLOSED)
        self.accept(await execution_review.verify_execution_result(self.value["ecn_id"], "张三", "复核", user_service=self.service, storage=self.storage))
        confirmation = copy.deepcopy(self.value["execution_info"]["material_confirmations"]["material"]["traceability_tasks"][second])
        self.accept(await execution_review.revoke_execution_confirmation(self.value["ecn_id"], "material", "material", second, confirmation, "核对数量", [], "张三", "复核", user_service=self.service, storage=self.storage))
        self.assertFalse((await self.material(second, "王五", final=True))["reason"])
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.CLOSED)

    async def test_overview_full_lifecycle_writes_real_project_and_closes(self):
        await self.scheming()
        item = ordinary_scheme()
        item.update(item_id="overview", type="overview_update", scheme_category="overview_document", label="参数", first_col_label="参数",
                    config_processing_type="text", new_data={"content": "实施结果"}, project_states={"P1": {"action": "add"}})
        await self.save_scheme(item)
        await self.review()
        await self.special("__erp__", confirmed=True)
        await self.overview()
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.CLOSED)
        data = await self.storage.get_fresh_item("P1_over_data", {})
        self.assertEqual(len(data["参数"]), 1)
        self.assertEqual(next(iter(data["参数"].values()))["content"], "实施结果")
        await self.overview()
        self.assertEqual(await self.storage.get_fresh_item("P1_over_data", {}), data)

    async def test_complex_report_blocks_review_and_changes_invalidate_approval(self):
        await self.scheming()
        self.accept(await actions.set_ecn_level(self.value["ecn_id"], self.value, "complex", "before_scheme_review", "张三", "判定", user_service=self.service, storage=self.storage))
        await self.save_scheme(ordinary_scheme())
        self.accept(await validation_reports.set_validation_report_required(self.value["ecn_id"], "ordinary", {}, True, "李四", "指定", user_service=self.service, storage=self.storage))
        await self.ready()
        self.assertFalse((await self.action("initiate_scheme_review")).ok)
        staged = await attachments.stage_upload(SmallFileUpload(name="report.txt", content_type="text/plain", _data=b"acceptance report"), "张三")
        self.accept(await attachments.attach_existing(self.value["ecn_id"], "validation", "ordinary", "", staged, "张三", "申请", self.service))
        item = self.value["change_items"][0]
        self.accept(await validation_reports.review_validation_report(self.value["ecn_id"], "ordinary", item["validation_report"], True, "", "李四", "审核", expected_scheme=build_ecn_scheme_snapshot(item), user_service=self.service, storage=self.storage))
        original = copy.deepcopy(self.value["change_items"][0])
        changed = copy.deepcopy(original)
        changed["new_content"] = "更新实施内容"
        await self.save_scheme(changed, original)
        await self.ready()
        self.assertFalse((await self.action("initiate_scheme_review")).ok)
        item = self.value["change_items"][0]
        self.accept(await validation_reports.review_validation_report(self.value["ecn_id"], "ordinary", item["validation_report"], True, "复核新方案", "李四", "审核", expected_scheme=build_ecn_scheme_snapshot(item), user_service=self.service, storage=self.storage))
        self.accept(await self.action("initiate_scheme_review"))
        await self.approve_phase()
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.ECN_EXECUTING)

    async def test_withdraw_cancel_and_simple_workflow_rounds(self):
        await self.create()
        self.accept(await self.action("submit_ecr"))
        self.accept(await self.action("withdraw"))
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.DRAFT)
        self.accept(await self.action("submit_ecr"))
        self.accept(await actions.set_ecn_level(self.value["ecn_id"], self.value, "simple", "ecr_review", "李四", "判定", user_service=self.service, storage=self.storage))
        self.assertIn("simple", str(self.value["workflow"]["ecr_workflow_assignment"]))
        self.accept(await self.action("cancel"))
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.CANCEL)
        self.assertFalse((await self.action("submit_ecr")).ok)

    async def test_all_seven_material_routes_complete_independently(self):
        await self.scheming()
        item = ordinary_scheme()
        item.update(item_id="material", scheme_category="material", change_type="新增",
                    traceability_levels=list(ECN_EXECUTION_EVENT_BY_LEVEL),
                    material_change={"material_name": "全范围验收", "material_code": "AUDIT-007", "quantity": 1, "unit": "pcs"})
        await self.save_scheme(item)
        await self.review()
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.ECN_EXECUTING)
        await self.special("__erp__", confirmed=True)
        await self.overview()
        tasks = self.value["execution_info"]["material_confirmations"]["material"]["traceability_tasks"]
        self.assertEqual(len(tasks), 14)
        for key, task in copy.deepcopy(tasks).items():
            user = "李四" if task["stage_index"] == 0 else "王五"
            blocked = await self.material(key, user, final=True)
            self.assertFalse(blocked["reason"], blocked)
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.CLOSED)

    async def test_simple_level_uses_simple_scheme_route_until_close(self):
        await self.create()
        self.accept(await self.action("submit_ecr"))
        self.accept(await actions.set_ecn_level(self.value["ecn_id"], self.value, "simple", "ecr_review", "李四", "判定", user_service=self.service, storage=self.storage))
        await self.approve_phase()
        await self.save_scheme(ordinary_scheme())
        await self.review()
        self.assertIn("scheme_review_simple", str(self.value["workflow"]["scheme_workflow_assignment"]))
        await self.special("ordinary", confirmed=True)
        await self.special("__erp__", confirmed=True)
        await self.overview()
        self.assertEqual(self.value["workflow"]["current_state"], ECNState.CLOSED)
