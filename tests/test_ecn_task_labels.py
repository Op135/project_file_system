from src.modules.ecn.notifications import material_task_summary
from src.modules.ecn.task_labels import (
    compact_material_confirmation_label,
    humanize_ecn_log_action,
    material_confirmation_tooltip_text,
    material_task_label,
    special_task_label,
)


def _record():
    item_id = "32c626f7-3956-4ecf-8209-e7bace12c434"
    task_key = "客户/在途::项目销售::RFFM-1009-B"
    task = {
        "level": "客户/在途",
        "project": "RFFM-1009-B",
        "responsible_key": "销售主管",
        "label": "RFFM-1009-B · 销售主管",
    }
    return {
        "change_items": [
            {
                "item_id": item_id,
                "change_type": "测试报告内容格式",
                "projects": ["RFFM-1009-B"],
            }
        ],
        "execution_info": {
            "material_confirmations": {
                item_id: {"traceability_tasks": {task_key: task}},
            }
        },
    }, item_id, task_key, task


def test_special_task_labels_use_scheme_number_and_business_name():
    record, item_id, _, _ = _record()

    assert special_task_label(record, item_id) == "特定事项 #01 测试报告内容格式"
    assert special_task_label(record, "__erp__") == "ERP相关变更"
    assert item_id not in humanize_ecn_log_action(record, f"特定事项 {item_id} 确认完成")
    assert humanize_ecn_log_action(record, "特定事项 __erp__ 取消确认") == "ERP相关变更 取消确认"


def test_material_task_labels_hide_item_and_task_keys():
    record, item_id, task_key, task = _record()
    label = material_task_label(record, item_id, task)
    action = humanize_ecn_log_action(record, f"物料责任项 {item_id} / {task_key} 改派：原责任路线 → 张三")

    assert label.startswith("物料方案 #01 测试报告内容格式")
    assert item_id not in action
    assert task_key not in action
    assert action.endswith("改派：原责任路线 → 张三")


def test_compact_material_confirmation_label_removes_repeated_responsibility_text():
    missing_sales = {
        "key": "客户/在途::项目销售::RFFM-1009-B",
        "project": "RFFM-1009-B",
        "responsible_type": "hierarchy_users",
        "responsible_key": "销售主管",
        "users": ["邓俊豪"],
        "label": "RFFM-1009-B · 项目销售未识别，转销售主管：邓俊豪",
    }
    supervisor = {
        "key": "客户/在途::销售主管::RFFM-1009-A",
        "project": "RFFM-1009-A",
        "responsible_type": "hierarchy_users",
        "responsible_key": "销售主管",
        "users": ["邓俊豪"],
        "label": "RFFM-1009-A · 销售主管：邓俊豪",
    }

    assert compact_material_confirmation_label(missing_sales) == "RFFM-1009-B · 邓俊豪（代确认）"
    assert compact_material_confirmation_label(supervisor) == "RFFM-1009-A · 销售主管 邓俊豪"

    escalated_project_sales = {
        "key": "客户/在途::项目销售::RFFM-1108-F",
        "project": "RFFM-1108-F",
        "responsible_type": "hierarchy_users",
        "responsible_key": "项目销售",
        "users": ["邓俊豪"],
        "resolution_mode": "manager_escalation",
    }
    assert compact_material_confirmation_label(escalated_project_sales) == "RFFM-1108-F · 邓俊豪（代确认）"

    director_for_missing_sales = {
        **missing_sales,
        "users": ["销售总监"],
        "resolution_mode": "manager_escalation",
    }
    assert compact_material_confirmation_label(director_for_missing_sales) == (
        "RFFM-1009-B · 销售总监（代确认）"
    )
    assert "销售主管也无法处理" in material_confirmation_tooltip_text(
        director_for_missing_sales, {}, True, False
    )

    escalated_purchase = {
        "key": "供应商::采购",
        "responsible_type": "hierarchy_users",
        "responsible_key": "采购",
        "users": ["采购经理"],
        "resolution_mode": "manager_escalation",
    }
    assert compact_material_confirmation_label(escalated_purchase) == "采购经理（代确认）"

    record = {
        "change_items": [
            {
                "item_id": "M1",
                "change_type": "更改",
                "projects": ["RFFM-1108-F"],
            }
        ]
    }
    notification = material_task_summary(record, "M1", {"level": "客户/在途", **escalated_project_sales})
    assert "项目：RFFM-1108-F" in notification
    assert "确认：邓俊豪（代确认）" in notification
    assert "项目销售" not in notification


def test_material_confirmation_tooltip_explains_assignment_instead_of_repeating_name():
    spec = {
        "key": "供应商::采购",
        "responsible_type": "hierarchy_users",
        "responsible_key": "采购",
        "resolution_mode": "current_level",
        "users": ["王冰泽"],
    }

    tooltip = material_confirmation_tooltip_text(spec, {}, True, False)

    assert tooltip == "分配依据：按“采购”岗位自动匹配\n当前状态：等待确认"
    assert "指定人" not in tooltip
    assert "王冰泽" not in tooltip
