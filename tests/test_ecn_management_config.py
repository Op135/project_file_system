import json
from typing import Any

from src.ecn_management_config import (
    ECN_CONFIG_PATH,
    ECN_SCHEME_GROUP_MATERIAL,
    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
    ECN_SCHEME_GROUP_UNKNOWN,
    ECN_ITEM_STATUS_NEEDS_IMPROVEMENT,
    ECN_ITEM_STATUS_REVISED_CONFIRMED,
    ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION,
    ECN_PARTICIPANT_STATUS_CONFIRMED,
    ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
    ECNState,
    build_overview_validation_signature,
    can_view_ecn_scheme_non_image_file,
    classify_ecn_change_item,
    collect_ecn_pending_overview_overrides,
    get_active_overview_row_contents,
    get_ecn_overview_project_new_data,
    get_ecn_scheme_target_projects,
    get_ecn_material_change_display,
    get_ecn_material_change_missing_fields,
    get_ecn_scheme_coverage,
    get_ecn_dashboard_pending_count,
    has_unrevised_rejected_scheme_items,
    is_ecn_pending_for_user,
    is_ecn_review_info_blank,
    is_ecn_scheme_ready_for_review,
    load_ecn_config,
    merge_ecn_impact_audit_log,
    register_ecn_impact_handler,
    resolve_ecn_overview_parameter_config,
    confirm_revised_scheme_items,
    mark_rejected_scheme_item_revised,
    expand_new_material_traceability_selection,
    ecn_overview_requires_new_content,
    reject_ecn_scheme_items,
)


def _ecn_record(
    *,
    state=ECNState.ECN_SCHEMING,
    impact_handlers=None,
    impact_selected=False,
    participants=None,
    pending_roles=None,
    applicant="申请人",
) -> dict[str, Any]:
    return {
        "basic_info": {"applicant": applicant},
        "review_info": {
            "expanded_projects_mass": [],
            "expanded_projects_non_mass": [],
            "impacts": {"光学部件": impact_selected},
            "involved_docs": {"光学件图纸": False},
            "involved_materials": {"光源": {"新增": False}},
            "other_docs_desc": "",
        },
        "workflow": {
            "current_state": state,
            "pending_roles": pending_roles or [],
            "impact_handlers": impact_handlers or [],
            "scheme_participants": participants or {},
        },
    }


def test_checked_in_config_file_is_valid():
    with ECN_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        raw_config = json.load(config_file)

    loaded = load_ecn_config(raw_config)

    assert loaded["permissions"]["impact_initial_reminder_roles"] == ["研发助理"]
    assert loaded["permissions"]["ordinary_document_file_view_roles_by_type"] == raw_config[
        "permissions"
    ]["ordinary_document_file_view_roles_by_type"]
    assert loaded["reminders"]["impact_followup_states"] == [
        ECNState.ECN_SCHEMING,
        ECNState.ECN_REVIEWING,
    ]
    assert loaded["ui"]["overview_conflict_auto_close_seconds"] == 5.0
    assert loaded["scheme_review"]["require_rejected_item_selection"] is True
    assert loaded["scheme_review"]["require_revision_before_reconfirmation"] is True
    assert loaded["scheme_review"]["participant_statuses"]["confirmed"]["remind"] is False
    assert loaded["scheme_review"]["participant_statuses"]["needs_reconfirmation"]["remind"] is True
    assert loaded["scheme_review"]["transitions"] == {
        "participant_after_edit": "editing",
        "participant_after_confirmation": "confirmed",
        "participant_after_rejection": "needs_reconfirmation",
        "item_after_rejection": "needs_improvement",
        "item_after_revision": "revised_pending_confirmation",
        "item_after_reconfirmation": "revised_confirmed",
    }
    assert loaded["scheme_tracking"]["traceability_levels"] == raw_config["scheme_tracking"][
        "traceability_levels"
    ]
    assert loaded["scheme_tracking"]["disposition_measures"] == raw_config["scheme_tracking"][
        "disposition_measures"
    ]
    assert loaded["scheme_options"]["overview_actions"] == {
        "add": "新增",
        "update": "更换",
        "deactivate": "失效",
    }
    assert loaded["scheme_options"] == raw_config["scheme_options"]


def test_all_scheme_dialogs_share_ecr_and_expanded_target_projects():
    record = _ecn_record()
    record["target_projects"] = ["P-ECR-1", "P-SHARED"]
    record["review_info"]["expanded_projects_mass"] = ["P-MASS", "P-SHARED"]
    record["review_info"]["expanded_projects_non_mass"] = ["P-RD"]

    assert get_ecn_scheme_target_projects(record) == [
        "P-ECR-1",
        "P-SHARED",
        "P-MASS",
        "P-RD",
    ]


def test_overview_deactivate_only_scheme_does_not_require_new_content():
    assert ecn_overview_requires_new_content({"P1": {"action": "deactivate"}}) is False
    assert ecn_overview_requires_new_content(
        {
            "P1": {"action": "deactivate"},
            "P2": {"action": "update"},
        }
    ) is True


def test_overview_project_new_data_uses_each_projects_validated_svn_path():
    shared_data = {"content": "RFFM-1009-app.zip"}
    rd_state = {
        "new_file_data": {
            "url_path": "https://svn/Control/Controlled/RFFM-1009/src/RFFM-1009-app.zip",
            "file_type": "application/zip",
            "warehouse": "Control/Controlled",
        }
    }
    mass_state = {
        "new_file_data": {
            "url_path": "https://svn/Product/RFFM-1009/src/RFFM-1009-app.zip",
            "file_type": "application/zip",
            "warehouse": "Product",
        }
    }

    assert get_ecn_overview_project_new_data(shared_data, rd_state)["url_path"].startswith(
        "https://svn/Control/Controlled/"
    )
    assert get_ecn_overview_project_new_data(shared_data, mass_state)["url_path"].startswith(
        "https://svn/Product/"
    )
    assert "url_path" not in shared_data


def test_non_image_file_view_permission_uses_the_correct_configuration_source():
    overview_item = {
        "type": "overview_update",
        "scheme_category": ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
        "label": "software_manual",
    }
    overview_configs = {
        "software_manual": {
            "permission": {
                "read_role": ["质量"],
                "edit_role": ["研发软件"],
            }
        }
    }
    assert can_view_ecn_scheme_non_image_file(
        overview_item,
        "质量",
        overview_configs,
        {"图纸更新": ["销售"]},
    ) is True
    assert can_view_ecn_scheme_non_image_file(
        overview_item,
        "研发软件",
        overview_configs,
        {"图纸更新": ["销售"]},
    ) is True
    assert can_view_ecn_scheme_non_image_file(
        overview_item,
        "质量主管",
        overview_configs,
        {"图纸更新": ["质量"]},
    ) is False

    ordinary_item = {
        "type": "text_desc",
        "scheme_category": ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
        "change_type": "图纸更新",
    }
    assert can_view_ecn_scheme_non_image_file(
        ordinary_item,
        "质量主管",
        overview_configs,
        {"图纸更新": ["质量", "admin"], "SOP修改": ["工程"]},
    ) is True
    assert can_view_ecn_scheme_non_image_file(
        ordinary_item,
        "采购",
        overview_configs,
        {"图纸更新": ["质量", "admin"], "SOP修改": ["工程"]},
    ) is False
    ordinary_item["change_type"] = "SOP修改"
    assert can_view_ecn_scheme_non_image_file(
        ordinary_item,
        "质量主管",
        overview_configs,
        {"图纸更新": ["质量"], "SOP修改": ["工程"]},
    ) is False
    assert can_view_ecn_scheme_non_image_file(
        ordinary_item,
        "工程主管",
        overview_configs,
        {"图纸更新": ["质量"], "SOP修改": ["工程"]},
    ) is True

    with ECN_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        empty_role_config = json.load(config_file)
    empty_role_config["permissions"]["ordinary_document_file_view_roles_by_type"]["其它"] = []
    empty_other_roles = load_ecn_config(empty_role_config)["permissions"][
        "ordinary_document_file_view_roles_by_type"
    ]
    assert empty_other_roles["其它"] == []


def test_pending_overview_overrides_exclude_the_item_being_edited():
    change_items = [
        {
            "item_id": "CURRENT",
            "type": "overview_update",
            "label": "folder_key",
            "project_states": {"P1": {"action": "update"}},
            "new_data": {"content": "被编辑项的旧值"},
        },
        {
            "item_id": "OTHER",
            "type": "overview_update",
            "label": "other_key",
            "project_states": {"P1": {"action": "add"}},
            "new_data": {"content": "其它暂存值"},
        },
        {
            "item_id": "DEACTIVATED",
            "type": "overview_update",
            "label": "inactive_key",
            "project_states": {"P1": {"action": "deactivate"}},
            "new_data": {"content": "不应作为覆盖值"},
        },
    ]

    assert collect_ecn_pending_overview_overrides(change_items, "P1", "CURRENT") == {
        "other_key": "其它暂存值"
    }


def test_overview_parameter_config_is_resolved_before_edit_control_events():
    flat_configs = {
        "file_label": {
            "processing_type": "search",
            "upload_path": "X:/engineering",
            "search_folder_according": ["folder_label"],
        }
    }

    config, processing_type = resolve_ecn_overview_parameter_config(flat_configs, "file_label")

    assert processing_type == "search"
    assert config["upload_path"] == "X:/engineering"
    assert config["search_folder_according"] == ["folder_label"]
    config["upload_path"] = "changed"
    assert flat_configs["file_label"]["upload_path"] == "X:/engineering"


def test_impact_audit_log_merges_by_unique_event_id_without_overwrite():
    review_info = {
        "impact_change_log": [
            {"event_id": "E1", "user": "工程师A", "field": "impacts", "target": "光学部件"}
        ]
    }
    incoming = [
        {"event_id": "E1", "user": "工程师A"},
        {
            "event_id": "E2",
            "user": "工程师B",
            "field": "expanded_projects_mass",
            "target": "RFFM-1009-A",
            "action": "add",
        },
    ]

    assert merge_ecn_impact_audit_log(review_info, incoming) == 1
    assert [event["event_id"] for event in review_info["impact_change_log"]] == ["E1", "E2"]
    assert merge_ecn_impact_audit_log(review_info, incoming) == 0


def test_empty_impact_only_reminds_rd_assistant():
    record = _ecn_record()

    assert is_ecn_pending_for_user(record, "助理A", "研发助理") is True
    assert is_ecn_pending_for_user(record, "工程师A", "研发硬件") is False
    assert is_ecn_pending_for_user(record, "工程师B", "工程") is False


def test_explicit_impact_handlers_replace_broad_role_reminder():
    record = _ecn_record(
        impact_handlers=["工程师A", "工程师B"],
        impact_selected=True,
    )

    assert is_ecn_pending_for_user(record, "工程师A", "研发硬件") is True
    assert is_ecn_pending_for_user(record, "工程师B", "工程") is True
    assert is_ecn_pending_for_user(record, "助理A", "研发助理") is False
    assert is_ecn_pending_for_user(record, "工程师C", "质量") is False


def test_registering_impact_handlers_supports_multiple_people_and_does_not_duplicate():
    record = _ecn_record(impact_selected=True)

    assert register_ecn_impact_handler(record, "工程师A") is True
    assert register_ecn_impact_handler(record, "工程师A") is False
    assert register_ecn_impact_handler(record, "工程师B") is True
    assert record["workflow"]["impact_handlers"] == ["工程师A", "工程师B"]

    record["review_info"]["impacts"]["光学部件"] = False
    assert register_ecn_impact_handler(record, "工程师C") is False
    assert record["workflow"]["impact_handlers"] == ["工程师A", "工程师B"]


def test_confirmed_handler_is_not_reminded_but_rejected_handler_is_reminded():
    reviewing = _ecn_record(
        state=ECNState.ECN_REVIEWING,
        impact_handlers=["工程师A"],
        impact_selected=True,
        participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED},
    )
    rejected = _ecn_record(
        state=ECNState.ECN_SCHEMING,
        impact_handlers=["工程师A"],
        impact_selected=True,
        participants={"工程师A": ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION},
    )

    assert is_ecn_pending_for_user(reviewing, "工程师A", "研发硬件") is False
    assert is_ecn_pending_for_user(rejected, "工程师A", "研发硬件") is True


def test_scheme_initiator_is_reminded_when_scheme_is_ready_for_review():
    record = _ecn_record(
        impact_selected=True,
        impact_handlers=["工程师A"],
        participants={"工程师A": "confirmed", "工程师B": "confirmed"},
    )
    record["review_info"]["involved_docs"] = {"光学件图纸": True}
    record["review_info"]["involved_materials"] = {"光源": {"新增": True}}
    record["change_items"] = [
        {
            "linked_docs": ["光学件图纸"],
            "linked_materials": ["光源-新增"],
        }
    ]

    assert is_ecn_scheme_ready_for_review(record) is True
    assert is_ecn_pending_for_user(record, "经理A", "研发经理") is True
    assert is_ecn_pending_for_user(record, "管理员", "admin") is True
    assert is_ecn_pending_for_user(record, "未参与工程师", "研发硬件") is False


def test_scheme_initiator_is_not_reminded_until_confirmation_and_coverage_are_complete():
    unconfirmed = _ecn_record(
        impact_selected=True,
        impact_handlers=["工程师A"],
        participants={"工程师A": "editing"},
    )
    missing_coverage = _ecn_record(
        impact_selected=True,
        impact_handlers=["工程师A"],
        participants={"工程师A": "confirmed"},
    )
    missing_coverage["review_info"]["involved_docs"] = {"光学件图纸": True}
    missing_coverage["change_items"] = []

    assert is_ecn_scheme_ready_for_review(unconfirmed) is False
    assert is_ecn_pending_for_user(unconfirmed, "经理A", "研发经理") is False
    assert is_ecn_scheme_ready_for_review(missing_coverage) is False
    assert is_ecn_pending_for_user(missing_coverage, "经理A", "研发经理") is False


def test_every_change_requirement_must_be_linked_by_at_least_one_scheme():
    record = _ecn_record(
        impact_selected=True,
        impact_handlers=["工程师A"],
        participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED},
    )
    record["basic_info"]["requirements"] = [
        {"idx": 1, "content": "更新电子BOM"},
        {"idx": 2, "content": "同步修改说明书"},
    ]
    change_items: list[dict[str, Any]] = [{"req_idxs": [1]}]
    record["change_items"] = change_items

    coverage = get_ecn_scheme_coverage(record)
    assert coverage["missing_requirements"] == {"2"}
    assert is_ecn_scheme_ready_for_review(record) is False
    assert is_ecn_pending_for_user(record, "经理A", "研发经理") is False

    change_items.append({"req_idxs": ["2"]})
    assert get_ecn_scheme_coverage(record)["missing_requirements"] == set()
    assert is_ecn_scheme_ready_for_review(record) is True


def test_rejecting_selected_items_only_reopens_their_authors():
    record = _ecn_record(
        state=ECNState.ECN_REVIEWING,
        impact_selected=True,
        impact_handlers=["工程师A", "工程师B"],
        participants={
            "工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED,
            "工程师B": ECN_PARTICIPANT_STATUS_CONFIRMED,
        },
    )
    record["change_items"] = [
        {
            "item_id": "A1",
            "author": "工程师A",
            "review_status": "normal",
            "projects": ["RFFM-1009-A"],
            "old_content": "旧方案",
            "new_content": "改进前内容",
            "file_server_path": r"\\file-server\ecn\RFFM-1009-A",
        },
        {"item_id": "B1", "author": "工程师B", "review_status": "normal"},
    ]

    authors = reject_ecn_scheme_items(
        record,
        ["A1"],
        "经理A",
        "研发经理",
        "参数需要修订",
        "2026-08-12 10:00:00",
    )

    assert authors == {"工程师A"}
    assert record["change_items"][0]["review_status"] == ECN_ITEM_STATUS_NEEDS_IMPROVEMENT
    assert record["change_items"][0]["rejection_history"] == [
        {
            "reviewer": "经理A",
            "reviewer_role": "研发经理",
            "note": "参数需要修订",
            "time": "2026-08-12 10:00:00",
            "before_snapshot": {
                "item_id": "A1",
                "author": "工程师A",
                "projects": ["RFFM-1009-A"],
                "old_content": "旧方案",
                "new_content": "改进前内容",
                "file_server_path": r"\\file-server\ecn\RFFM-1009-A",
            },
        }
    ]
    assert record["change_items"][1]["review_status"] == "normal"
    assert record["workflow"]["scheme_participants"] == {
        "工程师A": ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
        "工程师B": ECN_PARTICIPANT_STATUS_CONFIRMED,
    }
    assert is_ecn_pending_for_user(record, "工程师A", "研发硬件") is True
    assert is_ecn_pending_for_user(record, "工程师B", "工程") is False


def test_rejected_item_must_be_revised_before_reconfirmation():
    record = _ecn_record(
        participants={"工程师A": ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION}
    )
    item = {
        "item_id": "A1",
        "author": "工程师A",
        "review_status": ECN_ITEM_STATUS_NEEDS_IMPROVEMENT,
    }
    record["change_items"] = [item]

    assert has_unrevised_rejected_scheme_items(record, "工程师A") is True
    mark_rejected_scheme_item_revised(item)
    assert item["review_status"] == ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION
    assert has_unrevised_rejected_scheme_items(record, "工程师A") is False

    confirm_revised_scheme_items(record, "工程师A")
    assert item["review_status"] == ECN_ITEM_STATUS_REVISED_CONFIRMED


def test_revising_rejected_item_records_after_snapshot_in_same_history_entry():
    record = _ecn_record(
        participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED}
    )
    item = {
        "item_id": "A1",
        "author": "工程师A",
        "projects": ["P1"],
        "old_content": "旧内容",
        "new_content": "驳回时方案",
    }
    record["change_items"] = [item]
    reject_ecn_scheme_items(
        record,
        ["A1"],
        "经理A",
        "研发经理",
        "请调整",
        "2026-08-13 09:00:00",
    )

    item["new_content"] = "整改后方案"
    mark_rejected_scheme_item_revised(item)

    history_record = item["rejection_history"][0]
    assert history_record["before_snapshot"]["new_content"] == "驳回时方案"
    assert history_record["after_snapshot"]["new_content"] == "整改后方案"
    assert "rejection_history" not in history_record["before_snapshot"]
    assert "review_status" not in history_record["after_snapshot"]


def test_material_traceability_and_disposition_are_kept_in_rejection_snapshots():
    record = _ecn_record(participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED})
    item = {
        "item_id": "M1",
        "type": "text_desc",
        "scheme_category": ECN_SCHEME_GROUP_MATERIAL,
        "author": "工程师A",
        "traceability_levels": ["追溯至供应商存量", "追溯至零件/返修/在线"],
        "disposition_measure": "返工",
    }
    record["change_items"] = [item]

    reject_ecn_scheme_items(
        record,
        ["M1"],
        "经理A",
        "研发经理",
        "重新确认处置范围",
        "2026-08-13 10:00:00",
    )

    snapshot = item["rejection_history"][0]["before_snapshot"]
    assert snapshot["traceability_levels"] == ["追溯至供应商存量", "追溯至零件/返修/在线"]
    assert snapshot["disposition_measure"] == "返工"


def test_material_scheme_must_have_traceability_and_disposition_before_review():
    record = _ecn_record(participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED})
    change_items: list[dict[str, Any]] = [
        {
            "item_id": "M1",
            "type": "text_desc",
            "scheme_category": ECN_SCHEME_GROUP_MATERIAL,
            "author": "工程师A",
        }
    ]
    record["change_items"] = change_items

    coverage = get_ecn_scheme_coverage(record)
    assert coverage["incomplete_material_schemes"] == {"方案 #01"}
    assert is_ecn_scheme_ready_for_review(record) is False

    change_items[0]["traceability_levels"] = ["无影响", "追溯至文件"]
    change_items[0]["disposition_measure"] = "返工"
    assert get_ecn_scheme_coverage(record)["incomplete_material_schemes"] == set()
    assert is_ecn_scheme_ready_for_review(record) is True


def test_new_material_scheme_does_not_require_old_material_disposition():
    record = _ecn_record(participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED})
    record["change_items"] = [
        {
            "item_id": "M1",
            "type": "text_desc",
            "scheme_category": ECN_SCHEME_GROUP_MATERIAL,
            "change_type": "新增",
            "traceability_levels": ["文件"],
        }
    ]

    assert get_ecn_scheme_coverage(record)["incomplete_material_schemes"] == set()
    assert is_ecn_scheme_ready_for_review(record) is True


def test_adjust_quantity_material_scheme_does_not_require_old_material_disposition():
    record = _ecn_record(participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED})
    record["change_items"] = [
        {
            "item_id": "M1",
            "type": "text_desc",
            "scheme_category": ECN_SCHEME_GROUP_MATERIAL,
            "change_type": "调量",
            "traceability_levels": ["文件"],
        }
    ]

    assert get_ecn_scheme_coverage(record)["incomplete_material_schemes"] == set()


def test_discontinued_material_scheme_requires_old_material_disposition():
    record = _ecn_record(participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED})
    record["change_items"] = [
        {
            "item_id": "M1",
            "type": "text_desc",
            "scheme_category": ECN_SCHEME_GROUP_MATERIAL,
            "change_type": "弃用",
            "traceability_levels": ["文件"],
        }
    ]

    assert get_ecn_scheme_coverage(record)["incomplete_material_schemes"] == {"方案 #01"}


def test_conditional_disposition_requires_specific_condition():
    record = _ecn_record(participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED})
    record["change_items"] = [
        {
            "item_id": "M1",
            "type": "text_desc",
            "scheme_category": ECN_SCHEME_GROUP_MATERIAL,
            "change_type": "更换",
            "traceability_levels": ["文件"],
            "disposition_measure": "有条件用完止",
        }
    ]

    assert get_ecn_scheme_coverage(record)["incomplete_material_schemes"] == {"方案 #01"}
    record["change_items"][0]["disposition_condition"] = "仅限内部试制批次使用"
    assert get_ecn_scheme_coverage(record)["incomplete_material_schemes"] == set()


def test_traceability_only_cascades_when_a_new_level_is_selected():
    configured_levels = load_ecn_config()["scheme_tracking"]["traceability_levels"]
    assert len(configured_levels) >= 5

    expanded = expand_new_material_traceability_selection(
        [configured_levels[3]],
        [],
    )
    assert expanded == configured_levels[:4]

    with_gap = [configured_levels[0], configured_levels[2], configured_levels[3]]
    assert expand_new_material_traceability_selection(with_gap, expanded) == with_gap

    expanded_again = expand_new_material_traceability_selection(
        [*with_gap, configured_levels[4]],
        with_gap,
    )
    assert expanded_again == configured_levels[:5]


def test_normal_approval_and_applicant_pending_rules_are_preserved():
    approval_record = _ecn_record(
        state=ECNState.ECR_REVIEWING,
        pending_roles=["研发经理"],
    )
    draft_record = _ecn_record(state=ECNState.DRAFT, applicant="申请人A")

    assert is_ecn_pending_for_user(approval_record, "经理A", "研发经理") is True
    assert is_ecn_pending_for_user(draft_record, "申请人A", "销售") is True


def test_dashboard_count_counts_each_ecn_once():
    all_ecns = {
        "ECN1": _ecn_record(impact_handlers=["工程师A"], impact_selected=True),
        "ECN2": _ecn_record(impact_handlers=["工程师A"], impact_selected=True),
        "ECN3": _ecn_record(impact_handlers=["工程师B"], impact_selected=True),
        "dirty": None,
    }

    assert get_ecn_dashboard_pending_count(all_ecns, "工程师A", "研发硬件") == 2


def test_impact_blank_detection_covers_project_document_material_and_description():
    base_review = _ecn_record()["review_info"]
    assert is_ecn_review_info_blank(base_review) is True

    for field, value in [
        ("expanded_projects_mass", ["P1"]),
        ("other_docs_desc", "补充说明"),
    ]:
        review = {**base_review, field: value}
        assert is_ecn_review_info_blank(review) is False

    document_review = {**base_review, "involved_docs": {"光学件图纸": True}}
    material_review = {**base_review, "involved_materials": {"光源": {"新增": True}}}
    assert is_ecn_review_info_blank(document_review) is False
    assert is_ecn_review_info_blank(material_review) is False


def test_active_overview_row_contents_only_returns_current_cell_active_data():
    raw_data = {
        "active-a": {
            "row_id": "ROW-1",
            "content": "已有参数A",
            "select_activ_dic": {"2.0": True},
        },
        "active-b": {
            "row_id": "ROW-1",
            "content": "已有参数B",
            "select_activ_dic": {"2.0": True},
        },
        "old-version": {
            "row_id": "ROW-1",
            "content": "旧版本内容",
            "select_activ_dic": {"2.0": False, "1.0": True},
        },
        "other-row": {
            "row_id": "ROW-2",
            "content": "其他基准行内容",
            "select_activ_dic": {"2.0": True},
        },
    }

    assert get_active_overview_row_contents(raw_data, "ROW-1", "2.0") == ["已有参数A", "已有参数B"]


def test_overview_validation_signature_changes_with_content_or_context():
    validated = build_overview_validation_signature(
        "search",
        "有效文件.pdf",
        ["P1", "P2"],
        "光学",
        "light_driver",
    )

    assert validated == build_overview_validation_signature(
        "search",
        "  有效文件.pdf  ",
        ["P1", "P2"],
        "光学",
        "light_driver",
    )
    assert validated != build_overview_validation_signature(
        "search",
        "无效文件.pdf",
        ["P1", "P2"],
        "光学",
        "light_driver",
    )
    assert validated != build_overview_validation_signature(
        "search",
        "有效文件.pdf",
        ["P1", "P3"],
        "光学",
        "light_driver",
    )


def test_current_change_items_are_split_into_explicit_document_groups():
    assert classify_ecn_change_item(
        {"type": "overview_update", "scheme_category": ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT}
    ) == ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT
    assert classify_ecn_change_item(
        {"type": "text_desc", "scheme_category": ECN_SCHEME_GROUP_ORDINARY_DOCUMENT}
    ) == ECN_SCHEME_GROUP_ORDINARY_DOCUMENT
    assert classify_ecn_change_item(
        {"type": "text_desc", "scheme_category": ECN_SCHEME_GROUP_MATERIAL}
    ) == ECN_SCHEME_GROUP_MATERIAL
    assert classify_ecn_change_item({"type": "unknown"}) == ECN_SCHEME_GROUP_UNKNOWN


def test_structured_material_change_display_covers_all_change_types():
    assert get_ecn_material_change_display(
        {
            "change_type": "新增",
            "material_change": {"material_name": "螺钉", "quantity": 2, "unit": "pcs"},
        }
    ) == ("无", "螺钉\n用量：2 pcs")
    assert get_ecn_material_change_display(
        {
            "change_type": "调量",
            "material_change": {
                "material_name": "螺钉",
                "old_quantity": 2,
                "new_quantity": 3.5,
                "unit": "pcs",
            },
        }
    ) == ("螺钉\n用量：2 pcs", "螺钉\n用量：3.5 pcs")
    assert get_ecn_material_change_display(
        {
            "change_type": "弃用",
            "material_change": {"material_name": "旧线材", "quantity": 1, "unit": "m"},
        }
    ) == ("旧线材\n用量：1 m", "弃用")
    assert get_ecn_material_change_display(
        {
            "change_type": "更换",
            "material_change": {
                "old_material_name": "旧螺钉",
                "old_quantity": 2,
                "old_unit": "pcs",
                "new_material_name": "新螺钉",
                "new_quantity": 3,
                "new_unit": "pcs",
            },
        }
    ) == ("旧螺钉\n用量：2 pcs", "新螺钉\n用量：3 pcs")


def test_structured_material_change_required_fields_accept_zero_quantity():
    material_change = {"material_name": "试剂", "quantity": 0, "unit": "pcs"}
    assert get_ecn_material_change_missing_fields("新增", material_change) == []
    assert get_ecn_material_change_missing_fields("调量", material_change) == ["改前用量", "改后用量"]


def test_invalid_material_change_does_not_use_unstructured_text_fields():
    assert get_ecn_material_change_display(
        {"change_type": "物料变更", "old_content": "旧物料", "new_content": "新物料"}
    ) == ("", "")
